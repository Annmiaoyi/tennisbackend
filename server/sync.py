# -*- coding: utf-8 -*-
"""离线优先同步引擎。

============================================================================
 协议总览
============================================================================
数据流向：
    客户端本地库 ──(写先落本地 + 入待同步队列)──> /api/sync/push ──> 后端权威库
    后端权威库 ──(服务器游标增量)────────────────> /api/sync/pull ──> 客户端本地库

不变量（Invariants）
  1. 后端数据库是唯一真实数据源；客户端只是缓存 + 待同步队列。
  2. 所有写操作「先写本地，再进同步队列」，因此客户端可以完全离线工作。
  3. 推送必须**幂等**：以 operation_id 为主键去重，重试任意多次结果一致。
  4. 拉取基于**服务器游标 seq**（全库单调递增），绝不使用客户端时间排序。
  5. 冲突用 LWW：比较 client_updated_at 与服务器行的 updated_at，**严格大于**才覆盖。
     严格大于这个细节同时带来了天然的幂等性 —— 重放已应用的操作会因为
     相等而不产生任何修改。
  6. 删除是软删除（deleted_at），保留行以便把删除事件同步给其它设备。
  7. 每条记录都带 id/user_id/created_at/updated_at/deleted_at/version/sync_status。

============================================================================
"""
import json
import uuid

from . import db

# ---------------------------------------------------------------------------
# 可同步实体注册表
#   table    : 物理表名
#   fields   : 允许客户端写入的字段白名单（其余字段一律忽略，防止越权写入
#              server_seq / version / sync_status 这类服务端专属列）
#   required : 创建时必备字段
# ---------------------------------------------------------------------------
SYNCABLE = {
    'student_profile': {
        'table': 'students',
        'fields': [
            'name', 'avatar_url', 'avatar_initial', 'tier', 'device_id', 'watch_model',
            'batch', 'years_playing', 'hand', 'backhand', 'racket', 'racket_tension',
            'nt_level', 'nt_score', 'sessions_count', 'hours_total', 'strokes_total',
            'forehand_avg', 'serve_peak', 'hit_rate',
            'training_load', 'acwr', 'last_training_at', 'last_training_note',
            'location', 'is_online',
        ],
        'required': ['name'],
    },
    'training_session': {
        'table': 'training_sessions',
        'fields': [
            'student_id', 'title', 'session_type', 'location', 'court_type',
            'started_at', 'ended_at', 'duration_sec', 'stroke_count', 'rally_max',
            'distance_km', 'calories_kcal', 'avg_hr', 'max_hr', 'avg_speed_kmh',
            'peak_speed_kmh', 'forehand_avg_kmh', 'backhand_avg_kmh', 'serve_avg_kmh',
            'serve_peak_kmh', 'hr_zone', 'notes',
            # 采集侧元数据与击球分项计数（Apple Watch 接入新增）
            'worn_wrist', 'source', 'external_id',
            'forehand_count', 'backhand_count', 'serve_count',
            'slice_count', 'volley_count', 'smash_count',
        ],
        'required': ['student_id', 'started_at'],
    },
    'stroke_record': {
        'table': 'stroke_records',
        'fields': [
            'session_id', 'seq_in_session', 'stroke_type', 'is_slice', 'speed_kmh',
            'impact_ms', 'confidence', 'anomaly',
        ],
        'required': ['session_id', 'stroke_type'],
    },
    'feedback_ticket': {
        'table': 'feedback_tickets',
        'fields': [
            'student_id', 'code', 'title', 'body', 'category', 'status', 'status_label',
            'priority', 'votes', 'reporter_name', 'reporter_meta', 'reporter_device',
            'source', 'roadmap', 'assignee', 'occurred_at',
        ],
        'required': ['title'],
    },
}

# 客户端可提交的 action
ACTIONS = ('create', 'update', 'delete')

# 单次推送 / 拉取上限
MAX_OPS_PER_PUSH = 500
MAX_CHANGES_PER_PULL = 1000


class SyncError(ValueError):
    """请求本身不合法（400）。与业务冲突（conflict_lost）区分开。"""


def _json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _append_changelog(conn, user_id, entity_type, entity_id, action, version,
                      updated_at, deleted_at, payload, operation_id, now):
    """写变更日志并返回分配的游标 seq。

    游标是**全库单调递增**的 AUTOINCREMENT，跨实体统一，客户端只需存一个
    数字即可增量拉取所有实体 —— 比按表各存一个时间戳可靠得多
    （时间戳会因时钟回拨、同毫秒并列而出错）。
    """
    cur = conn.execute(
        'INSERT INTO sync_changelog (user_id, entity_type, entity_id, action, version,'
        ' updated_at, deleted_at, payload, operation_id, created_at)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?)',
        (user_id, entity_type, entity_id, action, version, updated_at, deleted_at,
         _json(payload), operation_id, now),
    )
    return cur.lastrowid


def _record_operation(conn, op_id, user_id, device_id, entity_type, entity_id, action,
                      payload, client_ts, result, reason, server_version,
                      server_updated_at, server_seq, applied, now):
    conn.execute(
        'INSERT INTO sync_operations (operation_id, user_id, device_id, entity_type,'
        ' entity_id, action, payload, client_updated_at, result, reason, server_version,'
        ' server_updated_at, server_seq, applied, received_at)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (op_id, user_id, device_id, entity_type, entity_id, action, _json(payload),
         client_ts, result, reason, server_version, server_updated_at, server_seq,
         1 if applied else 0, now),
    )


def apply_operation(conn, user_id, device_id, op, now):
    """在**已开启写事务**的前提下应用单个操作。

    返回 (结果字典, is_duplicate)。is_duplicate=True 表示该 operation_id
    之前已处理过，本次只是回放历史结果（未改库）。
    """
    op_id = (op.get('operation_id') or '').strip()
    if not op_id:
        raise SyncError('operation_id 必填（幂等键）')

    # ---------- 幂等第一道闸：operation_id 去重 ----------
    prev = conn.execute(
        'SELECT result, reason, server_version, server_updated_at, server_seq, applied'
        ' FROM sync_operations WHERE operation_id = ?', (op_id,)).fetchone()
    if prev is not None:
        return {
            'operation_id': op_id,
            'entity_type': op.get('entity_type'),
            'entity_id': op.get('entity_id'),
            'action': op.get('action'),
            'result': 'duplicate',
            'previous_result': prev['result'],
            'applied': False,
            'server_version': prev['server_version'],
            'server_updated_at': prev['server_updated_at'],
            'server_seq': prev['server_seq'],
            'reason': 'operation_id 已处理过，回放历史结果（幂等，未改库）',
        }, True

    entity_type = (op.get('entity_type') or '').strip()
    entity_id = (op.get('entity_id') or '').strip()
    action = (op.get('action') or '').strip().lower()
    payload = op.get('payload') or {}
    client_ts = db.normalize_ts(op.get('client_updated_at')) or now

    # 把 operation_id 先占位写入，避免同一批里出现重复 operation_id 时第二次仍通过查重。
    # 主键冲突 = 同批内重复，直接按重复处理。
    try:
        conn.execute(
            'INSERT INTO sync_operations (operation_id, user_id, device_id, entity_type,'
            ' entity_id, action, payload, client_updated_at, result, reason, received_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (op_id, user_id, device_id, entity_type, entity_id, action, _json(payload),
             client_ts, 'pending', '占位：防止同批重复 operation_id', now))
    except Exception:
        return {
            'operation_id': op_id, 'entity_type': entity_type, 'entity_id': entity_id,
            'action': action, 'result': 'duplicate', 'applied': False,
            'reason': '同一批次内 operation_id 重复',
        }, True

    def finish(result, reason, applied=False, version=None, updated_at=None, seq=None):
        conn.execute(
            'UPDATE sync_operations SET result=?, reason=?, server_version=?,'
            ' server_updated_at=?, server_seq=?, applied=? WHERE operation_id=?',
            (result, reason, version, updated_at, seq, 1 if applied else 0, op_id))
        return {
            'operation_id': op_id, 'entity_type': entity_type, 'entity_id': entity_id,
            'action': action, 'result': result, 'applied': applied,
            'server_version': version, 'server_updated_at': updated_at,
            'server_seq': seq, 'reason': reason,
        }, False

    # ---------- 参数校验 ----------
    if not entity_id:
        return finish('rejected', 'entity_id 必填')
    if action not in ACTIONS:
        return finish('rejected', 'action 必须是 %s 之一，收到 %r' % ('/'.join(ACTIONS), action))
    spec = SYNCABLE.get(entity_type)
    if spec is None:
        return finish('rejected', '未知 entity_type=%r，可选：%s'
                      % (entity_type, ','.join(sorted(SYNCABLE))))

    table = spec['table']
    if action in ('create', 'update'):
        missing = [f for f in spec['required'] if payload.get(f) in (None, '')]
        # update 允许不携带全部必填字段（部分更新）
        if action == 'create' and missing:
            return finish('rejected', '缺少必填字段：%s' % ','.join(missing))

    clean = {k: v for k, v in payload.items() if k in spec['fields']}

    # ---------- 读当前服务端行 ----------
    row = conn.execute('SELECT * FROM %s WHERE id = ?' % table, (entity_id,)).fetchone()
    if row is not None and row['user_id'] != user_id:
        return finish('rejected', '该记录属于其它用户，拒绝跨租户写入')

    # ---------- 软删除 ----------
    if action == 'delete':
        if row is None:
            # 服务端从没见过这条：仍写入一个「墓碑」，
            # 否则客户端的删除事件无法传播到其它设备。
            #
            # ⚠️ 墓碑只填同步列，不含业务字段（业务列在 schema 里刻意不加 NOT NULL，
            #    改用 CHECK(deleted_at IS NOT NULL OR <必填列> IS NOT NULL)）。
            #    但客户端若在 delete 的 payload 里带了最后已知快照，就一并保留下来，
            #    便于排障与「回收站」类 UI 展示。
            cols = ['id', 'user_id', 'created_at', 'updated_at', 'deleted_at', 'version',
                    'sync_status']
            vals = [entity_id, user_id, client_ts, client_ts, client_ts, 1, 'synced']
            for k, v in clean.items():
                if k in cols:
                    continue
                cols.append(k)
                vals.append(v if not isinstance(v, (dict, list)) else _json(v))
            conn.execute(
                'INSERT INTO %s (%s) VALUES (%s)'
                % (table, ','.join(cols), ','.join('?' * len(vals))), vals)
            snapshot = _row_snapshot(conn, table, entity_id)
            seq = _append_changelog(conn, user_id, entity_type, entity_id, 'delete', 1,
                                    client_ts, client_ts, snapshot, op_id, now)
            conn.execute('UPDATE %s SET server_seq=? WHERE id=?' % table, (seq, entity_id))
            return finish('applied', '服务端无此记录，已写入删除墓碑', True, 1, client_ts, seq)

        if row['deleted_at']:
            return finish('conflict_lost', '服务端已是删除状态（幂等重复）',
                          False, row['version'], row['updated_at'], row['server_seq'])
        if not (client_ts > row['updated_at']):
            return finish('conflict_lost',
                          'LWW：客户端时间 %s 不晚于服务端 %s，保留服务端版本'
                          % (client_ts, row['updated_at']),
                          False, row['version'], row['updated_at'], row['server_seq'])
        version = row['version'] + 1
        conn.execute('UPDATE %s SET deleted_at=?, updated_at=?, version=?, sync_status=?'
                     ' WHERE id=?' % table, (client_ts, client_ts, version, 'synced', entity_id))
        snapshot = _row_snapshot(conn, table, entity_id)
        seq = _append_changelog(conn, user_id, entity_type, entity_id, 'delete', version,
                                client_ts, client_ts, snapshot, op_id, now)
        conn.execute('UPDATE %s SET server_seq=? WHERE id=?' % table, (seq, entity_id))
        return finish('applied', '软删除已应用', True, version, client_ts, seq)

    # ---------- 新建 / 更新（统一走 upsert + LWW）----------
    if row is None:
        cols = ['id', 'user_id', 'created_at', 'updated_at', 'version', 'sync_status']
        vals = [entity_id, user_id, client_ts, client_ts, 1, 'synced']
        for k, v in clean.items():
            cols.append(k)
            vals.append(v if not isinstance(v, (dict, list)) else _json(v))
        conn.execute('INSERT INTO %s (%s) VALUES (%s)'
                     % (table, ','.join(cols), ','.join('?' * len(vals))), vals)
        snapshot = _row_snapshot(conn, table, entity_id)
        seq = _append_changelog(conn, user_id, entity_type, entity_id, 'create', 1,
                                client_ts, None, snapshot, op_id, now)
        conn.execute('UPDATE %s SET server_seq=? WHERE id=?' % table, (seq, entity_id))
        return finish('applied', '服务端不存在，已创建', True, 1, client_ts, seq)

    # 已存在 → LWW 比较
    if not (client_ts > row['updated_at']):
        return finish('conflict_lost',
                      'LWW：客户端时间 %s 不晚于服务端 %s，保留服务端版本'
                      % (client_ts, row['updated_at']),
                      False, row['version'], row['updated_at'], row['server_seq'])

    # 对「墓碑行」做 update 等于**复活**（下面会把 deleted_at 清空）。
    # 若该墓碑是客户端删除一条服务端从未见过的记录而生成的，它没有业务字段，
    # 直接复活会违反 schema 的 CHECK(deleted_at IS NOT NULL OR <必填列> IS NOT NULL)。
    # 因此复活前要求：payload + 现有行 能凑齐必填字段。
    if row['deleted_at']:
        missing = [f for f in spec['required']
                   if (clean.get(f) or row[f]) in (None, '')]
        if missing:
            return finish('rejected',
                          '复活已删除记录时缺少必填字段：%s（墓碑行无业务数据，'
                          '需由本次 payload 补全）' % ','.join(missing),
                          False, row['version'], row['updated_at'], row['server_seq'])

    version = row['version'] + 1
    sets, params = [], []
    for k, v in clean.items():
        sets.append('%s = ?' % k)
        params.append(v if not isinstance(v, (dict, list)) else _json(v))
    sets += ['updated_at = ?', 'version = ?', 'deleted_at = NULL', 'sync_status = ?']
    params += [client_ts, version, 'synced', entity_id]
    conn.execute('UPDATE %s SET %s WHERE id = ?' % (table, ','.join(sets)), params)
    snapshot = _row_snapshot(conn, table, entity_id)
    seq = _append_changelog(conn, user_id, entity_type, entity_id, action, version,
                            client_ts, None, snapshot, op_id, now)
    conn.execute('UPDATE %s SET server_seq=? WHERE id=?' % table, (seq, entity_id))
    return finish('applied', 'LWW 客户端版本更新，已应用', True, version, client_ts, seq)


def _row_snapshot(conn, table, entity_id):
    row = conn.execute('SELECT * FROM %s WHERE id = ?' % table, (entity_id,)).fetchone()
    return dict(row) if row else {'id': entity_id}


def push(user_id, operations, device_id=None):
    """批量推送。整批在**一个写事务**里完成：
    任一条失败不会中断其余操作（逐条独立裁决），但全部变更要么一起提交、
    要么一起回滚，保证客户端重试时不会出现「一半已写」的中间态。
    """
    if not isinstance(operations, list):
        raise SyncError('operations 必须是数组')
    if len(operations) > MAX_OPS_PER_PUSH:
        raise SyncError('单次推送最多 %d 条，本次 %d 条' % (MAX_OPS_PER_PUSH, len(operations)))

    now = db.utcnow()
    results = []
    with db.tx() as conn:
        for op in operations:
            res, _dup = apply_operation(conn, user_id, device_id, op, now)
            results.append(res)

    applied = sum(1 for r in results if r['applied'])
    dup = sum(1 for r in results if r['result'] == 'duplicate')
    lost = sum(1 for r in results if r['result'] == 'conflict_lost')
    rejected = sum(1 for r in results if r['result'] == 'rejected')
    # 推送后服务端的最新游标，客户端可直接用它作为下次拉取的起点
    cursor = db.query_one('SELECT COALESCE(MAX(seq), 0) AS c FROM sync_changelog')['c']
    return {
        'received_at': now,
        'total': len(results),
        'applied': applied,
        'duplicate': dup,
        'conflict_lost': lost,
        'rejected': rejected,
        'cursor': cursor,
        'results': results,
    }


def pull(user_id, cursor=0, limit=100, entity_types=None):
    """按服务器游标增量拉取。

    · 只用 `seq > cursor` 定位，**绝不使用客户端时间**（时钟不可信）。
    · 返回的每条 change 都是该实体的**完整快照**，因此客户端可无序应用、
      可重复应用；配合每条自带的 version 做幂等。
    · next_cursor 是本次最后一个 seq；客户端原子保存「数据 + next_cursor」。
    """
    try:
        cursor = int(cursor)
    except (TypeError, ValueError):
        raise SyncError('cursor 必须是整数')
    limit = max(1, min(int(limit or 100), MAX_CHANGES_PER_PULL))

    sql = 'SELECT * FROM sync_changelog WHERE user_id = ? AND seq > ?'
    params = [user_id, cursor]
    if entity_types:
        if isinstance(entity_types, str):
            entity_types = [t for t in entity_types.split(',') if t]
        unknown = [t for t in entity_types if t not in SYNCABLE]
        if unknown:
            raise SyncError('未知 entity_type：%s' % ','.join(unknown))
        sql += ' AND entity_type IN (%s)' % ','.join('?' * len(entity_types))
        params += list(entity_types)
    sql += ' ORDER BY seq ASC LIMIT ?'
    params.append(limit + 1)          # 多取一条用于判断 has_more

    rows = db.query(sql, params)
    has_more = len(rows) > limit
    rows = rows[:limit]

    changes = []
    for r in rows:
        changes.append({
            'seq': r['seq'],
            'entity_type': r['entity_type'],
            'entity_id': r['entity_id'],
            'action': r['action'],
            'version': r['version'],
            'updated_at': r['updated_at'],
            'deleted_at': r['deleted_at'],
            'payload': json.loads(r['payload']) if r['payload'] else None,
        })

    next_cursor = rows[-1]['seq'] if rows else cursor
    now = db.utcnow()
    db.execute(
        'INSERT INTO sync_pulls (id, user_id, device_id, from_cursor, to_cursor,'
        ' change_count, created_at) VALUES (?,?,?,?,?,?,?)',
        (str(uuid.uuid4()), user_id, None, cursor, next_cursor, len(changes), now))

    return {
        'cursor': cursor,
        'next_cursor': next_cursor,
        'has_more': has_more,
        'count': len(changes),
        'server_time': now,
        'changes': changes,
    }


def snapshot(user_id, entity_types=None):
    """首次同步（全新设备）时的一次性基线。

    pull 从 0 开始会回放全部历史变更；对全新设备这很浪费，因此提供 snapshot：
    返回当前全量状态（含墓碑行，让新设备也知道哪些 id 已删除）+ 当前游标，
    客户端之后从这个游标继续增量拉取即可。
    """
    types = entity_types or list(SYNCABLE)
    if isinstance(types, str):
        types = [t for t in types.split(',') if t]
    unknown = [t for t in types if t not in SYNCABLE]
    if unknown:
        raise SyncError('未知 entity_type：%s' % ','.join(unknown))

    entities = {}
    for t in types:
        table = SYNCABLE[t]['table']
        rows = db.query('SELECT * FROM %s WHERE user_id = ? ORDER BY updated_at' % table,
                        (user_id,))
        entities[t] = rows

    cursor = db.query_one('SELECT COALESCE(MAX(seq), 0) AS c FROM sync_changelog')['c']
    return {
        'cursor': cursor,
        'server_time': db.utcnow(),
        'entity_types': types,
        'counts': {k: len(v) for k, v in entities.items()},
        'entities': entities,
    }
