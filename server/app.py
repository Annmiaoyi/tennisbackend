# -*- coding: utf-8 -*-
"""AceMate 后端管理站 —— FastAPI 应用入口。

启动：
    python -m server.app                 # 等价于 uvicorn server.app:app --port 8787
    python -m server.app --port 9000
或：
    uvicorn server.app:app --reload --port 8787

路由分区：
    /                     管理站页面（5 个设计稿页面 + 设置页 + 标注工作台）
    /annotation           数据采集与标注工作台（原独立 :8000 服务，已并入本站）
    /api/platform/*       概览看板数据
    /api/students/*       学员与用户档案
    /api/training/*       训练记录与横向对比
    /api/personas/*       用户画像与分群
    /api/feedback/*       用户建议与工单
    /api/sync/*           离线优先同步协议（push / pull / snapshot）
    /api/ingest/*         Apple Watch / NetPulse 采集端接入（分析侧）
    /api/raw/*            L0 原始数据层：只追加、按内容寻址、可重放
    /api/sessions/*       标注工作台：会话 / 波形 / 标注 / 导出（采集训练侧）
    /api/prod/*           终端用户（iOS App ↔ 小程序）：历史 / 逐拍 / 纵向分析 / 排行榜
    /api/wechat/*         微信登录与令牌
    /docs                 OpenAPI 交互文档

数据分层（自 2026-09-29 起）：
    L0 原始数据层   var/raw/acemate_raw.db        只追加、sha256 幂等、可重放（server/rawstore.py）
    L1 标注层       var/annotation/annotations.db 逐拍真值标签，raw_id 指向 L0（server/annotation/db.py）
    L2 识别·分析层  var/acemate.db                结构化逐拍 + 场次聚合，可重算（server/db.py）
    L3 终端展示层   /api/prod/*                   只读查询 L2，复用 server/analytics.py
"""
import argparse
import os
import sys
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# 允许 `python server/app.py` 直接跑（把仓库根加进 sys.path）
if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import adminauth, annotation, db, rawstore, security    # noqa: E402
from server.routers import (admin_api, annotation_api, data_api,    # noqa: E402
                            ingest_api, pages, raw_api, sync_api,
                            user_api)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, 'web')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动即建库，保证首次运行不会因为缺表而 500。

    四件事：分析库（L2）、原始库（L0）、标注库（L1）、管理员账号。
    """
    db.init_db()
    rawstore.init_db()
    annotation.init()
    if adminauth.init(print_password=True):
        print('[adminauth] 已创建默认管理员账号 —— 请用上面的口令登录 /login。')
    print(security.warn_if_open())
    yield


app = FastAPI(
    title='AceMate 后端管理站 API',
    description='网脉 AceMate 网球训练监测系统的后端管理与数据同步服务',
    version='1.0.0',
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# 登录守卫
# --------------------------------------------------------------------------- #
# 永远公开：登录页本身、静态资源、以及**自带鉴权**的终端用户与微信接口
# （前者是给浏览器登录用的；后两者各自有 Bearer token / Ingest-Key 体系）
ALWAYS_PUBLIC = (
    '/assets/', '/login', '/logout', '/favicon.ico',
    '/api/prod/', '/api/wechat/',          # 终端用户：Bearer token 鉴权
    '/docs', '/openapi.json', '/redoc',    # OpenAPI 文档
)

# 设备口：由采集端（手表 / 手机）调用，它没有浏览器登录态。
# 放行规则 = 管理后台登录态 **或** `X-Ingest-Key`（未配置口令时开发期放行，
# 启动会打印告警；生产必须配置，见 server/security.py）
DEVICE_GATED = ('/api/ingest/', '/api/raw/')


def _is_always_public(path):
    return any(path == p or path.startswith(p) for p in ALWAYS_PUBLIC)


def _is_device_gated(path):
    return any(path.startswith(p) for p in DEVICE_GATED)


def _wants_html(request):
    """决定未登录时给「跳登录页」还是「401 JSON」。

    判据是**路径**而非 `Accept` 头：浏览器发 `Accept: text/html` 好判断，
    但 curl / urllib / 各种 SDK 常常不带 Accept —— 若按 Accept 判断，
    一个未登录的页面请求会拿到 401 JSON 而不是跳转，体验很怪。
    只有 `/api/*` 一律给 JSON（调用方需要可编程处理）。
    """
    return not request.url.path.startswith('/api/')


@app.middleware('http')
async def admin_guard(request, call_next):
    """未登录不放行管理后台。页面跳登录页，接口返回 401（前端可据此判断）。"""
    path = request.url.path
    if _is_always_public(path):
        return await call_next(request)
    if _is_device_gated(path):
        if security.ingest_key_ok(request.headers.get('x-ingest-key')) \
                or adminauth.session_valid(request):
            return await call_next(request)
    elif adminauth.session_valid(request):
        return await call_next(request)

    if _wants_html(request):
        return RedirectResponse('/login?next=' + quote(path), status_code=302)
    return JSONResponse(status_code=401, content={
        'error': 'unauthenticated',
        'detail': '需要管理后台登录态：请先 POST /login 取得会话 Cookie',
        'login_url': '/login',
    })


@app.exception_handler(Exception)
async def unhandled(request, exc):                       # pragma: no cover
    return JSONResponse(status_code=500,
                        content={'error': 'internal_error', 'detail': str(exc)})


# 静态资源：编译好的 CSS、本地字体、本地图片、前端 JS
app.mount('/assets', StaticFiles(directory=os.path.join(WEB, 'assets')), name='assets')

# 页面路由必须在最后挂载的 StaticFiles 之前注册（这里 pages 用显式路由，见下）
app.include_router(admin_api.router)      # 登录/登出（自带公开白名单）
app.include_router(pages.router)          # 管理台页面（受守卫保护）
app.include_router(data_api.router)
app.include_router(sync_api.router)
app.include_router(ingest_api.router)     # 采集端接入（设备口）
app.include_router(raw_api.router)        # L0 原始数据层（设备口 + 管理员）
app.include_router(user_api.router)       # L3 终端用户：历史 / 逐拍 / 分析 / 排行榜
# 标注工作台 + 微信登录/会话同步（原 :8000 服务，已并入本站）
app.include_router(annotation_api.router)


def main():
    import uvicorn
    ap = argparse.ArgumentParser(description='AceMate 后端管理站')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--reload', action='store_true')
    ap.add_argument('--seed', action='store_true', help='启动前重建并灌入演示数据')
    args = ap.parse_args()

    if args.seed:
        from server import seed
        seed.run(force=True)

    print('AceMate 后端管理站  ->  http://%s:%d' % (args.host, args.port))
    print('数据库：%s' % db.db_path())
    uvicorn.run('server.app:app', host=args.host, port=args.port, reload=args.reload,
                log_level='info')


if __name__ == '__main__':
    main()
