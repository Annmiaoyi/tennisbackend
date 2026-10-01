# -*- coding: utf-8 -*-
"""标注后端 API —— 统一出口。

标注能力原本跑在独立进程（miniprogram-3 的 :8000），现已并入 :8787：
    · `server/annotation/api.py`       标注工作台的读写接口（会话 / 波形 / 标注 / 导出）
    · `server/annotation/prod_api.py`  微信登录与令牌（**身份层**，2026-09-29 起只做登录）
    · 工作台页面改为 `pages.py` 的 `/annotation`（Jinja 渲染，套 AceMate 外壳）

⚠️ 终端用户的会话读写（`/api/prod/*`）**不在这里** —— 已迁到
`server/routers/user_api.py`，改读 L2 分析库（原先读标注库的整包 JSON 原文，
终端用户因此拿不到任何分析）。本模块只保留 `/api/sessions*`（标注）
与 `/api/wechat/*`（登录）。

这里把它们并成一个 router 交给 app.py 注册，再加一个给后台页面用的聚合接口。
"""
from fastapi import APIRouter

from server.annotation import api as annotation_api
from server.annotation import prod_api
from server.annotation import stats

router = APIRouter()


@router.get('/api/annotation/overview', tags=['annotation'])
def annotation_overview():
    """标注链路总览：会话 / 标注进度 / 待纠错 / 难例比例。

    供「系统与硬件设置」页面的数据采集与标注区块与外部监控使用。
    """
    return {
        'overview': stats.overview(),
        'sessions': stats.session_rows(limit=50),
    }


# 顺序注意：/api/sessions 等静态段路径要在 /api/sessions/{sid} 之前被登记，
# 两个子路由各自内部已保证顺序，这里只需保证 prod 与 annotation 互不冲突
# （前者前缀 /api/prod 与 /api/wechat，后者 /api/sessions）。
router.include_router(annotation_api.router)
router.include_router(prod_api.router)
