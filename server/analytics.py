# -*- coding: utf-8 -*-
"""训练数据聚合层：个人纵向分析 + 跨学员横向排行。

===================== 口径说明（重要，改前必读） =====================
本模块**只依赖真实训练记录**，两个数据源：

  · 会话级  training_sessions   —— 每次训练的时长 / 卡路里 / 心率 /
                                   击球总数 / 甜区命中率 / 各路均速
  · 逐拍级  stroke_records      —— 单次击球的球速 / 转速 / 甜区 /
                                   落点 / 置信度（球速类指标的**唯一**依据）

刻意**不读** students 表里的 sessions_count / strokes_total / serve_peak /
forehand_avg / sweet_spot 等「档案汇总字段」。原因：那些是人工维护的展示值，
与真实记录**对不上**（实测：陈雨菲 serve_peak=179，但逐拍发球样本最高仅 151.6；
sessions_count=124 而实际只落库 8 场）。列表与排行榜一律现算，
保证页面上任何一个数字都能用一条 SQL 复现，不会出现「两个页面同一指标不同值」。

===================== 排行口径 =====================
每个指标出**两张榜**：
  · 最大值排行 —— 该学员在区间内达到的最好单次表现
                  （球速类=单拍最高；会话类=最好那一次）
  · 平均值排行 —— 该学员在区间内的整体平均水位
                  （球速类=全部样本均值；会话类=每场均值再平均）
两张榜方向都可正可负（心率越低越省力、击球量越高负荷越大），
故每个指标带 `better` 字段（'high' / 'low' / None）供前端标注语义，
但**排名本身一律按数值降序**，避免「越低越好」把榜单读反。
"""

from datetime import date, timedelta

from server import db

# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #
# 击球类型：颜色沿用设计系统（正手电光绿 / 反手蓝 / 发球紫 / 切削橙 /
# 截击灰 / 高压柔红），与前端小程序「击球分布」配色保持一致。
STROKE_TYPES = [
    {'key': 'forehand', 'label': '正手', 'label_en': 'Forehand',
     'color': '#c3f400', 'icon': 'sports_baseball'},
    {'key': 'backhand', 'label': '反手', 'label_en': 'Backhand',
     'color': '#7bd0ff', 'icon': 'swap_calls'},
    {'key': 'serve', 'label': '发球', 'label_en': 'Serve',
     'color': '#c4a7ff', 'icon': 'bolt'},
    {'key': 'slice', 'label': '切削', 'label_en': 'Slice',
     'color': '#ffb783', 'icon': 'content_cut'},
    {'key': 'volley', 'label': '截击', 'label_en': 'Volley',
     'color': '#8e9379', 'icon': 'pan_tool'},
    {'key': 'smash', 'label': '高压', 'label_en': 'Smash',
     'color': '#ffb4ab', 'icon': 'expand'},
]
STROKE_MAP = {s['key']: s for s in STROKE_TYPES}

# 训练形式的中文名
SESSION_TYPE_LABEL = {
    'drill': '专项练习', 'match': '实战对抗',
    'rally': '底线对拉', 'serve': '发球训练',
}
COURT_TYPE_LABEL = {
    'hard': '室外硬地', 'clay': '室外红土',
    'grass': '草地', 'indoor': '室内场地',
}

# 时间筛选档位。刻意提供 7 / 14 / 30 / 全部 四档 + 自定义区间：
# 演示数据每 3 天一次训练，近 7 天只会命中最后一天（用于验证稀疏与空态）。
RANGES = [
    {'key': '7', 'label': '近 7 天', 'days': 7},
    {'key': '14', 'label': '近 14 天', 'days': 14},
    {'key': '30', 'label': '近 30 天', 'days': 30},
    {'key': 'all', 'label': '全部记录', 'days': None},
]
DEFAULT_RANGE = '30'

# 区间内没有任何记录时的兜底提示（前端空态文案）
EMPTY_HINT = '该时间区间内没有任何训练记录，请放宽筛选条件或切换学员。'


# --------------------------------------------------------------------------- #
# 时间区间
# --------------------------------------------------------------------------- #
def _to_day(value):
    """把 'YYYY-MM-DD' / 'YYYY/MM/DD' 解析成 date；失败返回 None。"""
    if not value:
        return None
    s = str(value).strip().replace('/', '-')
    if not s:
        return None
    try:
        y, m, d = (int(x) for x in s[:10].split('-'))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def resolve_range(range_key=DEFAULT_RANGE, date_from=None, date_to=None):
    """把 UI 上的筛选条件解析成统一的区间描述。

    优先级：显式 from/to > 档位。自定义区间即使只填一端也成立
    （只填 from 表示「从那一天至今」，只填 to 表示「截止那一天」）。
    """
    today = date.today()
    d_from = _to_day(date_from)
    d_to = _to_day(date_to)

    if d_from or d_to:
        key = 'custom'
        label = '自定义区间'
        if not d_from:
            d_from = None          # 不限起点
        if not d_to:
            d_to = today
    else:
        key = (range_key or DEFAULT_RANGE).strip().lower()
        spec = next((r for r in RANGES if r['key'] == key), None)
        if spec is None:
            spec = next(r for r in RANGES if r['key'] == DEFAULT_RANGE)
        key, label = spec['key'], spec['label']
        if spec['days']:
            d_from = today - timedelta(days=spec['days'] - 1)
            d_to = today
        else:
            d_from, d_to = None, today

    # 起止颠倒时自动纠正，避免用户手填反了看到空结果
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from

    return {
        'key': key,
        'label': label,
        'from': d_from.isoformat() if d_from else None,
        'to': d_to.isoformat() if d_to else None,
        'from_input': d_from.isoformat() if d_from else '',
        'to_input': d_to.isoformat() if d_to else '',
        'display': '%s ~ %s' % (d_from.isoformat() if d_from else '不限',
                                d_to.isoformat() if d_to else '不限'),
        'days': (d_to - d_from).days + 1 if (d_from and d_to) else None,
    }


def _range_clause(alias, rng):
    """生成区间过滤 SQL 片段 + 参数。

    用 date(started_at) 而不是字符串比较：种子数据是
    '2026-09-23T05:01:28.707Z'（UTC，带 Z），直接与 '2026-09-23' 比字符串
    虽然也能工作，但一旦出现其它时区写法就会错位 —— 走 SQLite 的
    date() 归一化更稳。
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
# 数值格式化（模板里只做拼接，不在 Jinja 里算，避免两处口径不一致）
# --------------------------------------------------------------------------- #
def fmt(value, decimals=0, unit='', dash='—'):
    if value is None:
        return dash
    try:
        f = float(value)
    except (TypeError, ValueError):
        return dash
    if decimals <= 0:
        return '%s%s' % (format(int(round(f)), ',d'), unit)
    return '%s%s' % (('%.' + str(decimals) + 'f') % f, unit)


def _minutes(seconds):
    return None if seconds is None else float(seconds) / 60.0


def _pct(part, whole):
    if not whole:
        return 0.0
    return round(float(part) * 100.0 / float(whole), 1)


def _round(value, decimals=1):
    """按展示精度舍入，None 透传。

    SQL 的 AVG()/SUM() 会吐出 110.25436893203884 这种长尾浮点；模板侧有 fmt()
    兜底，但接口调用方（小程序 / iOS）拿到的是原始数字，因此必须在**数据层**
    就统一精度，避免同一指标在页面与接口里长得不一样。
    """
    if value is None:
        return None
    try:
        v = round(float(value), decimals)
        return int(v) if decimals <= 0 else v      # decimals=0 时给整数，避免 2850.0
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 学员
# --------------------------------------------------------------------------- #
def list_students():
    """所有学员（供选择器使用），按 NTRP 由高到低。"""
    rows = db.query(
        'SELECT id, name, avatar_url, avatar_initial, tier, nt_level, hand,'
        ' backhand, years_playing, racket, device_id, watch_model, location,'
        ' is_online, nt_score'
        ' FROM students WHERE deleted_at IS NULL'
        ' ORDER BY nt_score DESC, name ASC')
    for r in rows:
        r['initial'] = r.get('avatar_initial') or (r['name'][:1] if r.get('name') else '?')
        r['nt_label'] = ('NTRP %s' % r['nt_level']) if r.get('nt_level') else '—'
        r['hand_label'] = ' · '.join([x for x in (r.get('hand'), r.get('backhand')) if x])
    return rows


def get_student(student_id):
    if not student_id:
        return None
    row = db.query_one(
        'SELECT * FROM students WHERE id = ? AND deleted_at IS NULL', (student_id,))
    if row:
        row['initial'] = row.get('avatar_initial') or (row['name'][:1] if row.get('name') else '?')
    return row


def benchmarks():
    """段位常模：{level: row}。用于把个人数据放到同段位坐标里判断。"""
    rows = db.query('SELECT * FROM nt_benchmarks WHERE deleted_at IS NULL')
    return {r['level']: r for r in rows if r.get('level')}


# --------------------------------------------------------------------------- #
# 聚合查询
# --------------------------------------------------------------------------- #
def stroke_aggs(rng):
    """逐拍样本聚合：{(student_id, stroke_type): {...}}。"""
    clause, params = _range_clause('s', rng)
    rows = db.query(
        'SELECT s.student_id AS sid, sr.stroke_type AS st,'
        ' COUNT(sr.id) AS n,'
        ' MAX(sr.speed_kmh) AS mx, AVG(sr.speed_kmh) AS av,'
        ' AVG(sr.spin_rpm) AS spin,'
        ' SUM(CASE WHEN sr.sweet_spot = 1 THEN 1 ELSE 0 END) AS sweet,'
        ' AVG(sr.confidence) AS conf'
        ' FROM stroke_records sr'
        ' JOIN training_sessions s ON s.id = sr.session_id'
        ' WHERE sr.deleted_at IS NULL AND s.deleted_at IS NULL' + clause +
        ' GROUP BY s.student_id, sr.stroke_type', params)
    return {(r['sid'], r['st']): r for r in rows}


def session_aggs(rng):
    """会话聚合：{student_id: {...}}。

    这里是**球速类指标的主力数据源**：training_sessions 里每场都带
    serve_peak_kmh / serve_avg_kmh / forehand_avg_kmh / backhand_avg_kmh /
    peak_speed_kmh / avg_speed_kmh —— 这是手表对整场的汇总，**没有抽样偏差**，
    且每条训练记录都齐全。

    为什么不拿 stroke_records 算球速：那张表是**逐拍抽样**（一场只存 10~20 条），
    用样本算出的峰值会系统性低于真实峰值（实测同一场：样本峰值 165.5，
    会话汇总 166.9），而且同一学员在「明细表」与「排行榜」会出现两个数。
    逐拍样本只用于**击球构成 / 落点 / 旋转**这类会话表没有的维度。
    """
    clause, params = _range_clause('training_sessions', rng)
    rows = db.query(
        'SELECT student_id AS sid, COUNT(*) AS sessions,'
        ' COALESCE(SUM(stroke_count),0) AS tot_stroke,'
        ' AVG(stroke_count) AS avg_stroke, MAX(stroke_count) AS max_stroke,'
        ' COALESCE(SUM(duration_sec),0) AS tot_dur,'
        ' AVG(duration_sec) AS avg_dur, MAX(duration_sec) AS max_dur,'
        ' COALESCE(SUM(calories_kcal),0) AS tot_cal,'
        ' AVG(calories_kcal) AS avg_cal, MAX(calories_kcal) AS max_cal,'
        ' AVG(sweet_spot_rate) AS avg_ss, MAX(sweet_spot_rate) AS max_ss,'
        ' AVG(avg_hr) AS avg_hr, MAX(avg_hr) AS max_hr_of_avg,'
        ' MAX(max_hr) AS peak_hr, MIN(date(started_at)) AS first_day,'
        ' MAX(date(started_at)) AS last_day,'
        ' MAX(serve_peak_kmh) AS max_serve_peak, AVG(serve_avg_kmh) AS avg_serve,'
        ' MAX(peak_speed_kmh) AS max_peak_speed, AVG(avg_speed_kmh) AS avg_speed,'
        ' MAX(forehand_avg_kmh) AS max_fh, AVG(forehand_avg_kmh) AS avg_fh,'
        ' MAX(backhand_avg_kmh) AS max_bh, AVG(backhand_avg_kmh) AS avg_bh,'
        ' SUM(CASE WHEN session_type = ? THEN 1 ELSE 0 END) AS n_match'
        ' FROM training_sessions WHERE deleted_at IS NULL' + clause +
        ' GROUP BY student_id', ['match'] + params)
    return {r['sid']: r for r in rows}


# --------------------------------------------------------------------------- #
# 排行榜
# --------------------------------------------------------------------------- #
# kind='session' → 会话级字段（training_sessions），**没有抽样偏差**，每条记录齐全
# kind='stroke'  → 逐拍样本（stroke_records），仅用于会话表没有的维度（切削/转速/落点）
#
# 为什么球速优先走 session：
#   ① 会话表每场都有 serve_peak / serve_avg / forehand_avg / backhand_avg，
#      而 stroke_records 是抽样（一场十几条），用样本算峰值会系统性偏低；
#   ② 这样「同一学员的同一指标」在个人明细、技术分析、排行榜里是**同一个数**。
LEADERBOARD_METRICS = [
    {'key': 'serve', 'label': '发球速度', 'label_en': 'Serve Speed',
     'unit': 'km/h', 'icon': 'bolt', 'better': 'high', 'decimals': 1,
     'kind': 'session', 'max_col': 'max_serve_peak', 'avg_col': 'avg_serve',
     'note': '最大值 = 个人区间内最好一场的发球峰值；'
             '平均值 = 各场发球均速的均值。取自训练记录的会话汇总字段'},
    {'key': 'overall', 'label': '整体球速', 'label_en': 'Overall Speed',
     'unit': 'km/h', 'icon': 'speed', 'better': 'high', 'decimals': 1,
     'kind': 'session', 'max_col': 'max_peak_speed', 'avg_col': 'avg_speed',
     'note': '最大值 = 单场最高球速的区间最高；平均值 = 各场场均球速的均值'},
    {'key': 'forehand', 'label': '正手球速', 'label_en': 'Forehand Speed',
     'unit': 'km/h', 'icon': 'sports_baseball', 'better': 'high', 'decimals': 1,
     'kind': 'session', 'max_col': 'max_fh', 'avg_col': 'avg_fh',
     'note': '最大值 = 个人最好一场的正手均速；平均值 = 各场正手均速的均值'},
    {'key': 'backhand', 'label': '反手球速', 'label_en': 'Backhand Speed',
     'unit': 'km/h', 'icon': 'swap_calls', 'better': 'high', 'decimals': 1,
     'kind': 'session', 'max_col': 'max_bh', 'avg_col': 'avg_bh',
     'note': '最大值 = 个人最好一场的反手均速；平均值 = 各场反手均速的均值。'
             '这一项最能看出能否被持续压制'},
    {'key': 'slice', 'label': '切削球速', 'label_en': 'Slice Speed',
     'unit': 'km/h', 'icon': 'content_cut', 'better': 'high', 'decimals': 1,
     'kind': 'stroke', 'stroke': 'slice',
     'note': '会话表没有切削列，故取自逐拍样本（抽样统计，样本量已标注）'},
    {'key': 'stroke_count', 'label': '单场击球量', 'label_en': 'Strokes / Session',
     'unit': '次', 'icon': 'sports_tennis', 'better': 'high', 'decimals': 0,
     'kind': 'session', 'max_col': 'max_stroke', 'avg_col': 'avg_stroke',
     'note': '最大值 = 单场最多击球量；平均值 = 场均击球量'},
    {'key': 'duration', 'label': '单场训练时长', 'label_en': 'Duration',
     'unit': '分钟', 'icon': 'timer', 'better': 'high', 'decimals': 0,
     'kind': 'session', 'max_col': 'max_dur', 'avg_col': 'avg_dur', 'scale': 1 / 60.0,
     'note': '最大值 = 区间内最长一场；平均值 = 场均时长'},
    {'key': 'calories', 'label': '单场消耗卡路里', 'label_en': 'Active Energy',
     'unit': 'kcal', 'icon': 'local_fire_department', 'better': 'high', 'decimals': 0,
     'kind': 'session', 'max_col': 'max_cal', 'avg_col': 'avg_cal',
     'note': '最大值 = 单场最高消耗；平均值 = 场均消耗'},
    {'key': 'sweet_spot', 'label': '甜区命中率', 'label_en': 'Sweet Spot',
     'unit': '%', 'icon': 'adjust', 'better': 'high', 'decimals': 1,
     'kind': 'session', 'max_col': 'max_ss', 'avg_col': 'avg_ss',
     'note': '最大值 = 单场最佳命中率；平均值 = 场均命中率'},
    {'key': 'avg_hr', 'label': '平均心率', 'label_en': 'Avg Heart Rate',
     'unit': 'BPM', 'icon': 'favorite', 'better': None, 'decimals': 0,
     'kind': 'session', 'max_col': 'max_hr_of_avg', 'avg_col': 'avg_hr',
     'note': '心率不是越高越好：数值透露强度与恢复水平，仅作负荷参考'},
]
METRIC_MAP = {m['key']: m for m in LEADERBOARD_METRICS}


def _metric_pairs(metric, session_map, stroke_map):
    """算出每个学员在该指标上的 (最大值, 平均值)，单位已换算。"""
    out = {}
    scale = metric.get('scale', 1) or 1
    if metric['kind'] == 'stroke':
        for (sid, st), agg in stroke_map.items():
            if st != metric['stroke']:
                continue
            mx, av = agg.get('mx'), agg.get('av')
            out[sid] = {
                'max': None if mx is None else float(mx) * scale,
                'avg': None if av is None else float(av) * scale,
                'n': agg.get('n') or 0,
                'spin': agg.get('spin'),
            }
    else:
        for sid, agg in session_map.items():
            mx, av = agg.get(metric['max_col']), agg.get(metric['avg_col'])
            out[sid] = {
                'max': None if mx is None else float(mx) * scale,
                'avg': None if av is None else float(av) * scale,
                'n': agg.get('sessions') or 0,
            }
    return out


def _board(pairs, students, metric, which, selected_id):
    """生成单张榜（which='max' | 'avg'），数值降序。"""
    items = []
    for sid, p in pairs.items():
        v = p.get(which)
        if v is None:
            continue
        stu = students.get(sid)
        if not stu:
            continue
        items.append({
            'student_id': sid,
            'name': stu['name'],
            'initial': stu.get('initial') or '?',
            'avatar_url': stu.get('avatar_url'),
            'tier': stu.get('tier'),
            'nt_level': stu.get('nt_level'),
            'nt_label': ('NTRP %s' % stu['nt_level']) if stu.get('nt_level') else '—',
            'hand_label': ' · '.join([x for x in (stu.get('hand'), stu.get('backhand')) if x]),
            'value': round(v, metric['decimals']) if metric['decimals'] else round(v, 1),
            'display': fmt(v, metric['decimals']),
            'n': p.get('n'),
            'selected': sid == selected_id,
        })
    items.sort(key=lambda x: x['value'], reverse=True)
    top = items[0]['value'] if items else 0
    for i, it in enumerate(items, 1):
        it['rank'] = i
        # 条形宽度：以榜首为 100%，最低保留 6% 以便看得见
        it['pct'] = max(6.0, round(it['value'] * 100.0 / top, 1)) if top else 0.0
    return items


def leaderboards(rng, selected_id=None, metric_key=None):
    """横向对比：全指标矩阵 + 选中指标的排行榜双榜。"""
    students = {s['id']: s for s in list_students()}
    session_map = session_aggs(rng)
    stroke_map = stroke_aggs(rng)

    boards, matrix_rows = {}, []
    for metric in LEADERBOARD_METRICS:
        pairs = _metric_pairs(metric, session_map, stroke_map)
        boards[metric['key']] = {
            'metric': metric,
            'max_board': _board(pairs, students, metric, 'max', selected_id),
            'avg_board': _board(pairs, students, metric, 'avg', selected_id),
            'pairs': pairs,
        }

    # 全指标对比矩阵：行=学员，列=指标，单元格=「最大 / 平均」
    ordered = sorted(
        students.values(),
        key=lambda s: -(session_map.get(s['id'], {}).get('tot_dur') or 0))
    for stu in ordered:
        cells = []
        for metric in LEADERBOARD_METRICS:
            p = boards[metric['key']]['pairs'].get(stu['id'])
            if not p or (p.get('max') is None and p.get('avg') is None):
                cells.append({'metric': metric, 'max': None, 'avg': None,
                              'max_display': '—', 'avg_display': '—', 'empty': True})
            else:
                cells.append({
                    'metric': metric,
                    'max': p.get('max'), 'avg': p.get('avg'),
                    'max_display': fmt(p.get('max'), metric['decimals']),
                    'avg_display': fmt(p.get('avg'), metric['decimals']),
                    'empty': False,
                })
        agg = session_map.get(stu['id'], {})
        matrix_rows.append({
            'student': stu,
            'cells': cells,
            'sessions': agg.get('sessions') or 0,
            'total_label': fmt(_minutes(agg.get('tot_dur')), 0, ' 分钟')
            if agg.get('tot_dur') else '—',
            'selected': stu['id'] == selected_id,
        })

    active_key = metric_key if metric_key in boards else None
    if not active_key:
        # 默认落在「有数据且非心率」的第一项上，保证首屏就能看到有意义的两张榜
        active_key = next((m['key'] for m in LEADERBOARD_METRICS
                           if boards[m['key']]['max_board']), LEADERBOARD_METRICS[0]['key'])

    # 给每个指标挂上「数据口径」标签，页面上直接显示，避免读者误以为
    # 逐拍抽样指标（切削）与会话汇总指标是同一口径。
    def _basis(m):
        return '会话汇总' if m['kind'] == 'session' else '逐拍抽样'

    metrics_view = []
    for m in LEADERBOARD_METRICS:
        item = dict(m)
        item['basis'] = _basis(m)
        metrics_view.append(item)
    active_metric = next(m for m in metrics_view if m['key'] == active_key)

    return {
        'range': rng,
        'students': list(students.values()),
        'metrics': metrics_view,
        'matrix_rows': matrix_rows,
        'metric_tabs': [
            {'key': m['key'], 'label': m['label'], 'icon': m['icon'],
             'basis': m['basis'],
             'count': len(boards[m['key']]['max_board']),
             'active': m['key'] == active_key}
            for m in metrics_view
        ],
        'active': active_key,
        'max_board': boards[active_key]['max_board'],
        'avg_board': boards[active_key]['avg_board'],
        'active_metric': active_metric,
        'total_sessions': sum(v.get('sessions') or 0 for v in session_map.values()),
        'empty': not any(b['max_board'] for b in boards.values()),
    }


# --------------------------------------------------------------------------- #
# 个人分析
# --------------------------------------------------------------------------- #
def _kpi(label, value, unit, icon, tone='primary', sub=''):
    return {'label': label, 'value': value, 'unit': unit,
            'icon': icon, 'tone': tone, 'sub': sub}


def stroke_mix_for(session_ids_or_student, rng, student_id=None):
    """击球构成（按类型计数 + 均速 + 极速）。被个人分析与会话明细共用。"""
    if student_id:
        clause, params = _range_clause('s', rng)
        rows = db.query(
            'SELECT sr.stroke_type AS st, COUNT(*) AS n,'
            ' AVG(sr.speed_kmh) AS av, MAX(sr.speed_kmh) AS mx,'
            ' AVG(sr.spin_rpm) AS spin,'
            ' SUM(CASE WHEN sr.sweet_spot = 1 THEN 1 ELSE 0 END) AS sweet'
            ' FROM stroke_records sr'
            ' JOIN training_sessions s ON s.id = sr.session_id'
            ' WHERE sr.deleted_at IS NULL AND s.deleted_at IS NULL'
            ' AND s.student_id = ?' + clause +
            ' GROUP BY sr.stroke_type', [student_id] + params)
    else:
        rows = db.query(
            'SELECT sr.stroke_type AS st, COUNT(*) AS n,'
            ' AVG(sr.speed_kmh) AS av, MAX(sr.speed_kmh) AS mx,'
            ' AVG(sr.spin_rpm) AS spin,'
            ' SUM(CASE WHEN sr.sweet_spot = 1 THEN 1 ELSE 0 END) AS sweet'
            ' FROM stroke_records sr WHERE sr.deleted_at IS NULL'
            ' AND sr.session_id = ? GROUP BY sr.stroke_type', (session_ids_or_student,))
    total = sum((r['n'] or 0) for r in rows)
    out = []
    by_key = {r['st']: r for r in rows}
    for meta in STROKE_TYPES:
        r = by_key.get(meta['key'])
        # ⚠️ 该学员在本区间内**没有打过这一类球**时 r 为 None ——
        # 必须先归一成字典再取值，否则 r['av'] 会抛
        # TypeError: 'NoneType' object is not subscriptable，
        # 整个个人分析接口 500（新用户、或某一类球从未出现时必现）。
        rv = r or {}
        n = (rv.get('n') if r else 0) or 0
        out.append({
            'key': meta['key'], 'label': meta['label'], 'label_en': meta['label_en'],
            'color': meta['color'], 'icon': meta['icon'],
            'count': n,
            'pct': _pct(n, total),
            # 舍入到展示精度：SQL 的 AVG() 会返回 110.25436893203884 这种长尾浮点，
            # 直接下发会让接口调用方（含小程序）拿到无法直接展示的数字
            'avg_speed': _round(rv.get('av'), 1),
            'max_speed': _round(rv.get('mx'), 1),
            'spin': _round(rv.get('spin'), 0),
            'sweet_pct': _pct(rv.get('sweet'), rv.get('n')) if rv.get('n') else None,
            'display': fmt(n, 0, ' 次') if n else '—',
            'total': total,
        })
    return out, total


def _sessions_of(student_id, rng):
    clause, params = _range_clause('training_sessions', rng)
    return db.query(
        'SELECT * FROM training_sessions WHERE deleted_at IS NULL'
        ' AND student_id = ?' + clause + ' ORDER BY started_at DESC',
        [student_id] + params)


def _session_stroke_mix(student_id, rng):
    """{session_id: {stroke_type: {n, av, mx}}}"""
    clause, params = _range_clause('s', rng)
    rows = db.query(
        'SELECT sr.session_id AS sid, sr.stroke_type AS st, COUNT(*) AS n,'
        ' AVG(sr.speed_kmh) AS av, MAX(sr.speed_kmh) AS mx'
        ' FROM stroke_records sr'
        ' JOIN training_sessions s ON s.id = sr.session_id'
        ' WHERE sr.deleted_at IS NULL AND s.deleted_at IS NULL'
        ' AND s.student_id = ?' + clause +
        ' GROUP BY sr.session_id, sr.stroke_type', [student_id] + params)
    out = {}
    for r in rows:
        out.setdefault(r['sid'], {})[r['st']] = r
    return out


def _std(values):
    """总体标准差（用于判断「单场表现是否忽上忽下」）。"""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var ** 0.5


def _build_insights(stu, totals, mix, bm, peer, rng, mix_total):
    """规则化技术分析。

    刻意不接大模型：离线环境必须能复现，且每条结论都要能指回具体数字。
    规则彼此独立、命中就产出一条，因此换成 LLM 时只需替换本函数。

    ⚠️ 占比一律以 **mix_total（逐拍样本数）** 为分母，不要用
    totals['strokes']（会话里手表统计的击球总数）。两者不是一个量级：
    实测某学员会话击球总数 1781，而 stroke_records 只落了 96 条样本
    （逐拍明细是**抽样**存储的），拿样本数去除总数会得出「发球占 0.8%」
    这种错得离谱的占比。
    """
    out = []
    by_key = {m['key']: m for m in mix}
    # 球速一律取**会话汇总**（totals）—— 与排行榜、逐场明细表同源，
    # 保证同一学员同一指标在任何区块都是同一个数。
    fh = totals.get('fh_avg')
    bh = totals.get('bh_avg')
    sv = totals.get('serve_avg')
    sv_max = totals.get('serve_peak')
    # 切削在会话表里没有对应列，只能走逐拍样本（会标明样本量）
    sl = by_key.get('slice', {})
    total_strokes = mix_total or 0

    bm_serve = (bm or {}).get('serve_kmh')
    bm_fh = (bm or {}).get('forehand_kmh')
    bm_sweet = (bm or {}).get('sweet_spot_rate')

    # ---- 1. 发球 ----
    if sv_max:
        if bm_serve:
            diff = sv_max - bm_serve
            ratio = diff * 100.0 / bm_serve
            if diff >= 0:
                out.append({
                    'tone': 'good', 'icon': 'bolt', 'title': '发球具备段位优势',
                    'body': '区间内发球最高速 %s km/h，高于 NTRP %s 段位常模 %s km/h '
                            '（%+.1f%%）。发球均速 %s km/h，可作为稳定的先手武器。'
                            % (fmt(sv_max, 1), stu.get('nt_level') or '—',
                               fmt(bm_serve, 0), ratio, fmt(sv, 1))})
            else:
                out.append({
                    'tone': 'warn', 'icon': 'bolt', 'title': '发球速度低于段位常模',
                    'body': '区间内发球最高速 %s km/h，比 NTRP %s 段位常模 %s km/h 低 '
                            '%s km/h（%.1f%%）。建议增加发球专项课，先把一发均速拉起来。'
                            % (fmt(sv_max, 1), stu.get('nt_level') or '—',
                               fmt(bm_serve, 0), fmt(abs(diff), 1), abs(ratio))})
        else:
            out.append({
                'tone': 'info', 'icon': 'bolt', 'title': '缺少段位基准可对照',
                'body': '区间内发球最高速 %s km/h。该 NTRP 档位暂无云端常模，'
                        '可参考下方排行榜中的同侪位置。' % fmt(sv_max, 1)})

    # ---- 2. 正反手均衡度 ----
    if fh and bh:
        r = bh / fh
        gap = (1 - r) * 100.0
        if r < 0.78:
            out.append({
                'tone': 'warn', 'icon': 'balance', 'title': '反手是当前明显短板',
                'body': '正手均速 %s km/h、反手均速 %s km/h，反手只有正手的 %.0f%%'
                        '（差 %s km/h）。对手一旦持续压反手，主动进攻链条会断；'
                        '建议把反手稳定性与深度作为下一阶段主线。'
                        % (fmt(fh, 1), fmt(bh, 1), r * 100, fmt(fh - bh, 1))})
        elif r > 0.92:
            out.append({
                'tone': 'good', 'icon': 'balance', 'title': '正反手较为均衡',
                'body': '正手均速 %s km/h、反手均速 %s km/h，两侧差距仅 %.0f%%，'
                        '没有明显可被针对的一侧，技战术选择空间大。'
                        % (fmt(fh, 1), fmt(bh, 1), gap)})
        else:
            out.append({
                'tone': 'info', 'icon': 'balance', 'title': '正反手存在可控差距',
                'body': '反手均速为正手的 %.0f%%（%s vs %s km/h）。差距在可接受范围，'
                        '但高强度对抗下仍可能被当作突破口。'
                        % (r * 100, fmt(bh, 1), fmt(fh, 1))})

    # ---- 3. 切削使用率 ----
    if sl.get('count') and total_strokes:
        share = sl['count'] * 100.0 / total_strokes
        if share >= 12:
            out.append({
                'tone': 'info', 'icon': 'content_cut', 'title': '切削使用率偏高',
                'body': '切削占击球样本 %.1f%%（%d 拍，均速 %s km/h），'
                        '呈现明显的防守/节奏变化取向。切削本身没问题，'
                        '但要留意它是否替代了本该进攻的正手机会球。'
                        % (share, sl['count'], fmt(sl.get('avg_speed'), 1))})

    # ---- 4. 甜区命中率 ----
    ss = totals.get('avg_sweet')
    if ss is not None:
        if bm_sweet and ss >= bm_sweet:
            out.append({
                'tone': 'good', 'icon': 'adjust', 'title': '击球点控制优于段位常模',
                'body': '场均甜区命中率 %.1f%%，高于 NTRP %s 段位标杆 %.1f%%，'
                        '说明击球点稳定、容错率高。'
                        % (ss, stu.get('nt_level') or '—', bm_sweet)})
        else:
            ref = bm_sweet or 68.2
            out.append({
                'tone': 'warn', 'icon': 'adjust', 'title': '甜区命中率有提升空间',
                'body': '场均甜区命中率 %.1f%%，低于段位标杆 %.1f%%。'
                        '从逐拍数据看，偏差多集中在拍面上沿 —— 通常是准备时间不足、'
                        '抢点击球导致，建议配合小场快节奏多球练习。'
                        % (ss, ref)})

    # ---- 5. 体能与心率 ----
    ahr, phr = totals.get('avg_hr'), totals.get('peak_hr')
    if ahr:
        if ahr >= 150:
            out.append({
                'tone': 'warn', 'icon': 'favorite', 'title': '整体强度偏高',
                'body': '区间内平均心率 %s BPM（峰值 %s BPM），已长时间处于无氧区间。'
                        '若恢复心率不足，连续高强度训练会抬升受伤风险，'
                        '建议穿插低强度技术课。'
                        % (fmt(ahr, 0), fmt(phr, 0))})
        elif ahr <= 120:
            out.append({
                'tone': 'info', 'icon': 'favorite', 'title': '整体强度偏低',
                'body': '区间内平均心率 %s BPM，多数时间处于有氧区间。'
                        '若是技术打磨期属正常；若目标是比赛，需要加入对抗强度更高的内容。'
                        % fmt(ahr, 0)})

    # ---- 6. 训练频率 ----
    n = totals.get('sessions') or 0
    span = totals.get('span_days')
    if n and span:
        gap = span / float(n)
        if gap > 4.5:
            out.append({
                'tone': 'warn', 'icon': 'event_busy', 'title': '训练密度偏低',
                'body': '区间内 %d 场训练、跨度 %d 天，平均 %.1f 天一场。'
                        '技术定型期建议把间隔压到 2~3 天，否则每次都在「重新找手感」。'
                        % (n, span, gap)})
        else:
            out.append({
                'tone': 'good', 'icon': 'event_available', 'title': '训练密度保持良好',
                'body': '区间内 %d 场训练、跨度 %d 天，平均 %.1f 天一场，'
                        '频率处于技术巩固的理想区间。' % (n, span, gap)})

    # ---- 7. 单场稳定性 ----
    fh_series = [s.get('forehand_avg_kmh') for s in totals.get('session_rows') or []]
    sd = _std(fh_series)
    mean_fh = (sum(v for v in fh_series if v) / len([v for v in fh_series if v])
               if any(fh_series) else 0)
    if mean_fh and sd and sd / mean_fh > 0.06:
        out.append({
            'tone': 'warn', 'icon': 'show_chart', 'title': '单场表现波动较大',
            'body': '各场正手均速标准差 %.1f km/h（均值 %s km/h，波动 %.0f%%），'
                    '说明状态起伏明显。通常与体能储备、热身充分度相关，'
                    '建议固定赛前热身的时长与强度。'
                    % (sd, fmt(mean_fh, 1), sd / mean_fh * 100)})

    # ---- 8. 同侪位置 ----
    if peer and peer.get('total'):
        rk = peer.get('serve_rank')
        if rk:
            top_pct = rk * 100.0 / peer['total']
            if top_pct <= 40:
                out.append({
                    'tone': 'good', 'icon': 'military_tech', 'title': '发球速度位居同侪前列',
                    'body': '在本次统计的 %d 名学员中，发球最高速排名第 %d（前 %.0f%%）。'
                            % (peer['total'], rk, top_pct)})
            else:
                out.append({
                    'tone': 'info', 'icon': 'leaderboard', 'title': '发球速度同侪位置',
                    'body': '在本次统计的 %d 名学员中，发球最高速排名第 %d（后 %.0f%%）。'
                            '详见下方排行榜。'
                            % (peer['total'], rk, 100 - top_pct)})

    return out


def student_analysis(student_id, rng):
    """单个学员在指定区间内的完整分析。student_id 不存在时返回 None。"""
    stu = get_student(student_id)
    if not stu:
        return None

    rows = _sessions_of(student_id, rng)
    mix, total_strokes = stroke_mix_for(None, rng, student_id=student_id)
    session_mix = _session_stroke_mix(student_id, rng)

    seconds = sum((r.get('duration_sec') or 0) for r in rows)
    strokes = sum((r.get('stroke_count') or 0) for r in rows)
    cals = sum((r.get('calories_kcal') or 0) for r in rows)
    hrs = [r.get('avg_hr') for r in rows if r.get('avg_hr')]
    peaks = [r.get('max_hr') for r in rows if r.get('max_hr')]
    sss = [r.get('sweet_spot_rate') for r in rows if r.get('sweet_spot_rate') is not None]

    span_days = None
    if rows:
        try:
            d0 = min(r['started_at'][:10] for r in rows)
            d1 = max(r['started_at'][:10] for r in rows)
            y0, m0, dd0 = (int(x) for x in d0.split('-'))
            y1, m1, dd1 = (int(x) for x in d1.split('-'))
            span_days = (date(y1, m1, dd1) - date(y0, m0, dd0)).days + 1
        except (ValueError, KeyError, TypeError):
            span_days = None

    # 球速类一律取会话汇总（与排行榜同源，避免同一指标出现两个数）
    def _vals(key):
        return [r.get(key) for r in rows if r.get(key) is not None]

    serve_peaks, serve_avgs = _vals('serve_peak_kmh'), _vals('serve_avg_kmh')
    fh_avgs, bh_avgs = _vals('forehand_avg_kmh'), _vals('backhand_avg_kmh')

    def _mean(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    totals = {
        'sessions': len(rows),
        'strokes': strokes,
        'seconds': seconds,
        'calories': cals,
        'avg_hr': round(sum(hrs) / len(hrs)) if hrs else None,
        'peak_hr': max(peaks) if peaks else None,
        'avg_sweet': round(sum(sss) / len(sss), 1) if sss else None,
        'span_days': span_days,
        'match_count': sum(1 for r in rows if r.get('session_type') == 'match'),
        'session_rows': rows,
        # 球速（会话汇总口径）
        'serve_peak': max(serve_peaks) if serve_peaks else None,
        'serve_avg': _mean(serve_avgs),
        'fh_peak': max(fh_avgs) if fh_avgs else None,
        'fh_avg': _mean(fh_avgs),
        'bh_peak': max(bh_avgs) if bh_avgs else None,
        'bh_avg': _mean(bh_avgs),
    }

    # 逐场明细（带格式化字段与击球构成，便于模板直接渲染）
    detail = []
    for r in rows:
        sm = session_mix.get(r['id'], {})
        parts = []
        for meta in STROKE_TYPES:
            got = sm.get(meta['key'])
            if got and got['n']:
                parts.append({'label': meta['label'], 'color': meta['color'],
                              'icon': meta['icon'], 'count': got['n'],
                              'avg': got['av']})
        dur = r.get('duration_sec') or 0
        detail.append({
            'raw': r,
            'date_label': (r.get('started_at') or '')[:10] or '—',
            'time_label': (r.get('started_at') or '')[11:16],
            'title': r.get('title') or '训练',
            'type_label': SESSION_TYPE_LABEL.get(r.get('session_type'), '训练'),
            'court_label': COURT_TYPE_LABEL.get(r.get('court_type'), '—'),
            'location': r.get('location') or '—',
            'duration_label': fmt(_minutes(dur), 0, ' 分钟') if dur else '—',
            'stroke_label': fmt(r.get('stroke_count'), 0, ' 次'),
            'calorie_label': fmt(r.get('calories_kcal'), 0, ' kcal'),
            'hr_label': fmt(r.get('avg_hr'), 0, ' BPM'),
            'peak_hr_label': fmt(r.get('max_hr'), 0, ' BPM'),
            'serve_peak_label': fmt(r.get('serve_peak_kmh'), 1, ' km/h'),
            'forehand_label': fmt(r.get('forehand_avg_kmh'), 1, ' km/h'),
            'backhand_label': fmt(r.get('backhand_avg_kmh'), 1, ' km/h'),
            'sweet_label': fmt(r.get('sweet_spot_rate'), 1, ' %'),
            'rally_label': fmt(r.get('rally_max'), 0, ' 拍'),
            'mix': parts,
        })

    # KPI 卡
    kpi = [
        _kpi('训练场次', fmt(len(rows), 0), '场', 'event_note', 'primary',
             '其中实战对抗 %d 场' % totals['match_count']),
        _kpi('累计训练时长', fmt(_minutes(seconds), 0), '分钟', 'timer', 'primary',
             '场均 %s 分钟' % fmt(_minutes(seconds / len(rows)) if rows else None, 0)),
        _kpi('累计击球量', fmt(strokes, 0), '次', 'sports_tennis', 'primary-fixed',
             '场均 %s 次' % fmt(strokes / len(rows) if rows else None, 0)),
        _kpi('累计消耗', fmt(cals, 0), 'kcal', 'local_fire_department', 'tertiary-fixed-dim',
             '场均 %s kcal' % fmt(cals / len(rows) if rows else None, 0)),
        _kpi('发球最高速', fmt(totals['serve_peak'], 1), 'km/h', 'bolt', 'primary-fixed',
             '各场均速均值 %s km/h' % fmt(totals['serve_avg'], 1)),
        _kpi('正手 / 反手均速',
             '%s / %s' % (fmt(totals['fh_avg'], 1), fmt(totals['bh_avg'], 1)),
             'km/h', 'sports_baseball', 'secondary',
             '反手为正手的 %s' % (
                 '%.0f%%' % (totals['bh_avg'] * 100.0 / totals['fh_avg'])
                 if totals['fh_avg'] else '—')),
        _kpi('场均甜区命中率', fmt(totals['avg_sweet'], 1), '%', 'adjust', 'primary',
             '标杆 %s%%' % fmt((benchmarks().get(stu.get('nt_level')) or {}).get('sweet_spot_rate'), 1)),
        _kpi('平均 / 峰值心率',
             '%s / %s' % (fmt(totals['avg_hr'], 0), fmt(totals['peak_hr'], 0)),
             'BPM', 'favorite', 'error', '峰值取自各场最高心率'),
    ]

    bms = benchmarks()
    bm = bms.get(stu.get('nt_level')) or {}

    # 同侪位置：无论页面当前激活的是哪个指标，都用「发球最高速」榜算位置
    students_map = {s['id']: s for s in list_students()}
    pairs = _metric_pairs(METRIC_MAP['serve'], session_aggs(rng), stroke_aggs(rng))
    board = _board(pairs, students_map, METRIC_MAP['serve'], 'max', student_id)
    peer = {'total': len(board), 'serve_rank': None}
    for it in board:
        if it['student_id'] == student_id:
            peer['serve_rank'] = it['rank']
            break

    insights = _build_insights(stu, totals, mix, bm, peer, rng, total_strokes)

    # 强项 / 短板标签
    tags = []
    if totals['serve_peak'] and bm.get('serve_kmh'):
        tags.append({'label': '发球强项' if totals['serve_peak'] >= bm['serve_kmh'] else '发球待提升',
                     'tone': 'good' if totals['serve_peak'] >= bm['serve_kmh'] else 'warn'})
    if totals['fh_avg'] and totals['bh_avg']:
        ratio = totals['bh_avg'] / totals['fh_avg']
        tags.append({'label': '正反手均衡' if ratio > 0.92 else '反手短板',
                     'tone': 'good' if ratio > 0.92 else 'warn'})
    if totals['avg_sweet'] is not None:
        ref = bm.get('sweet_spot_rate') or 68.2
        tags.append({'label': '击球点稳定' if totals['avg_sweet'] >= ref else '击球点待稳定',
                     'tone': 'good' if totals['avg_sweet'] >= ref else 'warn'})
    if totals['avg_hr']:
        tags.append({'label': '负荷偏高' if totals['avg_hr'] >= 150 else '负荷适中',
                     'tone': 'warn' if totals['avg_hr'] >= 150 else 'good'})

    headline = '暂无'
    if rows:
        parts = ['区间内共 %d 场训练、累计 %s 次击球、%s 分钟'
                 % (len(rows), fmt(strokes, 0), fmt(_minutes(seconds), 0))]
        # 只有该区间真的存在发球数据时才提发球，否则会输出「达 — km/h」这种残句
        with_serve = [d for d in detail if d['raw'].get('serve_peak_kmh')]
        if with_serve:
            best = max(with_serve, key=lambda d: d['raw']['serve_peak_kmh'])
            # serve_peak_label 已自带单位（例 '178.0 km/h'），不要再拼一次
            parts.append('发球最高速出现在 %s 的「%s」，达 %s（该场汇总值）'
                         % (best['date_label'], best['title'], best['serve_peak_label']))
        else:
            parts.append('本区间没有记录到发球数据，发球类结论暂缺')
        headline = '；'.join(parts) + '。'

    return {
        'student': stu,
        'range': rng,
        'kpi': kpi,
        'mix': mix,
        'total_strokes': total_strokes,
        'sessions': detail,
        # 段位常模的**展示用格式化值**（原始列是 REAL，直接输出会变成 168.0）
        'bm_disp': {
            'has': bool(bm),
            'level': stu.get('nt_level'),
            'serve': fmt(bm.get('serve_kmh'), 0),
            'forehand': fmt(bm.get('forehand_kmh'), 0),
            'sweet': fmt(bm.get('sweet_spot_rate'), 1),
            'sample': fmt(bm.get('sample_size'), 0),
            'version': bm.get('algorithm_version') or '—',
        },
        'totals': totals,
        'insights': insights,
        'tags': tags,
        'peer': peer,
        'headline': headline,
        'benchmark': bm,
        'empty': not rows,
        'empty_hint': EMPTY_HINT,
    }
