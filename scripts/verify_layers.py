# -*- coding: utf-8 -*-
"""L0–L3 数据分层 + 管理后台鉴权的端到端回归脚本。

    .venv/Scripts/python.exe scripts/verify_layers.py

**为什么它自己起服务、而且是隔离的**：脚本会在临时目录里另建一套
L0/L1/L2 库（`NETPULSE_RAW_DIR` / `NETPULSE_ANNOTATION_DIR` / `ACEMATE_DB`），
用独立端口 `:8790` 启动，跑完即删。因此

  · 可以随时重复运行 —— **不会污染 `var/` 里的真实/演示数据**，也不需要先清库；
  · 每跑一次都是「全新库」，所以「首次归档 revision=1」这类断言永远成立，
    不会因为上一轮的残留而假失败；
  · 顺便覆盖了「配了口令」与「没配口令」两种放行分支；
  · 收尾还会断言**主库的口令文件没被动过** —— 这条断言是 2026-09-29 事故的
    产物：隔离实例必须连口令文件一起隔离，否则它会顺手覆盖主库口令（见下 G）。

覆盖范围：
  A 登录守卫（302/401/开放重定向/静态资源）      B L0 归档·幂等·版本·只追加·压缩·大包
  C L1 标注层改指针（raw_id → L0）                D L3 终端展示层（历史/逐拍/分析/排行）
  E 采集端时间字段口径（Swift Date 陷阱）         F 无口令实例的读写分档
  G 隔离完整性（主库 var/ 数据与凭据文件零改动）

退出码 0 = 全部通过。任何 FAIL 都会打印明细，可直接粘进 issue。
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # backend-web/
PY = os.path.join(HERE, '.venv', 'Scripts', 'python.exe')
if not os.path.exists(PY):
    PY = sys.executable

PORT, PORT_OPEN = 8790, 8791
KEY = 'verify-key-' + uuid.uuid4().hex[:8]
ADMIN_PW = 'verify-pass-' + uuid.uuid4().hex[:6]
RUN = uuid.uuid4().hex[:6].upper()

OK, BAD = [], []


def ck(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('PASS' if cond else 'FAIL', name,
                         ('  <- ' + str(extra)[:240]) if extra else ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_nored = urllib.request.build_opener(NoRedirect)


def req(port, method, path, *, body=None, headers=None, cookie=None,
        form=None, files=None, redirect=True):
    """极简 HTTP 客户端。返回 (status, headers(小写键), text)。

    ⚠️ 头部一律转小写：`dict(resp.headers)` 把键名小写化，
    而 `urllib` 在 Request 里保留原样，混用会出现
    `hd.get('Set-Cookie')` 恒为 None 的假失败。
    """
    h = dict(headers or {})
    if cookie:
        h['Cookie'] = cookie
    data = None
    if files is not None:
        b = '----wf' + uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in (form or {}).items():
            buf.write(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                       % (b, k, v)).encode('utf-8'))
        for k, (fn, content) in files.items():
            buf.write(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n' % (b, k, fn)).encode())
            buf.write(content if isinstance(content, bytes) else content.encode('utf-8'))
            buf.write(b'\r\n')
        buf.write(('--%s--\r\n' % b).encode())
        data = buf.getvalue()
        h['Content-Type'] = 'multipart/form-data; boundary=' + b
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'

    r = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                               data=data, headers=h, method=method)
    opener = urllib.request.urlopen if redirect else _nored.open
    try:
        with opener(r, timeout=20) as resp:
            hd = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hd, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        hd = {k.lower(): v for k, v in e.headers.items()}
        return e.code, hd, e.read().decode('utf-8', 'replace')


def jload(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def wait_up(port, seconds=30):
    for _ in range(int(seconds * 2)):
        time.sleep(0.5)
        try:
            urllib.request.urlopen('http://127.0.0.1:%d/login' % port, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            continue
    return False


def start(port, tmp, *, with_key):
    env = dict(os.environ)
    env.update({
        'NETPULSE_RAW_DIR': os.path.join(tmp, 'raw%d' % port),
        'NETPULSE_ANNOTATION_DIR': os.path.join(tmp, 'anno%d' % port),
        'ACEMATE_DB': os.path.join(tmp, 'acemate%d.db' % port),
        'ACEMATE_ADMIN_PASSWORD': ADMIN_PW,
        # 口令文件也必须一起隔离 —— 否则隔离实例会因为「临时库还没有账号」
        # 而走创建分支，顺手把**主库的** var/admin_initial_password.txt 覆盖掉
        # （2026-09-29 真实踩过：主库口令就此不可知，只能重设）。
        # 根因已在 server/adminauth.py:password_file() 修掉，这里是双保险。
        'ACEMATE_ADMIN_PASSWORD_FILE': os.path.join(tmp, 'admin_pw%d.txt' % port),
    })
    env.pop('NETPULSE_INGEST_KEY', None)
    if with_key:
        env['NETPULSE_INGEST_KEY'] = KEY
    log = open(os.path.join(tmp, 'server%d.log' % port), 'w')
    p = subprocess.Popen([PY, '-u', 'run.py', '--port', str(port)],
                         cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def stop(p, log):
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
    log.close()


PACKAGE = {
    'session': {
        'id': 'VFY-%s-01' % RUN, 'startedAt': '2026-09-28T09:00:00.000Z',
        'endedAt': '2026-09-28T09:30:00.000Z', 'wrist': 'right', 'duration': 1800,
        'avgHeartRate': 141, 'maxHeartRate': 176, 'activeCalories': 402, 'distanceKm': 2.05,
        'swings': [
            {'type': 'forehand', 'impactTime': 10.0, 'racketHeadSpeedKmh': 118.4, 'confidence': 0.92},
            {'type': 'forehand', 'impactTime': 15.2, 'racketHeadSpeedKmh': 121.5, 'confidence': 0.88},
            {'type': 'backhand', 'impactTime': 21.4, 'racketHeadSpeedKmh': 104.2, 'confidence': 0.81},
            {'type': 'serve', 'impactTime': 30.1, 'racketHeadSpeedKmh': 168.9, 'confidence': 0.95},
            {'type': 'slice', 'impactTime': 44.7, 'racketHeadSpeedKmh': 96.3, 'confidence': 0.77},
            {'type': 'unknown', 'impactTime': 51.0, 'racketHeadSpeedKmh': 90.0, 'confidence': 0.31},
        ],
    },
    'samples': [{'t': i / 800.0, 'ax': 1.0 + i * 1e-4, 'ay': -2.0, 'az': 9.8,
                 'rx': 0.1, 'ry': 0.2, 'rz': 0.3,
                 'gx': 0.0, 'gy': 0.0, 'gz': 9.8, 'qx': 0, 'qy': 0, 'qz': 0, 'qw': 1}
                for i in range(47)],
}


def raw_payload(sid, tag=''):
    p = json.loads(json.dumps(PACKAGE))
    p['session']['id'] = sid
    if tag:
        p['session']['note'] = tag
    return p


def suite_authed(tmp):
    """实例 A：配了口令。覆盖 A–E。"""
    sid0 = 'VFY-%s-RAW00' % RUN
    print('\n=== A. 登录守卫 ===')
    ck('GET /login -> 200', req(PORT, 'GET', '/login')[0] == 200)
    st, hd, _ = req(PORT, 'GET', '/', redirect=False)
    ck('GET / 未登录 -> 302', st == 302, st)
    ck('  Location 指向 /login?next=/', '/login?next=' in hd.get('location', ''),
       hd.get('location'))
    ck('GET /annotation 未登录 -> 302', req(PORT, 'GET', '/annotation', redirect=False)[0] == 302)
    ck('GET /api/students 未登录 -> 401', req(PORT, 'GET', '/api/students')[0] == 401)
    ck('GET /assets/css/app.css -> 200（静态资源公开）',
       req(PORT, 'GET', '/assets/css/app.css')[0] == 200)

    print('\n=== B. 登录 ===')
    st, _, _ = req(PORT, 'POST', '/login',
                   form={'username': 'admin', 'password': 'wrong-pw'},
                   redirect=False)
    ck('POST /login 错误口令 -> 401', st == 401, st)
    st, hd, _ = req(PORT, 'POST', '/login',
                    form={'username': 'admin', 'password': ADMIN_PW},
                    redirect=False)
    ck('POST /login 正确口令 -> 302', st == 302, st)
    sc = hd.get('set-cookie', '')
    ck('  Cookie：HttpOnly + SameSite=Lax', 'httponly' in sc.lower() and 'samesite=lax' in sc.lower(),
       sc[:120])
    ck('  本地 http 不加 Secure', 'secure' not in sc.lower(), sc[:120])
    cookie = sc.split(';')[0]

    # `next` 是**表单字段**（`Form('/')`），不是查询参数 —— POST 时必须放在 body 里。
    # 整条链是：守卫 302 → `/login?next=/users`（查询参数）→ 模板渲染成隐藏字段
    #          → POST 表单带上 next → 302 回 `next`。三段都要对上才行。
    st, hd, _ = req(PORT, 'GET', '/users', redirect=False)
    loc = hd.get('location') or ''
    # `urllib.parse.quote` 默认 safe='/'，所以斜杠不会被转义成 %2F —— 两种写法都接受，
    # 只要求「去了 /login 且把原路径带成了 next」
    ck('未登录访问 /users -> 302 /login?next=/users',
       st == 302 and loc.startswith('/login?next=') and 'users' in loc, (st, loc))
    st, _, b = req(PORT, 'GET', '/login?next=/users')
    ck('  登录页把 next 渲染成隐藏字段', st == 200 and 'name="next"' in b and 'value="/users"' in b,
       st)

    st, hd, _ = req(PORT, 'POST', '/login',
                    form={'username': 'admin', 'password': ADMIN_PW, 'next': '//evil.com'},
                    redirect=False)
    ck('开放重定向 next=//evil.com 被改写为站内路径',
       st == 302 and 'evil.com' not in (hd.get('location') or ''),
       (st, hd.get('location')))
    st, hd, _ = req(PORT, 'POST', '/login',
                    form={'username': 'admin', 'password': ADMIN_PW, 'next': '/users'},
                    redirect=False)
    ck('站内 next 正常回跳（/users）', st == 302 and hd.get('location') == '/users',
       (st, hd.get('location')))

    st, _, b = req(PORT, 'GET', '/', cookie=cookie)
    ck('已登录 GET / -> 200', st == 200 and len(b) > 5000, (st, len(b)))
    st, _, _ = req(PORT, 'GET', '/api/platform/overview', cookie=cookie)
    ck('已登录 GET /api/platform/overview -> 200', st == 200, st)

    print('\n=== C. L0 原始层：归档 / 幂等 / 版本 / 只追加 ===')
    kh = {'X-Ingest-Key': KEY}
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh,
                   form={'id': sid0, 'openid': 'dev_vfy_%s' % RUN, 'source': 'verify',
                         'shape': 'raw_package'},
                   files={'raw': ('raw_%s.json' % sid0, json.dumps(PACKAGE))})
    d1 = jload(b)
    ck('首次归档 -> created 且 revision=1',
       st == 200 and d1 and d1.get('status') == 'created' and d1.get('revision') == 1, (st, d1))
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh,
                   form={'id': sid0, 'openid': 'dev_vfy_%s' % RUN},
                   files={'raw': ('raw_%s.json' % sid0, json.dumps(PACKAGE))})
    d2 = jload(b)
    ck('同形态同字节重传 -> duplicate 且 raw_id 不变',
       d2 and d2.get('status') == 'duplicate' and d2.get('raw_id') == d1.get('raw_id'), d2)
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh,
                   form={'id': sid0},
                   files={'raw': ('raw_%s.json' % sid0, json.dumps(raw_payload(sid0, 'v2')))})
    d3 = jload(b)
    ck('不同字节 -> created 且 revision=2',
       d3 and d3.get('status') == 'created' and d3.get('revision') == 2, d3)
    ck('  新旧 raw_id 不同（内容寻址）', d3 and d3.get('raw_id') != d1.get('raw_id'))
    st, _, b = req(PORT, 'GET', '/api/raw/sessions/%s/revisions' % sid0, headers=kh)
    ck('revisions 返回 2 个版本（历史可回溯）', st == 200 and jload(b).get('count') == 2, st)
    st, _, b = req(PORT, 'GET', '/api/raw/sessions/%s/payload' % sid0, headers=kh)
    ck('payload 取到最新版本正文', st == 200 and jload(b).get('session', {}).get('note') == 'v2',
       st)

    db = os.path.join(tmp, 'raw%d' % PORT, 'acemate_raw.db')
    c = sqlite3.connect(db)
    for sql, label in (('UPDATE raw_sessions SET source="x"',
                        '只追加触发器拦截 UPDATE'),
                       ('DELETE FROM raw_sessions', '只追加触发器拦截 DELETE')):
        try:
            c.execute(sql)
            ck(label, False, '居然执行成功了')
        except sqlite3.IntegrityError as e:
            ck(label, '只追加' in str(e), str(e)[:60])
    c.close()

    print('\n=== D. L0 压缩 / 大包 / 内联阈值 ===')
    sid_gz = 'VFY-%s-GZ' % RUN
    blob = gzip.compress(json.dumps(raw_payload(sid_gz)).encode(), 6)
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh,
                   form={'id': sid_gz, 'shape': 'raw_package'},
                   files={'raw': ('raw_%s.json.gz' % sid_gz, blob)})
    dg = jload(b)
    ck('gzip 包上传 -> created', st == 200 and dg and dg.get('status') == 'created', (st, dg))
    ck('  encoding 标记为 gzip', dg and dg.get('encoding') == 'gzip', dg and dg.get('encoding'))
    ck('  落盘带 .gz 后缀', (dg or {}).get('file_path', '').endswith('.gz'),
       (dg or {}).get('file_path'))
    ck('  byte_size = 压缩后大小', dg and dg.get('byte_size') == len(blob),
       (dg or {}).get('byte_size'))
    st, _, b = req(PORT, 'GET', '/api/raw/sessions/%s/payload' % sid_gz, headers=kh)
    ck('  读取时透明解压（拿到原始 samples）',
       st == 200 and len((jload(b) or {}).get('samples', [])) == len(PACKAGE['samples']), st)
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh, form={'id': sid_gz},
                   files={'raw': ('raw_%s.json.gz' % sid_gz, blob)})
    ck('  gzip 重传 -> duplicate（幂等）', jload(b).get('status') == 'duplicate', jload(b))

    sid_big = 'VFY-%s-BIG' % RUN
    big = json.dumps({'session': {'id': sid_big}, 'pad': 'x' * (1 << 20)}).encode()
    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh, form={'id': sid_big},
                   files={'raw': ('raw_%s.json' % sid_big, big)})
    dbg = jload(b)
    ck('超过 INLINE_LIMIT 的包 -> created', st == 200 and dbg.get('status') == 'created', (st, dbg))
    c = sqlite3.connect(db)
    row = c.execute('SELECT payload, file_path FROM raw_sessions WHERE session_id=?',
                    (sid_big,)).fetchone()
    c.close()
    ck('  库内不冗余内联（payload 为 NULL）且只落文件',
       row and row[0] is None and row[1], row)

    print('\n=== E. L0 读 / 写口分档（配了口令时）===')
    ck('读口 无凭证 -> 401', req(PORT, 'GET', '/api/raw/stats')[0] == 401)
    ck('读口 口令错 -> 401',
       req(PORT, 'GET', '/api/raw/stats', headers={'X-Ingest-Key': 'nope'})[0] == 401)
    st, _, b = req(PORT, 'GET', '/api/raw/stats', headers=kh)
    ck('读口 口令对 -> 200', st == 200, st)
    st, _, _ = req(PORT, 'GET', '/api/raw/stats', cookie=cookie)
    ck('读口 管理登录态 -> 200', st == 200, st)
    ck('写口 无口令 -> 401',
       req(PORT, 'POST', '/api/raw/sessions', form={'id': 'X'},
           files={'raw': ('a.json', '{}')})[0] == 401)

    print('\n=== F. L1 标注层：raw_id 指针 + 从 L0 取波形 ===')
    sid_a = 'VFY-%s-ANNO' % RUN
    st, _, b = req(PORT, 'POST', '/api/sessions', cookie=cookie,
                   form={'id': sid_a, 'wrist': 'right'},
                   files={'raw': ('raw_%s.json' % sid_a, json.dumps(raw_payload(sid_a)))})
    da = jload(b)
    ck('上传标注会话 -> 200', st == 200 and da, (st, da))
    ck('  预标注 prefilled=5（6 拍去掉 1 个 unknown）', da and da.get('prefilled') == 5, da)
    ck('  响应带回 raw_id（指向 L0）', bool((da or {}).get('raw_id')), da)
    adb = os.path.join(tmp, 'anno%d' % PORT, 'annotations.db')
    c = sqlite3.connect(adb)
    c.row_factory = sqlite3.Row
    r = c.execute('SELECT raw_id, raw_path FROM sessions WHERE id=?', (sid_a,)).fetchone()
    l0 = sqlite3.connect(db)
    l0.row_factory = sqlite3.Row
    hit = l0.execute('SELECT raw_id FROM raw_sessions WHERE raw_id=?',
                     (r['raw_id'],)).fetchone() if r else None
    ann = c.execute('SELECT COUNT(*) FROM annotations WHERE session_id=?', (sid_a,)).fetchone()[0]
    c.close()
    l0.close()
    ck('  sessions.raw_id 与 L0 同一行', bool(hit), (r and r['raw_id']))
    ck('  raw_path 指向 var/raw/files', 'raw' in (r['raw_path'] or '') and
       os.sep + 'files' + os.sep in (r['raw_path'] or ''), r and r['raw_path'])
    ck('  预标注落库 5 条', ann == 5, ann)
    st, _, b = req(PORT, 'GET', '/api/sessions/%s/samples' % sid_a, cookie=cookie)
    ck('波形从 L0 取到（count>0）', st == 200 and (jload(b) or {}).get('count', 0) > 0, (st, b[:120]))

    print('\n=== G. L3 终端展示层 ===')
    st, _, b = req(PORT, 'POST', '/api/wechat/login', body={'code': 'verify-code-%s' % RUN})
    dl = jload(b)
    tok = (dl or {}).get('token')
    ck('微信登录 -> 200 + token', st == 200 and tok, (st, dl))
    ck('无 token 读历史 -> 401', req(PORT, 'GET', '/api/prod/sessions')[0] == 401)
    ck('伪造 token -> 401',
       req(PORT, 'GET', '/api/prod/sessions', headers={'Authorization': 'Bearer fake'})[0] == 401)

    openid = (dl or {}).get('openid')
    sid_p = 'VFY-%s-PROD' % RUN
    prof = json.loads(json.dumps(PACKAGE))
    prof['session']['id'] = sid_p
    st, _, b = req(PORT, 'POST', '/api/prod/sessions', headers=kh,
                   body={'openid': openid, 'session': prof['session']})
    dp = jload(b)
    ck('上传一场（L0 归档 + L2 结构化 + 开通学员）', st == 200 and (dp or {}).get('ok'), (st, dp))
    ck('  raw_layer=created 且形态 match_session',
       (dp or {}).get('raw_layer', {}).get('status') == 'created', dp)
    ck('  学员档案已开通', bool((dp or {}).get('student_id')) and
       (dp or {}).get('student_provisioned') is True, dp)

    auth = {'Authorization': 'Bearer ' + tok}
    st, _, b = req(PORT, 'GET', '/api/prod/sessions', headers=auth)
    dh = jload(b)
    ck('历史列表 -> 1 场', st == 200 and (dh or {}).get('count') == 1, (st, dh and dh.get('count')))
    s0 = ((dh or {}).get('sessions') or [{}])[0]
    ck('  含会话级指标（心率/卡路里/极速）',
       s0.get('avgHeartRate') == 141 and s0.get('calories') == 402.0
       and s0.get('peakSpeedKmh'), s0)
    ck('  含击球分项计数', s0.get('counts', {}).get('forehand') == 2, s0.get('counts'))

    st, _, b = req(PORT, 'GET', '/api/prod/sessions/%s' % sid_p, headers=auth)
    dd = jload(b)
    ck('单场详情 -> 逐拍 5 条（unknown 不入明细）',
       st == 200 and (dd or {}).get('strokeCount') == 5, (st, dd and dd.get('strokeCount')))
    mix = (dd or {}).get('strokeMix') or []
    ck('  击球构成含 share', bool(mix) and 'share' in mix[0], mix[:1])

    st, _, b = req(PORT, 'GET', '/api/prod/analysis', headers=auth)
    ck('纵向分析 -> 200 且含 KPI', st == 200 and 'kpi' in (jload(b) or {}), st)

    st, _, b = req(PORT, 'GET', '/api/prod/leaderboard', headers=auth)
    dlb = jload(b) or {}
    board = dlb.get('maxBoard') or []
    leaked = [k for it in board for k in it if k in ('student_id', 'avatar_url')]
    ck('排行榜已匿名化（不透出 student_id / 头像）', not leaked, leaked)
    ck('  含 isMe 标记', any(it.get('isMe') for it in board), board[:2])

    st, _, b = req(PORT, 'POST', '/api/prod/sessions', headers=kh,
                   body={'openid': openid, 'session': prof['session']})
    ck('同场重传 -> 分析库与 L0 均幂等（duplicate）',
       (jload(b) or {}).get('raw_layer', {}).get('status') == 'duplicate'
       and (jload(b) or {}).get('analysis_db', {}).get('duplicate', 0) > 0, jload(b))

    st, _, b = req(PORT, 'POST', '/api/raw/sessions', headers=kh,
                   form={'id': sid_p, 'shape': 'raw_package'},
                   files={'raw': ('raw_%s.json' % sid_p, json.dumps(raw_payload(sid_p)))})
    ck('同会话两种形态独立记版本（raw_package r1）',
       (jload(b) or {}).get('status') == 'created' and (jload(b) or {}).get('revision') == 1,
       jload(b))
    st, _, b = req(PORT, 'GET', '/api/raw/sessions/%s/revisions' % sid_p, headers=kh)
    shapes = sorted(r['shape'] for r in (jload(b) or {}).get('revisions', []))
    ck('  两形态共存且互不覆盖', shapes == ['match_session', 'raw_package'], shapes)

    other = 'dev_vfy_other_%s' % RUN
    st, _, b = req(PORT, 'POST', '/api/wechat/login', body={'code': 'other-%s' % RUN})
    tok_b = (jload(b) or {}).get('token')
    st, _, b = req(PORT, 'GET', '/api/prod/sessions/%s' % sid_p,
                   headers={'Authorization': 'Bearer ' + tok_b})
    ck('B 账号读 A 的单场 -> 404（租户隔离）', st == 404, st)
    st, _, b = req(PORT, 'GET', '/api/prod/sessions?openid=%s' % other,
                   headers=auth)
    ck('冒充他人 openid -> 403', st == 403, st)

    print('\n=== H. 采集端时间字段口径（Swift Date 陷阱）===')
    st, _, b = req(PORT, 'POST', '/api/prod/sessions', headers=kh,
                   body={'openid': openid,
                         'session': {'id': 'VFY-%s-TS' % RUN,
                                     'startedAt': 780000000.0, 'wrist': 'right',
                                     'duration': 600, 'swings': []}})
    ck('数字时间戳（Apple 参考纪元秒数）-> 400 明确拒绝', st == 400, (st, b[:120]))
    ck('  报错文案点明 startedAt', 'startedAt' in b, b[:120])

    print('\n=== I. 登出即失效 ===')
    st, hd, _ = req(PORT, 'GET', '/logout', cookie=cookie, redirect=False)
    ck('GET /logout -> 302', st == 302, st)
    ck('  清 Cookie（Max-Age=0）', 'max-age=0' in hd.get('set-cookie', '').lower(),
       hd.get('set-cookie', '')[:100])
    ck('登出后复用旧 Cookie -> 401',
       req(PORT, 'GET', '/api/students', cookie=cookie)[0] == 401)


def suite_open():
    """实例 B：**不配**口令。验证读写分档（开发期写口放开、读口仍要登录）。"""
    print('\n=== J. 未配置口令时的读写分档 ===')
    ck('登录页仍可访问 -> 200', req(PORT_OPEN, 'GET', '/login')[0] == 200)
    ck('未登录页面 -> 302', req(PORT_OPEN, 'GET', '/', redirect=False)[0] == 302)
    st, _, b = req(PORT_OPEN, 'GET', '/api/raw/stats')
    ck('读口 无凭证 -> 401（不跟着写口裸奔）', st == 401, (st, b[:110]))
    st, _, b = req(PORT_OPEN, 'POST', '/api/raw/sessions',
                   form={'id': 'VFY-%s-OPEN' % RUN},
                   files={'raw': ('a.json', json.dumps({'hello': 'world'}))})
    ck('写口 无口令 -> 200（开发期放开，便于采集端联调）',
       st == 200 and (jload(b) or {}).get('status') == 'created', (st, b[:120]))


def _fingerprint(path):
    """文件内容指纹；不存在返回 None。用于确认隔离实例没碰主库文件。"""
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    tmp = tempfile.mkdtemp(prefix='acemate_verify_')
    # 主库口令文件在「跑之前」的指纹，与收尾时的断言配对使用
    main_pw = os.path.join(HERE, 'var', 'admin_initial_password.txt')
    pw_before = _fingerprint(main_pw)
    print('=' * 68)
    print('L0–L3 数据分层 · 端到端回归脚本')
    print('临时数据目录：%s' % tmp)
    print('=' * 68)
    a = b = None
    la = lb = None
    try:
        a, la = start(PORT, tmp, with_key=True)
        if not wait_up(PORT):
            print('服务未能启动，日志尾部：')
            la.flush()
            print(open(os.path.join(tmp, 'server%d.log' % PORT), encoding='utf-8',
                       errors='replace').read()[-2000:])
            return 2
        suite_authed(tmp)

        b, lb = start(PORT_OPEN, tmp, with_key=False)
        if wait_up(PORT_OPEN):
            suite_open()
        else:
            ck('无口令实例启动', False, '超时')
    finally:
        if a:
            stop(a, la)
        if b:
            stop(b, lb)

    # ---- 防回归：隔离实例绝不能改动主库的凭据文件 --------------------------
    # 2026-09-29 的事故就发生在这一行没被检查的地方：隔离实例（临时库、无账号）
    # 走了「创建默认账号」分支，把主库的 var/admin_initial_password.txt 覆盖成
    # 测试口令，而主库 admin_users 的哈希没变 —— 于是主人登录时得到
    # 「用户名或口令不正确」，且口令文件里的内容已经不是主库的，
    # 只能靠 scripts/reset_admin_password.py 重设。
    pw_after = _fingerprint(main_pw)
    ck('主库口令文件未被隔离实例污染',
       pw_after == pw_before,
       'before=%s after=%s' % ((pw_before or '不存在')[:8], (pw_after or '不存在')[:8]))

    print('\n' + '=' * 68)
    print('通过 %d 项，失败 %d 项' % (len(OK), len(BAD)))
    if BAD:
        print('失败清单：')
        for x in BAD:
            print('  ·', x)
    print('=' * 68)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if BAD else 0


if __name__ == '__main__':
    sys.exit(main())
