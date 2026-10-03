#!/usr/bin/env bash
# =============================================================================
# AceMate 后端 —— Ubuntu 服务器一次性初始化（在**服务器上**执行）
#
#   sudo bash deploy/bootstrap-server.sh
#
# 做五件事：
#   1. 装系统依赖（python3-venv / sqlite3 / rsync / curl）
#   2. 装 Node.js + PM2（PM2 自己是 Node 程序，跑 Python 应用也需要它）
#   3. 建 .venv 并装 Python 依赖
#   4. 编译 Tailwind 产物 web/assets/css/app.css（**漏了页面会完全没样式**）
#   5. 生成 deploy/acemate.env + pm2 start + pm2 save
#
# 前置条件：代码已经推到目标目录（在 Mac 上执行 deploy/push-from-mac.sh）。
# 幂等：可以重复跑，已完成的步骤会跳过。
#
# 可覆盖的变量：
#   APP_DIR=/srv/acemate/backend   PORT=8787   NODE_MAJOR=22
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/acemate/backend}"
PORT="${PORT:-8787}"
NODE_MAJOR="${NODE_MAJOR:-22}"
APP_NAME="acemate-backend"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[警告] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[失败] %s\033[0m\n' "$*" >&2; exit 1; }

[ -f /etc/os-release ] || die "看起来不是 Linux，本脚本只用于 Ubuntu/Debian。"
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

# ---------------------------------------------------------------------------
say "0/6 检查代码是否已就位"
# ---------------------------------------------------------------------------
[ -d "$APP_DIR" ] || die "目录不存在：$APP_DIR
先在本机（Mac）执行：  bash deploy/push-from-mac.sh <user>@172.20.49.43"
for f in run.py ecosystem.config.js requirements.txt server/app.py; do
  [ -e "$APP_DIR/$f" ] || die "$APP_DIR 里缺少 $f —— 代码没推全，或推错目录了。"
done
cd "$APP_DIR"
echo "项目目录：$APP_DIR"

# ---------------------------------------------------------------------------
say "1/6 安装系统依赖"
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
  python3 python3-venv python3-pip \
  sqlite3 rsync curl ca-certificates git
python3 -V

# ---------------------------------------------------------------------------
say "2/6 检查 Node.js 与 PM2"
# ---------------------------------------------------------------------------
need_node=1
if command -v node >/dev/null 2>&1; then
  major="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "$major" -ge 18 ]; then
    need_node=0
    echo "已有 Node $(node -v)，跳过安装"
  else
    warn "已有 Node $(node -v) 太旧（Tailwind 3.4 与 PM2 都要求 >= 18），将升级"
  fi
fi
if [ "$need_node" -eq 1 ]; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | $SUDO -E bash -
  $SUDO apt-get install -y -qq nodejs
  echo "已安装 Node $(node -v)"
fi

if ! command -v pm2 >/dev/null 2>&1; then
  # 用 npm 全局装；不用 sudo npm -g 是因为某些发行版会装到 /usr/local 但 PATH 里没有
  $SUDO npm install -g pm2
fi
echo "PM2 $(pm2 -v)"

# ---------------------------------------------------------------------------
say "3/6 建虚拟环境并安装 Python 依赖"
# ---------------------------------------------------------------------------
if [ ! -x .venv/bin/python3 ]; then
  python3 -m venv .venv
  echo "已创建 .venv"
else
  echo ".venv 已存在，复用"
fi
.venv/bin/python3 -m pip install -q --upgrade pip
.venv/bin/python3 -m pip install -q -r requirements.txt
if [ -f requirements-dev.txt ]; then
  echo "（requirements-dev.txt 是开发/验收脚本用的，生产不必装）"
fi
.venv/bin/python3 - <<'PY'
import fastapi, uvicorn, jinja2, multipart
print('依赖 OK: fastapi %s / uvicorn %s' % (fastapi.__version__, uvicorn.__version__))
try:
    import pypinyin
    print('pypinyin OK（学员搜索支持拼音检索）')
except ImportError:
    print('[警告] 缺 pypinyin —— 学员搜索会降级为只按原名匹配，不崩但体验不完整')
PY

# ---------------------------------------------------------------------------
say "4/6 编译前端样式"
# ---------------------------------------------------------------------------
# web/assets/css/app.css 是 Tailwind 的**编译产物**，且在 .gitignore 里 ——
# 全新 clone 出来的仓库没有它。少了它页面不报错、只是完全没有样式。
# 如果 push-from-mac.sh 已经把本机编译好的 app.css 带过来了，也仍然重编一次：
# 保证产物与服务器上的模板严格对应。
if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
npm run build:css
[ -s web/assets/css/app.css ] || die "web/assets/css/app.css 没生成，页面会没有样式。"
echo "app.css $(wc -c < web/assets/css/app.css) 字节"

# ---------------------------------------------------------------------------
say "5/6 准备环境变量"
# ---------------------------------------------------------------------------
mkdir -p var/log
if [ ! -f deploy/acemate.env ]; then
  cp deploy/acemate.env.example deploy/acemate.env
  KEY="$(openssl rand -hex 24)"
  # 按**行**精确替换，不用 str.replace(…,1)：
  # 后者命中的是"第一处出现"，将来若有人在上面补一行注释掉的同名变量，
  # 就会替换到注释上去 —— 生效行仍是空的，而且看起来"明明填了"。
  .venv/bin/python3 - "$KEY" <<'PY'
import sys, pathlib

key = sys.argv[1]
name = 'NETPULSE_INGEST_KEY='
p = pathlib.Path('deploy/acemate.env')
lines = p.read_text(encoding='utf-8').splitlines(keepends=True)

for i, line in enumerate(lines):
    # startswith ⇒ 未被注释；后半为空 ⇒ 是那个待填的空行
    if line.startswith(name) and not line[len(name):].strip():
        lines[i] = name + key + '\n'
        break
else:
    sys.exit('在 deploy/acemate.env 里找不到空的 "NETPULSE_INGEST_KEY=" 行，'
             '请手工填写该口令。')

p.write_text(''.join(lines), encoding='utf-8')
PY
  chmod 600 deploy/acemate.env
  warn "已生成 deploy/acemate.env，并随机填了采集口令："
  printf '\n    NETPULSE_INGEST_KEY=%s\n\n' "$KEY"
  echo "这个值要和采集端（手表 / 手机 App）配置的一致，否则采集上传会 401。"
  echo "另外请检查里面的 TZ=Asia/Shanghai 是否适合你（详见文件内注释）。"
else
  echo "deploy/acemate.env 已存在，未改动"
fi

# 只在需要时提示：口令为空 + 对外绑定 = 写接口敞开
BIND_HOST="$(grep -E '^ACEMATE_BIND_HOST=' deploy/acemate.env 2>/dev/null | cut -d= -f2 || true)"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
if [ "$BIND_HOST" != "127.0.0.1" ] && ! grep -qE '^NETPULSE_INGEST_KEY=.+' deploy/acemate.env; then
  die "NETPULSE_INGEST_KEY 为空，而绑定地址是 $BIND_HOST（对外暴露）。
留空时 /api/ingest、/api/raw 的写接口无需口令即可调用，任何人都能灌数据。
填写后重跑本脚本，或把 ACEMATE_BIND_HOST 改成 127.0.0.1 交给 Nginx 反代。"
fi

# ---------------------------------------------------------------------------
say "6/6 启动并做健康检查"
# ---------------------------------------------------------------------------
# 首次启动会建三个库、生成管理员初始口令（打印在日志里）
if pm2 describe "$APP_NAME" >/dev/null 2>&1; then
  pm2 reload "$APP_NAME" --update-env
else
  pm2 start ecosystem.config.js
fi
pm2 save

# 等到真的能应答（uvicorn 起来 + 建库需要一两秒）
ok=0
for i in $(seq 1 20); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/login"; then ok=1; break; fi
  sleep 1
done

printf '\n'
pm2 status
if [ "$ok" -eq 1 ]; then
  printf '\033[1;32m健康检查通过： http://127.0.0.1:%s/login\033[0m\n' "$PORT"
else
  printf '\033[1;31m健康检查失败 —— 看日志： pm2 logs %s --lines 50\033[0m\n' "$APP_NAME"
  exit 1
fi

# ---------------------------------------------------------------------------
cat <<EOF

------------------------------------------------------------------
 后续两件事
------------------------------------------------------------------

1) 开机自启（只做一次，会打印一条需要 sudo 的命令，照抄执行）

     pm2 startup
     # 它会打印形如：sudo env PATH=... pm2 startup systemd -u <user> --hp <home>
     # 复制那一条执行，然后 pm2 save

2) 管理员口令

   初始口令在 var/admin_initial_password.txt（首次启动生成），
   也可以用 pm2 logs $APP_NAME 看启动日志里那行。

   改口令（唯一正确入口，别手搓哈希）：

     .venv/bin/python3 scripts/reset_admin_password.py --password '<新口令≥8位>'

 常用命令

     pm2 logs $APP_NAME          # 看日志（Ctrl+C 退出，不影响服务）
     pm2 reload $APP_NAME        # 改完 Python 代码后重载（模板改动不用重启）
     pm2 restart $APP_NAME       # 重新起进程
     pm2 monit                   # CPU / 内存 / 日志面板
     pm2 describe $APP_NAME      # 完整运行信息

 注意：PM2 不会因为改了代码就自动重载，改了 Python 必须手动 pm2 reload。
       （配置里 watch 是 false —— 打开它会让 SQLite 写库触发无限重启，别开。）

EOF
