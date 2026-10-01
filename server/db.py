# -*- coding: utf-8 -*-
"""SQLite 连接与基础工具。

为什么用 SQLite：后端管理站的读多写少、单机部署、零运维；同时 SQLite 的
`BEGIN IMMEDIATE` 能提供**写串行化**，这正是同步接口「幂等 + 不重复插入」
所需要的语义（比在应用层加锁更可靠，且跨进程有效）。
"""
import io
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA = os.path.join(HERE, 'schema.sql')
DEFAULT_DB = os.path.join(ROOT, 'var', 'acemate.db')

_local = threading.local()


def db_path():
    return os.environ.get('ACEMATE_DB', DEFAULT_DB)


def utcnow():
    """服务器权威时间，ISO8601（毫秒精度，UTC，带 Z 后缀）。

    全部时间统一用 UTC + 毫秒，字符串字典序即时间序 —— 这样 LWW 的
    `client_updated_at > server.updated_at` 可以直接用字符串比较，无需解析。
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def normalize_ts(value):
    """把客户端时间统一成可比较的 ISO8601 UTC 字符串。

    客户端可能发来 '2026-09-24T17:03:15Z' / '...T17:03:15.123+08:00' /
    '2026-09-24 17:03:15' 等格式。统一归一化后，LWW 的字符串比较才成立。
    无法解析则返回 None（调用方回退到服务器时间）。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(' ', 'T')
    try:
        if s.endswith('Z'):
            dt = datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%fZ') if '.' in s \
                else datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ')
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
    except ValueError:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def connect():
    """取当前线程的连接（SQLite 连接不可跨线程共享）。"""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        path = db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 30000')
        conn.execute('PRAGMA synchronous = NORMAL')
        _local.conn = conn
    return conn


@contextmanager
def tx(immediate=True):
    """事务上下文。

    immediate=True 时用 BEGIN IMMEDIATE：立刻取写锁，把并发写**串行化**。
    同步接口的「查重 → 写入」必须在同一把写锁内完成，否则两个并发的
    相同 operation_id 会同时通过查重阶段，造成重复插入。
    """
    conn = connect()
    conn.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
    try:
        yield conn
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise


def table_columns(table):
    return [r['name'] for r in query('PRAGMA table_info(%s)' % table)]


# ---------------------------------------------------------------------------
# 轻量迁移
# ---------------------------------------------------------------------------
# schema.sql 用的是 CREATE TABLE IF NOT EXISTS —— 对**已存在的旧表不会补列**。
# 于是这里维护一份「本版本新增列」清单，init_db 时按需 ALTER TABLE ADD COLUMN，
# 让旧库原地升级而不必删库重建（旧数据得以保留）。
#
# 只允许追加可空列：SQLite 的 ALTER TABLE ADD COLUMN 不支持「NOT NULL 且无默认值」。
# 删列 / 改类型请写显式迁移脚本，不要塞进这张表。
MIGRATIONS = {
    'training_sessions': [
        ('worn_wrist', 'TEXT'),          # left | right，决定正/反手符号
        ('source', 'TEXT'),              # acemate | netpulse_watch
        ('external_id', 'TEXT'),         # 采集端原始会话 ID
        ('forehand_count', 'INTEGER'),   # 以下 6 列为击球分项计数，
        ('backhand_count', 'INTEGER'),   # 供 G2 配平门禁校验：
        ('serve_count', 'INTEGER'),      # 六类之和 ≈ stroke_count
        ('slice_count', 'INTEGER'),
        ('volley_count', 'INTEGER'),
        ('smash_count', 'INTEGER'),
    ],
}


def ensure_columns(conn=None):
    """把 MIGRATIONS 里登记的列补到已存在的表上。返回实际新增的列名列表。"""
    conn = conn or connect()
    added = []
    for table, columns in MIGRATIONS.items():
        have = {r['name'] for r in conn.execute('PRAGMA table_info(%s)' % table)}
        if not have:
            continue                      # 表还不存在 —— 会由 schema.sql 直接建全
        for name, decl in columns:
            if name in have:
                continue
            conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, name, decl))
            added.append('%s.%s' % (table, name))
    return added


def init_db(force=False):
    """建表 + 补列。force=True 时先删库重建（仅用于开发/测试）。"""
    path = db_path()
    if force:
        for suffix in ('', '-wal', '-shm'):
            p = path + suffix
            if os.path.exists(p):
                os.remove(p)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sql = io.open(SCHEMA, encoding='utf-8').read()
    conn = connect()
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError:
        # 旧库路径：schema.sql 里新增的索引引用了本版本才加的列，执行会在建索引处中断。
        # 先补列再整体重跑（executescript 幂等），旧数据因此不会被 DROP。
        ensure_columns(conn)
        conn.executescript(sql)
    else:
        ensure_columns(conn)
    return path


def query(sql, params=()):
    cur = connect().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def query_one(sql, params=()):
    cur = connect().execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def execute(sql, params=()):
    return connect().execute(sql, params)


def table_columns(table):
    return [r['name'] for r in query('PRAGMA table_info(%s)' % table)]
