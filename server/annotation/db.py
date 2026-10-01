"""
db.py — 标注库（annotations.db）的连接与建表。

并入 AceMate 后端后，数据落在 `backend-web/var/annotation/` 下，
与训练分析库 `var/acemate.db` **分库并存**：
  · var/acemate.db      ← server/db.py      训练会话 / 学员 / 同步协议（分析侧）
  · var/annotation/annotations.db ← 本文件  原始传感器会话 / 人工标注 / 登录令牌（采集训练侧）

两库各管各的连接与事务，互不影响 —— 标注库的高频写入（每拍一条记录）
不会去争分析库的写锁。

标注表（annotations）字段说明：
  annotator : 谁打的标（"heuristic" = Watch 启发式预标注；其余为人工标注者名）
  source    : 'auto'(启发式) | 'human'(人工新标/纠错) | 'corrected'(纠错后的硬例)
  status    : 'proposed'(未审阅的预标注) | 'confirmed'(已确认/人工确认) | 'rejected'(被纠错或判为误检)
"""
import os

from .. import rawstore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # server/annotation
ROOT = os.path.dirname(os.path.dirname(BASE_DIR))              # backend-web/

# 可用 NETPULSE_ANNOTATION_DIR 整体重定向标注数据目录（测试 / 多环境用）
DATA_DIR = os.environ.get('NETPULSE_ANNOTATION_DIR') or os.path.join(ROOT, 'var', 'annotation')
# ⚠️ 原始文件**不再由标注层自己保管** —— 统一落在 L0 原始层（server/rawstore.py）。
# 标注层只保存一个指向 L0 的 `raw_id`，`raw_path` 退化为「L0 文件路径的缓存」。
# 这样「原始数据」只有一份，不会出现标注目录与原始目录各存一份、日后对不上的问题。
RAW_DIR = rawstore.FILES_DIR
VIDEO_DIR = os.path.join(DATA_DIR, 'video')
os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

# 可用 NETPULSE_DB 直接指定库文件（原标注后端的同名变量，保持兼容）
DB_PATH = os.environ.get('NETPULSE_DB', os.path.join(DATA_DIR, 'annotations.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    wrist       TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    duration    REAL,
    player_id   TEXT,
    raw_id      TEXT,
    raw_path    TEXT,
    video_path  TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    impact_time REAL NOT NULL,
    label       TEXT NOT NULL,
    confidence  REAL,
    note        TEXT,
    annotator   TEXT NOT NULL DEFAULT 'human',
    source      TEXT NOT NULL DEFAULT 'human',
    status      TEXT NOT NULL DEFAULT 'confirmed',
    created_at  TEXT,
    updated_at  TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_anno_session ON annotations(session_id);
"""

# 旧库迁移：已存在的 annotations 表补列（一次性 ALTER）
_NEW_ANNOTATION_COLUMNS = {
    "annotator": "TEXT NOT NULL DEFAULT 'human'",
    "source": "TEXT NOT NULL DEFAULT 'human'",
    "status": "TEXT NOT NULL DEFAULT 'confirmed'",
    "updated_at": "TEXT",
}

# 旧库迁移：sessions 表补 `raw_id`（指向 L0 原始层的 raw_sessions.raw_id）
_NEW_SESSION_COLUMNS = {
    "raw_id": "TEXT",
}


def get_conn():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 并发写入时等待而非直接报错
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    for table, columns in (("annotations", _NEW_ANNOTATION_COLUMNS),
                           ("sessions", _NEW_SESSION_COLUMNS)):
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()
