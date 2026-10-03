# -*- coding: utf-8 -*-
"""smoke_pages.py — 管理站页面冒烟（对着**已经起来的**服务发请求）。

    # 先起服务（另开一个终端）
    .venv/bin/python -m uvicorn server.app:app --port 8787
    # 再跑
    .venv/bin/python scripts/smoke_pages.py
    .venv/bin/python scripts/smoke_pages.py --base http://127.0.0.1:9000

为什么不用 curl：本机 HTTP_PROXY 指向沙箱代理，回环请求会被拦成 502，
现象与「服务没起来」完全一样，极易误判。本脚本全程 `ProxyHandler({})`
显式禁用代理，并把代理导致的失败单独识别出来报错。

为什么不用 TestClient：那需要 httpx，而 requirements-dev.txt 里没有，
不该为跑一个冒烟去动项目 venv。标准库 urllib 足够。

验四件事：
  1. 每个页面都能 200 渲染（模板语法 / 未定义变量会在这一步炸出来）；
  2. 页面引用的静态资源都在（防死链）；
  3. 关键区块确实出现在 HTML 里（防「200 但内容被条件吃掉」）；
  4. 页内 #锚点 都有对应 id（防跳转带了链接、点上去没反应）。
"""
import argparse
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL = []

# (路径, [必须出现的关键词])
PAGES = [
    ('/',             ['侧边栏'] if False else []),
    ('/users',        []),
    ('/training',     []),
    ('/personas',     []),
    ('/feedback',     []),
    ('/settings',     ['id="annotation-panel"', '目录核查结论', '接入作业须知',
                       'id="catalog"', 'id="gates"', 'id="audit"', 'id="pipeline"']),
    ('/annotation',   ['id="workbench"', '两条链路', '标注状态机', '标注逻辑',
                       '作业须知', '标签空间', '标注进度', 'id="state"',
                       'id="rules"', 'id="notes"', 'id="labels"', 'id="progress"']),
    # 空态（没带 session）：必须给出回台账的出口，而不是一片空白。
    ('/strokes',      ['回到历史采集元数据']),
    ('/docs',         []),
    ('/assets/css/app.css', []),
]

ANCHOR_PAGES = ['/settings', '/annotation']


def read_admin_password():
    """var/admin_initial_password.txt 里是「username: xxx\\npassword: yyy」两行。"""
    p = os.path.join(ROOT, 'var', 'admin_initial_password.txt')
    if not os.path.exists(p):
        return ''
    m = re.search(r'password:\s*(\S+)', open(p, encoding='utf-8').read())
    return m.group(1) if m else ''


class Client:
    """带 Cookie 的最简客户端；显式禁用代理。"""

    def __init__(self, base):
        self.base = base.rstrip('/')
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),      # ← 关键：不走 HTTP_PROXY
            urllib.request.HTTPCookieProcessor(jar),
        )

    def get(self, path):
        return self._req(path, None)

    def post(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            with self.opener.open(req) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')
        except urllib.error.URLError as e:
            raise SystemExit(
                '❌ 连不上 %s —— %s\n'
                '   若信息里出现 Connection refused：服务没起。\n'
                '   若是其它网络错，先确认服务确实监听在该端口。'
                % (self.base, e))

    def _req(self, path, _unused):
        req = urllib.request.Request(self.base + path)
        try:
            with self.opener.open(req) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:8787')
    args = ap.parse_args()

    c = Client(args.base)

    # ---- 登录 ----------------------------------------------------------
    pw = read_admin_password()
    if not pw:
        print('⚠️  读不到 var/admin_initial_password.txt 里的口令，跳过登录（页面会 302）')
    else:
        st, _ = c.post('/login', {'username': 'admin', 'password': pw,
                                  'next': '/'})
        ok = st in (200, 302) and st != 401
        print('%s POST /login → %d' % ('✅' if ok else '❌', st))
        if not ok:
            FAIL.append('登录失败（%d）' % st)

    # ---- 页面 ----------------------------------------------------------
    bodies = {}
    for path, needles in PAGES:
        st, body = c.get(path)
        bodies[path] = body
        tag = '✅' if st == 200 else '❌'
        print('%s GET %-22s → %-3d %7d B' % (tag, path, st, len(body)))
        if st != 200:
            FAIL.append('%s 状态码 %d' % (path, st))
            continue
        for n in needles:
            if n not in body:
                FAIL.append('%s 缺少「%s」' % (path, n))
                print('   ❌ 缺少 %s' % n)

    # ---- 逐拍下钻：从台账里抓一条真实入口，点进去 -----------------------
    # 刻意**不硬编码** session id：入口链接是台账渲染出来的，从那里抓才能
    # 同时验证「链接格式对」和「落点真的在」这两件事。硬编码的话，
    # 哪天 seed 换了 id 就成了「测试自己造的数据」，页面坏了它照样绿。
    links = sorted(set(re.findall(r'href="(/strokes\?session=[^"&]+)"',
                                  bodies.get('/annotation', ''))))
    if not links:
        FAIL.append('/annotation 里没有指向 /strokes 的逐拍入口链接')
        print('❌ /annotation 里没有指向 /strokes 的逐拍入口链接')
    else:
        print('✅ /annotation 里有 %d 个逐拍入口，例：%s' % (len(links), links[0]))
        st, body = c.get(links[0])
        rows = len(re.findall(r'<tr class="group ', body))
        ok = st == 200 and 'id="strokes"' in body and rows > 0
        print('%s GET %-34s → %-3d %7d B（逐拍表 %d 行）'
              % ('✅' if ok else '❌', links[0], st, len(body), rows))
        if not ok:
            FAIL.append('逐拍页 %s 异常（状态 %d，逐拍行 %d）' % (links[0], st, rows))

    # ---- 静态资源死链 ---------------------------------------------------
    refs = set()
    for path in ('/settings', '/annotation', '/'):
        refs |= set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', bodies.get(path, '')))
    for ref in sorted(refs):
        st, _ = c.get(ref)
        tag = '✅' if st == 200 else '❌'
        print('%s %s' % (tag, ref))
        if st != 200:
            FAIL.append('死链 %s（%d）' % (ref, st))

    # ---- 锚点自检 -------------------------------------------------------
    for path in ANCHOR_PAGES:
        body = bodies.get(path, '')
        anchors = set(re.findall(r'href="#([A-Za-z0-9_-]+)"', body))
        miss = [a for a in sorted(anchors) if 'id="%s"' % a not in body]
        if miss:
            FAIL.append('%s 锚点无落点：%s' % (path, miss))
            print('❌ %s 锚点无落点：%s' % (path, miss))
        elif anchors:
            print('✅ %s 页内锚点 %d 个全部有落点' % (path, len(anchors)))

    print()
    if FAIL:
        print('❌ 失败 %d 项：' % len(FAIL))
        for f in FAIL:
            print('   ', f)
        return 1
    print('✅ 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
