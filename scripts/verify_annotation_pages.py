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

# 与 pages.py 保持一致的 bold 过滤器
from server.routers.pages import _bold_filter    # noqa: E402
env.filters['bold'] = _bold_filter

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
from server import datasources                          # noqa: E402
from server.annotation import stats as ann_stats        # noqa: E402


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


common = {
    'nav_active': 'system-settings',
    'online_now': 1428,
    'online_label': '1,428',
}

ds = datasources.catalog()
html_s = render('pages/settings.html', dict(
    common, ds=ds, annot_pointer=ann_stats.overview(),
    sync={'server_cursor': 0, 'server_time': '2026-10-01 00:00:00',
          'operations': {'applied': 0, 'duplicate': 0, 'conflict_lost': 0, 'rejected': 0},
          'entities': {}},
    devices=[], protocols=[],
))

ctx_a = dict(common, nav_active='data-annotation')
ctx_a.update({'ov': ann_stats.overview(),
              'spec': ann_stats.annotation_spec(),
              'progress': ann_stats.session_rows(limit=12)})
html_a = render('pages/annotation.html', ctx_a)

# ---- 3. 断言：关键内容确实进了 HTML ---------------------------------------
print()
checks = [
    ('settings: 入口块', html_s, 'annotation-panel'),
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
]
fail = 0
for c in checks:
    name, hay, needle = c[0], c[1], c[2]
    want = c[3] if len(c) > 3 else True
    got = needle in hay
    ok = (got == want)
    print('%s %-42s %s' % ('✅' if ok else '❌', name, '' if ok else '(got=%s want=%s)' % (got, want)))
    if not ok:
        fail += 1

print()
print('裸 ** 残留（settings）：', html_s.count('**'))
print('裸 ** 残留（annotation）：', html_a.count('**'))
if html_s.count('**') or html_a.count('**'):
    fail += 1
    print('❌ 有未渲染的 ** 标记')
print('结论：', '全部通过' if not fail else '%d 项失败' % fail)
sys.exit(1 if fail else 0)
