# -*- coding: utf-8 -*-
"""security.py —— 采集口令与「采集或管理员」双通道放行。

采集端（Apple Watch / iPhone）走的是**设备级共享口令** `NETPULSE_INGEST_KEY`，
因为它不掌握管理后台的登录态，也不掌握小程序 wx.login 的 code。

    · 未配置 `NETPULSE_INGEST_KEY` 时放行（开发期），启动时会打印告警。
    · 配置后，写接口必须带 `X-Ingest-Key: <同值>`。

管理后台的登录态见 `server/adminauth.py`。某些接口（如 L0 原始层）两者皆可
——管理员在工作台上传样本、设备在球场自动上传，都走同一个入口。
"""
import os
from typing import Optional

from fastapi import Header, HTTPException, Request

INGEST_KEY = os.environ.get('NETPULSE_INGEST_KEY', '').strip()


def ingest_key_ok(value):
    """校验 `X-Ingest-Key`。未配置口令时一律通过（开发期）。"""
    if not INGEST_KEY:
        return True
    return (value or '').strip() == INGEST_KEY


def require_ingest_key(x_ingest_key=None):
    if not ingest_key_ok(x_ingest_key):
        raise HTTPException(401, 'X-Ingest-Key 缺失或不正确')


def require_ingest_header(x_ingest_key: str = Header(None)):
    """FastAPI 依赖版：直接放进 `dependencies=[...]`。"""
    require_ingest_key(x_ingest_key)


def device_or_admin_ok(request: Request, x_ingest_key: Optional[str] = None):
    """L0 原始层的放行规则：设备口令 **或** 管理后台登录态，二选一。

    为什么两者皆可：同一次采集既可能由球场上的手表自动上传（无登录态），
    也可能由管理员在「数据采集与标注」工作台手工补传（有登录态）。
    """
    if ingest_key_ok(x_ingest_key):
        return True
    try:
        from . import adminauth
    except ImportError:                                    # pragma: no cover
        return False
    return adminauth.session_valid(request)


def require_device_or_admin(request: Request,
                            x_ingest_key: Optional[str] = Header(None)):
    """⚠️ 这两个注解（`request: Request` 与 `Header(None)`）都是**功能必需**的，
    不是写着好看：

      · 若 `request` 漏了类型注解，FastAPI 会把它当成一个**必填的 query 参数**
        `?request=`，于是挂上 `Depends(require_device_or_admin)` 的每个端点
        一律 422「Field required」—— 接口全废，而单元测试（直接函数调用）却看不出。
      · 若在函数体里手工调用本函数（不经 `Depends`），`x_ingest_key` 拿到的是
        `Header(None)` 这个**参数对象**而不是字符串，配上非空 `NETPULSE_INGEST_KEY`
        时 `(value or '').strip()` 直接 AttributeError → 500
        （未配口令时恰好被 `ingest_key_ok` 的提前 return 掩盖，
          属于「开发期看不出、一上线就炸」的坑）。

    正确用法只有一个：
        APIRouter(..., dependencies=[Depends(security.require_device_or_admin)])
        或 @router.get(..., dependencies=[Depends(security.require_device_or_admin)])
    """
    if not device_or_admin_ok(request, x_ingest_key):
        raise HTTPException(
            401, '需要 X-Ingest-Key 口令或管理后台登录态（先在 /login 登录）')


def read_ok(request: Request, x_ingest_key: Optional[str] = None):
    """**读**原始数据的放行规则，比写口更严一档。

    写口在「未配置口令」时放开（开发期方便调采集端），但读口不能跟着放开 ——
    L0 的读接口吐出的是**整包原始运动数据**（含 openid、逐样本波形），
    只要有人能碰到端口就能全量拖走。而采集端**从来不需要读**（它只上传），
    所以读口没有任何理由跟着裸奔。

    规则：
      · 配了口令 → 口令正确 **或** 管理后台登录态，任一即可
        （管理员在工作台看波形、运维脚本用口令直取 payload，都是正当用法）
      · 未配口令 → 只认管理后台登录态
    """
    if INGEST_KEY and ingest_key_ok(x_ingest_key):
        return True
    try:
        from . import adminauth
    except ImportError:                                    # pragma: no cover
        return False
    return adminauth.session_valid(request)


def require_read_access(request: Request,
                        x_ingest_key: Optional[str] = Header(None)):
    """读 L0 的依赖版。理由见 `read_ok`（同样必须走 `Depends`，注解不可省）。"""
    if not read_ok(request, x_ingest_key):
        raise HTTPException(
            401, '读取原始数据需要管理后台登录态（先在 /login 登录）；'
                 '若已配置 NETPULSE_INGEST_KEY，也可用 X-Ingest-Key 直取')


def warn_if_open():
    """启动自检：口令未配 = 采集**写**口敞着，控制台明确提示。

    读口不受此影响 —— 未配口令时读口仍要求管理后台登录态（见 `read_ok`）。
    """
    if not INGEST_KEY:
        return ('[security] ⚠️  未配置 NETPULSE_INGEST_KEY —— 采集**上传**口当前无口令保护'
                '（原始数据的**读取**口仍要求管理后台登录态）。生产部署前务必设置。')
    return '[security] 采集上传口已启用 X-Ingest-Key 校验。'
