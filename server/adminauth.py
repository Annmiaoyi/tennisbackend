# -*- coding: utf-8 -*-
"""adminauth.py —— 管理后台登录态（账号 + 服务端会话）。

============================================================================
 为什么必须加
============================================================================
在此之前，管理后台的所有接口（`/api/students/*`、`/api/training/*`、
`/api/sync/*`、`/api/ingest/*`、`/api/sessions*` …）**完全没有鉴权**，
`X-User-Id` 只是「自报身份」用于防误写，不是安全边界 —— 任何人打开
浏览器就能读到全部学员的训练数据、导出数据集、甚至改写同步数据。

============================================================================
 设计选择
============================================================================
· **服务端会话**（`admin_sessions` 表）而不是签名 Cookie：令牌随时可吊销，
  吊销是立刻生效的（签名 Cookie 做不到），并且能看到「谁在什么时候从哪登录」。
· **PBKDF2-HMAC-SHA256**（20 万轮 + 每用户随机盐）而不是裸 sha256：
  口令库被盗时裸哈希可被彩虹表秒破。
· **恒定时间比较**（`hmac.compare_digest`）：避免按字节比较泄露前缀信息。
· 账号表建在**分析库**里（`var/acemate.db`），但**不进 `schema.sql`** ——
  它不属于同步协议管辖的业务实体，混进去会让 changelog 水位语义变浑。

============================================================================
 放行规则（见 app.py 的中间件）
============================================================================
  公开：`/login`、`/logout`、`/assets/*`、`/api/prod/*`、`/api/wechat/*`、
        `/docs`、`/openapi.json`
  放行：带合法 `X-Ingest-Key` 的采集端请求（设备没有浏览器登录态）
  其余：必须已登录，否则页面 302 跳 `/login`、接口 401
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = 'acemate_admin'
SESSION_TTL_HOURS = 12
PBKDF2_ROUNDS = 200_000

DEFAULT_USERNAME = 'admin'


def password_file():
    """初始口令文件的落点 —— **跟随数据目录**，而不是写死在仓库里。

    ⚠️ 为什么必须跟随（2026-09-29 实测踩过，代价是主库口令永久丢失）：
    `scripts/verify_layers.py` 这类**隔离实例**会通过 `ACEMATE_DB` 把分析库指向
    临时目录，但如果口令文件仍固定在本仓库 `var/` 下，就会出现这样一条静默路径 ——

        临时库还没有任何账号 → init() 走「创建默认账号」分支
        → 把**主库的真实口令文件覆盖掉**（写成隔离实例的测试口令）
        → 而主库 `admin_users` 里那条记录的哈希没变
        → 主人登录时「用户名或口令不正确」，且口令文件里的内容已经不是主库的

    路径跟随 `db.db_path()` 所在目录后，各库各写各的文件，天然不串。
    部署到容器/多实例时可用 `ACEMATE_ADMIN_PASSWORD_FILE` 显式覆盖。
    """
    override = os.environ.get('ACEMATE_ADMIN_PASSWORD_FILE', '').strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(db.db_path())),
                        'admin_initial_password.txt')

DDL = """
CREATE TABLE IF NOT EXISTS admin_users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token      TEXT PRIMARY KEY,
    admin_id   TEXT NOT NULL,
    username   TEXT NOT NULL,
    user_agent TEXT,
    ip         TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_exp ON admin_sessions(expires_at);
"""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + 'Z'


# --------------------------------------------------------------------------- #
# 口令
# --------------------------------------------------------------------------- #
def hash_password(password, *, rounds=PBKDF2_ROUNDS, salt=None):
    """产出 `pbkdf2_sha256$<rounds>$<salt_hex>$<hash_hex>`。"""
    if not password:
        raise ValueError('口令不能为空')
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, rounds)
    return 'pbkdf2_sha256$%d$%s$%s' % (rounds, salt.hex(), h.hex())


def verify_password(password, stored):
    """恒定时间校验。格式不合法一律判失败（不抛异常，避免暴露内部结构）。"""
    if not password or not stored:
        return False
    try:
        algo, rounds, salt_hex, hash_hex = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        calc = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                   bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), hash_hex)


# --------------------------------------------------------------------------- #
# 建表 / 初始化
# --------------------------------------------------------------------------- #
def init(print_password=False):
    """建表；若一个账号都没有，创建默认管理员。返回 True 表示本次新建了账号。

    初始口令优先级：
      1. 环境变量 `ACEMATE_ADMIN_PASSWORD`（部署时显式指定）
      2. 随机生成（`token_urlsafe(12)` = 16 字符），写入 `password_file()` 并打印

    ⚠️ **只有「随机生成」那一支才写口令文件**：走环境变量的调用方（部署脚本、
      隔离测试实例）本来就知道口令，再写文件既多一个泄露面，又正是
      2026-09-29「隔离实例覆盖主库口令」那次事故的放大器。
    """
    conn = db.connect()
    conn.executescript(DDL)
    row = conn.execute('SELECT COUNT(*) AS n FROM admin_users').fetchone()
    if row and row['n']:
        return False

    password = os.environ.get('ACEMATE_ADMIN_PASSWORD', '').strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)

    conn.execute(
        'INSERT INTO admin_users(id, username, display_name, password_hash, role,'
        ' is_active, created_at) VALUES(?,?,?,?,?,?,?)',
        ('adm_' + secrets.token_hex(6), DEFAULT_USERNAME, '系统管理员',
         hash_password(password), 'admin', 1, _iso(_now())),
    )

    if generated:
        path = password_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('username: %s\npassword: %s\n' % (DEFAULT_USERNAME, password))

    if print_password:
        print('=' * 68)
        print(' 管理后台已创建默认账号（请立即登录并修改口令）')
        print('   用户名：%s' % DEFAULT_USERNAME)
        print('   口  令：%s' % password)
        if generated:
            print('   已写入：%s' % path)
        else:
            print('   （口令来自 ACEMATE_ADMIN_PASSWORD，未写入口令文件）')
        print('=' * 68)
    return True


# --------------------------------------------------------------------------- #
# 登录 / 会话
# --------------------------------------------------------------------------- #
def authenticate(username, password):
    """校验账号口令。成功返回账号行 dict，失败返回 None。"""
    conn = db.connect()
    row = conn.execute(
        'SELECT * FROM admin_users WHERE username=? AND is_active=1',
        ((username or '').strip(),)).fetchone()
    if not row:
        # 仍然跑一次哈希，抹平「用户不存在」与「口令错误」的响应时间差异
        verify_password(password or 'x', hash_password('dummy', rounds=1000))
        return None
    if not verify_password(password, row['password_hash']):
        return None
    return dict(row)


def create_session(admin, user_agent=None, ip=None):
    token = secrets.token_urlsafe(36)
    now = _now()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    conn = db.connect()
    conn.execute(
        'INSERT INTO admin_sessions(token, admin_id, username, user_agent, ip,'
        ' created_at, expires_at) VALUES(?,?,?,?,?,?,?)',
        (token, admin['id'], admin['username'], (user_agent or '')[:200],
         ip, _iso(now), _iso(expires)))
    conn.execute('UPDATE admin_users SET last_login_at=? WHERE id=?',
                 (_iso(now), admin['id']))
    return token, _iso(expires)


def session_of(token):
    """按令牌取会话（含过期判断）。无效返回 None。"""
    if not token:
        return None
    conn = db.connect()
    row = conn.execute('SELECT * FROM admin_sessions WHERE token=?', (token,)).fetchone()
    if not row:
        return None
    if row['expires_at'] <= _iso(_now()):
        conn.execute('DELETE FROM admin_sessions WHERE token=?', (token,))
        return None
    return dict(row)


def revoke(token):
    if not token:
        return 0
    conn = db.connect()
    cur = conn.execute('DELETE FROM admin_sessions WHERE token=?', (token,))
    return cur.rowcount


def purge_expired():
    conn = db.connect()
    return conn.execute('DELETE FROM admin_sessions WHERE expires_at <= ?',
                        (_iso(_now()),)).rowcount


def get_admin(admin_id):
    conn = db.connect()
    row = conn.execute('SELECT * FROM admin_users WHERE id=?', (admin_id,)).fetchone()
    return dict(row) if row else None


def change_password(admin_id, new_password):
    conn = db.connect()
    conn.execute('UPDATE admin_users SET password_hash=? WHERE id=?',
                 (hash_password(new_password), admin_id))
    # 改口令后，该账号的其它会话一律失效（防「改了密码但旧会话还活着」）
    conn.execute('DELETE FROM admin_sessions WHERE admin_id=?', (admin_id,))


def session_valid(request):
    """请求是否带有效管理后台登录态。给 security.py 用。"""
    try:
        token = request.cookies.get(SESSION_COOKIE)
    except AttributeError:                                 # pragma: no cover
        return False
    return session_of(token) is not None
