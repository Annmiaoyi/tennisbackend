"""
prod_api.py — 微信登录与令牌（**身份层**）。

    POST /api/wechat/login   { code } → { openid, token, expires_at }
    POST /api/wechat/logout  Bearer  → { ok: true }

============================================================================
 2026-09-29 职责收窄
============================================================================
原先这个模块还负责 `/api/prod/*` 的会话同步，直接读写标注库的
`prod_sessions` 表（整包 JSON 字符串）。那条路径有两个问题：

  1. 终端用户读到的只是**整包原文**，一个分析指标都没有；
  2. 它与管理后台分析的 `var/acemate.db` 是**两个库**，同一场训练存两份，
     终端用户那侧天生拿不到分析结果。

现在会话读写的职责搬到 `server/routers/user_api.py`，统一走 L2 分析层。
本模块只保留**身份**相关的部分：登录换 openid、签发/吊销令牌、
以及「由令牌反查 openid」这一个权威函数供其它模块复用。

`prod_sessions` 表仍会建（避免老库缺表），但**已不再是任何接口的真源**。
============================================================================
 鉴权模型
============================================================================
登录时把 token **落库**（`auth_tokens` 表），带 30 天有效期；读接口一律
由 token 反查 openid —— **openid 是校验结果，不是入参**。老客户端若继续
传 `?openid=`，必须与 token 所属 openid 一致，否则 403。
"""
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .db import get_conn

router = APIRouter()

WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")

TOKEN_TTL_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_prod_db(conn):
    # 登录令牌表。token 是随机会话凭据，与 openid 多对一（一个用户可多端登录）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_tokens(
               token      TEXT PRIMARY KEY,
               openid     TEXT NOT NULL,
               created_at TEXT,
               expires_at TEXT
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_openid ON auth_tokens(openid)")
    # ⚠️ 已废弃（deprecated）：仅保留建表语句，避免老库缺表导致迁移脚本报错。
    #    会话数据现在落在 L2 分析库（training_sessions + stroke_records），
    #    本表不再被任何读写路径使用。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS prod_sessions(
               id TEXT PRIMARY KEY,
               openid TEXT,
               payload TEXT,
               created_at TEXT
           )"""
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def openid_of_token(authorization: Optional[str]) -> str:
    """校验 Bearer token 并返回其所属 openid。失败一律 401。"""
    if not authorization or not authorization.strip():
        raise HTTPException(401, "缺少 Authorization 头：请先调用 /api/wechat/login 取 token")
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Authorization 格式应为 `Bearer <token>`")
    token = parts[1].strip()
    if not token:
        raise HTTPException(401, "token 为空")

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT openid, expires_at FROM auth_tokens WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "token 无效（未登录、已登出或服务端已重建令牌表）")
    expires = row["expires_at"]
    if expires and expires < _now():
        raise HTTPException(401, "token 已过期，请重新登录")
    return row["openid"]


# 兼容旧名（同模块内外的历史调用点）
_openid_from_token = openid_of_token


def authorize(authorization: Optional[str], openid: Optional[str] = None) -> str:
    """取得本次请求的合法 openid。

    token 是唯一身份来源；`openid` 查询参数仅用于**一致性自检** ——
    客户端若还带着它，必须和 token 对上，对不上说明在冒充他人。
    """
    owner = openid_of_token(authorization)
    if openid and openid != owner:
        raise HTTPException(403, "openid 与登录身份不符（禁止查询他人的训练数据）")
    return owner


_authorize = authorize


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
@router.post("/api/wechat/login")
def wechat_login(body: dict):
    code = (body or {}).get("code")
    if not code:
        raise HTTPException(400, "code required")

    openid = None
    if WECHAT_APPID and WECHAT_SECRET:
        url = (
            "https://api.weixin.qq.com/sns/jscode2session?"
            "appid=%s&secret=%s&js_code=%s&grant_type=authorization_code"
        ) % (WECHAT_APPID, WECHAT_SECRET, urllib.parse.quote(code))
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            openid = data.get("openid")
        except Exception:
            openid = None

    if not openid:
        # 开发期 mock：未配置 AppSecret 时由 code 派生稳定 openid。
        #
        # ⚠️ 这里**必须**用 hashlib，绝不能用内置 hash()：
        #    Python 的 str hash 默认带随机盐（PYTHONHASHSEED 随机），跨进程不稳定。
        #    实测同一个 code 在三次独立启动中分别得到 dev_2120932761 /
        #    dev_3474682280 / dev_7637982259 —— 服务一重启，用户身份就变，
        #    之前上传的训练数据全部「查不到」，看起来像同步坏了，实则是身份漂移。
        #    用 SHA-1 截断后得到与进程无关的固定值。
        openid = "dev_" + hashlib.sha1(code.encode("utf-8")).hexdigest()[:16]

    # 开发期身份对齐：iOS 端不掌握小程序 wx.login 的 code，无法自行推导出同一个 openid。
    # 配置 NETPULSE_DEV_OPENID 后，mock 模式统一返回该值 —— 两端约定同一个身份即可打通
    # 「iOS 上传 → 小程序查看」。生产环境配了真实 AppSecret 时本分支不生效。
    dev_openid = os.environ.get("NETPULSE_DEV_OPENID", "").strip()
    if dev_openid and not (WECHAT_APPID and WECHAT_SECRET):
        openid = dev_openid

    now = datetime.now(timezone.utc)
    token = secrets.token_hex(24)
    expires_at = (now + timedelta(days=TOKEN_TTL_DAYS)).isoformat()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO auth_tokens(token, openid, created_at, expires_at) VALUES(?,?,?,?)",
            (token, openid, now.isoformat(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {"openid": openid, "token": token, "expires_at": expires_at}


@router.post("/api/wechat/logout")
def wechat_logout(authorization: Optional[str] = Header(None)):
    """主动失效当前 token（换设备 / 怀疑泄露时用）。"""
    openid_of_token(authorization)             # 顺带校验一次
    token = authorization.strip().split(None, 1)[1].strip()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM auth_tokens WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
