# -*- coding: utf-8 -*-
"""同步协议接口（离线优先 + 后端权威 + 最终一致）。

    POST /api/sync/push       提交本地待同步队列（幂等，可重试）
    GET  /api/sync/pull       按服务器游标增量拉取
    GET  /api/sync/snapshot   全新设备的一次性基线
    POST /api/sync/ack        客户端确认已落库（可选，用于清理服务端队列）
    GET  /api/sync/status     查询某用户/设备的同步水位与统计

用户识别：优先 `X-User-Id` 请求头，其次 `?user_id=`，最后用默认演示用户。
设备识别：`X-Device-Id`（可空）。
"""
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import JSONResponse

from server import db, sync

router = APIRouter(prefix='/api/sync', tags=['同步协议'])

DEFAULT_USER = 'u_demo'


def resolve_user(x_user_id: Optional[str], user_id: Optional[str]) -> str:
    return (x_user_id or user_id or DEFAULT_USER).strip() or DEFAULT_USER


@router.post('/push', summary='推送本地待同步操作（幂等）')
def push(
    operations: List[dict] = Body(..., description='待同步操作数组'),
    x_user_id: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """**幂等**：每个操作带唯一 `operation_id`，重复推送任意多次结果一致。

    冲突策略：LWW，比较 `client_updated_at` 与服务器行 `updated_at`，
    严格大于才覆盖。落败的操作返回 `conflict_lost` 并附带服务端当前版本，
    客户端据此刷新本地缓存。

    返回体里的 `cursor` 是**本次写入后服务端的最新游标**，客户端可直接把它
    作为下次增量拉取的起点（省掉一次往返）。
    """
    try:
        result = sync.push(resolve_user(x_user_id, user_id), operations, x_device_id)
    except sync.SyncError as e:
        return JSONResponse(status_code=400, content={'error': 'bad_request', 'detail': str(e)})
    return result


@router.get('/pull', summary='按服务器游标增量拉取')
def pull(
    cursor: int = Query(0, description='上次拿到的 next_cursor，首次传 0'),
    limit: int = Query(200, ge=1, le=1000),
    entity_types: Optional[str] = Query(None, description='逗号分隔，缺省为全部实体'),
    x_user_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """**只依赖服务器游标**，不使用客户端时间（客户端时钟可能被改、可能回拨）。

    每条 change 携带实体**完整快照**，客户端可无序 / 重复应用；
    配合 `deleted_at` 处理软删除，配合 `version` 做幂等。
    """
    types = [t for t in (entity_types.split(',') if entity_types else []) if t]
    try:
        return sync.pull(resolve_user(x_user_id, user_id), cursor, limit, types or None)
    except sync.SyncError as e:
        return JSONResponse(status_code=400, content={'error': 'bad_request', 'detail': str(e)})


@router.get('/snapshot', summary='全新设备的一次性全量基线')
def snapshot(
    entity_types: Optional[str] = Query(None),
    x_user_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """新设备首次同步时用，避免从 seq=0 回放全部历史变更。

    返回当前全量状态（**含墓碑行**，因此新设备也能得知哪些 id 已删除）
    以及当前游标；之后从该游标继续增量拉取即可，不会漏也不会重。
    """
    types = [t for t in (entity_types.split(',') if entity_types else []) if t]
    try:
        return sync.snapshot(resolve_user(x_user_id, user_id), types or None)
    except sync.SyncError as e:
        return JSONResponse(status_code=400, content={'error': 'bad_request', 'detail': str(e)})


@router.post('/ack', summary='客户端确认已落本地（清理服务端待同步水位）')
def ack(
    payload: dict = Body(default={}),
    x_user_id: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """同步是「客户端拉→客户端确认」的两段式语义。

    服务端不保存待确认队列（changelog 是只增的、无状态的），
    这里只把确认水位记录到审计表，便于排查「某设备卡在哪个游标」。
    """
    uid = resolve_user(x_user_id, user_id)
    cursor = int(payload.get('cursor') or 0)
    now = db.utcnow()
    import uuid
    db.execute(
        'INSERT INTO sync_pulls (id, user_id, device_id, from_cursor, to_cursor,'
        ' change_count, created_at) VALUES (?,?,?,?,?,?,?)',
        (str(uuid.uuid4()), uid, x_device_id or payload.get('device_id'), cursor, cursor,
         0, now))
    return {'ok': True, 'ack_cursor': cursor, 'server_time': now}


@router.get('/status', summary='同步水位与统计')
def status(
    x_user_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    uid = resolve_user(x_user_id, user_id)
    head = db.query_one('SELECT COALESCE(MAX(seq), 0) AS c FROM sync_changelog')
    ops = db.query_one(
        'SELECT COUNT(*) AS total,'
        ' SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS applied,'
        ' SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS duplicate,'
        ' SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS conflict_lost,'
        ' SUM(CASE WHEN result = ? THEN 1 ELSE 0 END) AS rejected'
        ' FROM sync_operations WHERE user_id = ?',
        ('applied', 'duplicate', 'conflict_lost', 'rejected', uid)) or {}
    counts = {}
    for name, spec in sync.SYNCABLE.items():
        row = db.query_one('SELECT COUNT(*) AS n FROM %s WHERE user_id = ? AND deleted_at IS NULL'
                           % spec['table'], (uid,))
        tomb = db.query_one('SELECT COUNT(*) AS n FROM %s WHERE user_id = ? AND deleted_at IS NOT NULL'
                            % spec['table'], (uid,))
        counts[name] = {'live': row['n'] if row else 0, 'tombstone': tomb['n'] if tomb else 0}
    recent = db.query(
        'SELECT * FROM sync_pulls WHERE user_id = ? ORDER BY created_at DESC LIMIT 5', (uid,))
    return {
        'user_id': uid,
        'server_cursor': head['c'] if head else 0,
        'server_time': db.utcnow(),
        'operations': {k: (v or 0) for k, v in (ops or {}).items()},
        'entities': counts,
        'recent_pulls': recent,
        'entity_types': sorted(sync.SYNCABLE),
    }
