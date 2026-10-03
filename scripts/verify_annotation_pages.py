# -*- coding: utf-8 -*-
"""离线校验：两个模板能否解析 + 渲染（不启服务、不走 HTTP）。

Jinja 语法错只会在真正渲染那一行报错，而 Jinja 的报错行号常常指到模板末尾，
所以这里额外做一件事：渲染失败时把 traceback 里的模板行号提取出来，
连同该行原文一起打印，省去对着几百行模板数行号。
"""
import io
import os
import sys
import traceback

os.environ.setdefault('ACEMATE_USER', 'u_demo')
sys.path.insert(0, '/Users/Project/Atennis/Tennisbackend')

from jinja2 import TemplateSyntaxError            # noqa: E402
from fastapi.templating import Jinja2Templates    # noqa: E402

ROOT = '/Users/Project/Atennis/Tennisbackend'
TEMPLATES = os.path.join(ROOT, 'server', 'templates')

env = Jinja2Templates(directory=TEMPLATES).env

# 与 pages.py 保持一致的 bold / plain 过滤器
# （plain 用于 title 这类属性位置：属性里塞 <strong> 会被当字符显示出来）
from server.routers.pages import _bold_filter, _plain_filter   # noqa: E402
env.filters['bold'] = _bold_filter
env.filters['plain'] = _plain_filter

# ---- 1. 语法检查 ---------------------------------------------------------
tpls = ['pages/settings.html', 'pages/annotation.html']
bad = 0
for t in tpls:
    try:
        env.get_template(t)
        print('语法 OK   %s' % t)
    except TemplateSyntaxError as e:
        bad += 1
        print('语法 FAIL %s  line %s: %s' % (t, e.lineno, e.message))
if bad:
    sys.exit(1)

# ---- 2. 渲染检查（用真实数据源，DB 走 var/ 下已有文件） --------------------
from server import analytics                              # noqa: E402
from server import datasources                          # noqa: E402
from server.annotation import stats as ann_stats        # noqa: E402
from server.routers import pages                        # noqa: E402
# 筛选条链接直接调路由里的真函数（见 render_annotation 的注释）

# base.html 顶栏要的两个值（真实路由里由 _context() 注入）
common = {
    'nav_active': 'system-settings',
    'online_now': 1428,
    'online_label': '1,428',
}


class _FakeReq:
    """base.html 里只用到 request.url.path 之类，给个最小替身。"""
    class _URL:
        path = '/settings'
        query = ''
    url = _URL()
    headers = {}
    cookies = {}


def render(name, ctx):
    try:
        html = env.get_template(name).render(request=_FakeReq(), **ctx)
        print('渲染 OK   %-26s %6d 字符' % (name, len(html)))
        return html
    except Exception:
        tb = traceback.format_exc()
        print('渲染 FAIL %s\n%s' % (name, tb))
        # 把报错行原文捞出来
        import re
        for m in re.finditer(r'line (\d+)', tb):
            n = int(m.group(1))
            src = io.open(os.path.join(TEMPLATES, name), encoding='utf-8').read().split('\n')
            if 1 <= n <= len(src):
                print('   → 第 %d 行：%s' % (n, src[n - 1].strip()[:160]))
        sys.exit(1)


def render_annotation(student=None, date_from=None, date_to=None, range_key='all'):
    """按 /annotation 路由的真实口径渲染一页，返回 (html, hist)。

    刻意**复刻** pages.py 里的那段筛选归一化（校验学员、纠正未知档位），
    而不是直接调路由函数 —— 路由要 Request 对象，且这一步的目的是让本脚本
    能在不起服务的情况下验「筛选 → 渲染」整条链。
    """
    valid = {s['id'] for s in analytics.list_students()}
    if student and student not in valid:
        student = None
    if range_key not in {r['key'] for r in analytics.RANGES}:
        range_key = 'all'
    rng = analytics.resolve_range(range_key, date_from, date_to)
    hist = ann_stats.history_metadata(student=student, rng=rng)
    # 筛选条链接**调路由里那个真函数**，不在这里复刻一份：
    # 「切档位要不要丢掉自定义起止」这类规则肉眼看不出来（链接长得都正常，
    # 只是数据不对），必须由断言守住 —— 而复刻一份就等于只测了复刻品。
    hist.update(pages._hist_filter_links(student or None, rng,
                                         hist['filter']['n_all'], hist['students']))
    ctx = dict(common, nav_active='data-annotation',
               ov=ann_stats.overview(), spec=ann_stats.annotation_spec(),
               progress=ann_stats.session_rows(limit=12), hist=hist)
    return env.get_template('pages/annotation.html').render(
        request=_FakeReq(), **ctx), hist


ds = datasources.catalog()
html_s = render('pages/settings.html', dict(
    common, ds=ds, annot_pointer=ann_stats.overview(),
    sync={'server_cursor': 0, 'server_time': '2026-10-01 00:00:00',
          'operations': {'applied': 0, 'duplicate': 0, 'conflict_lost': 0, 'rejected': 0},
          'entities': {}},
    devices=[], protocols=[],
))

html_a, hist_a = render_annotation()
html_stu, hist_stu = render_annotation(student='stu-00001001')
html_win, hist_win = render_annotation(student='stu-00001001',
                                       date_from='2026-09-27', date_to='2026-10-03')
html_none, hist_none = render_annotation(date_from='2026-01-01', date_to='2026-01-31')


def slice_between(html, start, end):
    i = html.index(start)
    j = html.index(end, i)
    return html[i:j]


def tbody_of(html):
    """取 #history 里那张矩阵表的 <tbody>（不含其它表格）。"""
    sec = slice_between(html, 'id="history"', '个字段没有逐场值')
    return slice_between(sec, '<tbody>', '</tbody>')


def thead_of(html):
    sec = slice_between(html, 'id="history"', '个字段没有逐场值')
    return slice_between(sec, '<thead>', '</thead>')


# ---- 3. 断言：关键内容确实进了 HTML ---------------------------------------
print()
checks = [    ('settings: 入口块', html_s, 'annotation-panel'),
    ('settings: 不再重复渲染标注表', html_s, 'annot_sessions', False),
    ('settings: 目录核查结论', html_s, '目录核查结论'),
    ('settings: 接入作业须知', html_s, '接入作业须知'),
    ('settings: 锚点 catalog', html_s, 'id="catalog"'),
    ('settings: tone 已生效(text-primary-fixed)', html_s, 'text-primary-fixed'),
    ('annotation: 两条链路', html_a, '两条链路'),
    ('annotation: 标注状态机', html_a, '标注状态机'),
    ('annotation: 三元组提示', html_a, '三元组'),
    ('annotation: 标注逻辑', html_a, '标注逻辑'),
    ('annotation: 作业须知', html_a, '作业须知'),
    ('annotation: 标签空间', html_a, '标签空间'),
    ('annotation: 标注进度', html_a, '标注进度'),
    ('annotation: 工作台', html_a, 'id="stage"'),
    ('annotation: bold 已渲染', html_a, '<strong class="font-bold text-on-surface">'),
    # ---- 历史采集元数据：结构与筛选（2026-10-03 转置为「字段为列、场次为行」）----
    # 这几条盯的不是"有没有画出来"，而是**方向对不对、筛选有没有真的落到数据上**：
    #   · 字段必须出现在 <thead>、会话必须出现在 <tbody>（转置的方向就是靠这个界定）
    #   · 列数必须等于 hist.fields 的长度（列与格错位会静默少一列，肉眼很难发现）
    #   · 覆盖数必须随筛选重算（切学员后是 5/5 而不是全批的 16/16）
    ('annotation: 历史元数据区块', html_a, 'id="history"'),
    ('annotation: 字段编号来自登记表', html_a, 'MD-001'),
    ('annotation: 逐场单元格有值', html_a, 'hist-S01-01'),
    ('annotation: 无逐场值分档说明', html_a, '个字段没有逐场值'),
    ('annotation: 筛选条有学员 chip', html_a, '全部学员'),
    ('annotation: 筛选条有自定义区间表单', html_a, 'id="histFilter"'),
    ('annotation: 表头是字段（编号在 thead）',
     thead_of(html_a), 'MD-001'),
    ('annotation: 表头不在 tbody', tbody_of(html_a), 'MD-001', False),
    ('annotation: 会话在 tbody（不在表头）', tbody_of(html_a), 'hist-S01-01'),
    ('annotation: 会话不在 thead', thead_of(html_a), 'hist-S01-01', False),
    ('annotation: 数据行列数 = 字段数',
     tbody_of(html_a), '<td', True),
    ('annotation: 冻结列 sticky', html_a, 'position: sticky; left: 0'),
    ('annotation: 表头 sticky', html_a, 'position: sticky; top: 0'),
    ('annotation: 分组 colspan 表头', html_a, 'colspan="'),
    # 筛选：张哲恒 5 场、区间内 2 场、筛空 0 场
    ('annotation: 筛选 张哲恒 = 5 行', tbody_of(html_stu), 'hist-S01-01'),
    ('annotation: 筛选 张哲恒 不含他人', tbody_of(html_stu), 'hist-S02-01', False),
    ('annotation: 筛选后覆盖数重算为 5/5', html_stu, '>5/5'),
    ('annotation: 张哲恒+近7天 = 2 行（含 S01-02）', tbody_of(html_win), 'hist-S01-02'),
    ('annotation: 张哲恒+近7天 不含 S01-03', tbody_of(html_win), 'hist-S01-03', False),
    ('annotation: 筛空有专门空态', html_none, '这组条件下没有命中任何场次'),
    ('annotation: 筛空不当成「库里没有」', html_none, '分析库里还没有历史采集记录', False),
]

# 筛选条的**链接规则**单独验：这三条错了页面照样 200、表格照样好看，
# 只是「点了近 7 天还是老区间」这种数据不对 —— 肉眼看链接发现不了。
_r7 = next(l for l in hist_win['range_links'] if l['key'] == '7')
_rall = next(l for l in hist_win['range_links'] if l['key'] == 'all')
_stu = next(l for l in hist_win['student_links'] if l['id'] == 'stu-00001001')
_ldef = {l['label']: l['href'] for l in hist_a['range_links']}
checks += [
    ('链接: 切档位丢掉自定义起止（from）', _r7['href'], 'from=', False),
    ('链接: 切档位丢掉自定义起始（to）', _r7['href'], 'to=', False),
    ('链接: 切档位丢掉 range=custom', _r7['href'], 'range=custom', False),
    ('链接: 切档位后目标为 range=7', _r7['href'], '/annotation?student=stu-00001001&range=7#history'),
    ('链接: 切学员保留当前自定义区间', _stu['href'],
     '/annotation?student=stu-00001001&range=custom&from=2026-09-27&to=2026-10-03#history'),
    # 默认值不进 URL：否则「全部记录」与「无参数」会变成两个地址却渲染同一张表
    ('链接: 全部记录不带参数', _rall['href'], '/annotation?student=stu-00001001#history'),
    ('链接: 无筛选时「全部记录」链接干净', _ldef.get('全部记录'), '/annotation#history'),
    ('链接: 无筛选时学员链接不带 range=all', _ldef.get('全部记录'), 'range=all', False),
]
for l in hist_a['student_links'] + hist_a['range_links']:
    _lb = l.get('label') or l.get('name')          # 档位叫 label，学员 chip 叫 name
    checks.append(('链接: %s 带 #history 锚点' % _lb, l['href'], '#history'))
    checks.append(('链接: %s 不带 range=all' % _lb, l['href'], 'range=all', False))
fail = 0
for c in checks:
    name, hay, needle = c[0], c[1], c[2]
    want = c[3] if len(c) > 3 else True
    got = needle in hay
    ok = (got == want)
    print('%s %-42s %s' % ('✅' if ok else '❌', name, '' if ok else '(got=%s want=%s)' % (got, want)))
    if not ok:
        fail += 1

# ---- 4. 逐行逐列的**算术**自检（HTML 层面数出来，与 stats 的返回对照） ----
print()
n_cols = len(hist_a['fields'])
n_rows = len(hist_a['sessions'])
body = tbody_of(html_a)
head = thead_of(html_a)
got_rows = body.count('<tr class="group ')
got_cells = body.count('<td')
# 每行 3 个冻结列 + n_cols 个数据格
want_cells = n_rows * (3 + n_cols)
for label, got, want in [
        ('表体会话行数', got_rows, n_rows),
        ('表体格数（3 冻结 + %d 字段）× %d 行' % (n_cols, n_rows), got_cells, want_cells),
        # ⚠️ 数 '<th ' 而不是 '<th'：<thead> 本身就含子串 '<th'，会平白多算 1 个
        ('表头字段列数（<th> 减去 3 个冻结 + %d 个分组行）'
         % len(hist_a['col_groups']), head.count('<th '),
         n_cols + 3 + len(hist_a['col_groups'])),
        ('分组 colspan 之和 = 字段列数',
         sum(g['span'] for g in hist_a['col_groups']), n_cols)]:
    ok = got == want
    print('%s %-52s got=%s want=%s' % ('✅' if ok else '❌', label, got, want))
    if not ok:
        fail += 1

print()


def body_text(html):
    """只剩「读者能看到的正文」—— 注释里写 **强调** 是给人读源码的，不算残留。

    模板与数据源都用 `**…**` 标关键短语（这样 /api/datasources 的纯文本
    也读得出重点）。凡是渲染进**可见正文**的都要过 `| bold`；但 HTML 注释、
    Jinja 注释、<script> 里的 JS 注释中的星号不必渲染，混在一起数会一直假报失败。
    """
    import re as _re
    html = _re.sub(r'<!--.*?-->', '', html, flags=_re.S)
    html = _re.sub(r'\{#.*?#\}', '', html, flags=_re.S)
    html = _re.sub(r'<script\b.*?</script>', '', html, flags=_re.S)
    return _re.sub(r'<style\b.*?</style>', '', html, flags=_re.S)


s_left = body_text(html_s).count('**')
a_left = body_text(html_a).count('**')
print('裸 ** 残留（settings 正文）：', s_left)
print('裸 ** 残留（annotation 正文）：', a_left)
if s_left or a_left:
    fail += 1
    print('❌ 有未渲染的 ** 标记（注释里的不计）')
print('结论：', '全部通过' if not fail else '%d 项失败' % fail)
sys.exit(1 if fail else 0)
