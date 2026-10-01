# -*- coding: utf-8 -*-
"""采集端接入接口（Apple Watch / NetPulse → AceMate 后端）。

    POST /api/ingest/watch-session    上传一场采集端训练会话（幂等）
    GET  /api/ingest/sessions         列出已接入的采集端会话，便于核对

用户识别与同步协议保持一致：优先 `X-User-Id` 请求头，其次 `?user_id=`，
最后回落到默认演示用户。设备识别用 `X-Device-Id`。

这一层只是**入口**：真正的落库由 `server/ingest.py` 翻译成 sync operation
后走 `sync.push`，因此幂等 / 变更日志 / LWW / 软删除语义与 App 同步完全一致。
"""
from typing import Optional

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse

from server import db, ingest

router = APIRouter(prefix='/api/ingest', tags=['采集端接入'])

DEFAULT_USER = 'u_demo'


def _resolve_user(x_user_id: Optional[str], user_id: Optional[str]) -> str:
    return (x_user_id or user_id or DEFAULT_USER).strip() or DEFAULT_USER


@router.post('/watch-session', summary='接入一场 Apple Watch 训练会话（幂等）')
def watch_session(
    payload: dict = Body(..., description='{ session, student_id?, include_strokes?, force? }'),
    x_user_id: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """把采集端的整包会话翻译成结构化数据落库。

    **幂等**：`operation_id` 由 `session.id` 派生且不含时间戳，因此同一场
    训练重复上传任意多次，第二次起均返回 `duplicate` 且不改库。

    `force=true` 用于覆盖已入库的场次（如补传了卡路里）：此时 op_id 带时间戳
    并改用服务器时间做版本戳，由 LWW 裁决。

    请求体：
    ```json
    {
      "session": {
        "id": "A1B2C3D4-...", 
        "startedAt": "2026-09-28T10:00:00Z",
        "endedAt":   "2026-09-28T11:05:00Z",
        "wrist": "right",
        "duration": 3900,
        "avgHeartRate": 139, "maxHeartRate": 173,
        "activeCalories": 514, "distanceKm": 2.31,
        "swings": [
          {"type": "forehand", "impactTime": 12.34,
           "racketHeadSpeedKmh": 128.4, "confidence": 0.91}
        ]
      },
      "student_id": "stu-00001001",
      "include_strokes": true
    }
    ```
    注意 `session.swings[].type` 只接受 forehand / backhand / serve /
    slice / volley / smash；`unknown`（置信度不足）不计入分项与逐拍明细，
    但仍计入 `stroke_count`。
    """
    uid = _resolve_user(x_user_id, user_id)
    session = (payload or {}).get('session')
    if not isinstance(session, dict):
        return JSONResponse(status_code=400, content={
            'error': 'bad_request', 'detail': '缺少 session 对象'})

    student_id = ingest.resolve_student_id(uid, (payload or {}).get('student_id'))
    if not student_id:
        return JSONResponse(status_code=400, content={
            'error': 'bad_request',
            'detail': '找不到归属学员：请显式传 student_id，或先为该用户建立学员档案'})

    body = payload or {}
    force = bool(body.get('force', False))
    try:
        result = ingest.ingest_session(
            uid, session,
            student_id=student_id,
            device_id=x_device_id,
            source=body.get('source') or ingest.DEFAULT_SOURCE,
            include_strokes=bool(body.get('include_strokes', True)),
            force=force,
            title=body.get('title'),
            location=body.get('location'),
            court_type=body.get('court_type'),
            session_type=body.get('session_type'),
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={
            'error': 'bad_request', 'detail': str(e)})

    applied = result['applied']
    dup = result['duplicate']
    existed = result.get('existed', False)
    if applied and not dup:
        # force 覆盖已入库的场次 → overwritten；否则是首次创建
        verdict = 'overwritten' if (existed and force) else 'created'
    elif dup and not applied:
        verdict = 'duplicate'
    elif applied and dup:
        verdict = 'partial'
    else:
        verdict = 'rejected'
    result['verdict'] = verdict
    result['student_id'] = student_id
    return result


@router.get('/sessions', summary='列出已接入的采集端会话')
def list_ingested(
    source: str = Query(ingest.DEFAULT_SOURCE, description='数据来源端标记'),
    limit: int = Query(50, ge=1, le=500),
    x_user_id: Optional[str] = Header(None),
    user_id: Optional[str] = Query(None),
):
    """按会话粒度回看采集端数据是否完整落库（含配平校验结果）。"""
    uid = _resolve_user(x_user_id, user_id)
    rows = db.query(
        'SELECT s.id, s.external_id, s.student_id, s.title, s.session_type,'
        ' s.started_at, s.duration_sec, s.stroke_count,'
        ' s.forehand_count, s.backhand_count, s.serve_count,'
        ' s.slice_count, s.volley_count, s.smash_count,'
        ' s.peak_speed_kmh, s.serve_peak_kmh, s.avg_hr, s.max_hr,'
        ' s.calories_kcal, s.distance_km, s.worn_wrist,'
        ' (SELECT COUNT(*) FROM stroke_records r'
        '   WHERE r.session_id = s.id AND r.deleted_at IS NULL) AS stroke_rows'
        ' FROM training_sessions s'
        ' WHERE s.user_id = ? AND s.source = ? AND s.deleted_at IS NULL'
        ' ORDER BY s.started_at DESC LIMIT ?', (uid, source, limit))

    out = []
    for r in rows:
        parts = [r[k] for k in ('forehand_count', 'backhand_count', 'serve_count',
                                'slice_count', 'volley_count', 'smash_count')]
        known = [p for p in parts if p is not None]
        total = sum(known) if known else None
        declared = r['stroke_count']
        # G2 配平门禁：六类之和 **小于等于** 检测总数，差额即未识别（unknown）拍数。
        # 阈值取 5%：真实场景下 2~3% 的拍因置信度不足归 unknown 属正常波动
        # （对应 G3 门限 0.60），超过 5% 才说明本场分类器整体失灵。
        # 分项和超过总数（unidentified < 0）则说明分类计数串味，同样判 suspect。
        balanced = None
        unidentified = None
        if total is not None and declared:
            unidentified = declared - total
            balanced = 0 <= unidentified <= max(3, declared * 0.05)
        item = dict(r)
        item['parts_total'] = total
        item['unidentified'] = unidentified
        item['balanced'] = balanced
        out.append(item)
    return {'source': source, 'count': len(out), 'sessions': out}
