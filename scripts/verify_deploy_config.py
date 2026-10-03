# -*- coding: utf-8 -*-
"""部署配置对账 —— 不需要起服务、不需要 node，纯读文件。

  python scripts/verify_deploy_config.py

为什么值得单独一个门禁：这套东西**每一项错了都不报错**。

  · 环境变量名写错（`NETPULSE_INGEST_KEY` 写成 `NETPULSE_INGESTKEY`）
    → 应用只当成"没配"，静默走默认值。而默认值恰好是「写接口无需口令」，
      页面照常、采集照常，只有内网任何人都能往库里灌数据；
  · `watch` 被打开 → SQLite 写库触发无限重启，且会把 WAL 搅坏；
  · `exec_mode` 改成 cluster → `database is locked` 从必现变成偶现；
  · `deploy/acemate.env` 被从 .gitignore 里删掉 → 采集口令跟着 commit 进 git；
  · `app.css` 漏进仓库/漏编译 → 页面完全没有样式（不报错、不塌格）。

这五条都属于「页面看着正常、数字是错的」那一类，靠肉眼评审抓不住。
本文件把 `server/datasources.py` 那条教训（**人工维护的文档必须与代码对账**）
从字段登记表推广到部署配置。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

fails = []
checks = []


def check(name, got, want=True):
    ok = (got == want)
    checks.append((name, ok, got, want))
    if not ok:
        fails.append(name)
    return ok


def read(rel):
    p = ROOT / rel
    return p.read_text(encoding='utf-8') if p.exists() else ''


# --------------------------------------------------------------------------- #
# 1. 代码真正读取的环境变量全集（唯一真源：server/**.py + run.py）
# --------------------------------------------------------------------------- #
CODE_ENV = set()
for p in list((ROOT / 'server').rglob('*.py')) + [ROOT / 'run.py']:
    text = p.read_text(encoding='utf-8')
    for m in re.finditer(
            r"""(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault|os\.environ)\s*[(\[]\s*['"]([A-Z][A-Z0-9_]*)['"]""",
            text):
        CODE_ENV.add(m.group(1))

# 这些是**部署**要用的、代码不读的（时区由解释器与 libc 读；绑定地址由
# ecosystem.config.js 读成 `--host/--port`，server/**.py 只认命令行参数）
DEPLOY_ONLY_ENV = {'TZ', 'PYTHONUNBUFFERED', 'ACEMATE_BIND_HOST', 'ACEMATE_BIND_PORT'}

ENV_EXAMPLE = 'deploy/acemate.env.example'
example_text = read(ENV_EXAMPLE)
example_lines = [l.strip() for l in example_text.splitlines()]
# 生效行（未注释且形如 KEY=...）
active_names = {
    m.group(1) for m in
    (re.match(r'^([A-Z][A-Z0-9_]*)\s*=', l) for l in example_lines) if m
}
# 被注释掉的也算"提到过"（模板里刻意注释说明的那几个）
mentioned_names = set(re.findall(r'^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=', example_text, re.M))


def section(title):
    print()
    print('-' * 74)
    print(title)
    print('-' * 74)


# --------------------------------------------------------------------------- #
section('1. 环境变量：模板 vs 代码')
# --------------------------------------------------------------------------- #
unknown = sorted(active_names - CODE_ENV - DEPLOY_ONLY_ENV)
check('生效的环境变量名必须都是代码真的会读的（防拼错）', unknown, [])
if unknown:
    print('    ⚠️ 模板里生效但代码从不读取的名字：', ', '.join(unknown))
    print('       ⇒ 应用会把它当"没配"、静默走默认值，是本次部署最危险的一类错误。')

missing = sorted(CODE_ENV - mentioned_names)
check('代码读取的每个变量都必须在模板里出现过（防漏文档）', missing, [])
if missing:
    print('    ⚠️ 代码会读、但模板里一个字没提：', ', '.join(missing))

# 生产必须显式配置的两个：口令与数据路径
check('模板生效行包含 NETPULSE_INGEST_KEY（采集口口令）',
      'NETPULSE_INGEST_KEY' in active_names)
check('模板提到 TZ（时区，影响"近 7 天"的边界）',
      'TZ' in active_names or 'TZ' in mentioned_names)
print('    代码读取的环境变量全集（%d 个）：%s' % (len(CODE_ENV), ', '.join(sorted(CODE_ENV))))
print('    模板里生效的（%d 个）：%s' % (len(active_names), ', '.join(sorted(active_names)) or '(无)'))

# --------------------------------------------------------------------------- #
section('2. ecosystem.config.js：这些值错了都不会报错')
# --------------------------------------------------------------------------- #
ECO = 'ecosystem.config.js'
eco = read(ECO)
check('%s 存在' % ECO, bool(eco))
if eco:
    # 归一掉空白，避免格式化差异造成假失败
    flat = re.sub(r'\s+', ' ', eco)
    check('单实例（SQLite 不能多进程写）', bool(re.search(r"instances:\s*1\b", flat)))
    check("fork 模式（不是 cluster）", bool(re.search(r"exec_mode:\s*'fork'", flat)))
    # ⚠️ 不要写成「文件里不出现 cluster 这个词」—— 上面的注释里就写着
    # 「不能改成 cluster」，那样会永远红。要查的是**真的被配成了 cluster**：
    # exec_mode: 'cluster'，或 instances: 0（0 在 PM2 里 = 用满所有核心）。
    check('没有真的配成 cluster 模式',
          not re.search(r"exec_mode:\s*['\"]cluster['\"]", flat)
          and not re.search(r'instances:\s*0\b', flat))
    check('watch 关闭（开了会被 SQLite 写库触发无限重启）',
          bool(re.search(r"watch:\s*false", flat)))
    check('kill_timeout 已设（uvicorn 优雅关闭，默认 1600ms 太短）',
          bool(re.search(r'kill_timeout:\s*\d+', flat)))
    check('内存上限已设', bool(re.search(r'max_memory_restart:', flat)))
    check('PYTHONUNBUFFERED 已设（否则 print 出来的初始口令会卡在缓冲区里）',
          'PYTHONUNBUFFERED' in eco)
    check('走 run.py（它会在启动前检查依赖与 CSS 产物）',
          bool(re.search(r"script:\s*'run\.py'", flat)))
    check('interpreter 指向项目内 .venv（不是 PATH 里的 python）',
          "'.venv'" in eco or '".venv"' in eco or "'.venv', 'bin'" in eco or '.venv/bin' in eco)
    check('环境文件缺失时 fail-fast，不兜底',
          bool(re.search(r'缺少环境文件|ENV_FILE\)\s*\{|fs\.existsSync', eco)))
    check('口令为空 + 对外绑定 → 拒绝启动',
          'NETPULSE_INGEST_KEY' in eco and 'EXPOSED' in eco)
    # 绑定地址的真源必须是**环境文件**，不是 PM2 进程自己的 process.env：
    # 后者是"启动那一刻的 shell 环境"，而 run.py / server 只认 `--host`，
    # 于是往 acemate.env 里写 ACEMATE_BIND_HOST 会**静默无效**，
    # 而 bootstrap-server.sh 又 grep 这个文件做 fail-fast —— 两个真源互相矛盾。
    check('绑定地址以 deploy/acemate.env 为真源（不是 PM2 的 process.env）',
          'appEnv.ACEMATE_BIND_HOST' in eco)

# --------------------------------------------------------------------------- #
section('3. 机密不进 git')
# --------------------------------------------------------------------------- #
gi = read('.gitignore')
check('deploy/acemate.env 被 gitignore', 'deploy/acemate.env' in gi)
# 用 git 自己判断，而不是靠字符串
try:
    r = subprocess.run(['git', 'check-ignore', '-q', 'deploy/acemate.env'],
                       cwd=str(ROOT), capture_output=True)
    check('git 确认它真的被忽略（不是只写了规则）', r.returncode == 0)
    r2 = subprocess.run(['git', 'check-ignore', '-q', 'deploy/acemate.env.example'],
                        cwd=str(ROOT), capture_output=True)
    check('模板本身**不**被忽略（要进仓库）', r2.returncode != 0)
except FileNotFoundError:
    print('    (跳过：没有 git)')

# --------------------------------------------------------------------------- #
section('4. 脚本可执行且语法正确')
# --------------------------------------------------------------------------- #
for rel in ('deploy/push-from-mac.sh', 'deploy/bootstrap-server.sh'):
    p = ROOT / rel
    check('%s 存在' % rel, p.exists())
    if p.exists():
        check('%s 有执行位' % rel, os.access(p, os.X_OK))
        r = subprocess.run(['bash', '-n', str(p)], capture_output=True, text=True)
        check('%s bash 语法通过' % rel, r.returncode == 0)
        if r.returncode != 0:
            print('   ', r.stderr.strip()[:300])

# ⚠️ 数据同步**不能**带 --delete：服务器上可能有本机没有的新采集数据
push = read('deploy/push-from-mac.sh')
check('搬数据那一份 rsync 没有 --delete（否则会删掉服务器上的新数据）',
      '--exclude \'log/\' ./var/' in push and './var/' in push)

# --------------------------------------------------------------------------- #
section('4b. PM2 必须跑在应用属主名下（以 root 跑这一条不报错但运维会失联）')
# --------------------------------------------------------------------------- #
# 现象全是安静的：普通用户 `pm2 list` 看不到应用、应用写出的 SQLite/日志归 root、
# 若该用户本来就有 PM2 守护进程则变成两个守护进程抢同一端口与同一批库。
bs = read('deploy/bootstrap-server.sh')
check('bootstrap 引入了应用属主概念（APP_USER / as_app）',
      'APP_USER=' in bs and 'as_app()' in bs)
# 裸 pm2 命令 = 以当前(可能是 root)身份跑。
# 只看**代码段**：末尾那段 `cat <<EOF … EOF` 的帮助文本里就写着
# `pm2 reload $APP_NAME` 之类的示例命令，扫进去会永远是假阳性。
bs_code = bs.split('cat <<EOF')[0]
_bare = re.findall(r'^[ \t]*(pm2 (?:start|reload|restart|save|status|delete|kill))\b',
                   bs_code, re.M)
check('bootstrap 代码段里没有裸 pm2 启动/重载命令（都要经 as_app）', _bare, [])
if _bare:
    print('    ⚠️ 以 root 身份执行的 pm2 命令：', ', '.join(_bare))
# .venv / node_modules 的写入者也必须是应用属主，否则属主是 root
check('bootstrap 以应用属主建 venv / 装 pip 依赖',
      "as_app \"cd '$APP_DIR' && .venv/bin/python3 -m pip install" in bs)
check('bootstrap 以应用属主跑 npm（ci 会先删 node_modules 再重装）',
      "as_app \"cd '$APP_DIR' && npm ci" in bs or "as_app \"cd '$APP_DIR' && npm install" in bs)

# 环境文件是 chmod 600 的：属主一旦是 root，PM2（以 APP_USER 跑）读它就是 EACCES，
# 应用起不来，日志里只有一行读文件失败 —— 与"文件缺失"表现不同，很难往回查。
_e = 'chown "$APP_USER:$APP_GROUP" deploy/acemate.env'
_c = 'chmod 600 deploy/acemate.env'
check('生成 acemate.env 时先 chown 再 chmod 600（顺序不能反）',
      _e in bs and _c in bs and bs.index(_e) < bs.index(_c))

# 改端口之后健康检查必须跟着改，否则"改端口 → 永远检查失败"，会把人带进沟里
check('bootstrap 的健康检查端口跟随环境文件的 ACEMATE_BIND_PORT',
      'ACEMATE_BIND_PORT' in bs)

# --------------------------------------------------------------------------- #
section('5. 文档与实际文件一致')
# --------------------------------------------------------------------------- #
dep = read('docs/DEPLOY.md')
check('DEPLOY.md 或 README 提到 PM2 部署',
      'PM2' in dep or 'PM2' in read('README.md'))
for rel in ('ecosystem.config.js', 'deploy/push-from-mac.sh',
            'deploy/bootstrap-server.sh', 'deploy/acemate.env.example',
            'deploy/nginx-acemate.conf'):
    check('DEPLOY.md 引用了 %s' % rel, rel in dep)

# --------------------------------------------------------------------------- #
print()
print('=' * 74)
n_ok = sum(1 for _, ok, _, _ in checks if ok)
for name, ok, got, want in checks:
    if not ok:
        print('❌ %s' % name)
        print('     实际 = %r' % (got,))
        print('     期望 = %r' % (want,))
print('=' * 74)
if fails:
    print('部署配置对账：%d / %d 通过，%d 项失败' % (n_ok, len(checks), len(fails)))
    sys.exit(1)
print('部署配置对账：%d / %d 全部通过 ✅' % (n_ok, len(checks)))
