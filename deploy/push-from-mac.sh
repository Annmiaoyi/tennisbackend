#!/usr/bin/env bash
# =============================================================================
# AceMate 后端 —— 从 Mac 推送代码（和可选的数据）到 Ubuntu 服务器
#
#   在**本机 Mac** 上执行：
#
#     bash deploy/push-from-mac.sh <user>@172.20.49.43            # 只推代码
#     bash deploy/push-from-mac.sh <user>@172.20.49.43 --data     # 再搬数据（首次部署用）
#
#   DRY_RUN=1 bash deploy/push-from-mac.sh ...    # 只打印会做什么，不动任何文件
#
# 为什么用 rsync 而不是在服务器上 git clone：
#   本机当前到 GitHub 不通（HTTP2 framing 报错），本地还有一个提交没推上去；
#   走局域网直连最可靠，也避免了"服务器上那份代码是哪个版本"的歧义。
#   等网络恢复后，也完全可以改成服务器上 git pull，见 docs/DEPLOY.md。
# =============================================================================
set -euo pipefail

REMOTE="${1:-}"
MODE="${2:-}"
APP_DIR="${APP_DIR:-/srv/acemate/backend}"
DRY="${DRY_RUN:+--dry-run}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m[失败] %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$REMOTE" ] || die "用法： bash deploy/push-from-mac.sh <user>@172.20.49.43 [--data]"
[ -f ecosystem.config.js ] || die "请在项目根目录执行（当前：$(pwd)）"

# ---------------------------------------------------------------------------
say "0/4 检查连通性"
# ---------------------------------------------------------------------------
ssh -o ConnectTimeout=8 -o BatchMode=no "$REMOTE" 'echo "已连通： $(hostname) / $(lsb_release -ds 2>/dev/null || cat /etc/os-release | head -1)"' \
  || die "连不上 $REMOTE。确认地址、用户名，以及本机是否已把公钥放到服务器（ssh-copy-id）。"

if [ "$MODE" = "--data" ]; then
  # 迁移期间服务必须停：SQLite 在运行中被复制，拿到的可能是撕裂的快照。
  # 本机那一侧同理 —— 你已经在 Mac 上停掉了后端。
  say "1/4 停掉服务器上的服务（搬数据期间不能有写入）"
  ssh "$REMOTE" "pm2 stop acemate-backend 2>/dev/null || true"
  echo "已停（若此前就没在跑，忽略上面的提示）"

  # ---- 数据：只增不删 -------------------------------------------------------
  # ⚠️ 这一处**没有** --delete，是刻意的。
  #    代码那份带 --delete（要清掉被删掉的旧文件），但数据不能：
  #    服务器可能已经采集了 Mac 上没有的新数据，--delete 会把它们直接删掉。
  say "2/4 搬迁数据 var/（只增不删，忽略 var/log）"
  ssh "$REMOTE" "mkdir -p '$APP_DIR/var/log'"
  rsync -av $DRY --exclude 'log/' ./var/ "$REMOTE:$APP_DIR/var/"
  echo "已同步。本机的三个库（L2/L1/L0）与原始载荷都在这一份里。"
else
  say "1/4 跳过数据搬迁（要搬请加 --data）"
  say "2/4 跳过数据搬迁"
fi

# ---------------------------------------------------------------------------
say "3/4 同步代码"
# ---------------------------------------------------------------------------
# 排除项都是"服务器上自己生成/本机私有"的东西，覆盖过去只会帮倒忙：
#   .venv/ node_modules/  服务器上是 Linux 二进制，Mac 的是 macOS 的 —— 覆盖必炸
#   var/                  数据，单独搬（上面那段）
#   deploy/acemate.env    ⚠️ 里面有服务器自己的路径与口令，推过去会把它顶掉
#   .workbuddy/           本机智能体工作区
#   web/assets/css/app.css  **不排除** —— 带上本机编译好的产物，服务器即使没编译也有样式
rsync -av $DRY --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'node_modules/' \
  --exclude 'var/' \
  --exclude 'deploy/acemate.env' \
  --exclude '.workbuddy/' \
  --exclude '_shots/' \
  --exclude '_chrome/' \
  --exclude '_*.log' \
  --exclude '.DS_Store' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  ./ "$REMOTE:$APP_DIR/"

# ---------------------------------------------------------------------------
if [ "$MODE" = "--data" ]; then
  say "4/4 重启服务"
  ssh "$REMOTE" "cd '$APP_DIR' && pm2 start ecosystem.config.js 2>/dev/null || pm2 reload acemate-backend"
  sleep 3
  ssh "$REMOTE" "curl -sf -o /dev/null http://127.0.0.1:8787/login && echo '健康检查通过：/login 有响应' || (echo '健康检查失败，去看 pm2 logs acemate-backend'; exit 1)"
else
  say "4/4 代码已更新 —— 还没生效"
  cat <<EOF

  接下来在**服务器上**执行（改了 Python 必须重载；只改模板可跳过第一步）：

      cd $APP_DIR
      npm run build:css            # 如果这次改了模板/类名，必须重编，否则样式会缺
      pm2 reload acemate-backend   # 重载（零停机）

  如果这次带了新的 requirements.txt：

      .venv/bin/python3 -m pip install -r requirements.txt
      pm2 reload acemate-backend

EOF
fi
