# -*- coding: utf-8 -*-
"""L3 终端展示层 —— 给微信端 / iOS App 用的数据接口。

============================================================================
 这一层和 L0/L1 的分工
============================================================================
    L0 原始层  var/raw/acemate_raw.db   只追加封存原始字节（server/rawstore.py）
    L1 标注层  var/annotation/…         逐拍真值标签，训练识别模型
    L2 分析层  var/acemate.db           **识别出来的结构化结果**（可重算）
    L3 本模块  只读 L2 + 复用 server/analytics.py

终端用户看到的必须是**识别之后、对用户有用的数据**，而不是原始整包——
所以这里一次都不读 `raw_sessions`，也不读标注库；历史、逐拍、纵向分析、
排行榜全部来自 L2。

============================================================================
 上传口做三件事（原先只做一件）
============================================================================
`POST /api/prod/sessions` 收到整包会话后：

  ① **归档**：原样封存进 L0（sha256 幂等，可日后重算）
  ② **结构化**：翻译成 training_sessions + stroke_records 写入 L2
     （复用 `server/ingest.py`，因此幂等 / changelog / LWW 语义与 App 同步一致）
  ③ 自动开通学员档案（openid 首次出现时，幂等）

原先它只把整包 JSON 塞进标注库的 `prod_sessions` 表 —— 终端用户读到的
永远是原文，拿不到任何分析。现在那条路径已废弃。

============================================================================
 鉴权
============================================================================
读接口：`Authorization: Bearer <token>`（由 `/api/wechat/login` 签发），
        openid 是**校验结果**而非入参；传 `?openid=` 只作一致性自检。
写接口：`X-Ingest-Key`（设备级共享口令，见 server/security.py）。
"""
import hashlib
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

from server import analytics, db, ingest, rawstore, security, sync
from server.annotation import prod_api

router = APIRouter(prefix='/api/prod', tags=['终端用户 L3'])

DEFAULT_STUDENT_NAME = '网脉用户'
TOP_N = 10          # 排行榜对终端用户只透出前 N 名


# --------------------------------------------------------------------------- #
# openid ↔ user_id / student_id 映射
# --------------------------------------------------------------------------- #
def user_id_of(openid):
    """openid → L2 分析库里的租户 id。

    加 `wx_` 前缀是为了与 AceMate 自身的演示账号（`u_demo`）以及其它
    接入方隔离 —— L2 的每张表都靠 `user_id` 做多租户过滤。
    """
    return 'wx_' + (openid or '')


def student_id_of(openid):
    """openid → 学员 id。**由 openid 派生**，因此天然幂等、可重复调用。"""
    return 'stu-wx-' + hashlib.sha1((openid or '').encode('utf-8')).hexdigest()[:12]


def ensure_student(openid, name=None):
    """保证该 openid 名下有一个学员档案。返回 (student_id, created)。

    走 `sync.push` 而不是裸 INSERT：这样学员档案会进 changelog，
    小程序端增量拉取时能同步到，不会出现「App 有、小程序没有」。
    """
    sid = student_id_of(openid)
    row = db.query_one('SELECT id FROM students WHERE id=? AND deleted_at IS NULL', (sid,))
    if row:
        return sid, False

    ops = [{
        'operation_id': 'prov-%s' % sid,       # 固定 op_id → 天然幂等
        'entity_type': 'student_profile',
        'entity_id': sid,
        'action': 'create',
        'client_updated_at': db.utcnow(),
        'payload': {
            'name': name or DEFAULT_STUDENT_NAME,
            'avatar_initial': (name or DEFAULT_STUDENT_NAME)[:1],
            'tier': 'Standard',
            'source_note': 'openid 首次上传时自动开通',
        },
    }]
    sync.push(user_id_of(openid), ops, 'server-auto-provision')
    return sid, True


def _require_student(authorization, openid_q):
    """统一入口：校验令牌 → 拿到 openid → 保证学员存在。"""
    owner = prod_api.authorize(authorization, openid_q)
    sid, _ = ensure_student(owner)
    return owner, sid


def _rng(range_key, date_from, date_to):
    """时间档位：7 / 14 / 30 / all，或传 date_from/date_to 走自定义区间。"""
    return analytics.resolve_range(range_key or '30', date_from, date_to)


def _range_filter(alias, rng):
    """与 analytics._range_clause 同口径：用 date() 归一化后再比。

    直接拿 '2026-09-28' 与 '2026-09-28T10:00:00.000Z' 做字符串比较，
    同一天的记录会因为「前缀相同但更长」而被判为大于，从而漏掉最后一天。
    """
    sql, params = [], []
    if rng.get('from'):
        sql.append('date(%s.started_at) >= ?' % alias)
        params.append(rng['from'])
    if rng.get('to'):
        sql.append('date(%s.started_at) <= ?' % alias)
        params.append(rng['to'])
    return ((' AND ' + ' AND '.join(sql)) if sql else ''), params


# --------------------------------------------------------------------------- #
# ① 上传（设备口）
# --------------------------------------------------------------------------- #
@router.post('/sessions', summary='上传一场训练（归档 L0 + 结构化 L2）')
def upload_session(
    body: dict = Body(..., description='{ openid, session, student_id? }'),
    x_ingest_key: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
):
    security.require_ingest_key(x_ingest_key)

    openid = (body or {}).get('openid')
    session = (body or {}).get('session')
    if not openid or not isinstance(session, dict):
        raise HTTPException(400, 'openid 与 session 必填')

    ext_id = str(session.get('id') or '').strip()
    if not ext_id:
        raise HTTPException(400, 'session.id 必填 —— 它是跨端幂等键')

    # ① 归档进 L0（原样字节，只追加）。幂等：同形态同字节重传不产生新行。
    #    形态标记为 match_session：它没有 samples 波形，与标注工作台传的
    #    raw_package 分属两条版本线，互不覆盖。
    archived = rawstore.archive(
        session, session_id=ext_id, shape=rawstore.SHAPE_MATCH_SESSION,
        openid=openid, source=ingest.DEFAULT_SOURCE, device_id=x_device_id)

    # ③ 保证学员档案存在
    sid, provisioned = ensure_student(openid, (body or {}).get('student_name'))

    # ② 结构化进 L2（复用采集端接入的翻译层：幂等 / changelog / LWW 全继承）
    try:
        result = ingest.ingest_session(
            user_id_of(openid), session,
            student_id=(body or {}).get('student_id') or sid,
            device_id=x_device_id,
            source=ingest.DEFAULT_SOURCE,
            include_strokes=bool((body or {}).get('include_strokes', True)),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        'ok': True,
        'session_id': result['session_id'],
        'external_id': result['external_id'],
        'strokes_ingested': result['stroke_ops'],
        'analysis_db': {'applied': result['applied'], 'duplicate': result['duplicate']},
        'raw_layer': {
            'status': archived['status'],          # created | duplicate
            'raw_id': archived['raw_id'],
            'revision': archived['revision'],
            'sha256': archived['sha256'],
            'byte_size': archived['byte_size'],
        },
        'student_id': sid,
        'student_provisioned': provisioned,
    }


# --------------------------------------------------------------------------- #
# ② 身份
# --------------------------------------------------------------------------- #
@router.get('/me', summary='当前登录身份与可用的数据范围')
def me(authorization: Optional[str] = Header(None),
       openid: Optional[str] = Query(None)):
    owner = prod_api.authorize(authorization, openid)
    sid, created = ensure_student(owner)
    stu = db.query_one(
        'SELECT id, name, avatar_url, avatar_initial, tier, nt_level, hand,'
        ' backhand, years_playing, racket FROM students WHERE id=?', (sid,)) or {}
    n = db.query_one(
        'SELECT COUNT(*) AS n FROM training_sessions'
        ' WHERE user_id=? AND deleted_at IS NULL', (user_id_of(owner),))
    return {
        'openid': owner,
        'user_id': user_id_of(owner),
        'student': stu,
        'provisioned': created,
        'session_count': n['n'] if n else 0,
    }


@router.post('/profile', summary='更新昵称等基础档案（终端用户自助）')
def update_profile(body: dict = Body(...),
                   authorization: Optional[str] = Header(None),
                   openid: Optional[str] = Query(None)):
    owner = prod_api.authorize(authorization, openid)
    sid, _ = ensure_student(owner)
    payload = {}
    for key in ('name', 'avatar_url', 'hand', 'backhand', 'years_playing',
                'racket', 'nt_level'):
        if (body or {}).get(key) not in (None, ''):
            payload[key] = (body or {})[key]
    if not payload:
        raise HTTPException(400, '没有可更新的字段')
    if 'name' in payload:
        payload.setdefault('avatar_initial', payload['name'][:1])

    sync.push(user_id_of(owner), [{
        'operation_id': 'prof-%s-%s' % (sid, db.utcnow()),
        'entity_type': 'student_profile',
        'entity_id': sid,
        'action': 'update',
        'client_updated_at': db.utcnow(),
        'payload': payload,
    }], 'terminal-user')
    return {'ok': True, 'student_id': sid, 'updated': sorted(payload)}


# --------------------------------------------------------------------------- #
# ③ 训练历史（读 L2，不再是整包 JSON 原文）
# --------------------------------------------------------------------------- #
_SESSION_COLS = (
    'id', 'external_id', 'student_id', 'title', 'session_type', 'location',
    'court_type', 'started_at', 'ended_at', 'duration_sec', 'worn_wrist',
    'source', 'stroke_count', 'forehand_count', 'backhand_count', 'serve_count',
    'slice_count', 'volley_count', 'smash_count', 'rally_max',
    'avg_hr', 'max_hr', 'calories_kcal', 'distance_km',
    'avg_speed_kmh', 'peak_speed_kmh', 'forehand_avg_kmh', 'backhand_avg_kmh',
    'serve_avg_kmh', 'serve_peak_kmh', 'spin_rpm', 'sweet_spot_rate',
)


def _duration_label(sec):
    if not sec:
        return '—'
    sec = int(sec)
    if sec < 60:
        return '%d 秒' % sec
    if sec < 3600:
        return '%d 分 %d 秒' % (sec // 60, sec % 60)
    return '%d 时 %02d 分' % (sec // 3600, (sec % 3600) // 60)


@router.get('/sessions', summary='训练历史（含会话级指标）')
def list_sessions(authorization: Optional[str] = Header(None),
                  openid: Optional[str] = Query(None),
                  range: str = Query('all', description='all | 7 | 14 | 30 | custom'),
                  date_from: Optional[str] = Query(None),
                  date_to: Optional[str] = Query(None),
                  limit: int = Query(100, ge=1, le=500)):
    owner, sid = _require_student(authorization, openid)
    uid = user_id_of(owner)
    rng = _rng(range, date_from, date_to)

    sql = ('SELECT %s FROM training_sessions WHERE user_id=? AND deleted_at IS NULL'
           % ', '.join(_SESSION_COLS))
    params = [uid]
    clause, cparams = _range_filter('training_sessions', rng)
    sql += clause
    params += cparams
    sql += ' ORDER BY started_at DESC LIMIT ?'
    params.append(int(limit))

    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        counts = {t: r['%s_count' % t] for t in
                  ('forehand', 'backhand', 'serve', 'slice', 'volley', 'smash')}
        out.append({
            'id': r['id'],
            'externalId': r['external_id'],
            'title': r['title'],
            'sessionType': r['session_type'],
            'location': r['location'],
            'startedAt': r['started_at'],
            'endedAt': r['ended_at'],
            'duration': r['duration_sec'],
            'durationLabel': _duration_label(r['duration_sec']),
            'wrist': r['worn_wrist'],
            'counts': counts,
            'total': r['stroke_count'],
            # 会话级指标 —— 原先终端用户一个都拿不到
            'avgHeartRate': r['avg_hr'],
            'maxHeartRate': r['max_hr'],
            'calories': r['calories_kcal'],
            'distanceKm': r['distance_km'],
            'avgSpeedKmh': r['avg_speed_kmh'],
            'peakSpeedKmh': r['peak_speed_kmh'],
            'servePeakKmh': r['serve_peak_kmh'],
            'rallyMax': r['rally_max'],
            'sweetSpotRate': r['sweet_spot_rate'],
        })

    return {'count': len(out), 'range': rng['key'], 'studentId': sid, 'sessions': out}


@router.get('/sessions/{sid}', summary='单场详情（含逐拍明细与击球构成）')
def session_detail(sid: str,
                   authorization: Optional[str] = Header(None),
                   openid: Optional[str] = Query(None)):
    owner, _ = _require_student(authorization, openid)
    uid = user_id_of(owner)

    # 终端用户拿到的 sid 是**采集端会话 id**（external_id）；也允许直接传 L2 主键
    row = db.query_one(
        'SELECT %s FROM training_sessions WHERE user_id=? AND deleted_at IS NULL'
        ' AND (id=? OR external_id=?)' % ', '.join(_SESSION_COLS),
        (uid, sid, sid))
    if not row:
        raise HTTPException(404, '找不到该场训练（或不属于当前账号）')

    strokes = db.query(
        'SELECT seq_in_session, stroke_type, is_slice, speed_kmh, spin_rpm,'
        ' spin_type, sweet_spot, depth_m, landing_zone, lateral_offset_m,'
        ' net_clearance_m, impact_ms, confidence, anomaly'
        ' FROM stroke_records WHERE session_id=? AND deleted_at IS NULL'
        ' ORDER BY seq_in_session', (row['id'],))

    # 击球构成：按类型计数 + 均速 + 极速（终端用户最直观的一张图）
    mix = {}
    for st in strokes:
        t = st['stroke_type']
        b = mix.setdefault(t, {'type': t, 'count': 0, 'speeds': []})
        b['count'] += 1
        if st['speed_kmh'] is not None:
            b['speeds'].append(st['speed_kmh'])
    mix_out = []
    for t, b in mix.items():
        sp = b.pop('speeds')
        b['avgSpeedKmh'] = round(sum(sp) / len(sp), 1) if sp else None
        b['peakSpeedKmh'] = max(sp) if sp else None
        b['share'] = round(b['count'] * 100.0 / len(strokes), 1) if strokes else 0
        mix_out.append(b)
    mix_out.sort(key=lambda x: -x['count'])

    return {
        'session': dict(row),
        'durationLabel': _duration_label(row['duration_sec']),
        'strokeCount': len(strokes),
        'strokes': [dict(s) for s in strokes],
        'strokeMix': mix_out,
    }


# --------------------------------------------------------------------------- #
# ④ 纵向分析 / 排行榜（复用管理台同一套 analytics）
# --------------------------------------------------------------------------- #
@router.get('/analysis', summary='纵向技术分析（KPI + 构成 + 趋势 + 洞察）')
def analysis(authorization: Optional[str] = Header(None),
             openid: Optional[str] = Query(None),
             range: str = Query('30', description='7 | 14 | 30 | all'),
             date_from: Optional[str] = Query(None),
             date_to: Optional[str] = Query(None)):
    owner, sid = _require_student(authorization, openid)
    rng = _rng(range, date_from, date_to)
    data = analytics.student_analysis(sid, rng)
    if not data:
        raise HTTPException(404, '找不到该学员')
    return data


@router.get('/leaderboard', summary='排行榜（对终端用户匿名化）')
def leaderboard(authorization: Optional[str] = Header(None),
                openid: Optional[str] = Query(None),
                range: str = Query('30', description='7 | 14 | 30 | all'),
                metric: Optional[str] = Query(None),
                date_from: Optional[str] = Query(None),
                date_to: Optional[str] = Query(None),
                top: int = Query(TOP_N, ge=1, le=50)):
    """**注意隐私**：管理台的榜单带完整姓名与头像，终端用户看到的必须匿名化。

    只透出：名次、脱敏昵称、数值、是否是自己。不透出 student_id / 头像 /
    设备型号等可定位到具体个人的字段 —— 否则任何人都能通过榜单反查他人。
    """
    owner, sid = _require_student(authorization, openid)
    rng = _rng(range, date_from, date_to)
    data = analytics.leaderboards(rng, selected_id=sid, metric_key=metric)

    def _anon(items):
        out = []
        for it in items:
            is_me = it.get('student_id') == sid
            name = it.get('name') or '—'
            out.append({
                'rank': it.get('rank'),
                'name': name if is_me else _mask(name),
                'value': it.get('value'),
                'display': it.get('display'),
                'n': it.get('n'),
                'isMe': is_me,
            })
        # 前 top 名 + 自己（自己若在榜外也要能看到自己的名次）
        head = out[:top]
        mine = next((o for o in out if o['isMe']), None)
        if mine and mine not in head:
            head = head + [mine]
        return head

    m = data['active_metric']
    return {
        'range': rng['key'],
        'metric': {
            'key': m['key'], 'label': m['label'], 'unit': m.get('unit'),
            'better': m.get('better'), 'note': m.get('note'),
        },
        'metricTabs': [{'key': t['key'], 'label': t['label'], 'icon': t['icon'],
                        'count': t['count'], 'active': t['active']}
                       for t in data['metric_tabs']],
        'maxBoard': _anon(data['max_board']),
        'avgBoard': _anon(data['avg_board']),
        'myRank': next((o['rank'] for o in _anon(data['max_board']) if o['isMe']), None),
        'total': len(data['max_board']),
        'empty': data['empty'],
    }


def _mask(name):
    """中文姓名脱敏：保留姓，其余打星。英文名保留首字母。"""
    n = (name or '').strip()
    if not n:
        return '—'
    if len(n) == 1:
        return n
    if n[0].isascii():
        return n[0] + '*' * max(1, len(n) - 1)
    return n[0] + '*' * (len(n) - 1)
