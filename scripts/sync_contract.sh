#!/usr/bin/env bash
#
# sync_contract.sh — 把契约真源同步到两个 App 工程。
#
#   真源：  contract/TennisContract.swift
#   副本：  ../ATennis/ATennis/Shared/TennisContract.swift
#           ../WatchTennis/WatchTennis/Shared/TennisContract.swift
#
# 用法：
#   bash scripts/sync_contract.sh           同步（覆盖副本）
#   bash scripts/sync_contract.sh --check   只校验副本是否与真源一致（CI / 提交前）
#
# 退出码：0 = 一致 / 同步成功；1 = 存在漂移或出错。
#
# 为什么是"拷贝"而不是"跨仓引用"：
#   三端是三个独立 git 仓库。跨仓库引用文件会让"单独 clone 一个 App 仓库"
#   无法编译。所以副本必须随各自仓库一起提交，靠本脚本的 sha256 校验防漂移。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$ROOT/contract/TennisContract.swift"

ATENNIS_DIR="${TENNIS_ATENNIS_DIR:-$ROOT/../ATennis}"
WATCH_DIR="${TENNIS_WATCHTENNIS_DIR:-$ROOT/../WatchTennis}"

DEST_ATENNIS="$ATENNIS_DIR/ATennis/Shared/TennisContract.swift"
DEST_WATCH="$WATCH_DIR/WatchTennis/Shared/TennisContract.swift"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

if [[ ! -f "$SRC" ]]; then
  echo "错误：找不到契约真源 $SRC" >&2
  exit 1
fi

hash_of() {
  # macOS 用 shasum，Linux 用 sha256sum
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

SRC_HASH="$(hash_of "$SRC")"
VERSION="$(grep -m1 'public static let version' "$SRC" | sed -E 's/.*"([^"]+)".*/\1/')"

echo "契约真源 : $SRC"
echo "契约版本 : v${VERSION}"
echo "sha256   : ${SRC_HASH:0:16}…"
echo ""

DRIFT=0
FAILED=0

for dest in "$DEST_ATENNIS" "$DEST_WATCH"; do
  label="$(basename "$(dirname "$(dirname "$dest")")")"

  if [[ ! -d "$(dirname "$dest")" ]]; then
    if [[ $CHECK -eq 1 ]]; then
      echo "✗ $label : 副本目录不存在 → $(dirname "$dest")"
      FAILED=1
      continue
    fi
    mkdir -p "$(dirname "$dest")"
  fi

  if [[ -f "$dest" ]]; then
    DEST_HASH="$(hash_of "$dest")"
  else
    DEST_HASH=""
  fi

  if [[ "$DEST_HASH" == "$SRC_HASH" ]]; then
    echo "✓ $label : 与真源一致"
    continue
  fi

  DRIFT=1
  if [[ $CHECK -eq 1 ]]; then
    if [[ -z "$DEST_HASH" ]]; then
      echo "✗ $label : 副本缺失 → $dest"
    else
      echo "✗ $label : 与真源**不一致**（副本 ${DEST_HASH:0:16}…）"
    fi
    FAILED=1
  else
    cp "$SRC" "$dest"
    NEW_HASH="$(hash_of "$dest")"
    if [[ "$NEW_HASH" == "$SRC_HASH" ]]; then
      echo "→ $label : 已同步 → $dest"
    else
      echo "✗ $label : 同步后校验失败" >&2
      FAILED=1
    fi
  fi
done

echo ""
if [[ $CHECK -eq 1 ]]; then
  if [[ $FAILED -eq 0 ]]; then
    echo "契约一致（版本 v${VERSION}）"
    exit 0
  fi
  echo "契约存在漂移。请执行：bash scripts/sync_contract.sh" >&2
  echo "（注意：两个 App 仓库需要各自提交更新后的副本。）" >&2
  exit 1
fi

if [[ $FAILED -ne 0 ]]; then
  echo "同步过程出错" >&2
  exit 1
fi

if [[ $DRIFT -eq 0 ]]; then
  echo "副本本就与真源一致，无需改动。"
else
  echo "同步完成。两个 App 仓库需要各自提交更新后的副本。"
fi
