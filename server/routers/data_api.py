# -*- coding: utf-8 -*-
"""业务只读接口：为 5 个管理页提供数据。

设计取舍：列表 / 明细这类「有实体语义」的数据用规范化的表（students、
training_sessions、feedback_tickets、persona_segments）；而热力图、构成分布、
雷达图坐标这类**服务端算出来的展示型聚合**统一放 platform_metrics 的 JSON 里。
好处是既保留了关系模型的约束与增量同步能力，又不必为每个图表建一堆窄表。
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from server import analytics
from server import datasources
from server import db

router = APIRouter(prefix='/api', tags=['业务数据'])


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def metric(key, default=None):
    """读一个平台聚合指标（platform_metrics.metric_key -> value_json）。"""
    row = db.query_one('SELECT value_json FROM platform_metrics WHERE metric_key = ?', (key,))
    if not row or not row['value_json']:
        return default
    try:
        return json.loads(row['value_json'])
    except (TypeError, ValueError):
        return default


def num(value, spec='d'):
    if value is None:
        return '—'
    try:
        if spec == 'd':
            return format(int(round(float(value))), ',d')
        return ('%.' + spec + 'f') % float(value)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# 1. 概览看板
# --------------------------------------------------------------------------- #
@router.get('/platform/overview', summary='平台数据与运营概览')
def overview():
    kpis = metric('overview_kpis', [])
    return {
        'kpis': kpis,
        'online_now': metric('online_now', 1428),
        'heatmap': metric('heatmap_24h_7d', {}),
        'stroke_mix': metric('stroke_mix', []),
        'hardware': metric('hardware_telemetry', {}),
        'live_sessions': metric('live_sessions', []),
        'ntrp_distribution': metric('ntrp_distribution', []),
        'ntrp_insight': metric('ntrp_insight', ''),
        'ntrp_radar': metric('ntrp_radar', []),
        'feedback_digest': metric('feedback_digest', []),
        'hot_tags': metric('feedback_hot_tags', []),
        'positive_rate': metric('feedback_positive_rate', '—'),
    }


# --------------------------------------------------------------------------- #
# 2. 学员与用户档案
# --------------------------------------------------------------------------- #
@router.get('/students', summary='学员列表（支持筛选、分页、排序）')
def students(
    q: Optional[str] = Query(None, description='按姓名 / 设备 ID / 球拍模糊搜索'),
    tier: Optional[str] = Query(None, description='VIP|Elite|Pro|Club|Standard，逗号分隔'),
    nt_level: Optional[str] = Query(None, description='NTRP 档位，逗号分隔'),
    hand: Optional[str] = Query(None, description='右手|左手'),
    backhand: Optional[str] = Query(None, description='双反|单反'),
    online: Optional[bool] = Query(None, description='仅看当前在线'),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    sort: str = Query('updated_at', description='updated_at|nt_score|sessions_count|name'),
    order: str = Query('desc', description='asc|desc'),
):
    where = ['deleted_at IS NULL']
    params = []

    if q:
        where.append('(name LIKE ? OR device_id LIKE ? OR racket LIKE ? OR location LIKE ?)')
        like = '%%%s%%' % q
        params += [like, like, like, like]
    for col, val in (('tier', tier), ('nt_level', nt_level), ('hand', hand),
                     ('backhand', backhand)):
        if val:
            items = [v.strip() for v in val.split(',') if v.strip()]
            if items:
                where.append('%s IN (%s)' % (col, ','.join('?' * len(items))))
                params += items
    if online:
        where.append('is_online = 1')

    sort = sort if sort in ('updated_at', 'nt_score', 'sessions_count', 'name',
                            'forehand_avg', 'serve_peak') else 'updated_at'
    order = 'ASC' if str(order).lower() == 'asc' else 'DESC'
    sql_where = ' AND '.join(where)

    total = db.query_one('SELECT COUNT(*) AS n FROM students WHERE ' + sql_where, params)['n']
    rows = db.query(
        'SELECT * FROM students WHERE %s ORDER BY %s %s LIMIT ? OFFSET ?'
        % (sql_where, sort, order), params + [page_size, (page - 1) * page_size])

    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
        'items': rows,
        'facets': {
            'tiers': [r['tier'] for r in db.query(
                'SELECT DISTINCT tier FROM students WHERE deleted_at IS NULL AND tier IS NOT NULL')],
            'nt_levels': [r['nt_level'] for r in db.query(
                'SELECT DISTINCT nt_level FROM students WHERE deleted_at IS NULL'
                ' AND nt_level IS NOT NULL ORDER BY nt_level')],
        },
    }


@router.get('/students/{student_id}', summary='单个学员档案')
def student_detail(student_id: str):
    row = db.query_one('SELECT * FROM students WHERE id = ? AND deleted_at IS NULL', (student_id,))
    if not row:
        raise HTTPException(status_code=404, detail='学员不存在或已删除')
    sessions = db.query(
        'SELECT * FROM training_sessions WHERE student_id = ? AND deleted_at IS NULL'
        ' ORDER BY started_at DESC LIMIT 20', (student_id,))
    agg = db.query_one(
        'SELECT COUNT(*) AS sessions, COALESCE(SUM(stroke_count),0) AS strokes,'
        ' COALESCE(SUM(duration_sec),0) AS seconds, MAX(serve_peak_kmh) AS serve_peak,'
        ' AVG(forehand_avg_kmh) AS forehand_avg'
        ' FROM training_sessions WHERE student_id = ? AND deleted_at IS NULL', (student_id,))
    return {'student': row, 'stats': agg, 'recent_sessions': sessions}


# --------------------------------------------------------------------------- #
# 3. 训练记录与横向对比
# --------------------------------------------------------------------------- #
@router.get('/training/compare', summary='训练记录深度分析与横向对比')
def training_compare(
    a: Optional[str] = Query(None, description='对比选手 A 的学员 id'),
    b: Optional[str] = Query(None, description='对比选手 B 的学员 id'),
    level: str = Query('3.5', description='对比基准 NTRP 档位'),
):
    players = db.query('SELECT * FROM students WHERE deleted_at IS NULL'
                       ' ORDER BY nt_score DESC LIMIT 3')
    return {
        'players': players,
        'level': level,
        'benchmark': metric('ntrp_benchmark', {}),
        'diagnosis': metric('ai_diagnosis', {}),
        'radar': metric('compare_radar', {}),
        'speed_bars': metric('compare_speed_bars', []),
        'multi_rally': metric('compare_multi_rally', {}),
        # 硬件准入（2026-10-02）：landing_quadrant（落点象限）需球的飞行轨迹，已移除。
        'history': metric('training_history', []),
        'history_total': metric('training_history_total', 0),
    }


@router.get('/training/sessions', summary='训练会话列表（可按学员过滤）')
def sessions(
    student_id: Optional[str] = Query(None),
    session_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    where = ['deleted_at IS NULL']
    params = []
    if student_id:
        where.append('student_id = ?')
        params.append(student_id)
    if session_type:
        where.append('session_type = ?')
        params.append(session_type)
    sql_where = ' AND '.join(where)
    total = db.query_one('SELECT COUNT(*) AS n FROM training_sessions WHERE ' + sql_where,
                         params)['n']
    rows = db.query(
        'SELECT * FROM training_sessions WHERE %s ORDER BY started_at DESC LIMIT ? OFFSET ?'
        % sql_where, params + [page_size, (page - 1) * page_size])
    return {'total': total, 'page': page, 'page_size': page_size, 'items': rows}


@router.get('/training/sessions/{session_id}/strokes', summary='某次训练的击球明细')
def strokes(session_id: str, limit: int = Query(500, ge=1, le=5000)):
    session = db.query_one('SELECT * FROM training_sessions WHERE id = ?', (session_id,))
    if not session:
        raise HTTPException(status_code=404, detail='训练会话不存在')
    rows = db.query('SELECT * FROM stroke_records WHERE session_id = ? AND deleted_at IS NULL'
                    ' ORDER BY seq_in_session LIMIT ?', (session_id, limit))
    by_type = db.query(
        'SELECT stroke_type, COUNT(*) AS n, AVG(speed_kmh) AS avg_speed,'
        ' MAX(speed_kmh) AS peak_speed'
        ' FROM stroke_records WHERE session_id = ? AND deleted_at IS NULL'
        ' GROUP BY stroke_type', (session_id,))
    return {'session': session, 'count': len(rows), 'by_type': by_type, 'strokes': rows}


# --------------------------------------------------------------------------- #
# 3b. 个人纵向分析 + 跨学员横向排行（新增）
#     ⚠️ 这两个接口与 /training 页面共用 server/analytics.py 的同一套算法，
#        不要在这里另写一份 SQL —— 两处口径一旦分叉，页面与接口就会给出
#        不同的数字，而且很难发现。
# --------------------------------------------------------------------------- #
@router.get('/training/student-analysis', summary='单学员区间纵向分析（含规则化技术分析）')
def student_analysis(
    student: str = Query(..., description='学员 id，例 stu-00001001'),
    range: str = Query('30', description='7|14|30|all；传 from/to 时以 from/to 为准'),
    date_from: Optional[str] = Query(None, alias='from', description='自定义起始日 YYYY-MM-DD'),
    date_to: Optional[str] = Query(None, alias='to', description='自定义结束日 YYYY-MM-DD'),
):
    rng = analytics.resolve_range(range, date_from, date_to)
    result = analytics.student_analysis(student, rng)
    if not result:
        raise HTTPException(status_code=404, detail='学员不存在或已删除')
    return result


@router.get('/training/leaderboards', summary='跨学员横向排行（每指标 最大值榜 + 平均值榜）')
def leaderboards(
    range: str = Query('30', description='7|14|30|all；传 from/to 时以 from/to 为准'),
    date_from: Optional[str] = Query(None, alias='from'),
    date_to: Optional[str] = Query(None, alias='to'),
    student: Optional[str] = Query(None, description='高亮该学员在榜中的位置'),
    metric: Optional[str] = Query(None, description='激活指标：serve|forehand|backhand|slice|'
                                                   'stroke_count|duration|calories|avg_hr'),
    full: bool = Query(False, description='为 true 时同时返回全部指标的双榜，否则只返回激活指标'),
):
    rng = analytics.resolve_range(range, date_from, date_to)
    data = analytics.leaderboards(rng, selected_id=student, metric_key=metric)
    if not full:
        data.pop('matrix_rows', None)
    return data


@router.get('/datasources', summary='Apple Watch 训练数据源目录（字段 / 获取方式 / 判定规则）')
def datasources_api():
    return datasources.catalog()


# --------------------------------------------------------------------------- #
# 4. 用户画像与分群
# --------------------------------------------------------------------------- #
@router.get('/personas', summary='用户画像与技术分群体系')
def personas():
    segments = db.query('SELECT * FROM persona_segments WHERE deleted_at IS NULL'
                        ' ORDER BY sort_order')
    for s in segments:
        if s.get('metrics_json'):
            try:
                s['metrics'] = json.loads(s['metrics_json'])
            except (TypeError, ValueError):
                s['metrics'] = {}
    return {
        'segments': segments,
        'total_users': metric('persona_total_users', 0),
        'histogram': metric('persona_skill_histogram', {}),
        'donuts': metric('persona_donuts', {}),
        'insight': metric('persona_insight', {}),
        'spotlight': metric('persona_spotlight', {}),
        'recommendations': metric('persona_recommendations', []),
        'comparison': metric('persona_comparison', []),
    }


# --------------------------------------------------------------------------- #
# 5. 用户建议与反馈
# --------------------------------------------------------------------------- #
@router.get('/feedback', summary='用户建议与需求工单中心')
def feedback(
    status: Optional[str] = Query(None, description='triage|new|sprint|rejected|done'),
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    where = ['deleted_at IS NULL']
    params = []
    if status:
        where.append('status = ?')
        params.append(status)
    if category:
        where.append('category = ?')
        params.append(category)
    if q:
        where.append('(title LIKE ? OR body LIKE ? OR code LIKE ?)')
        like = '%%%s%%' % q
        params += [like, like, like]
    sql_where = ' AND '.join(where)

    total = db.query_one('SELECT COUNT(*) AS n FROM feedback_tickets WHERE ' + sql_where,
                         params)['n']
    rows = db.query(
        'SELECT * FROM feedback_tickets WHERE %s ORDER BY occurred_at DESC, updated_at DESC'
        ' LIMIT ? OFFSET ?' % sql_where, params + [page_size, (page - 1) * page_size])
    by_status = db.query(
        'SELECT status, COUNT(*) AS n FROM feedback_tickets WHERE deleted_at IS NULL'
        ' GROUP BY status')

    return {
        'total': total,
        'page': page,
        'page_size': page_size,
        'items': rows,
        'by_status': {r['status']: r['n'] for r in by_status},
        'kpis': metric('feedback_kpis', []),
        'status_filters': metric('feedback_status_filters', []),
        'hardware_split': metric('feedback_hardware_split', {}),
        'categories': metric('feedback_categories', []),
        'timeline': metric('feedback_timeline', []),
        'tags': metric('feedback_hot_tags', []),
        'nextgen': metric('feedback_nextgen', {}),
    }
