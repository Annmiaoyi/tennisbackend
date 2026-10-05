#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一份采集端 MatchSession JSON 上传到后端（**真机实测用**，不是生产链路）。

为什么需要它：四条上传口各落各的层，同一份数据发到不同口，"能在哪个页面
看到"完全不同（详见 docs/WATCH_FIELD_TEST.md §1.4）。手工 curl 很容易发错口
还以为"上传成功了"。本脚本做三件事：

  1. **先校验再上传** —— 时刻不单调 / 缺 id / 类型非法，当场指出，
     而不是让后端静默落一份脏数据；
  2. 按 `--to` 发到对应通道（可多选）；
  3. 结束时打印**该去哪个页面核对**，避免"传了但不知道看哪"。

用法：::

    # 只校验，不联网
    python scripts/upload_watch_session.py match_xxx.json --dry-run

    # 三通道全发（本地默认地址）
    python scripts/upload_watch_session.py match_xxx.json \\
        --to annotation,prod,raw \\
        --base http://127.0.0.1:8787 \\
        --admin-password admin123 \\
        --ingest-key "$NETPULSE_INGEST_KEY" \\
        --student stu-00001001 --openid dev_field_test

配置文件接受两种形态（自动识别）：
  · 整包 `{"session": {...}, "samples": [...]}`（标注/原始通道要 samples）
  · 裸 MatchSession `{"id": ..., "swings": [...]}`（终端通道够用）

**刻意不引第三方依赖**：用 urllib + 手写 multipart，与本项目
"标准库优先"的取向一致，也避免给运维加装包负担。
"""
import argparse
import json
import os
import sys
import uuid
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import (HTTPCookieProcessor, ProxyHandler, Request,
                            build_opener)

# `session.swings[].type` 的合法取值 —— 与 server/ingest.py 一致。
# 不在这个集合里的（如 unknown）**不计入逐拍明细**，但仍计入 stroke_count。
VALID_TYPES = {'forehand', 'backhand', 'serve', 'slice', 'volley', 'smash'}


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def ok(msg):
    print('  \033[1;32m✓\033[0m %s' % msg)


def bad(msg):
    print('  \033[1;31m✗\033[0m %s' % msg)


def warn(msg):
    print('  \033[1;33m!\033[0m %s' % msg)


def head(msg):
    print('\n\033[1;36m==> %s\033[0m' % msg)


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #
def load_package(path):
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        obj = json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise SystemExit('文件不是合法 JSON：%s（%s）' % (path, e))
    if not isinstance(obj, dict):
        raise SystemExit('顶层必须是对象，实际是 %s' % type(obj).__name__)

    # 两种形态归一
    if isinstance(obj.get('session'), dict):
        session = obj['session']
        samples = obj.get('samples') or []
    else:
        session = obj                 # 裸 MatchSession
        samples = obj.get('samples') or []
    return raw, session, samples


def validate(session, samples, *, want_samples):
    """返回 (errors, warnings)。errors 非空则不继续。"""
    errors, warnings = [], []

    sid = str(session.get('id') or '').strip()
    if not sid:
        errors.append('session.id 缺失 —— 它是跨端幂等键，后端会直接 400')

    swings = session.get('swings')
    if swings is None:
        warnings.append('session.swings 缺失 —— 会落一场"0 拍"的会话')
        swings = []
    if not isinstance(swings, list):
        errors.append('session.swings 必须是数组，实际是 %s' % type(swings).__name__)
        swings = []

    # 时刻单调性 —— 这是**最值钱的一条校验**。
    # seed.py 造演示数据时就是在这里翻车的（55/56 场不单调），
    # 真机上如果传输或编码串了，最先露馅的也是它。
    ts, prev = [], None
    n_bad_ts = 0
    for i, sw in enumerate(swings, 1):
        if not isinstance(sw, dict):
            errors.append('swings[%d] 不是对象' % i)
            continue
        t = sw.get('impactTime', sw.get('impact_time'))
        if t is None:
            n_bad_ts += 1
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            errors.append('swings[%d].impactTime 不是数字：%r' % (i, t))
            continue
        if prev is not None and t < prev:
            errors.append('swings[%d].impactTime=%s 小于前一拍 %s —— **时刻不单调**，'
                          '说明采集/编码环节有问题' % (i, t, prev))
            break
        prev = t
        ts.append(t)
    if n_bad_ts:
        warnings.append('%d 拍没有 impactTime，会被跳过' % n_bad_ts)
    if ts and session.get('duration'):
        if ts[-1] > float(session['duration']):
            warnings.append('末拍 %ss 超出会话时长 %ss' % (ts[-1], session['duration']))

    n_unknown = sum(1 for s in swings
                    if isinstance(s, dict)
                    and str(s.get('type') or '').strip().lower() not in VALID_TYPES)
    if n_unknown:
        warnings.append('%d 拍类型不在 %s 内（如 unknown）—— 不进逐拍明细，'
                        '但计入 stroke_count' % (n_unknown, '/'.join(sorted(VALID_TYPES))))

    if want_samples and not samples:
        warnings.append('本包没有 samples 波形 —— 标注通道仍可上传，'
                        '但工作台的波形图会是空的')
    return errors, warnings, sid


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_opener():
    """**禁用代理**。本机/局域网地址走系统代理会被拦成 502，
    症状与"服务没起来"一模一样（本机沙箱环境已知问题）。"""
    return build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))


def call(opener, method, url, *, data=None, ctype=None, headers=None, timeout=20):
    hdrs = dict(headers or {})
    if ctype:
        hdrs['Content-Type'] = ctype
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            return resp.status, body
    except HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except URLError as e:
        return None, str(e.reason)


def multipart(fields, files):
    """files: {name: (filename, bytes, content_type)}"""
    boundary = '----AceMateBoundary' + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, k, v)).encode('utf-8')
    for k, (fn, data, ct) in files.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (boundary, k, fn, ct)).encode('utf-8')
        out += data + b'\r\n'
    out += ('--%s--\r\n' % boundary).encode('utf-8')
    return bytes(out), 'multipart/form-data; boundary=%s' % boundary


def show(status, body, expect=200, label=''):
    tag = '%s ' % label if label else ''
    if status == expect:
        ok('%sHTTP %s' % (tag, status))
        return True
    bad('%sHTTP %s' % (tag, status))
    print('      %s' % body[:400].replace('\n', '\n      '))
    return False


# --------------------------------------------------------------------------- #
# 三条通道
# --------------------------------------------------------------------------- #
def login(opener, base, user, password):
    body, ct = multipart({'username': user, 'password': password, 'next': '/'}, {})
    st, txt = call(opener, 'POST', base + '/login', data=body, ctype=ct)
    return st in (200, 302, 303), st, txt


def to_annotation(opener, base, raw_bytes, sid, wrist):
    """标注通道：raw_package 形态 + 标注库 sessions 行 → **/strokes 页可见**。"""
    body, ct = multipart({'id': sid, 'wrist': wrist},
                         {'raw': ('raw_%s.json' % sid, raw_bytes, 'application/json')})
    st, txt = call(opener, 'POST', base + '/api/sessions', data=body, ctype=ct)
    if show(st, txt, label='[标注通道]'):
        try:
            d = json.loads(txt)
            print('      prefilled=%s raw_id=%s raw_revision=%s'
                  % (d.get('prefilled'), d.get('raw_id'), d.get('raw_revision')))
        except Exception:
            pass
        return True
    return False


def to_prod(opener, base, session, ingest_key, openid, student, include_strokes=True):
    """终端通道：match_session 形态 + L2 → **App / 小程序 可见**。"""
    payload = {'openid': openid, 'session': session,
               'include_strokes': bool(include_strokes)}
    if student:
        payload['student_id'] = student
    st, txt = call(opener, 'POST', base + '/api/prod/sessions',
                   data=json.dumps(payload).encode('utf-8'),
                   ctype='application/json',
                   headers={'X-Ingest-Key': ingest_key} if ingest_key else None)
    if show(st, txt, label='[终端通道]'):
        try:
            d = json.loads(txt)
            print('      session_id=%s strokes_ingested=%s analysis_db=%s'
                  % (d.get('session_id'), d.get('strokes_ingested'), d.get('analysis_db')))
        except Exception:
            pass
        return True
    return False


def to_raw(opener, base, raw_bytes, sid, ingest_key, shape='raw_package'):
    """原始层：只封存 L0（sha256 幂等、只追加）。"""
    body, ct = multipart({'id': sid, 'shape': shape},
                         {'raw': ('raw_%s.json' % sid, raw_bytes, 'application/json')})
    st, txt = call(opener, 'POST', base + '/api/raw/sessions', data=body, ctype=ct,
                   headers={'X-Ingest-Key': ingest_key} if ingest_key else None)
    return show(st, txt, label='[原始层]')


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description='上传一份采集端 MatchSession JSON（真机实测用）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', help='match_<id>.json（整包或裸 MatchSession 均可）')
    ap.add_argument('--base', default=os.environ.get('ACEMATE_BASE', 'http://127.0.0.1:8787'),
                    help='后端地址（默认 %(default)s）')
    ap.add_argument('--to', default='annotation',
                    help='逗号分隔：annotation,prod,raw（默认 annotation）')
    ap.add_argument('--session-id', default=None, help='覆盖文件里的 session.id')
    ap.add_argument('--wrist', default='right', choices=['left', 'right'])
    ap.add_argument('--admin-user', default='admin')
    ap.add_argument('--admin-password', default=os.environ.get('ACEMATE_ADMIN_PASSWORD', ''))
    ap.add_argument('--ingest-key', default=os.environ.get('NETPULSE_INGEST_KEY', ''))
    ap.add_argument('--openid', default='dev_field_test', help='终端通道的身份')
    ap.add_argument('--student', default=None, help='归属学员 id（终端通道）')
    ap.add_argument('--no-strokes', action='store_true', help='终端通道不带逐拍明细')
    ap.add_argument('--dry-run', action='store_true', help='只校验，不联网')
    args = ap.parse_args()

    targets = [t.strip() for t in args.to.split(',') if t.strip()]
    unknown = [t for t in targets if t not in ('annotation', 'prod', 'raw')]
    if unknown:
        raise SystemExit('未知通道：%s（可选 annotation/prod/raw）' % ','.join(unknown))

    head('1. 读取与校验')
    raw_bytes, session, samples = load_package(args.file)
    if args.session_id:
        session['id'] = args.session_id
    want_samples = 'annotation' in targets or 'raw' in targets
    errors, warnings, sid = validate(session, samples, want_samples=want_samples)

    print('  session.id   : %s' % (sid or '(缺失)'))
    print('  swings       : %d 拍' % len(session.get('swings') or []))
    print('  samples      : %d 个波形采样点' % len(samples))
    print('  duration     : %s' % session.get('duration'))
    for w in warnings:
        warn(w)
    if errors:
        for e in errors:
            bad(e)
        raise SystemExit('\n校验未通过，未上传。')
    ok('校验通过')

    if args.dry_run:
        head('--dry-run：到此为止')
        return 0

    opener = make_opener()
    results = {}

    if 'annotation' in targets:
        head('2. 标注通道 → /api/sessions（管理后台 /strokes 页可见）')
        if not args.admin_password:
            bad('缺少 --admin-password（或环境变量 ACEMATE_ADMIN_PASSWORD）—— 该口需登录态')
            results['annotation'] = False
        else:
            good, st, txt = login(opener, args.base, args.admin_user, args.admin_password)
            if not good:
                bad('登录失败 HTTP %s：%s' % (st, txt[:200]))
                results['annotation'] = False
            else:
                ok('已取得管理后台会话')
                results['annotation'] = to_annotation(opener, args.base, raw_bytes, sid, args.wrist)

    if 'raw' in targets:
        head('3. 原始层 → /api/raw/sessions（仅封存 L0）')
        results['raw'] = to_raw(opener, args.base, raw_bytes, sid, args.ingest_key)

    if 'prod' in targets:
        head('4. 终端通道 → /api/prod/sessions（L2 + App 可见）')
        results['prod'] = to_prod(opener, args.base, session, args.ingest_key,
                                  args.openid, args.student,
                                  include_strokes=not args.no_strokes)

    head('结论：去哪个页面核对')
    base = args.base.rstrip('/')
    if results.get('annotation'):
        print('  · 逐拍全量   %s/strokes?session=%s' % (base, sid))
    if results.get('prod'):
        print('  · L2 逐拍    %s/api/prod/sessions/%s   （需 Bearer token）' % (base, sid))
    if results.get('raw'):
        print('  · L0 版本    %s/api/raw/sessions/%s   （需 X-Ingest-Key / 登录态）' % (base, sid))
    print()
    print('  ⚠️ 管理后台 /strokes 只认「标注通道」的数据；终端通道传的场次在那里')
    print('     逐拍会是 0 条且**不报错** —— 详见 docs/WATCH_FIELD_TEST.md §1.4。')

    failed = [k for k, v in results.items() if not v]
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
