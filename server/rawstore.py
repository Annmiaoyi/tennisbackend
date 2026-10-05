# -*- coding: utf-8 -*-
"""rawstore.py —— L0 原始数据层（只追加、永不改删）。

============================================================================
 这一层为什么必须单独存在
============================================================================
采集端（Apple Watch / iPhone）上传的是一份**整包原始数据**。它有两个性质
决定了它不能被当成普通业务表：

  1. **它是所有派生的唯一来源。** 识别算法的每一版都要拿它重跑，
     标注、结构化、分析、排行全部可以由它重算出来。因此它必须原样保留，
     不能因为「解析逻辑改了」而失真。
  2. **它可能比当前算法更聪明。** 今天读不出某个字段，不代表以后读不出。
     一旦允许覆盖，旧包就永久丢了。

所以本层铁律：

    · **只 INSERT，永不 UPDATE / DELETE** —— 由 SQLite 触发器在库层面强制，
      任何绕过应用的写法也会被 RAISE(ABORT) 拦下。
    · **同一份字节重复上传 = 幂等命中**（`UNIQUE(session_id, sha256)`）。
    · **同一会话上传了不同字节 = 新版本（revision +1）**，而不是覆盖。
      修正数据、补传字段都走这条路，历史版本永远可查。

============================================================================
 主键设计
============================================================================
    raw_id = sha1(session_id + '|' + shape + '|' + sha256)[:16]

`raw_id` 由**内容 + 形态**决定，因此：

  · 同样的字节重复上传 → 同样的 raw_id → 主键冲突 → 幂等，不产生重复行
  · 不同字节 → 不同 raw_id → 追加为新版本，`revision` 在该形态内递增

============================================================================
 为什么按 (session_id, shape) 分版本，而不是只按 session_id
============================================================================
同一场训练，采集端可能分两路送来**两种不同形态**的东西：

  · `raw_package`   —— 标注工作台传的 `raw_*.json`：`{ session, samples[] }`
                        （含 800Hz 六轴波形，用来画波形图、做预标注）
  · `match_session` —— 终端 App 传的 `MatchSession`：`{ id, startedAt, swings[] }`
                        （只有会话元数据与逐拍结论，没有原始波形）

若只按 session_id 记版本，两种形态会互相盖成对方的「新版本」——
后到的 `match_session` 会让波形接口读不到 `samples`。按形态分开记版本，
两条线各自演进、互不干扰，`payload_of(..., shape=...)` 取到的一定是对应的那一份。

============================================================================
 与其它层的关系
============================================================================
    L0 本层          var/raw/acemate_raw.db + var/raw/files/*.json
    L1 标注层        var/annotation/annotations.db（`sessions.raw_id` → L0.raw_id）
    L2 识别·分析层   var/acemate.db（由 L0 + L1 派生，可重算）
"""
import gzip
import hashlib
import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 形态常量：决定 payload 里装的是什么
SHAPE_RAW_PACKAGE = 'raw_package'      # { session, samples[] } —— 标注工作台
SHAPE_MATCH_SESSION = 'match_session'  # MatchSession 平铺 —— 终端 App

# 压缩与内联阈值
#
# 为什么这两件事必须处理：Apple Watch 的高频采集是**加速度 800Hz + 设备运动 200Hz**
# （`SensorCollector` 对齐后得到 800Hz 的 SensorSample 流）。一场 1 小时的球，
# 单条样本序列化成 JSON 约 250 字节（含 36 字符的 UUID id），
#    2.88e6 样本 × 250 B ≈ 700 MB / 小时
# 直接入库既撑爆 SQLite 也传不动。因此：
#   · 采集端上传前先 gzip（浮点文本 JSON 通常压到 1/6 ~ 1/8）；
#   · 服务端**原样封存压缩字节**（原始层不做有损转换），读取时再解压；
#   · 超过 INLINE_LIMIT 的正文不再冗余内联进库，只留文件 —— 避免「库 + 文件」
#     各存一份把磁盘吃两倍。
GZIP_MAGIC = b'\x1f\x8b'
INLINE_LIMIT = 1 << 20                 # 1 MB
# 文件后缀：按实际压缩格式区分，便于人工辨认（解压逻辑不依赖后缀）
SUFFIX = {'identity': '.json', 'gzip': '.json.gz', 'zlib': '.json.zz', 'deflate': '.json.deflate'}


def _try_decompress(data):
    """识别压缩格式并解压，返回 (bytes, encoding)。都不像就原样返回。

    **三种都要认**，因为不同端的压缩器产出的容器格式不同：
      · `gzip`    —— RFC1952，magic `1f 8b`（Python `gzip`、curl --gzip）
      · `zlib`    —— RFC1950，带 2 字节头（Python `zlib.compress`）
      · `deflate` —— RFC1951 裸流，**无头**。Apple 的
                     `NSData.compressed(using: .zlib)` 产出的就是这个 ——
                     名字叫 zlib，实际是裸 DEFLATE，只看 magic 会误判成未压缩。
    判定以「解压结果是不是 JSON」为准，而不是只看魔数，避免误判。
    """
    if data[:2] == GZIP_MAGIC:
        try:
            out = gzip.decompress(data)
            if out[:1] in (b'{', b'['):
                return out, 'gzip'
        except Exception:
            pass
    for enc, wbits in (('zlib', 15), ('deflate', -15)):
        try:
            out = zlib.decompress(data, wbits)
        except Exception:
            continue
        if out[:1] in (b'{', b'['):
            return out, enc
    return data, 'identity'

# 可用 NETPULSE_RAW_DIR 整体重定向原始层目录（测试 / 多环境用）
DATA_DIR = os.environ.get('NETPULSE_RAW_DIR') or os.path.join(ROOT, 'var', 'raw')
FILES_DIR = os.path.join(DATA_DIR, 'files')
# 可用 NETPULSE_RAW_DB 直接指定库文件
DB_PATH = os.environ.get('NETPULSE_RAW_DB') or os.path.join(DATA_DIR, 'acemate_raw.db')

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS raw_sessions (
    raw_id            TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    shape             TEXT NOT NULL DEFAULT 'raw_package',
    revision          INTEGER NOT NULL DEFAULT 1,
    openid            TEXT,
    source            TEXT,
    device_id         TEXT,
    sha256            TEXT NOT NULL,
    byte_size         INTEGER NOT NULL,
    encoding          TEXT NOT NULL DEFAULT 'identity',
    payload           TEXT,
    file_path         TEXT,
    original_filename TEXT,
    sample_count      INTEGER,
    swing_count       INTEGER,
    started_at        TEXT,
    ended_at          TEXT,
    received_at       TEXT NOT NULL,
    UNIQUE(session_id, shape, sha256)
);
"""

# 列顺序（迁移时按它显式列出，不依赖 SELECT * 的顺序）
_COLUMNS = ('raw_id', 'session_id', 'shape', 'revision', 'openid', 'source',
            'device_id', 'sha256', 'byte_size', 'encoding', 'payload',
            'file_path', 'original_filename', 'sample_count', 'swing_count',
            'started_at', 'ended_at', 'received_at')

SCHEMA = _TABLE_DDL + """
CREATE INDEX IF NOT EXISTS idx_raw_session  ON raw_sessions(session_id, shape, revision);
CREATE INDEX IF NOT EXISTS idx_raw_received ON raw_sessions(received_at);
CREATE INDEX IF NOT EXISTS idx_raw_openid   ON raw_sessions(openid);

-- 只追加：库层面强制，防任何绕过应用的写法
CREATE TRIGGER IF NOT EXISTS trg_raw_no_update
BEFORE UPDATE ON raw_sessions
BEGIN
    SELECT RAISE(ABORT, 'L0 原始数据层只追加：禁止 UPDATE raw_sessions');
END;
CREATE TRIGGER IF NOT EXISTS trg_raw_no_delete
BEFORE DELETE ON raw_sessions
BEGIN
    SELECT RAISE(ABORT, 'L0 原始数据层只追加：禁止 DELETE raw_sessions');
END;
"""

# 轻量迁移：已存在的旧表补列（CREATE TABLE IF NOT EXISTS 不会给旧表补列）
_NEW_COLUMNS = {
    'encoding': "TEXT NOT NULL DEFAULT 'identity'",
}


class RawConflict(Exception):
    """同一会话上传了与已有版本字节不同、但业务上不允许并存的载荷。

    正常路径不会抛：不同字节会被接纳为新 revision。只有调用方显式传
    `allow_revision=False` 时才会撞到这里（例如「这一版之后不允许改」）。
    """


def utcnow():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _payload_is_notnull(conn):
    """旧表把 `payload` 建成了 NOT NULL，而 gzip / 大包路径必须写 NULL。"""
    for r in conn.execute('PRAGMA table_info(raw_sessions)'):
        if r[1] == 'payload':
            return bool(r[3])
    return False


def _rebuild_payload_nullable(conn):
    """把 `payload` 的 NOT NULL 约束去掉（SQLite 无 ALTER COLUMN，只能重建表）。

    ⚠️ 为什么必须做：`payload` 曾经是 `NOT NULL`，后来为了「gzip 包 / 超过
    INLINE_LIMIT 的大包只落文件、库里不冗余内联」改成了可空。但
    `CREATE TABLE IF NOT EXISTS` **永远不会修改已存在的表** ——
    于是老库上每一条压缩包上传都会 500：
        NOT NULL constraint failed: raw_sessions.payload
    本问题在全新库上完全看不出来（新库直接按新 DDL 建），只有升级环境才会炸。

    做法：建临时表 → 按列名显式拷贝 → 换名。迁移期间临时摘掉只追加触发器
    （SQLite 的 DROP TABLE 本身不触发 DELETE 触发器，但显式摘掉更清楚），
    迁完由 `SCHEMA` 的 `IF NOT EXISTS` 自动重建索引与触发器。
    """
    tmp = 'raw_sessions_migrate'
    conn.executescript(
        'DROP TRIGGER IF EXISTS trg_raw_no_update;'
        'DROP TRIGGER IF EXISTS trg_raw_no_delete;'
        'DROP TABLE IF EXISTS %s;' % tmp)
    conn.execute(_TABLE_DDL.replace('IF NOT EXISTS raw_sessions', tmp))
    cols = ', '.join(_COLUMNS)
    conn.execute('INSERT INTO %s (%s) SELECT %s FROM raw_sessions' % (tmp, cols, cols))
    moved = conn.execute('SELECT COUNT(*) FROM %s' % tmp).fetchone()[0]
    conn.execute('DROP TABLE raw_sessions')
    conn.execute('ALTER TABLE %s RENAME TO raw_sessions' % tmp)
    return moved


def init_db():
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        have = {r[1] for r in conn.execute('PRAGMA table_info(raw_sessions)')}
        for name, ddl in _NEW_COLUMNS.items():
            if name not in have:
                conn.execute('ALTER TABLE raw_sessions ADD COLUMN %s %s' % (name, ddl))
        conn.commit()

        if _payload_is_notnull(conn):
            moved = _rebuild_payload_nullable(conn)
            conn.executescript(SCHEMA)          # 重建索引与只追加触发器
            conn.commit()
            print('[rawstore] 迁移：raw_sessions.payload 去掉 NOT NULL，'
                  '搬运 %d 行历史版本' % moved)
    finally:
        conn.close()
    return DB_PATH


def _raw_id(session_id, shape, sha):
    return hashlib.sha1(('%s|%s|%s' % (session_id, shape, sha)).encode('utf-8')).hexdigest()[:16]


def _meta_of(obj):
    """从整包里尽力抽几个可检索字段。抽不出就算了 —— 原始层不依赖解析成功。"""
    if not isinstance(obj, dict):
        return {}
    session = obj.get('session') if isinstance(obj.get('session'), dict) else obj
    samples = obj.get('samples')
    swings = session.get('swings')
    return {
        'sample_count': len(samples) if isinstance(samples, list) else None,
        'swing_count': len(swings) if isinstance(swings, list) else None,
        'started_at': session.get('startedAt'),
        'ended_at': session.get('endedAt'),
    }


def archive(payload, *, session_id, shape=SHAPE_RAW_PACKAGE, openid=None, source=None,
            device_id=None, filename=None, write_file=True, allow_revision=True):
    """把一份原始载荷追加进 L0。**这是本层唯一的写入口。**

    :param payload:  bytes（原始字节，最保真）或 dict/str（会被序列化）
    :param session_id: 采集端会话 ID（必填，跨端幂等键）
    :param shape:    `raw_package`（含波形）| `match_session`（仅结论）。
                     版本号在**同一形态内**递增，两种形态互不干扰。
    :param write_file: 是否同时把原文件留档到 var/raw/files/
    :returns: {'status': 'created'|'duplicate', 'raw_id', 'session_id', 'shape',
               'revision', 'sha256', 'byte_size', 'file_path'}
    :raises ValueError: session_id 为空 / payload 无法序列化
    :raises RawConflict: 字节不同且 allow_revision=False
    """
    sid = str(session_id or '').strip()
    if not sid:
        raise ValueError('session_id 必填 —— 它是原始层的跨端幂等键')
    shape = (shape or SHAPE_RAW_PACKAGE).strip() or SHAPE_RAW_PACKAGE

    if isinstance(payload, bytes):
        raw_bytes = payload
    elif isinstance(payload, str):
        raw_bytes = payload.encode('utf-8')
    else:
        raw_bytes = json.dumps(payload, ensure_ascii=False,
                               separators=(',', ':')).encode('utf-8')

    sha = hashlib.sha256(raw_bytes).hexdigest()
    rid = _raw_id(sid, shape, sha)
    size = len(raw_bytes)

    # ---- 压缩与内联判定（理由见文件头 INLINE_LIMIT 处）----
    body, encoding = _try_decompress(raw_bytes)
    compressed = encoding != 'identity'
    if compressed:
        write_file = True                     # 压缩包必须落文件，库里不内联
    if body and len(body) <= INLINE_LIMIT and not compressed:
        inline = body.decode('utf-8', 'replace')
    else:
        inline = None                         # 大包 / 压缩包只留文件
    if inline is None and not write_file:
        raise ValueError('正文超过内联上限或为压缩包时必须落文件（write_file=True）')

    try:
        obj = json.loads(body.decode('utf-8')) if body else None
    except Exception:
        obj = None

    conn = get_conn()
    try:
        # 幂等闸：同形态 + 同字节 → 同 raw_id → 直接命中
        row = conn.execute('SELECT * FROM raw_sessions WHERE raw_id=?', (rid,)).fetchone()
        if row:
            return {
                'status': 'duplicate', 'raw_id': rid, 'session_id': sid,
                'shape': shape, 'revision': row['revision'], 'sha256': sha,
                'byte_size': size, 'encoding': row['encoding'],
                'file_path': row['file_path'],
            }

        latest = conn.execute(
            'SELECT MAX(revision) AS r FROM raw_sessions WHERE session_id=? AND shape=?',
            (sid, shape)).fetchone()
        prev = latest['r'] if latest and latest['r'] else 0
        if prev and not allow_revision:
            raise RawConflict('会话 %s 形态 %s 已有 %d 个版本，且不允许追加新版本'
                              % (sid, shape, prev))
        revision = prev + 1

        file_path = None
        if write_file:
            # 文件名带形态与版本，避免不同版本互相覆盖；压缩包带对应后缀
            name = 'raw_%s_%s_r%d%s' % (sid, shape, revision, SUFFIX.get(encoding, '.json'))
            dest = os.path.join(FILES_DIR, name)
            with open(dest, 'wb') as f:
                f.write(raw_bytes)
            # ← 库里存**相对 DATA_DIR** 的路径，不存绝对路径：
            #   `var/` 整体 rsync 到另一台机器后仍然有效。
            #   曾存绝对路径，换机会导致全部读取失败且**不报错**（见 resolve_path）。
            file_path = os.path.join('files', name)

        m = _meta_of(obj)

        conn.execute(
            'INSERT INTO raw_sessions(raw_id, session_id, shape, revision, openid,'
            ' source, device_id, sha256, byte_size, encoding, payload, file_path,'
            ' original_filename, sample_count, swing_count, started_at, ended_at,'
            ' received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (rid, sid, shape, revision, openid, source, device_id, sha, size,
             encoding, inline, file_path, filename,
             m.get('sample_count'), m.get('swing_count'),
             m.get('started_at'), m.get('ended_at'), utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        'status': 'created', 'raw_id': rid, 'session_id': sid, 'shape': shape,
        'revision': revision, 'sha256': sha, 'byte_size': size,
        'encoding': encoding, 'file_path': file_path,
    }


def latest(session_id, shape=None):
    """取该会话的**最新版本**（不含 payload 正文，便于列表展示）。"""
    conn = get_conn()
    try:
        if shape:
            row = conn.execute(
                'SELECT * FROM raw_sessions WHERE session_id=? AND shape=?'
                ' ORDER BY revision DESC LIMIT 1', (session_id, shape)).fetchone()
        else:
            row = conn.execute(
                'SELECT * FROM raw_sessions WHERE session_id=?'
                ' ORDER BY received_at DESC, revision DESC LIMIT 1',
                (session_id,)).fetchone()
        return _row(row, with_payload=False)
    finally:
        conn.close()


def by_raw_id(raw_id):
    conn = get_conn()
    try:
        row = conn.execute('SELECT * FROM raw_sessions WHERE raw_id=?', (raw_id,)).fetchone()
        return _row(row, with_payload=True)
    finally:
        conn.close()


def resolve_path(p):
    """把库里存的 `file_path` 解析成当前环境下**真实可读**的绝对路径；找不到返回 None。

    ⚠️ 为什么必须有这一层（2026-10-05 实测踩到，代价是「看起来数据丢了」）：

    `archive()` 曾经往库里写 `os.path.join(FILES_DIR, name)` —— **绝对路径**，
    而 `FILES_DIR` 取决于部署目录。把整个 `var/` 搬到另一台机器（或换个目录）之后，
    每一条 `file_path` 都还指着**旧机器**的位置，`os.path.exists()` 为假。

    后果**不报错**：`_decode_row` 直接返回 None ⇒ `payload_of` 返回 None ⇒
    逐拍页显示「原始包击球数 0 / 逐拍全量 0 行」、波形画不出来、导出说没有样本。
    看起来像数据丢了，其实文件就躺在 `var/raw/files/` 里。
    实测：**同一份库**，本机逐拍 106 行、服务器 0 行；只有 `file_path` 是差异。

    解析顺序（三级回落，与 `converter.load_raw_payload` 同一思路，**顺序不能换**）：
      1. 相对路径 → 相对 `DATA_DIR` 解析（新写入的形态，天然可搬迁，是目标形态）；
      2. 绝对路径且**存在** → 直接用（老库在本机仍可跑，向后兼容）；
      3. 兜底：按 `basename` 去当前 `FILES_DIR` 找（老绝对路径跨机搬迁后的唯一出路）。
         文件名形如 `raw_<sid>_<shape>_r<rev>.<ext>`，含会话与版本，basename 唯一，
         不会张冠李戴。
    """
    if not p:
        return None
    if os.path.isabs(p):
        if os.path.exists(p):
            return p
        cand = os.path.join(FILES_DIR, os.path.basename(p))
        return cand if os.path.exists(cand) else None
    cand = os.path.join(DATA_DIR, p)
    if os.path.exists(cand):
        return cand
    cand = os.path.join(FILES_DIR, os.path.basename(p))
    return cand if os.path.exists(cand) else None


def _decode_row(row):
    """把一行解成 dict。

    两种存储形态都要能读：
      · 小包 → `payload` 列内联着 JSON 文本
      · 大包 / gzip 包 → `payload` 为空，正文在 `file_path` 指向的文件里
        （gzip 包读取时透明解压 —— 调用方拿到的一律是**原始 JSON**，
         不需要知道它落盘时是不是压缩的）

    ⚠️ `file_path` 一律经 `resolve_path()` 解析，**不要直接 `os.path.exists(file_path)`** ——
    库里可能存着别的机器上的绝对路径（见 `resolve_path` 的注释）。
    """
    if row is None:
        return None
    text = row['payload']
    if text:
        try:
            return json.loads(text)
        except Exception:
            return None
    path = resolve_path(row['file_path'])
    if not path:
        return None
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    data, _enc = _try_decompress(data)
    try:
        return json.loads(data.decode('utf-8'))
    except Exception:
        return None


def payload_of(session_id, revision=None, shape=None):
    """取整包正文（dict）。解析不了则返回 None。

    `shape` 建议显式传：波形接口要的永远是 `raw_package`（含 `samples`），
    不传的话拿到的是「最近收到的那一份」，可能是没有波形的 `match_session`。
    """
    conn = get_conn()
    try:
        if revision:
            sql = ('SELECT payload, file_path FROM raw_sessions'
                   ' WHERE session_id=? AND revision=?')
            params = [session_id, revision]
            if shape:
                sql += ' AND shape=?'
                params.append(shape)
            sql += ' ORDER BY received_at DESC LIMIT 1'
            row = conn.execute(sql, tuple(params)).fetchone()
        elif shape:
            row = conn.execute(
                'SELECT payload, file_path FROM raw_sessions'
                ' WHERE session_id=? AND shape=?'
                ' ORDER BY revision DESC LIMIT 1', (session_id, shape)).fetchone()
        else:
            row = conn.execute(
                'SELECT payload, file_path FROM raw_sessions WHERE session_id=?'
                ' ORDER BY received_at DESC, revision DESC LIMIT 1',
                (session_id,)).fetchone()
    finally:
        conn.close()
    return _decode_row(row)


def revisions(session_id, shape=None):
    conn = get_conn()
    try:
        if shape:
            rows = conn.execute(
                'SELECT * FROM raw_sessions WHERE session_id=? AND shape=?'
                ' ORDER BY revision', (session_id, shape)).fetchall()
        else:
            rows = conn.execute(
                'SELECT * FROM raw_sessions WHERE session_id=?'
                ' ORDER BY shape, revision', (session_id,)).fetchall()
        return [_row(r, with_payload=False) for r in rows]
    finally:
        conn.close()


def list_recent(limit=50, openid=None, source=None, shape=None):
    sql = ('SELECT * FROM raw_sessions WHERE 1=1')
    params = []
    if openid:
        sql += ' AND openid=?'
        params.append(openid)
    if source:
        sql += ' AND source=?'
        params.append(source)
    if shape:
        sql += ' AND shape=?'
        params.append(shape)
    sql += ' ORDER BY received_at DESC LIMIT ?'
    params.append(int(limit))
    conn = get_conn()
    try:
        return [_row(r, with_payload=False) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def stats():
    """原始层水位：会话数 / 版本数 / 总字节 / 多版本会话数 / 各形态分布。"""
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT COUNT(*) AS rows_, COUNT(DISTINCT session_id) AS sessions,'
            ' COALESCE(SUM(byte_size),0) AS bytes,'
            ' COALESCE(SUM(sample_count),0) AS samples,'
            ' COALESCE(SUM(swing_count),0) AS swings,'
            ' MIN(received_at) AS first_at, MAX(received_at) AS last_at'
            ' FROM raw_sessions').fetchone()
        multi = conn.execute(
            'SELECT COUNT(*) AS n FROM (SELECT session_id, shape FROM raw_sessions'
            ' GROUP BY session_id, shape HAVING COUNT(*) > 1)').fetchone()
        by_shape = {r['shape']: r['n'] for r in conn.execute(
            'SELECT shape, COUNT(*) AS n FROM raw_sessions GROUP BY shape').fetchall()}
    finally:
        conn.close()
    return {
        'revisions': row['rows_'],
        'sessions': row['sessions'],
        'multi_revision': multi['n'] if multi else 0,
        'by_shape': by_shape,
        'bytes': row['bytes'],
        'samples': row['samples'],
        'swings': row['swings'],
        'first_at': row['first_at'],
        'last_at': row['last_at'],
        'db_path': DB_PATH,
        'files_dir': FILES_DIR,
    }


def _row(row, with_payload=True):
    if row is None:
        return None
    d = dict(row)
    if not with_payload:
        d.pop('payload', None)
    return d
