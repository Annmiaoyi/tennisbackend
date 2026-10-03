# -*- coding: utf-8 -*-
"""页面路由：把 5 个设计稿页面 + 1 个设置页渲染出来。

页面模板由 scripts/build_pages.py 从设计稿自动生成（逐字节保真），
这里只负责：注入当前页的导航激活态、以及页面所需的业务数据。
"""
import io
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from server import analytics
from server import datasources
from server import db

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(HERE, 'templates')

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=TEMPLATES)


def _bold_filter(text):
    """把正文里的 **强调** 渲染成粗体。

    数据源目录、标注须知这些长文案统一用 `**…**` 圈出关键短语 —— 这样
    /api/datasources 返回的纯文本也读得懂重点。但模板若直接 `{{ body }}`，
    页面上会原样露出两对星号。所有非等宽正文列都应挂这个过滤器。
    """
    if not text:
        return ''
    segs = str(text).split('**')
    out = []
    for i, seg in enumerate(segs):
        esc = str(escape(seg))
        out.append('<strong class="font-bold text-on-surface">%s</strong>' % esc
                   if i % 2 else esc)
    return Markup(''.join(out))


templates.env.filters['bold'] = _bold_filter


def _plain_filter(text):
    """**强调** → 纯文本（去掉标记，不加标签）。

    只给**属性值**用：HTML 的 title / aria-label 里塞 <strong> 会被原样当字符
    显示出来（`&lt;strong&gt;…`），所以这类位置要的是「把标记摘掉」，
    而不是「渲染成粗体」——后者用 `| bold`。
    """
    if not text:
        return ''
    return str(text).replace('**', '')


templates.env.filters['plain'] = _plain_filter

# 管理站页面默认归属的演示用户（真实部署时由登录态注入）
DEFAULT_PAGE_USER = os.environ.get('ACEMATE_USER', 'u_demo')

# 路由 -> (模板, 导航 data-path)
PAGES = {
    '/':          ('pages/dashboard.html', 'overview'),
    '/users':     ('pages/users.html',     'user-management'),
    '/training':  ('pages/training.html',  'analytics-comparison'),
    '/personas':  ('pages/personas.html',  'user-personas'),
    '/feedback':  ('pages/feedback.html',  'feedback'),
    '/settings':  ('pages/settings.html',  'system-settings'),
}


def _context(request, template, nav_active, **extra):
    """页面公共上下文。

    设计稿里的顶栏「实时在线 1,428 名学员」是写死的展示值，
    这里改为从数据库读取当日真实在线数；失败时回退到设计稿原值，
    保证任何情况下页面都不会因为取数异常而空白。
    """
    ctx = {
        'request': request,
        'nav_active': nav_active,
        'online_now': 1428,
        'online_label': '1,428',
    }
    try:
        row = db.query_one(
            "SELECT value_json FROM platform_metrics WHERE metric_key = 'online_now'")
        if row and row['value_json']:
            import json
            v = json.loads(row['value_json'])
            ctx['online_now'] = int(v)
            ctx['online_label'] = format(int(v), ',d')
    except Exception:
        pass
    ctx.update(extra)
    return ctx


@router.get('/', response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, 'pages/dashboard.html',
                                      _context(request, 'pages/dashboard.html', 'overview'))


@router.get('/users', response_class=HTMLResponse)
def users(request: Request):
    return templates.TemplateResponse(request, 'pages/users.html',
                                      _context(request, 'pages/users.html', 'user-management'))


def _qs(base, over=None, drop=(), path='/training'):
    """构造带筛选参数的 URL（/training 与 /annotation 共用）。

    所有筛选（学员 / 时间档位 / 指标）都走 **query param + 服务端渲染**，
    不使用前端状态：好处是任何一个视图都能直接分享/收藏，刷新不丢筛选，
    且禁用 JS 也能用。切换档位时要显式 drop 掉自定义起止日期，
    否则会一直沿用旧的自定义区间，表现为「点了近 7 天却还是老数据」。

    `path` 默认 /training（历史沿革），/annotation 的筛选条传自己的路径。
    """
    q = dict(base)
    for k in drop:
        q.pop(k, None)
    for k, v in (over or {}).items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = v
    q = {k: v for k, v in q.items() if v not in (None, '')}
    return ('%s?%s' % (path, urlencode(q))) if q else path


def _hist_filter_links(student_id, rng, n_all, students):
    """历史采集元数据筛选条的链接（学员 chips + 时间档位 + 恢复默认）。

    抽成模块级纯函数，是因为这三条规则**靠肉眼测不出来**，只能靠断言守住，
    而断言需要一个不起服务的入口（见 scripts/verify_annotation_pages.py）：
      · 切档位必须 **丢掉自定义起止**（from/to）。不丢的话表现为
        「点了近 7 天却还是老的日期区间」—— 链接看着正常，数据是错的。
      · `range=all` 是默认值，**不进 URL**；否则每个链接都拖一条尾巴，
        而且「全部记录」与「无参数」会变成两个不同的地址却渲染同一张表。
      · 切学员要 **保留当前时间区间**，否则每换一个学员就被重置回全部记录。
    """
    base = {'student': student_id or ''}
    if rng['key'] == 'custom':
        base['range'] = 'custom'
        base['from'], base['to'] = rng['from_input'], rng['to_input']
    elif rng['key'] != 'all':
        base['range'] = rng['key']

    def href(over=None, drop=()):
        # 一律带 #history：筛选条在长文档第 10 屏，不带锚点每次都会被甩回页顶。
        return _qs(base, over, drop=drop, path='/annotation') + '#history'

    student_links = [{
        'id': '', 'name': '全部学员', 'initial': '*', 'avatar_url': None,
        'n': n_all, 'href': href({'student': None}), 'active': not student_id,
    }] + [{
        'id': s['id'], 'name': s['name'], 'initial': s['initial'],
        'avatar_url': s.get('avatar_url'), 'n': s['n'],
        'href': href({'student': s['id']}), 'active': s['id'] == student_id,
    } for s in students]

    # 时间档位只给 近 7 天 / 近 30 天 / 全部 —— 台账这个场景不需要 14 天，
    # 档位越多越容易选错；想要别的区间就用筛选条上的自定义起止。
    range_links = [{
        'key': r['key'], 'label': r['label'],
        'href': href({'range': None} if r['key'] == 'all' else {'range': r['key']},
                     drop=('from', 'to')),
        'active': rng['key'] == r['key'],
    } for r in analytics.RANGES if r['key'] in ('7', '30', 'all')]

    return {'student_links': student_links, 'range_links': range_links,
            'reset_href': '/annotation#history'}


@router.get('/training', response_class=HTMLResponse)
def training(request: Request):
    """训练记录深度分析与横向对比。

    三段式：
      ① 筛选台  —— 学员 + 时间区间（服务端渲染，可分享 URL）
      ② 个人单项分析 —— 选定学员在区间内的历史记录 + 规则化技术分析
      ③ 横向对比 —— 全体学员的 max/avg 矩阵 + 单项排行榜（最大值榜 / 平均值榜）
    """
    qp = request.query_params
    students = analytics.list_students()

    student_id = qp.get('student') or (students[0]['id'] if students else None)
    if students and student_id not in {s['id'] for s in students}:
        student_id = students[0]['id']      # 传了不存在的学员 id 时回落到第一个

    rng = analytics.resolve_range(qp.get('range'), qp.get('from'), qp.get('to'))
    metric_key = qp.get('metric')

    analysis = analytics.student_analysis(student_id, rng) if student_id else None
    boards = analytics.leaderboards(rng, selected_id=student_id, metric_key=metric_key)

    base = {'range': rng['key'], 'student': student_id, 'metric': boards['active']}
    if rng['key'] == 'custom':
        base['from'] = rng['from_input']
        base['to'] = rng['to_input']

    # 时间档位链接（切档位时丢掉自定义起止）
    range_links = [
        {'key': r['key'], 'label': r['label'],
         'href': _qs(base, {'range': r['key']}, drop=('from', 'to')),
         'active': rng['key'] == r['key']}
        for r in analytics.RANGES
    ]
    # 学员链接
    student_links = [
        {'id': s['id'], 'name': s['name'], 'initial': s['initial'],
         'avatar_url': s.get('avatar_url'), 'tier': s.get('tier'),
         'nt_label': s['nt_label'], 'hand_label': s['hand_label'],
         'href': _qs(base, {'student': s['id']}),
         'active': s['id'] == student_id}
        for s in students
    ]
    # 指标链接
    metric_links = [
        {'key': t['key'], 'label': t['label'], 'icon': t['icon'], 'count': t['count'],
         'href': _qs(base, {'metric': t['key']}),
         'active': t['key'] == boards['active']}
        for t in boards['metric_tabs']
    ]

    ctx = _context(request, 'pages/training.html', 'analytics-comparison')
    ctx.update({
        'selector': students,
        'student_links': student_links,
        'range_links': range_links,
        'metric_links': metric_links,
        'analysis': analysis,
        'boards': boards,
        'rng': rng,
        'active_student': student_id,
        'share_url': str(request.url),
    })
    return templates.TemplateResponse(request, 'pages/training.html', ctx)


@router.get('/personas', response_class=HTMLResponse)
def personas(request: Request):
    return templates.TemplateResponse(request, 'pages/personas.html',
                                      _context(request, 'pages/personas.html', 'user-personas'))


@router.get('/feedback', response_class=HTMLResponse)
def feedback(request: Request):
    return templates.TemplateResponse(request, 'pages/feedback.html',
                                      _context(request, 'pages/feedback.html', 'feedback'))


@router.get('/settings', response_class=HTMLResponse)
def settings(request: Request):
    """系统与硬件设置页。

    设计稿只给了 5 个页面，但侧边栏第 6 项的「系统与硬件设置」是导航的一部分，
    留死链体验很差，因此按设计系统另建一页，并把**同步协议的真实运行水位**
    展示出来 —— 让这个页面不只是摆设，而是可用来排查同步问题的运维面板。
    """
    from server import sync as sync_engine
    from server.annotation import stats as annotation_stats
    import uuid as _uuid

    uid = DEFAULT_PAGE_USER
    status = {}
    try:
        head = db.query_one('SELECT COALESCE(MAX(seq),0) AS c FROM sync_changelog')
        ops = db.query_one(
            'SELECT COUNT(*) AS total,'
            ' SUM(CASE WHEN result=? THEN 1 ELSE 0 END) AS applied,'
            ' SUM(CASE WHEN result=? THEN 1 ELSE 0 END) AS duplicate,'
            ' SUM(CASE WHEN result=? THEN 1 ELSE 0 END) AS conflict_lost,'
            ' SUM(CASE WHEN result=? THEN 1 ELSE 0 END) AS rejected'
            ' FROM sync_operations WHERE user_id=?',
            ('applied', 'duplicate', 'conflict_lost', 'rejected', uid)) or {}
        counts = {}
        for name, spec in sync_engine.SYNCABLE.items():
            live = db.query_one('SELECT COUNT(*) AS n FROM %s WHERE user_id=? AND deleted_at IS NULL'
                                % spec['table'], (uid,))
            dead = db.query_one('SELECT COUNT(*) AS n FROM %s WHERE user_id=? AND deleted_at IS NOT NULL'
                                % spec['table'], (uid,))
            counts[name] = {'live': live['n'] if live else 0,
                            'tombstone': dead['n'] if dead else 0}
        status = {
            'server_cursor': head['c'] if head else 0,
            'server_time': db.utcnow(),
            'operations': {k: (v or 0) for k, v in (ops or {}).items()},
            'entities': counts,
        }
    except Exception as e:      # 库还没灌数据时也要能打开页面
        status = {'server_cursor': 0, 'server_time': db.utcnow(),
                  'operations': {'applied': 0, 'duplicate': 0, 'conflict_lost': 0,
                                 'rejected': 0},
                  'entities': {k: {'live': 0, 'tombstone': 0}
                               for k in getattr(sync_engine, 'SYNCABLE', {})},
                  'error': str(e)}

    ctx = _context(request, 'pages/settings.html', 'system-settings')
    ctx.update({
        'sync': status,
        'entity_desc': {
            'student_profile': '学员档案 · 可离线编辑后同步',
            'training_session': '训练会话 · App 离线记录的核心实体',
            'stroke_record': '单次击球样本 · 高频写入，依赖幂等去重',
            'feedback_ticket': '反馈工单 · App 内提交，可离线暂存',
        },
        # ⚠️ 这里的色值一律写**完整的 Tailwind 类名**（text-*），不要写裸色名。
        # 原因：模板里是 `class="material-symbols-outlined {{ d.tone_class }}"`，
        # 类名由数据拼进 class 属性 —— Tailwind 只认「被扫描文件里的字面量」，
        # 因此 tailwind.config.js 的 content 特意包含了 ./server/**/*.py。
        # 若这里写 'primary-fixed' 这种裸色名，编译产物里不会有对应规则，页面会静默丢色。
        'devices': [
            {'name': 'Apple Watch Ultra 2', 'note': '陀螺仪 200Hz · Haptic 就绪',
             'icon': 'watch', 'tone_class': 'text-primary-fixed',
             'state': '在线', 'state_tone': 'text-primary-fixed'},
            {'name': 'iPhone 15 Pro', 'note': 'CoreML 推理 · 本地缓存 1.2 GB',
             'icon': 'phone_iphone', 'tone_class': 'text-secondary',
             'state': '在线', 'state_tone': 'text-primary-fixed'},
            {'name': 'iPad Court Mount', 'note': '教练端 · 战术切片回放',
             'icon': 'tablet_mac', 'tone_class': 'text-tertiary-fixed-dim',
             'state': '离线', 'state_tone': 'text-outline'},
            {'name': '蓝牙雷达传感器', 'note': '球速/转速双通道',
             'icon': 'bluetooth_searching', 'tone_class': 'text-outline',
             'state': '待配对', 'state_tone': 'text-outline'},
        ],
        'protocols': [
            {'name': '01 · 权威源', 'title': '后端数据库是唯一真实数据源',
             'body': '客户端本地只做缓存与待同步队列，任何时候都可以整体丢弃并从后端重建。'},
            {'name': '02 · 写入路径', 'title': '先写本地，再进同步队列',
             'body': '所有写操作先落本地库并标记 pending，联网后按队列顺序推送，因此可完全离线训练。'},
            {'name': '03 · 幂等推送', 'title': 'operation_id 为主键去重',
             'body': '每个写操作带唯一 operation_id，服务端据此查重；重试任意多次都不会重复插入。'},
            {'name': '04 · 冲突解决', 'title': 'LWW，以 updated_at 为准',
             'body': '比较 client_updated_at 与服务器行的 updated_at，严格大于才覆盖；落败方拉取服务端版本。'},
            {'name': '05 · 软删除', 'title': 'deleted_at 标记而非物理删除',
             'body': '保留行与墓碑，删除事件才能同步到其它设备；服务端不会把已删除数据当作不存在。'},
            {'name': '06 · 增量拉取', 'title': '服务器游标，不依赖客户端时间',
             'body': '全库单调递增的 seq 作为唯一游标，避开客户端时钟回拨与同毫秒并排的问题。'},
        ],
        # 数据源目录：一次训练到底从 Apple Watch 采集哪些数据、怎么取、怎么算、
        # 变成什么、在哪儿展示。目录本体在 server/datasources.py
        # （页面与 /api/datasources 共用同一份，避免文档与接口两处口径漂移）。
        'ds': datasources.catalog(),
        # 标注侧只留一个「入口 + 待办计数」。2026-10-01 起「数据采集与标注」的
        # 全部内容（标注进度、状态机、规范、须知）已收敛到 /annotation 单一出口，
        # 本页不再重复渲染一份，避免同一件事在两个页面上各说一套。
        'annot_pointer': annotation_stats.overview(),
    })
    return templates.TemplateResponse(request, 'pages/settings.html', ctx)


@router.get('/annotation', response_class=HTMLResponse)
def annotation_workspace(request: Request):
    """数据采集与标注工作台（原独立 :8000 服务，已并入本站）。

    本页是「数据采集与标注」的**唯一出口**（2026-10-01 起）：
    采集链路、标注逻辑、作业须知、标注进度与交互工作台都在这里，
    /settings 只保留一条入口链接，避免同一件事两个页面各说一套。

    「视频 ↔ 波形」的全部交互由 `web/assets/js/annotation.js` 接管，
    数据接口见 `server/annotation/api.py`。
    拆开的好处是脚本能进 tailwind.config 的 content 扫描范围，
    动态生成的类名不会被 purge。
    """
    from server.annotation import stats

    # ---- 历史采集元数据的筛选（学员 + 时间范围）--------------------------
    # 与 /training 完全同一套口径：全部走 query param + 服务端渲染 ——
    # URL 可分享、刷新不丢筛选、禁 JS 也能用；区间解析直接调
    # analytics.resolve_range，不在本页另写一份边界规则。
    qp = request.query_params
    valid_students = {s['id'] for s in analytics.list_students()}
    student_id = qp.get('student') or None
    if student_id and student_id not in valid_students:
        student_id = None        # 传了不存在的学员 → 回落成「全部」，不留空表
    # resolve_range 对**不认识的档位**会静默回落到 DEFAULT_RANGE（近 30 天），
    # 而筛选表单在「清空日期后提交」时正好会送出一个 range=custom ——
    # 那会表现为「点了清空却只剩近 30 天」。所以在进 resolve_range 之前先归一。
    range_key = (qp.get('range') or 'all').strip().lower()
    if range_key not in {r['key'] for r in analytics.RANGES}:
        range_key = 'all'
    rng = analytics.resolve_range(range_key, qp.get('from'), qp.get('to'))
    hist = stats.history_metadata(student=student_id, rng=rng)
    # 筛选条链接：三条易错规则（切档位丢自定义起止 / range=all 不进 URL /
    # 切学员保留区间）集中在 _hist_filter_links 里，便于断言。
    hist.update(_hist_filter_links(student_id, rng, hist['filter']['n_all'],
                                   hist['students']))

    ctx = _context(request, 'pages/annotation.html', 'data-annotation')
    ctx.update({
        'ov': stats.overview(),
        # 标注规范（状态机 / 规则 / 须知 / 标签空间）的唯一真源
        'spec': stats.annotation_spec(),
        # 标注进度：最近若干场会话的人工介入情况
        # （上限给到 50，避免历史采集数据一多就把在标的场次挤出视野）
        'progress': stats.session_rows(limit=50),
        # 历史采集元数据：把 mock 出来的那批历史场次**逐场逐字段**摊开
        # （字段为列、场次为行的台账），既是「每个数字是多少」的账本，
        # 也是「下一批采集该采哪些字段」的清单 —— 字段口径取自
        # server/datasources.py，覆盖数是现算的，不在这里另写一份。
        'hist': hist,
    })
    return templates.TemplateResponse(request, 'pages/annotation.html', ctx)


# 常见误输入兜底
@router.get('/dashboard')
def dashboard_alias():
    return RedirectResponse('/')
