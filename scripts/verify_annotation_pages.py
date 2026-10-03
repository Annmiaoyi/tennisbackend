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
tpls = ['pages/settings.html', 'pages/annotation.html', 'pages/strokes.html']
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


def render_annotation(student=None, date_from=None, date_to=None, range_key='all',
                      q=None):
    """按 /annotation 路由的真实口径渲染一页，返回 (html, hist)。

    刻意**复刻** pages.py 里的那段筛选归一化（校验学员、纠正未知档位，
    再把搜索词交给 stats 解析），而不是直接调路由函数 —— 路由要 Request 对象，
    且这一步的目的是让本脚本能在不起服务的情况下验「筛选 → 渲染」整条链。
    """
    valid = {s['id'] for s in analytics.list_students()}
    if student and student not in valid:
        student = None
    if range_key not in {r['key'] for r in analytics.RANGES}:
        range_key = 'all'
    rng = analytics.resolve_range(range_key, date_from, date_to)
    hist = ann_stats.history_metadata(student=student, rng=rng, q=q)
    # 筛选条链接**调路由里那个真函数**，不在这里复刻一份：
    # 「切档位要不要丢掉自定义起止」这类规则肉眼看不出来（链接长得都正常，
    # 只是数据不对），必须由断言守住 —— 而复刻一份就等于只测了复刻品。
    # ⚠️ 传进去的学员要用 hist 里**解析过搜索词**的结果（搜索唯一命中会
    #    自动选中一个人），否则「搜 zzh」时下拉里高亮的是「全部学员」。
    hist.update(pages._hist_filter_links(hist['filter']['student'] or None, rng,
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

# ---- 学员搜索框（2026-10-03：一排 chip 改成可搜索下拉）----------------------
# 这四组把「输入什么 → 筛到谁」钉死。名单长了以后这四种输入都会用到，
# 而且**拼音那条最容易被改坏**（换个匹配规则、忘了更新首字母索引…），
# 所以每一类都留一条断言。
html_q_ini, hist_q_ini = render_annotation(q='zzh')          # 拼音首字母
html_q_han, hist_q_han = render_annotation(q='雨')            # 姓名任意字
html_q_py, hist_q_py = render_annotation(q='zhaoming')       # 全拼
html_q_id, hist_q_id = render_annotation(q='stu-00001003')   # 学员 id（粘贴）
html_q_many, hist_q_many = render_annotation(q='z')          # 多命中：张哲恒 + 赵明
html_q_miss, hist_q_miss = render_annotation(q='qqqq')       # 一个都不命中


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
    # ---- 学员搜索框：结构 + 有效性 + 唯一命中自动选中 ----
    # 盯三类事：① 框和候选列表在不在；② 四种输入能不能找到人（这是需求本身）；
    # ③ 唯一命中会不会真的把表格筛到那一个人（搜到却没筛 = 半截功能）。
    ('搜索框: 有输入框（name=q）', html_a, 'id="histQ"'),
    ('搜索框: 有候选列表', html_a, 'id="histPickList"'),
    ('搜索框: 候选列表默认收起', html_a, 'overflow-auto hidden" id="histPickList"'),
    ('搜索框: 候选带拼音检索串',
     html_a, 'data-search="张哲恒|zhangzheheng|zzh|stu-00001001"'),
    ('搜索框: 有「全部学员」兜底项', html_a, 'data-search="*"'),
    ('搜索框: 拼音首字母 zzh → 筛到张哲恒', tbody_of(html_q_ini), 'hist-S01-01'),
    ('搜索框: 拼音首字母 zzh → 不含他人', tbody_of(html_q_ini), 'hist-S02-01', False),
    ('搜索框: 拼音首字母 zzh → 唯一命中已自动选中',
     html_q_ini, '唯一匹配 → 已选中 张哲恒'),
    ('搜索框: 姓名任意字「雨」→ 筛到陈雨菲', tbody_of(html_q_han), 'hist-S03-01'),
    ('搜索框: 姓名任意字「雨」→ 不含他人', tbody_of(html_q_han), 'hist-S01-01', False),
    ('搜索框: 全拼 zhaoming → 筛到赵明', tbody_of(html_q_py), 'hist-S05-01'),
    ('搜索框: 学员 id → 筛到陈雨菲', tbody_of(html_q_id), 'hist-S03-01'),
    ('搜索框: 多命中(z)不擅自选中，表格保持全批',
     tbody_of(html_q_many), 'hist-S01-01'),
    ('搜索框: 多命中(z)含另一位命中者', tbody_of(html_q_many), 'hist-S05-01'),
    ('搜索框: 多命中(z)提示 2 位', html_q_many, '匹配到 2 位'),
    ('搜索框: 不命中时表格不被清空', tbody_of(html_q_miss), 'hist-S01-01'),
    ('搜索框: 不命中时有专门提示', html_q_miss, '没有匹配到学员'),
    # 不命中的候选在**服务端**就 hidden —— 这是「禁 JS 也能用」的根据：
    # 禁 JS 时用户看到的就是服务端筛好的那几位，不用等前端过滤。
    ('搜索框: 不命中的候选服务端已 hidden',
     slice_between(html_q_ini, 'id="histPickList"', '</ul>'),
     'data-search="李思源|lisiyuan|lsy|stu-00001002" role="option">'),
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
    _lb = l.get('label') or l.get('name')          # 档位叫 label，学员项叫 name
    checks.append(('链接: %s 带 #history 锚点' % _lb, l['href'], '#history'))
    checks.append(('链接: %s 不带 range=all' % _lb, l['href'], 'range=all', False))
    # 搜索词是**一次性的找人动作**，不该跟着链接流传（否则分享出去的地址里
    # 带着别人的搜索词，而且 q 与 student 同时出现时语义含糊）。
    checks.append(('链接: %s 不带搜索词 q' % _lb, l['href'], 'q=', False))
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
# ---- 3b. 拼音检索规则本身 ------------------------------------------------
# 上面那些模板断言只能证明「某个人被筛出来了」，证明不了**规则没被改坏**：
# 例如把首字母索引整个删掉，'zzh' 仍可能因为别的原因命中（haystack 里
# 还挂着姓名与 id），断言照样绿。所以规则单独钉一遍，四种输入各一条。
from server import pinyin as py_pinyin     # noqa: E402
_PY_NAMES = ['张哲恒', '李思源', '陈雨菲', '王浩然', '赵明']


def _py_hits(q):
    return [n for n in _PY_NAMES if py_pinyin.match(q, py_pinyin.haystack(n, ''))]


for label, q, want in [
        ('拼音首字母 zzh', 'zzh', ['张哲恒']),
        ('全拼 zhaoming', 'zhaoming', ['赵明']),
        ('全拼中段 zheheng', 'zheheng', ['张哲恒']),
        ('全拼前缀 zhang', 'zhang', ['张哲恒']),
        ('姓名任意字「雨」', '雨', ['陈雨菲']),
        ('首字母 zm', 'zm', ['赵明']),
        ('空白分词「张 zzh」', '张 zzh', ['张哲恒']),
        ('大小写不敏感 ZZH', 'ZZH', ['张哲恒']),
        ('多命中 z', 'z', ['张哲恒', '赵明']),
        ('不命中 qqqq', 'qqqq', []),
        ('空检索词不过滤', '   ', _PY_NAMES)]:
    got = _py_hits(q)
    ok = got == want
    print('%s 拼音: %-18s q=%-8r → %s' % ('✅' if ok else '❌', label, q, got))
    if not ok:
        fail += 1
        print('     期望：%s' % want)
print('   拼音库：%s' % ('pypinyin 已装（拼音检索可用）' if py_pinyin.HAS_PYPINYIN
                        else '⚠️ 缺 pypinyin，已降级为只按原名匹配'))
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


# ---- 3c. 横向滚动的入口（2026-10-03：右侧的列够不着） ----------------------
# 这张表 33 列 × 112px + 左侧 3 列冻结 ≈ 3978px，**必须**横向滚动才能看到右侧的列。
# 而设计稿在全局把滚动条隐藏了（src/tailwind.input.css 的 `*::-webkit-scrollbar`
# 与 `*{scrollbar-width:none}`）—— 于是「横向滚动」只剩「用带横滚的滚轮 / 触控板
# 横扫」这一条**看不见的**路径，没有横滚轮的人根本到不了右侧。
#
# 这里盯四件事，缺任何一件，右侧的列就又变成够不着：
#   ① 模板的滚动容器挂上了 .hist-scroll（例外类是**按类**生效的，类名掉了等于没写）；
#   ② CSS 源里那个例外**两条属性都在**（scrollbar-width 与 ::-webkit-scrollbar ——
#      少一条都不生效，原因见 src/tailwind.input.css 的注释）；
#   ③ 全局隐藏规则**还在**（它被删掉的话，这条例外就成了没人需要的死代码，
#      应该顺手清掉，而不是留着误导人）；
#   ④ 不依赖滚轮的入口在（可聚焦的容器 + 左右按钮 + 列位读数）。
# 断言读的是 **CSS 源** 而不是编译产物 app.css：产物被 .gitignore 忽略、
# 且本脚本本来就允许在「还没重编 CSS」的状态下跑。
_CSS_SRC = io.open(os.path.join(ROOT, 'src', 'tailwind.input.css'),
                   encoding='utf-8').read()
_css_exc = _CSS_SRC.split('.hist-scroll {')[1].split('}')[0] if '.hist-scroll {' in _CSS_SRC else ''
for label, got, want in [
        ('横滚: 滚动容器挂着 .hist-scroll', 'class="hist-scroll ' in html_a, True),
        ('横滚: 容器可聚焦（键盘方向键也能滚）',
         'id="histScroll" role="region"' in html_a and 'tabindex="0"' in html_a, True),
        ('横滚: 横扫到头不触发浏览器前进/后退',
         'overscroll-behavior-x: contain' in html_a, True),
        # 例外块里必须是 auto，不能是 thin、也不能配 scrollbar-color：
        # 只要 scrollbar-width/scrollbar-color 非 auto，浏览器就改用标准滚动条
        # 渲染并**忽略 ::-webkit-scrollbar**（标准滚动条在 macOS「自动隐藏滚动条」
        # 下会自动隐去 → 等于白恢复）。
        ('横滚: CSS 例外把 scrollbar-width 从 none 改回 auto',
         'scrollbar-width: auto' in _css_exc
         and 'scrollbar-width: none' not in _css_exc, True),
        ('横滚: CSS 例外里给了旧 Safari 兜底（::-webkit-scrollbar）',
         '.hist-scroll::-webkit-scrollbar {' in _CSS_SRC, True),
        ('横滚: 全局隐藏滚动条的设计要求仍在（例外才有存在意义）',
         '*::-webkit-scrollbar {' in _CSS_SRC and 'scrollbar-width: none' in _CSS_SRC, True),
        ('横滚: 有左/右翻页按钮与列位读数',
         all(k in html_a for k in ('id="histScrollPrev"', 'id="histScrollNext"',
                                   'id="histScrollPos"')), True),
        ('横滚: 控件默认隐藏（没有 JS 点了没反应，不如不显示）',
         'hidden items-center gap-space-xs" id="histScrollCtl"' in html_a, True),
        # JS 的「第 N–M 列」读数要拿列宽与冻结宽度。挂在同一处、由模板的 Jinja
        # 常量渲染出来，JS 就不再抄一份常量 —— 抄一份就会在改列宽时对不上。
        ('横滚: 列宽/冻结宽度/总列数挂在同一处',
         'data-colw="112" data-frozen="282" data-ncols="%d"' % n_cols in html_a, True),
        # 冻结列必须是 sticky，否则滚到右边就不知道自己在看哪一场（这也是
        # 「横向滚动可用」的前提：没有冻结列，横滚等于把行标识也一起滚走）。
        # 表头 3 个 th（rowspan=2 占满两行）+ 表体每行 3 个 td。
        ('横滚: 冻结三列仍是 sticky（表头 3 + 表体 3×%d 行）' % n_rows,
         thead_of(html_a).count('position: sticky; left:')
         + tbody_of(html_a).count('position: sticky; left:'), 3 + n_rows * 3),
]:
    ok = got == want
    print('%s %-52s got=%s want=%s' % ('✅' if ok else '❌', label, got, want))
    if not ok:
        fail += 1

print()

# ---- 5. 逐拍原始全量页（/strokes） -----------------------------------------
# 这一页存在的理由就是回答「台账写着 106 拍、逐拍字段却写着 12 条」那个疑问，
# 所以断言的重点**不是**「表能渲染出来」，而是**两个口径是否真的对得上**：
# 逐拍行数必须等于 L0 原始包里的全量条数，且与会话汇总的 stroke_count 一致。
# 只断言「页面上有 id="strokes"」的话，把数据源换回抽稀的 stroke_records
# 也照样能过 —— 那正是要防的回归。
print('逐拍原始全量页（/strokes）')


def render_strokes(session_id):
    detail = ann_stats.stroke_detail(session_id)
    ctx = dict(common, nav_active='data-annotation', detail=detail)
    return env.get_template('pages/strokes.html').render(
        request=_FakeReq(), **ctx), detail


html_sd, det_sd = render_strokes('hist-S01-02')      # 有人工介入的一场
html_sd1, det_sd1 = render_strokes('hist-S01-01')    # 该学员最早的一场
html_sd0, _ = render_strokes('')                     # 无参数
html_sdn, _ = render_strokes('hist-NOPE')            # 不存在的会话

# 注：不能用 tbody_of —— 它是台账专用的（会先切到 #history 区块再取 tbody）。
# /strokes 只有一张表，直接取第一个 <tbody> 即可。
_sd_rows = slice_between(html_sd, '<tbody>', '</tbody>').count('<tr class="group ')
for label, got, want in [
        ('逐拍页: 表体渲染出来', 'id="strokes"' in html_sd, True),
        # 这三条是同一件事的三个说法，缺一条就说明口径又被改回去了：
        # 行数 = L0 全量 = 会话汇总。
        ('逐拍页: 逐拍行数 = L0 原始包全量条数',
         _sd_rows, det_sd['stats']['n_swings_raw']),
        ('逐拍页: 逐拍行数 = 会话汇总 stroke_count',
         _sd_rows, det_sd['session']['stroke_count']),
        ('逐拍页: 两端计数比对为「一致」', det_sd['stats']['count_match'], True),
        ('逐拍页: 页面显示「一致」徽章',
         'verified' in html_sd and '<span>一致</span>' in html_sd, True),
        ('逐拍页: 标出原始包条数是 L0 全量口径',
         'L0 session.swings 全量' in html_sd, True),
        ('逐拍页: 写明与台账是两个粒度', '两个粒度' in html_sd, True),
        ('逐拍页: 点明 stroke_records 是抽稀副本', 'stroke_records' in html_sd, True),
        # 人工介入必须可视，否则这一页退化成一张纯序号表
        ('逐拍页: 有「已纠错」标记', '已纠错' in html_sd, True),
        ('逐拍页: 有「已确认」标记', '已确认' in html_sd, True),
        ('逐拍页: 有「未审阅」标记', '未审阅' in html_sd, True),
        ('逐拍页: 纠错行写明改判前后', '人工改判：' in html_sd, True),
        ('逐拍页: 会话元信息（学员名）在页头', det_sd['session']['student_name'] in html_sd, True),
        # 7 列 ≈ 900px，小屏必须能横滚 —— 全站隐藏了滚动条，不挂类就「能滚但看不出来」
        ('逐拍页: 表格容器挂了 .hist-scroll', 'class="hist-scroll ' in html_sd, True),
        ('逐拍页: 空参数走「没指定会话」空态', '没有指定会话' in html_sd0, True),
        ('逐拍页: 不存在的会话走「找不到」空态', '找不到会话' in html_sdn, True),
        ('逐拍页: 空态给出回台账的出口',
         'href="/annotation#history"' in html_sd0, True),
        ('逐拍页: 首场只有「下一场」没有「上一场」',
         ('下一场' in html_sd1) and ('上一场' not in html_sd1), True),
        # 台账 → 逐拍页的入口：每行 1 个会话号 + 5 个逐拍字段格。
        ('台账: 每行 1 个会话号入口 + 5 个逐拍格',
         tbody_of(html_a).count('/strokes?session=') // n_rows, 6),
        # 本次修复的核心：格子里必须是**本场击球总数**，不能再是抽稀的样本条数。
        ('台账: 逐拍格 title 给出本场击球总数',
         ('本场击球 %d 次' % det_sd1['session']['stroke_count'])
         in tbody_of(html_a), True),
]:
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
sd_left = body_text(html_sd).count('**')
print('裸 ** 残留（settings 正文）：', s_left)
print('裸 ** 残留（annotation 正文）：', a_left)
print('裸 ** 残留（strokes 正文）：', sd_left)
if s_left or a_left or sd_left:
    fail += 1
    print('❌ 有未渲染的 ** 标记（注释里的不计）')
print('结论：', '全部通过' if not fail else '%d 项失败' % fail)
sys.exit(1 if fail else 0)
