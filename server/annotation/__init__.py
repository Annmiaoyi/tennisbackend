# -*- coding: utf-8 -*-
"""标注后端（原 miniprogram-3/backend/annotation-backend）—— 已并入 AceMate 后端。

职责：接收 Apple Watch 上传的 raw_*.json + 视频，提供「视频 ↔ 传感器波形同步标注」，
标注结果导出为标准 annotation.json / 38 维 dataset.csv，供 CoreML 训练管线消费。

整合要点：
  · 数据落在 `var/annotation/`（annotations.db + raw/ + video/），与训练分析库
    `var/acemate.db` **分库并存** —— 标注属「采集 / 训练侧」数据，不混进分析表，
    避免污染后台的同步协议（LWW / changelog / 墓碑）。
  · 路由统一挂在 :8787，原 :8000 退役；**接口路径与契约保持不变**，客户端零改动。
  · 工作台页面改用 AceMate 设计系统，见 `templates/pages/annotation.html`。

对外只需两件事：
  · `init()`                               建表，由 app.py 的 lifespan 调用
  · `server.routers.annotation_api.router` API 路由，由 app.py 注册
"""


def init():
    """建表（幂等）。开库、建表、关库，失败向上抛 —— 启动即暴露问题。"""
    from . import db, prod_api
    conn = db.get_conn()
    try:
        db.init_db(conn)
        prod_api.init_prod_db(conn)
    finally:
        conn.close()
