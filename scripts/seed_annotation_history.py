# -*- coding: utf-8 -*-
"""seed_annotation_history.py — 往标注库灌**历史采集数据**（16 场，挂在 5 位学员名下）。

    # 1) 先看会写什么（默认 dry-run，不碰库）—— 会打印完整的元数据数字表
    .venv/bin/python scripts/seed_annotation_history.py

    # 2) 真正写入
    .venv/bin/python scripts/seed_annotation_history.py --apply

    # 3) 完全不碰真实库（四个库/目录重定向到临时目录）
    NETPULSE_DB=/tmp/ann-hist/annotations.db \
    NETPULSE_ANNOTATION_DIR=/tmp/ann-hist \
    NETPULSE_RAW_DIR=/tmp/ann-hist/raw \
    ACEMATE_DB=/tmp/ann-hist/acemate.db \
    .venv/bin/python scripts/seed_annotation_history.py --apply

为什么需要它
------------
`/annotation`（数据采集与标注）默认只有联调时手工上传的零散几场，会话清单看不出
「这是谁的训练、采了多少场」。本脚本按产品里 5 位真实学员（与 /users 的用户管理
同一批人，id 来自 server/seed.py 的 STUDENTS）造出他们的**历史采集记录**：

    张哲恒 5 场 · 李思源 4 场 · 陈雨菲 3 场 · 王浩然 2 场 · 赵明 2 场   = 16 场

每场都会：
  · 合成一份 raw 包（会话 + 逐拍 swing + 波形 samples）封存进 L0 原始层；
  · 写入标注库 sessions 行，`player_id` 指向学员 id（页面从而显示学员姓名）；
  · 逐拍写入启发式预标注（`heuristic/auto/proposed`）；部分场次叠加人工纠错，
    让「待纠错 / 人工标注数 / 一致率」不至于全 0 或全满。

⛳ 2026-10-03 新增：**同一场同时产出训练元数据**
-----------------------------------------------
上面那条链路只到「标注库」为止，产出的是一份 raw 包和一堆逐拍标注 ——
**没有任何训练元数据**（时长 / 心率 / 球速 / 击球量…）。于是"这 16 场"在
`/training` 的分析与排行里根本不存在，也没办法一眼看出「每个数字是多少、
下一批采集该采哪些字段」。

所以现在每场还会：
  · 生成一条 `training_sessions` 行（分析库 var/acemate.db，id = `hist-*`）；
  · 抽稀出 12 条 `stroke_records` 逐拍样本（与 server/seed.py 的口径一致：
    会话汇总字段是**全量计数**，逐拍表是**抽样**，两者分母不同）；
  · 字段严格按 server/datasources.py 的登记表来 —— **只写登记表里"腕部可得"
    的字段**。甜区 / 转速 / 落点 / 失误 / 制胜分等不可得字段一律不写（恒 NULL），
    与真实上传路径 server/ingest.py 完全一致。

dry-run 会把这些数字打成一张表，「哪些字段是必采、哪些本批有值」一目了然。
页面上同一张表见 `/annotation` 的「历史采集元数据」区块。

⚠️ 造的是**合成数据**：波形是叠加正弦的伪信号；逐拍时刻是按会话时长均匀铺开的，
   不是真实击球时刻；球速/心率由学员档位中枢加噪声生成。别拿它当物理真值。
⚠️ 会话 id 一律以 `hist-` 开头，与 `seed_annotation_demo.py` 的 `demo-` 互不干扰。
⚠️ 本脚本**没有**删除入口：删数据永远由人来做（清理语句见文件末尾）。
"""
import argparse
import gzip
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from server import rawstore                                    # noqa: E402
from server import db as analysis_db                           # noqa: E402
# 六类击球分项计数必须**六者之和恰好等于击球总数**（G2 配平门禁的前提），
# 这个「最大余数法」的实现只应有一处 —— 直接复用 seed.py 的那一份，
# 否则我这边算出的分项会在缓存/门禁处被判 suspect。
from server.seed import _split_counts, build_strokes           # noqa: E402
from server.annotation.db import DATA_DIR, get_conn, init_db    # noqa: E402
from seed_annotation_demo import POOL, WRONG_PICK, make_samples  # noqa: E402

# 5 位学员：(标注库 player_id, 姓名, 场次, 持拍, 起始日期偏移天数)
# player_id 必须与 server/seed.py 的 `stu-%08d`(1001+i) 对齐，页面才能把
# 会话挂到 /users 里同一个人身上。
STUDENT_PLAN = [
    ('stu-00001001', '张哲恒', 5, 'right', 0),
    ('stu-00001002', '李思源', 4, 'right', 1),
    ('stu-00001003', '陈雨菲', 3, 'left', 2),
    ('stu-00001004', '王浩然', 2, 'right', 3),
    ('stu-00001005', '赵明', 2, 'right', 4),
]

# 学员档位中枢。**只借它定"大概什么水平"**，不是照抄档案值 ——
# /users 里的 serve_peak / forehand_avg 是**人工维护的展示值**，
# /training 明确声明不采用（见该页「统计口径」第③条）。
# 这里取档位、留出随机波动，让 mock 出来的数字量级可信。
STUDENT_PROFILE = {
    'stu-00001001': {'serve': 168.0, 'fore': 118.0, 'avg_hr': 150, 'max_hr': 186},
    'stu-00001002': {'serve': 132.0, 'fore': 96.0, 'avg_hr': 136, 'max_hr': 168},
    'stu-00001003': {'serve': 179.0, 'fore': 129.0, 'avg_hr': 156, 'max_hr': 192},
    'stu-00001004': {'serve': 158.0, 'fore': 112.0, 'avg_hr': 146, 'max_hr': 180},
    'stu-00001005': {'serve': 115.0, 'fore': 84.0, 'avg_hr': 126, 'max_hr': 156},
}

SESSION_TITLES = {'drill': '底线多拍专项', 'match': '实战对抗',
                  'rally': '底线对拉', 'serve': '发球专项加练'}
COURT_TYPES = ['hard', 'clay', 'indoor']
LOCATIONS = ['深圳湾体育中心', '广州天河网球场', '杭州黄龙体育馆',
             '上海仙霞网球中心', '北京国家网球中心']

# 波形存储率：采集端是 800Hz，但这里是**历史存档演示**。
# 5Hz 足够画波形，并且压缩后每场只占几百 KB；按 800Hz 存会造出几十 MB。
STORE_STEP = 160          # 800 / 160 = 5 Hz

# 逐拍样本条数与 server/seed.py 的 build_strokes(per_session=12) 保持一致，
# 这样「会话汇总 vs 逐拍抽样」两个口径的差异不会被 mock 自己的参数掩盖。
STROKES_PER_SESSION = 12


def utc_iso(dt):
    return dt.replace(tzinfo=None).isoformat()


def human_window(n, rng, force=False):
    """给出「人工介入」的条号范围；空列表表示这场没人工介入。

    设计成：越早采集的场次越可能被审过 —— 符合「历史数据大多已标完、
    最近的还在队列里」的真实分布。`force=True`（该学员最早那场）必定被审阅，
    保证每位学员至少有一场能算出一致性评分。
    """
    if not force and rng.random() < 0.4:
        return []
    k = rng.randint(max(2, n // 6), max(3, n // 2))
    start = rng.randint(0, max(0, n - k))
    return list(range(start, start + k))


def ts_iso(dt):
    """分析库统一的时间写法：ISO8601 UTC 带毫秒与 Z（见 server/db.py.utcnow）。"""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def zone_split(stype, rnd):
    """按训练形式把整场时间分到 5 个心率区间，返回占比（五个数之和恰为 100）。

    口径与 seed.py 的 hr_zone 一致：存的是**百分比**，不是秒数。
    """
    base = {
        'drill': (14, 28, 34, 19, 5),
        'match': (6, 16, 26, 34, 18),
        'rally': (9, 22, 34, 27, 8),
        'serve': (22, 38, 28, 10, 2),
    }[stype]
    vals = [max(1, v + rnd.randint(-3, 3)) for v in base]
    # 把抖动带来的偏差补回「占比最高的那一档」，保证总和仍是 100
    top = vals.index(max(vals))
    vals[top] += 100 - sum(vals)
    return {'zone%d' % (i + 1): v for i, v in enumerate(vals)}


def build_meta(student, sid, wrist, dur, n_swings, started, ends, rng, prev_stype=None):
    """给一场历史采集生成**训练元数据**（= training_sessions 的一行）。

    ⚠️ 只写 server/datasources.py 登记表里「腕部单点 IMU + HealthKit 可得」的字段。
       甜区 / 转速 / 落点 / 失误 / 制胜分等不可得字段**一个都不写**（列留空 = NULL），
       与真实上传路径 server/ingest.py 保持一致 —— 这样这份 mock 才能当
       「后续采集该采什么」的样板，而不是又一个用假数字撑场面的表。

    返回 (row, sample_counts)；row 的键名与 training_sessions 的列名一一对应。

    ⚠️ 两个「来源类」字段的写法是**故意**的，不要顺手改成看起来更真的值：
      · source='seed_history' —— 这一列的全部职责就是记录**数据是谁产出的**。
        本批是脚本合成的，就必须显示成合成的；写成 netpulse_watch 会让
        「按来源过滤」这个查询把 mock 当成真实手表上传。
        （真实上传路径写的是 netpulse_watch，见 server/ingest.py:45）
      · external_id=sid —— 该列是幂等去重键，不承担溯源职责，所以照实填。
        真实采集端这里是一个 UUID（会话 id 则是 'nps-' + external_id），
        本脚本没有 UUID 可言，直接用可读的会话号，一眼能看出是演示数据。
    """
    pid, name, _n, _w, _off = student
    prof = STUDENT_PROFILE[pid]

    # 训练形式：随机但与「上一场」错开，避免连场同类型
    pool = ['drill', 'rally', 'match', 'serve']
    if prev_stype in pool:
        pool.remove(prev_stype)
    stype = rng.choice(pool)
    title = SESSION_TITLES[stype]

    # 心率：以学员中枢为基准，按形式加减强度
    bias = {'drill': -4, 'rally': -1, 'match': +7, 'serve': -9}[stype]
    avg_hr = prof['avg_hr'] + bias + rng.randint(-5, 5)
    max_hr = min(198, prof['max_hr'] + bias // 2 + rng.randint(-4, 6))
    avg_hr = min(avg_hr, max_hr - 8)

    # 卡路里 / 跑动距离：与时长和强度挂钩（kcal/min 落在合理文献区间）
    kcal_per_min = {'drill': 8.2, 'rally': 9.4, 'match': 11.6, 'serve': 6.1}[stype]
    calories = round(dur / 60.0 * (kcal_per_min + rng.uniform(-1.0, 1.2)), 1)
    distance = round(dur / 3600.0 * rng.uniform(2.2, 4.2), 2)

    # 球速：以档位中枢为基准
    serve_peak = round(prof['serve'] * rng.uniform(0.94, 1.03), 1)
    fore = round(prof['fore'] * rng.uniform(0.93, 1.05), 1)
    peak_speed = round(serve_peak + rng.uniform(0, 12), 1)

    counts = _split_counts(n_swings)
    row = dict(
        id=sid, user_id='u_demo', student_id=pid, title=title, session_type=stype,
        location='%s · 场地 %d' % (rng.choice(LOCATIONS), rng.randint(1, 8)),
        court_type=rng.choice(COURT_TYPES),
        started_at=ts_iso(started), ended_at=ts_iso(ends),
        duration_sec=dur, stroke_count=n_swings,
        worn_wrist=wrist, source='seed_history', external_id=sid,
        forehand_count=counts['forehand'], backhand_count=counts['backhand'],
        serve_count=counts['serve'], slice_count=counts['slice'],
        volley_count=counts['volley'], smash_count=counts['smash'],
        rally_max=rng.randint(6, 24),
        distance_km=distance, calories_kcal=calories,
        avg_hr=avg_hr, max_hr=max_hr,
        avg_speed_kmh=round((fore * 0.86 + serve_peak * 0.14), 1),
        peak_speed_kmh=peak_speed,
        forehand_avg_kmh=fore,
        backhand_avg_kmh=round(fore * rng.uniform(0.76, 0.90), 1),
        serve_avg_kmh=round(serve_peak * rng.uniform(0.85, 0.95), 1),
        serve_peak_kmh=serve_peak,
        hr_zone=json.dumps(zone_split(stype, rng), ensure_ascii=False),
        notes='%s · %s' % (name, title),
        # 硬件不可得 —— 显式留空，写在这里是为了让「为什么是 NULL」有据可依：
        # spin_rpm / sweet_spot_rate / unforced_errors / winners 需拍面传感器、
        # 球的飞行轨迹或计分，腕部单点 IMU 测不到。见 datasources.REMOVED。
        spin_rpm=None, sweet_spot_rate=None, unforced_errors=None, winners=None,
    )
    return row, counts


def build_session(student, idx, rng, day_offset, is_oldest=False, prev_stype=None):
    """生成一场历史采集会话：raw 包 + 逐条标注 + 训练元数据。

    `day_offset` 由调用方**累加**传入（每次 +2~4 天）。这样同一学员的场次按
    idx 严格单调变早 —— 否则「该学员最早那场」的判定（is_oldest）会名不副实，
    页面上按时间排序时也会出现后一场比前一场新的错乱。
    """
    _pid, name, _n, wrist, _off = student
    sid = 'hist-S%s-%02d' % (student[0][-2:], idx + 1)

    dur = rng.choice([2400, 2700, 3000, 3300, 3600, 4200, 4800])
    n_swings = rng.randint(70, 220)

    # 击球时刻均匀铺在会话内，两侧各留一点空白
    impacts = sorted(round(5.0 + i * (dur - 12.0) / n_swings + rng.uniform(-0.6, 0.6), 3)
                     for i in range(n_swings))
    swings = []
    for i, t in enumerate(impacts):
        swings.append({'type': POOL[i % len(POOL)], 'impactTime': t,
                       'confidence': round(rng.uniform(0.58, 0.97), 3)})

    # 采集时间：从「今天」往前铺，同日不再复用（偏移按天累加，至少隔 2 天）
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    started = base - timedelta(days=day_offset, hours=rng.choice([9, 16, 19]))
    ends = started + timedelta(seconds=dur)
    raw = {
        'session': {
            'id': sid,
            'wrist': wrist,
            'playerId': student[0],
            'playerName': name,
            'startedAt': utc_iso(started),
            'endedAt': utc_iso(ends),
            'duration': dur,
            'swings': swings,
        },
        'samples': make_samples(dur, step=STORE_STEP),
    }

    # 标注行：(impact_time, label, annotator, source, status, confidence)
    rows = [(t, sw['type'], 'heuristic', 'auto', 'proposed', sw['confidence'])
            for t, sw in zip(impacts, swings)]

    win = human_window(n_swings, rng, force=is_oldest)
    annotator = rng.choice(['小林', '老王', '阿May'])
    for j, i in enumerate(win):
        mode = rng.random()
        if mode < 0.7:              # 认可预标注
            t, lb, _a, _s, _st, c = rows[i]
            rows[i] = (t, lb, 'heuristic', 'auto', 'confirmed', c)
            rows.append((impacts[i], lb, annotator, 'human', 'confirmed', None))
        elif mode < 0.92:           # 纠错改标
            t, lb, _a, _s, _st, c = rows[i]
            new_lb = WRONG_PICK.get(lb, 'forehand')
            rows[i] = (t, lb, 'heuristic', 'auto', 'rejected', c)
            rows.append((impacts[i], new_lb, annotator, 'human', 'confirmed', None))
        else:                       # 判为误检，直接删掉预标注
            t, lb, _a, _s, _st, c = rows[i]
            rows[i] = (t, lb, 'heuristic', 'auto', 'rejected', c)

    meta, _counts = build_meta(student, sid, wrist, dur, n_swings, started, ends,
                               rng, prev_stype=prev_stype)
    return dict(sid=sid, wrist=wrist, name=name, raw=raw, rows=rows, started=started,
                annotator=annotator if win else None, meta=meta)


META_COLS = [
    ('采集时间', 'started_at', 16),
    ('形式', 'session_type', 6),
    ('时长', 'duration_sec', 6),
    ('击球量', 'stroke_count', 6),
    ('正/反', 'fa', 8),
    ('发/削', 'ss', 8),
    ('截/高', 'vs', 8),
    ('心率 均/峰', 'hr', 9),
    ('心率区间 z1-z5', 'hr_zone', 14),
    ('消耗kcal', 'calories_kcal', 8),
    ('距离km', 'distance_km', 7),
    ('最长相持', 'rally_max', 8),
    ('均速/极速', 'spd', 11),
    ('正手均速', 'forehand_avg_kmh', 8),
    ('反手均速', 'backhand_avg_kmh', 8),
    ('发球 均/峰', 'serve', 11),
]


def _meta_cells(m):
    """把一行元数据打平成与 META_COLS 对应的单元格字符串。"""
    z = json.loads(m['hr_zone'])
    return {
        'started_at': m['started_at'][5:16].replace('T', ' '),
        'session_type': m['session_type'],
        'duration_sec': '%d分' % round(m['duration_sec'] / 60.0),
        'stroke_count': str(m['stroke_count']),
        'fa': '%d/%d' % (m['forehand_count'], m['backhand_count']),
        'ss': '%d/%d' % (m['serve_count'], m['slice_count']),
        'vs': '%d/%d' % (m['volley_count'], m['smash_count']),
        'hr': '%d/%d' % (m['avg_hr'], m['max_hr']),
        'hr_zone': ','.join(str(z['zone%d' % i]) for i in range(1, 6)),
        'calories_kcal': '%.0f' % m['calories_kcal'],
        'distance_km': '%.2f' % m['distance_km'],
        'rally_max': str(m['rally_max']),
        'spd': '%.0f/%.0f' % (m['avg_speed_kmh'], m['peak_speed_kmh']),
        'forehand_avg_kmh': '%.0f' % m['forehand_avg_kmh'],
        'backhand_avg_kmh': '%.0f' % m['backhand_avg_kmh'],
        'serve': '%.0f/%.0f' % (m['serve_avg_kmh'], m['serve_peak_kmh']),
    }


def print_meta_table(built):
    """dry-run 时把「每个数字」打成表 —— 这正是这张 mock 存在的意义。"""
    print('=== 历史采集元数据（training_sessions，逐场逐字段的值）===')
    head = '%-14s %-4s ' % ('会话ID', '学员') + ' '.join(
        '%-*s' % (w, lab) for lab, _k, w in META_COLS)
    print(head)
    print('-' * len(head))
    for b in built:
        m = b['meta']
        c = _meta_cells(m)
        row = '%-14s %-4s ' % (b['sid'], b['name']) + ' '.join(
            '%-*s' % (w, c[k]) for _lab, k, w in META_COLS)
        print(row.rstrip())
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='真正写库；不给这个参数只打印将要做什么（dry-run）')
    ap.add_argument('--seed', type=int, default=20261002)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print('标注库： %s' % (os.environ.get('NETPULSE_DB')
                          or '<默认 var/annotation/annotations.db>'))
    print('数据目录：%s' % DATA_DIR)
    print('分析库： %s' % (os.environ.get('ACEMATE_DB') or analysis_db.DEFAULT_DB))
    print('模式：   %s' % ('APPLY（会写库）' if args.apply else 'dry-run（不写库）'))
    print()

    built = []
    for student in STUDENT_PLAN:
        prev = None
        day = student[4]          # 各学员的起始偏移错开，避免所有人都从同一天开始
        for i in range(student[2]):
            day += rng.choice([2, 3, 4])
            b = build_session(student, i, rng, day_offset=day,
                              is_oldest=(i == student[2] - 1), prev_stype=prev)
            prev = b['meta']['session_type']
            built.append(b)

    # ---- 预览：① 标注侧 -------------------------------------------------
    per = {}
    for b in built:
        rows = b['rows']
        props = sum(1 for r in rows if r[4] == 'proposed')
        rej = sum(1 for r in rows if r[4] == 'rejected')
        hum = sum(1 for r in rows if r[3] == 'human')
        per.setdefault(b['name'], []).append(
            (b['sid'], b['raw']['session']['duration'], len(rows), props, rej, hum,
             b['annotator']))
    print('=== ① 标注侧（标注库 sessions + annotations）===')
    for student in STUDENT_PLAN:
        name = student[1]
        print('%s（%d 场）' % (name, student[2]))
        for sid, dur, n, props, rej, hum, ann in per.get(name, []):
            print('   %-14s 时长 %4ds  标注 %3d 行｜待纠错 %3d / 误检 %2d / 人工 %3d  %s'
                  % (sid, dur, n, props, rej, hum, ('标注者 ' + ann) if ann else '未介入'))
    print()

    # ---- 预览：② 元数据侧 ----------------------------------------------
    print_meta_table(built)

    # ---- 预览：③ 字段覆盖 ----------------------------------------------
    print('=== ③ 字段覆盖自检（登记表里"该有值"的字段，本批有多少场真的写了）===')
    metas = [b['meta'] for b in built]
    n = len(metas)
    for key, lab in [('avg_hr', '平均心率'), ('max_hr', '最高心率'),
                     ('calories_kcal', '活动能量'), ('distance_km', '跑动距离'),
                     ('rally_max', '最长相持'), ('avg_speed_kmh', '整场平均球速'),
                     ('serve_peak_kmh', '发球峰值'), ('hr_zone', '心率区间分布'),
                     ('stroke_count', '击球总数')]:
        have = sum(1 for m in metas if m.get(key) is not None)
        print('   %-14s %2d/%d 场有值' % (lab, have, n))
    for key, lab in [('spin_rpm', '整场平均转速'), ('sweet_spot_rate', '甜区命中率'),
                     ('unforced_errors', '非受迫性失误'), ('winners', '制胜分')]:
        have = sum(1 for m in metas if m.get(key) is not None)
        print('   %-14s %2d/%d 场有值   ← 硬件不可得，**恒为空**（这是正确的）'
              % (lab, have, n))
    print()

    print('合计 %d 场会话。' % len(built))
    if not args.apply:
        print('以上为预览。确认无误后加 --apply 真正写入。')
        return 0

    # ---- 写盘 ----------------------------------------------------------
    # 与上传接口同一条路：先封存 L0（只追加、按 sha256 内容寻址），
    # 会话行只保存 raw_id 指针。这样历史数据与真实上传的数据形态完全一致。
    conn = get_conn()
    init_db(conn)
    rawstore.init_db()
    now = utc_iso(datetime.now(timezone.utc))
    for b in built:
        sid, wrist, name, raw, rows = b['sid'], b['wrist'], b['name'], b['raw'], b['rows']
        # 压成 gzip 再归档：rawstore 会识别 gzip magic，落文件但不内联，
        # 16 场也就几 MB（不压缩会是几十 MB）。
        blob = gzip.compress(json.dumps(raw, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8'), 6)
        archived = rawstore.archive(blob, session_id=sid, source='seed_history',
                                    filename='raw_%s.json.gz' % sid)
        conn.execute(
            'INSERT OR REPLACE INTO sessions'
            '(id, wrist, started_at, ended_at, duration, player_id,'
            ' raw_id, raw_path, video_path, created_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (sid, wrist, raw['session']['startedAt'], raw['session']['endedAt'],
             raw['session']['duration'], raw['session']['playerId'],
             archived['raw_id'], archived['file_path'], None, now))
        # 幂等：同会话重跑先清掉自己的标注，避免堆叠
        conn.execute('DELETE FROM annotations WHERE session_id=?', (sid,))
        for t, lb, a, src, st, cf in rows:
            conn.execute(
                'INSERT INTO annotations(session_id, impact_time, label, confidence,'
                ' annotator, source, status, created_at, updated_at)'
                ' VALUES(?,?,?,?,?,?,?,?,?)',
                (sid, t, lb, cf, a, src, st, now, now))
        print('写入 %-14s %s → L0 raw_id=%s（%d 条标注，%d 字节 gzip）'
              % (sid, name, archived['raw_id'][:24], len(rows),
                 archived['byte_size']))
    conn.commit()
    conn.close()

    # ---- 训练元数据（分析库）------------------------------------------
    # session_id 与标注侧同名，两边靠这个 id 对得上。
    aconn = analysis_db.connect()
    aconn.execute("DELETE FROM stroke_records WHERE session_id LIKE 'hist-%'")
    aconn.execute("DELETE FROM training_sessions WHERE id LIKE 'hist-%'")
    n_sess = n_str = 0
    for b in built:
        m = dict(b['meta'])
        t = m['started_at']
        m.update(dict(created_at=t, updated_at=t, deleted_at=None,
                      version=1, sync_status='synced', server_seq=None))
        cols = ','.join(m.keys())
        aconn.execute('INSERT OR REPLACE INTO training_sessions (%s) VALUES (%s)'
                      % (cols, ','.join('?' * len(m))), list(m.values()))
        n_sess += 1
    # 逐拍样本：复用 seed.py 的 build_strokes，保证「会话汇总 vs 逐拍抽样」
    # 两个口径的差异与真实数据完全一样（不是 mock 自己造的另一套规则）。
    rows_meta = analysis_db.query(
        "SELECT * FROM training_sessions WHERE id LIKE 'hist-%' ORDER BY id")
    for st in build_strokes(rows_meta, 'u_demo', seed=args.seed,
                            per_session=STROKES_PER_SESSION):
        st.update(dict(created_at=analysis_db.utcnow(),
                       updated_at=analysis_db.utcnow(), deleted_at=None,
                       version=1, sync_status='synced', server_seq=None))
        cols = ','.join(st.keys())
        aconn.execute('INSERT OR REPLACE INTO stroke_records (%s) VALUES (%s)'
                      % (cols, ','.join('?' * len(st))), list(st.values()))
        n_str += 1
    print()
    print('分析库写入 %d 场训练记录 + %d 条逐拍样本（%s）'
          % (n_sess, n_str, analysis_db.db_path()))
    print()
    print('完成。')
    print('  · 采集与标注：/annotation 的「标注进度」与「历史采集元数据」两块表')
    print('  · 训练分析：  /training 选中学员即可看到这 16 场')
    print('清理：DELETE FROM annotations WHERE session_id LIKE "hist-%%";'
          ' 再 DELETE FROM sessions WHERE id LIKE "hist-%%";')
    print('      DELETE FROM stroke_records WHERE session_id LIKE "hist-%%";'
          ' 再 DELETE FROM training_sessions WHERE id LIKE "hist-%%";')
    return 0


if __name__ == '__main__':
    sys.exit(main())
