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
#   APP_USER=<应用属主>   —— 默认取 SUDO_USER（即发起部署的人），不是 root。
#                           PM2 与 .venv 都归它；以 root 跑会是运维地雷，见文内说明。
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
# 应用属主 —— **PM2 必须以这个用户的身份跑，不是 root**
# ---------------------------------------------------------------------------
# 用 `sudo bash bootstrap-server.sh` 时，SUDO_USER 才是发起部署的人，
# 那才是应用该有的属主。以 root 跑 PM2 会同时踩三个坑，而且**一个都不报错**：
#
#   ① `pm2 list`（普通用户）看不到这个应用 —— 日常运维直接失联，
#      还容易被当成"服务没起来"；
#   ② 应用进程以 root 身份运行，写出的 SQLite/WAL/日志全归 root；
#      下次改回普通用户启动就是 permission denied（现象是"数据没了"）；
#   ③ 若那台机器本来就有该用户的 PM2 守护进程，会变成**两个守护进程都认为
#      自己在管同一个应用**，抢同一个端口与同一批 SQLite 文件 —— 最难查的一种。
#
# 想固定成某个用户：APP_USER=snowo sudo bash deploy/bootstrap-server.sh
APP_USER="${APP_USER:-${SUDO_USER:-$(id -un)}}"
APP_GROUP="$(id -gn "$APP_USER" 2>/dev/null || echo "$APP_USER")"

# 以应用属主身份执行。注意要补 PATH：非交互 shell 不读 ~/.bashrc，
# 而 pm2 常装在 ~/.npm-global/bin（不在默认 PATH 里）。
as_app() {
  local _c="export PATH=\"\$HOME/.npm-global/bin:\$PATH\"; $1"
  if [ "$(id -un)" = "$APP_USER" ]; then bash -lc "$_c"; else sudo -u "$APP_USER" -H bash -lc "$_c"; fi
}

if [ "$(id -un)" = "$APP_USER" ]; then
  echo "本次以 $(id -un) 身份运行；应用属主 = $APP_USER"
else
  echo "本次以 $(id -un) 身份运行；应用属主 = $APP_USER（PM2 与 .venv 都归它）"
fi

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

# 早一步统一属主。上一轮若曾以 root 跑过本脚本，.venv / node_modules /
# var/log 会归 root —— 那样下面换 $APP_USER 身份写文件就 permission denied，
# 而报错长得像"依赖装不上"，会被查错方向。
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
  echo "已把 $APP_DIR 属主统一为 $APP_USER:$APP_GROUP"
else
  warn "非 root 运行，跳过统一属主；若 $APP_DIR 归别人所有，下面会失败"
fi

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
  # 全局装（落在 /usr/local/bin/pm2）。**必须全局**，不能只装到用户目录：
  # 非交互 ssh 不读 ~/.bashrc，而 push-from-mac.sh 里那句
  # `ssh host "pm2 reload acemate-backend"` 正是非交互的 ——
  # 只装在 ~/.npm-global/bin 的话它会报 "pm2: command not found"。
  $SUDO npm install -g pm2
fi
echo "PM2 CLI $(pm2 -v)"

# ⚠️ CLI 版本必须与**已经在跑的守护进程**版本一致，否则每条命令都会打印
#    「In-memory PM2 is out-of-date, do: pm2 update」—— 而照着做会重启该守护
#    进程名下的**全部**应用（这台机器上很可能还跑着别人的生产服务）。
#    这里只警告，**绝不自动 update**。
daemon_v="$(as_app "pm2 -v" 2>/dev/null | tail -1 | tr -d '\r' || true)"
cli_v="$(pm2 -v 2>/dev/null | tail -1 | tr -d '\r' || true)"
if [ -n "$daemon_v" ] && [ -n "$cli_v" ] && [ "$daemon_v" != "$cli_v" ]; then
  warn "PM2 CLI($cli_v) 与 $APP_USER 的守护进程($daemon_v) 版本不一致。"
  echo "     每条 pm2 命令都会提示 out-of-date。**不要跑 pm2 update**（会重启该"
  echo "     守护进程下所有应用）；把 CLI 对齐即可："
  echo "       sudo npm install -g pm2@$daemon_v"
fi

# ---------------------------------------------------------------------------
say "3/6 建虚拟环境并安装 Python 依赖"
# ---------------------------------------------------------------------------
if [ ! -x .venv/bin/python3 ]; then
  as_app "cd '$APP_DIR' && python3 -m venv .venv"
  echo "已创建 .venv（属主 $APP_USER）"
else
  echo ".venv 已存在，复用"
fi
# pip 会往 .venv 里写文件，必须以属主身份跑：root 装进去的包归 root 所有，
# $APP_USER 之后跑 pm2 restart 时可能连 site-packages 都读不了。
as_app "cd '$APP_DIR' && .venv/bin/python3 -m pip install -q --upgrade pip"
as_app "cd '$APP_DIR' && .venv/bin/python3 -m pip install -q -r requirements.txt"
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
# npm ci 会**先删掉 node_modules 再重装** —— 以 root 跑的话重装出来的依赖全归
# root，$APP_USER 下次想 build:css 就 permission denied。
if [ -f package-lock.json ]; then
  as_app "cd '$APP_DIR' && npm ci --no-audit --no-fund"
else
  as_app "cd '$APP_DIR' && npm install --no-audit --no-fund"
fi
as_app "cd '$APP_DIR' && npm run build:css"
[ -s web/assets/css/app.css ] || die "web/assets/css/app.css 没生成，页面会没有样式。"
echo "app.css $(wc -c < web/assets/css/app.css) 字节"

# ---------------------------------------------------------------------------
say "5/6 准备环境变量"
# ---------------------------------------------------------------------------
as_app "mkdir -p '$APP_DIR/var/log'"
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
  # ⚠️ 顺序要紧：**先 chown 再 chmod**。若文件归 root 而权限是 600，
  #    PM2（以 $APP_USER 跑）读它时直接 EACCES，应用起不来，
  #    日志里只有一行读文件失败 —— 与"环境文件缺失"的表现完全不同，很难往这查。
  chown "$APP_USER:$APP_GROUP" deploy/acemate.env
  chmod 600 deploy/acemate.env
  warn "已生成 deploy/acemate.env，并随机填了采集口令："
  printf '\n    NETPULSE_INGEST_KEY=%s\n\n' "$KEY"
  echo "这个值要和采集端（手表 / 手机 App）配置的一致，否则采集上传会 401。"
  echo "另外请检查里面的 TZ=Asia/Shanghai 是否适合你（详见文件内注释）。"
else
  echo "deploy/acemate.env 已存在，未改动"
  # 早先以 root 跑过的话，它是 root 所有的 600 文件 → $APP_USER 读不到。
  if [ "$(id -u)" -eq 0 ] && [ "$(stat -c %U deploy/acemate.env)" != "$APP_USER" ]; then
    chown "$APP_USER:$APP_GROUP" deploy/acemate.env
    echo "（已修正 deploy/acemate.env 的属主为 $APP_USER）"
  fi
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
#   ⚠️ 必须以 $APP_USER 跑。以 root 跑会另起一个 root 的 PM2 守护进程，
#      于是"两个守护进程都认为自己在管 acemate-backend"，抢 8787 端口与 var/ 下的
#      SQLite，而且你日常 `pm2 list` 还看不到它。
if as_app "pm2 describe $APP_NAME >/dev/null 2>&1"; then
  as_app "pm2 reload $APP_NAME --update-env"
else
  as_app "cd '$APP_DIR' && pm2 start ecosystem.config.js"
fi
as_app "pm2 save"

# 等到真的能应答（uvicorn 起来 + 建库需要一两秒）
ok=0
for i in $(seq 1 20); do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/login"; then ok=1; break; fi
  sleep 1
done

printf '\n'
# 注意这里是 $APP_USER 的进程表，不是 root 的 ——
# 直接 `pm2 status`（root）看到的是**空表**，很容易被误判成"没起来"。
as_app "pm2 status"
if [ "$ok" -eq 1 ]; then
  printf '\033[1;32m健康检查通过： http://127.0.0.1:%s/login\033[0m\n' "$PORT"
else
  printf '\033[1;31m健康检查失败 —— 看日志： sudo -u %s bash -lc "pm2 logs %s --lines 50"\033[0m\n' "$APP_USER" "$APP_NAME"
  exit 1
fi

# ---------------------------------------------------------------------------
cat <<EOF

------------------------------------------------------------------
 后续两件事
------------------------------------------------------------------

1) 开机自启（只做一次）

     sudo -u $APP_USER -H bash -lc 'pm2 startup'
     # 它会打印形如：sudo env PATH=... pm2 startup systemd -u <user> --hp <home>
     # 复制那一条原样执行，然后：
     sudo -u $APP_USER -H bash -lc 'pm2 save'

   ⚠️ 必须用 $APP_USER 跑。给 root 装自启没用 —— 应用不在 root 的守护进程里，
      开机后 `pm2 list` 依旧是空的。
   ⚠️ pm2 startup 是**按守护进程**生效的：$APP_USER 名下的其它应用会一并获得
      开机自启（如果这不合你的意，就先看 `pm2 list` 确认里面都有谁）。

2) 管理员口令

   初始口令在 var/admin_initial_password.txt（首次启动生成），
   也可以用 pm2 logs $APP_NAME 看启动日志里那行。

   改口令（唯一正确入口，别手搓哈希）：

     sudo -u $APP_USER -H bash -lc 'cd $APP_DIR && .venv/bin/python3 scripts/reset_admin_password.py --password "<新口令≥8位>"'

 常用命令（**以 $APP_USER 登录后**执行，不要加 sudo）

     pm2 logs $APP_NAME          # 看日志（Ctrl+C 退出，不影响服务）
     pm2 reload $APP_NAME        # 改完 Python 代码后重载（模板改动不用重启）
     pm2 restart $APP_NAME       # 重新起进程
     pm2 monit                   # CPU / 内存 / 日志面板
     pm2 describe $APP_NAME      # 完整运行信息

 注意：PM2 不会因为改了代码就自动重载，改了 Python 必须手动 pm2 reload。
       （配置里 watch 是 false —— 打开它会让 SQLite 写库触发无限重启，别开。）
       `sudo pm2 list` 看到的是 root 的空表，不是这个应用 —— 别被它骗了。

EOF
