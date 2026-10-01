# -*- coding: utf-8 -*-
"""L0 原始数据层接口（`server/rawstore.py` 的 HTTP 出口）。

    POST /api/raw/sessions                  multipart 上传（设备 / 工作台手工补传）
    POST /api/raw/sessions/json             JSON 上传（会话已在内存里，无需落盘再传）
    GET  /api/raw/sessions                  版本列表（不含正文）
    GET  /api/raw/sessions/{sid}            某会话最新版本元数据
    GET  /api/raw/sessions/{sid}/revisions  某会话全部版本（只追加，可回溯）
    GET  /api/raw/sessions/{sid}/payload    某会话最新版本整包 JSON
    GET  /api/raw/stats                     原始层水位

**放行规则**（见 `server/security.py`）：**写口与读口不同档**。

  · 写口（两个 POST）：`X-Ingest-Key` 口令 **或** 管理后台登录态，二选一；
    未配置口令时放开（开发期方便采集端联调，启动日志会告警）。
  · 读口（五个 GET）：**未配置口令时只认管理后台登录态** ——
    读接口吐出的是整包原始运动数据（含 openid 与逐样本波形），
    而采集端从不读，所以没有理由跟着写口一起裸奔。

⚠️ 为什么用 `Depends` 而不是在函数体里手写 `security.require_device_or_admin(request)`：
`require_device_or_admin` 的第二个参数默认值是 `Header(None)` 这个**参数对象**，
只有在 FastAPI 依赖注入时才会被解析成真实请求头。若手工调用且不传该参数，
`x_ingest_key` 会是一个 `Header` 实例，配上非空 `NETPULSE_INGEST_KEY` 时
`(value or '').strip()` 直接 `AttributeError` → 500（未配口令时恰好被
`ingest_key_ok` 的提前 return 掩盖，属于「开发期看不出、一上线就炸」的坑）。
同理，`request: Request` 的注解也不能省，否则会被当成必填 query 参数 `?request=`，
整个 router 的端点全变 422。
"""
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile)
from fastapi.responses import JSONResponse

from server import rawstore, security

router = APIRouter(prefix='/api/raw', tags=['原始数据层 L0'])

# 写口：设备口令 或 管理登录态，二选一
WRITE = [Depends(security.require_device_or_admin)]
# 读口：更严一档（未配口令时只认登录态），理由见上面 docstring
READ = [Depends(security.require_read_access)]


@router.post('/sessions', summary='上传一份原始载荷（multipart）', dependencies=WRITE)
async def upload_raw(
    id: str = Form(..., description='采集端会话 ID（跨端幂等键）'),
    raw: UploadFile = File(..., description='raw_*.json 原始文件'),
    openid: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    device_id: Optional[str] = Form(None),
    shape: Optional[str] = Form(None, description='raw_package（默认）| match_session'),
):
    """把 `raw_*.json` 原样封存进 L0。

    **幂等**：同一形态下、同一份字节重复上传命中 `UNIQUE(session_id, shape, sha256)`，
    返回 `status=duplicate` 且不产生新行；同一会话同一形态上传了**不同字节**
    则追加为 `revision+1`，历史版本永远可查（原始层不可覆盖、不可删除）。

    两种形态互不干扰：`raw_package`（含 samples 波形）与 `match_session`
    （仅会话+逐拍结论）各自记版本号。
    """
    data = await raw.read()
    try:
        result = rawstore.archive(
            data, session_id=id, shape=shape or rawstore.SHAPE_RAW_PACKAGE,
            openid=openid, source=source,
            device_id=device_id, filename=raw.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@router.post('/sessions/json', summary='上传一份原始载荷（JSON 体内联）', dependencies=WRITE)
def upload_raw_json(body: dict):
    """体：`{ id, session | payload, openid?, source?, device_id?, shape? }`

    采集端若已经把整包放在内存里，直接内联上传，省一次落盘再读。
    """
    sid = (body or {}).get('id') or (body or {}).get('session_id')
    payload = (body or {}).get('session')
    if payload is None:
        payload = (body or {}).get('payload')
    if not sid or payload is None:
        raise HTTPException(400, 'id 与 session（或 payload）必填')
    try:
        return rawstore.archive(
            payload, session_id=sid,
            shape=(body or {}).get('shape') or rawstore.SHAPE_RAW_PACKAGE,
            openid=(body or {}).get('openid'),
            source=(body or {}).get('source'),
            device_id=(body or {}).get('device_id'))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get('/sessions', summary='原始载荷版本列表', dependencies=READ)
def list_raw(limit: int = 50, openid: Optional[str] = None,
             source: Optional[str] = None, shape: Optional[str] = None):
    limit = max(1, min(int(limit), 500))
    rows = rawstore.list_recent(limit=limit, openid=openid, source=source, shape=shape)
    return {'count': len(rows), 'revisions': rows}


@router.get('/stats', summary='原始层水位', dependencies=READ)
def raw_stats():
    return rawstore.stats()


@router.get('/sessions/{sid}', summary='某会话最新版本元数据', dependencies=READ)
def get_raw(sid: str, shape: Optional[str] = None):
    row = rawstore.latest(sid, shape=shape)
    if not row:
        raise HTTPException(404, '原始层没有该会话')
    return row


@router.get('/sessions/{sid}/revisions', summary='某会话全部版本', dependencies=READ)
def get_revisions(sid: str, shape: Optional[str] = None):
    rows = rawstore.revisions(sid, shape=shape)
    if not rows:
        raise HTTPException(404, '原始层没有该会话')
    return {'session_id': sid, 'count': len(rows), 'revisions': rows}


@router.get('/sessions/{sid}/payload', summary='某会话最新版本整包 JSON', dependencies=READ)
def get_payload(sid: str, revision: Optional[int] = None,
                shape: Optional[str] = None):
    obj = rawstore.payload_of(sid, revision, shape=shape)
    if obj is None:
        raise HTTPException(404, '原始层没有该会话内容，或正文无法解析为 JSON')
    return JSONResponse(obj)
