# -*- coding: utf-8 -*-
"""采集端 → 后端 的接入映射层（Apple Watch / NetPulse → AceMate）。

============================================================================
 这一层解决什么问题
============================================================================
采集端（iOS + Apple Watch）产出的是一份**整包的 MatchSession**：
一场训练 = 会话元数据 + 一个 `swings` 逐拍数组。而后端这边要的是
**结构化的两行数据**（training_sessions 一行 + stroke_records 若干行）。

如果接入方直接自己写 INSERT，就会踩三个坑：
  · 重复上传 → 同一场入库两次，统计翻倍
  · 绕过 changelog → 小程序端拉不到新数据
  · 绕过 LWW → 和 App 的同步写入互相打架

所以这里**不自己写库**，而是把整包会话**翻译成 sync 协议的 operation**，
再交给 `sync.push` 落库 —— 幂等、变更日志、冲突裁决、软删除墓碑全部继承，
两条写入路径（App 同步 / 采集端接入）共用同一套一致性语义。

============================================================================
 幂等设计
============================================================================
`operation_id` 只由「采集端会话 ID」决定，**不含时间戳**：

    ing-<externalId>-s           会话行
    ing-<externalId>-k0007       第 7 拍

因此同一场训练无论上传多少次，都命中 sync 的第一道闸（operation_id 去重）
返回 `duplicate` 且**不改库** —— 天然幂等，客户端可以放心重试。

若确实要覆盖已入库的场次（例如补传了卡路里），调用方需显式传 `force=True`，
此时 operation_id 追加时间戳、`client_updated_at` 改用服务器时间，
从而绕过第一道闸并由 LWW 判定成败。
============================================================================
"""
import re
from datetime import datetime, timezone

from . import db, sync

# 实体 ID 前缀：避免与 AceMate 自身生成的 id（sess-xxxx / str-xxxx）相撞
SESSION_PREFIX = 'nps-'
STROKE_PREFIX = 'npk-'

DEFAULT_SOURCE = 'netpulse_watch'

# 采集端会输出的击球类型（Swift SwingType）
STROKE_TYPES = ('forehand', 'backhand', 'serve', 'slice', 'volley', 'smash')

# 相邻两拍间隔小于该值视为「同一回合」（与数据源目录 rally_max 的口径一致）
RALLY_GAP_SEC = 2.5

# sync.push 单批上限 500，留出余量分批
_PUSH_CHUNK = 400


# --------------------------------------------------------------------------- #
# 取值辅助：采集端字段缺失时一律返回 None（**缺失 ≠ 0**）
# --------------------------------------------------------------------------- #
def _f(v):
    """安全转 float；无法转换或为空则 None。"""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    """安全转 int（四舍五入）；无法转换则 None。"""
    f = _f(v)
    return int(round(f)) if f is not None else None


def _mean(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _ttl(v):
    return v if v not in (None, '') else None


# --------------------------------------------------------------------------- #
# 逐拍聚合
# --------------------------------------------------------------------------- #
def summarize_swings(swings):
    """从逐拍数组算出会话级聚合量。

    产出三类东西：
      · count_fields —— 六类击球分项计数（供 G2 配平门禁校验）
      · 球速字段     —— 发球/正手/反手的峰值与均速、全场峰值与均速
      · rally_max    —— 最长连续回合长度

    ⚠️ 这里的「球速」取自采集端的 `racketHeadSpeedKmh`。该物理量的真实
       含义是**拍头线速度**（由腕部峰值角速度 × 球拍长度估算），并非球体
       飞行速度。产品文案统一沿用「球速」，此处不另立名称 —— 见
       docs/INTEGRATION.md「口径约定」一节。
    """
    counts = {t: 0 for t in STROKE_TYPES}
    by_type = {t: [] for t in STROKE_TYPES}
    all_speeds = []

    for s in swings or []:
        if not isinstance(s, dict):
            continue
        t = (s.get('type') or '').strip().lower()
        sp = _f(s.get('racketHeadSpeedKmh')) or _f(s.get('speed_kmh'))
        if t in counts:
            counts[t] += 1
            if sp is not None:
                by_type[t].append(sp)
        # unknown / 未识别类型不计入分项，但仍参与「全场球速」——
        # 否则一旦分类器降级，整场球速会凭空偏高
        if sp is not None:
            all_speeds.append(sp)

    count_fields = {'%s_count' % t: counts[t] for t in STROKE_TYPES}
    return {
        'count_fields': count_fields,
        'counts': counts,
        'peak': max(all_speeds) if all_speeds else None,
        'avg': _mean(all_speeds),
        'serve_peak': max(by_type['serve']) if by_type['serve'] else None,
        'serve_avg': _mean(by_type['serve']),
        'forehand_avg': _mean(by_type['forehand']),
        'backhand_avg': _mean(by_type['backhand']),
        'rally_max': rally_max(swings),
    }


def rally_max(swings):
    """最长相持拍数：相邻击球间隔 < RALLY_GAP_SEC 记作同一回合。"""
    times = sorted(t for t in (_f((s or {}).get('impactTime')) for s in swings or [])
                   if t is not None)
    if not times:
        return None
    best = cur = 1
    for prev, cur_t in zip(times, times[1:]):
        cur = cur + 1 if (cur_t - prev) < RALLY_GAP_SEC else 1
        best = max(best, cur)
    return best


# --------------------------------------------------------------------------- #
# 训练形式推断
# --------------------------------------------------------------------------- #
def infer_session_type(agg, duration_sec):
    """采集端未标注训练形式时，按击球构成推断。

    发球占比高 → 发球训练；各类均衡且发球少 → 对拉；
    相持长（rally 高）→ 实战对抗；其余归专项练习。判定结果会写进
    session_type，但调用方应优先使用用户手选值。
    """
    total = sum(agg['counts'].values())
    if not total:
        return 'drill'
    serve_ratio = agg['counts']['serve'] / total
    if serve_ratio >= 0.25:
        return 'serve'
    if (agg['rally_max'] or 0) >= 12:
        return 'match'
    if 0.06 <= serve_ratio < 0.25:
        return 'rally'
    return 'drill'


# --------------------------------------------------------------------------- #
# 主映射
# --------------------------------------------------------------------------- #
def _now_tag():
    return datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')


def build_operations(session, *, student_id, source=DEFAULT_SOURCE,
                     include_strokes=True, force=False, title=None,
                     location=None, court_type=None, session_type=None):
    """把一份采集端 MatchSession 翻译成 sync operation 数组。

    :param session:  采集端整包会话（含 swings 逐拍数组）
    :param student_id: 归属学员（必填，sync 侧 required 字段）
    :param include_strokes: 是否把逐拍明细一并上行（体量大时可关）
    :param force:    强制覆盖已入库的同一场次（op_id 加时间戳，走 LWW）
    """
    if not isinstance(session, dict):
        raise ValueError('session 必须是对象')
    ext_id = str(session.get('id') or '').strip()
    if not ext_id:
        raise ValueError('session.id 必填 —— 它是跨端幂等键，缺了会导致重复入库')
    if not student_id:
        raise ValueError('student_id 必填')

    swings = session.get('swings') or []
    agg = summarize_swings(swings)

    started = db.normalize_ts(session.get('startedAt'))
    ended = db.normalize_ts(session.get('endedAt')) or started
    if not started:
        raise ValueError('session.startedAt 缺失或无法解析为时间')

    duration_sec = _i(session.get('duration'))
    if duration_sec is None and started and ended:
        duration_sec = _i(_seconds_between(started, ended))

    # 版本戳：用会话结束时间。同一场重复上传 → 同一 op_id → 命中幂等闸。
    client_ts = db.utcnow() if force else ended
    tag = ('-%s' % _now_tag()) if force else ''

    sid = SESSION_PREFIX + ext_id
    payload = {
        'student_id': student_id,
        'title': title or _ttl(session.get('title')) or 'Apple Watch 训练',
        'session_type': session_type or _ttl(session.get('sessionType'))
                        or infer_session_type(agg, duration_sec),
        'location': location if location is not None else _ttl(session.get('location')),
        'court_type': court_type if court_type is not None else _ttl(session.get('courtType')),
        'started_at': started,
        'ended_at': ended,
        'duration_sec': duration_sec,
        # stroke_count = 采集端**检测到的总挥拍数**（含 type=unknown 的拍）。
        # 六个分项计数只统计**已成功分类**的拍，因此：
        #     六类之和 ≤ stroke_count，差额 = 未识别拍数
        # 这个差额就是 G2 配平门禁要看的「识别率」，不是数据错误。
        'stroke_count': len(swings) or None,
        # 采集侧元数据
        'worn_wrist': _ttl(session.get('wrist')),
        'source': source,
        'external_id': ext_id,
        # 会话级生理与负荷
        'avg_hr': _i(session.get('avgHeartRate')),
        'max_hr': _i(session.get('maxHeartRate')),
        'calories_kcal': _f(session.get('activeCalories') if
                            session.get('activeCalories') is not None
                            else session.get('calories')),
        'distance_km': _f(session.get('distanceKm')),
        # 球速（拍头线速度口径，见 docstring 说明）
        'peak_speed_kmh': agg['peak'],
        'avg_speed_kmh': agg['avg'],
        'serve_peak_kmh': agg['serve_peak'],
        'serve_avg_kmh': agg['serve_avg'],
        'forehand_avg_kmh': agg['forehand_avg'],
        'backhand_avg_kmh': agg['backhand_avg'],
        'rally_max': agg['rally_max'],
    }
    payload.update(agg['count_fields'])
    # 采集端没采到的维度一律不写（缺失 ≠ 0，写 0 会污染均值与配平门禁）
    payload = {k: v for k, v in payload.items() if v is not None}

    ops = [{
        'operation_id': 'ing-%s-s%s' % (ext_id, tag),
        'entity_type': 'training_session',
        'entity_id': sid,
        'action': 'create',
        'client_updated_at': client_ts,
        'payload': payload,
    }]

    if include_strokes:
        for idx, s in enumerate(swings, 1):
            if not isinstance(s, dict):
                continue
            t = (s.get('type') or '').strip().lower()
            if t not in STROKE_TYPES:
                continue                      # unknown 不入库，但仍计入 stroke_count
            impact = _f(s.get('impactTime'))
            ops.append({
                'operation_id': 'ing-%s-k%04d%s' % (ext_id, idx, tag),
                'entity_type': 'stroke_record',
                'entity_id': '%s%s-%04d' % (STROKE_PREFIX, ext_id, idx),
                'action': 'create',
                'client_updated_at': client_ts,
                'payload': {
                    'session_id': sid,
                    'seq_in_session': idx,
                    'stroke_type': t,
                    'is_slice': 1 if t == 'slice' else 0,
                    'speed_kmh': _f(s.get('racketHeadSpeedKmh')) or _f(s.get('speed_kmh')),
                    'confidence': _f(s.get('confidence')),
                    'impact_ms': _i(impact * 1000) if impact is not None else None,
                    # 以下维度手腕单点 IMU 测不到，采集端不产出 → 保持缺失：
                    # spin_rpm / spin_type / sweet_spot / depth_m /
                    # landing_zone / lateral_offset_m / net_clearance_m / anomaly
                },
            })
    return ops


def _seconds_between(a, b):
    try:
        da = datetime.strptime(a, '%Y-%m-%dT%H:%M:%S.%fZ')
        dbb = datetime.strptime(b, '%Y-%m-%dT%H:%M:%S.%fZ')
        return (dbb - da).total_seconds()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 落库入口
# --------------------------------------------------------------------------- #
def ingest_session(user_id, session, *, student_id, device_id=None,
                   source=DEFAULT_SOURCE, include_strokes=True, force=False,
                   title=None, location=None, court_type=None, session_type=None):
    """把一个采集端会话接入后端，返回 sync.push 的汇总结果。

    分批推送以免超过单批 500 条上限（一场 400 拍 = 401 条 operation）。
    """
    ops = build_operations(session, student_id=student_id, source=source,
                           include_strokes=include_strokes, force=force,
                           title=title, location=location, court_type=court_type,
                           session_type=session_type)
    # 落库前记录该场是否已存在 —— 调用方据此区分「首次创建」与「覆盖重传」
    existed = db.query_one(
        'SELECT id FROM training_sessions WHERE id = ?',
        (ops[0]['entity_id'],)) is not None
    total = {'received_at': None, 'total': 0, 'applied': 0, 'duplicate': 0,
             'conflict_lost': 0, 'rejected': 0, 'cursor': 0, 'results': [],
             'existed': existed}
    for i in range(0, len(ops), _PUSH_CHUNK):
        r = sync.push(user_id, ops[i:i + _PUSH_CHUNK], device_id)
        for key in ('total', 'applied', 'duplicate', 'conflict_lost', 'rejected'):
            total[key] += r[key]
        total['received_at'] = r['received_at']
        total['cursor'] = r['cursor']
        total['results'].extend(r['results'])
    total['session_id'] = ops[0]['entity_id']
    total['external_id'] = str(session.get('id') or '')
    total['stroke_ops'] = len(ops) - 1
    return total


def resolve_student_id(user_id, student_id=None):
    """确定会话归属学员：显式指定优先，否则回落到该用户的第一个学员。"""
    sid = (student_id or '').strip()
    if sid:
        row = db.query_one(
            'SELECT id FROM students WHERE id = ? AND deleted_at IS NULL', (sid,))
        if row:
            return sid
    row = db.query_one(
        'SELECT id FROM students WHERE user_id = ? AND deleted_at IS NULL'
        ' ORDER BY name LIMIT 1', (user_id,))
    return row['id'] if row else None
