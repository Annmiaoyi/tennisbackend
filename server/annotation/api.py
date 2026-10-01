# -*- coding: utf-8 -*-
"""api.py — 标注后端的 API 路由（原 annotation-backend/server.py 的全部接口）。

**接口路径与契约完全保持原样**，客户端（iOS App / 微信小程序 / 标注工作台）零改动：

    POST   /api/sessions                              上传 raw_*.json（+ 可选视频）→ 封存 L0 + 会话 + 自动预标注
    GET    /api/sessions                              会话列表（含标注数 / 有无视频）
    GET    /api/sessions/{sid}                        单会话元数据
    GET    /api/sessions/{sid}/samples                降采样波形（供 Canvas 绘制）
    GET    /api/sessions/{sid}/video                  原始视频
    GET    /api/sessions/{sid}/annotations            该会话全部标注
    PUT    /api/sessions/{sid}/annotations            保存本轮（按 annotator 维度替换）
    GET    /api/sessions/{sid}/consistency            单会话一致性
    GET    /api/consistency                           全局一致性
    GET    /api/export/annotation.json                全量导出
    GET    /api/sessions/{sid}/export/annotation.json 单会话导出
    GET    /api/export/dataset.csv                    38 维训练集

**原始文件不再由本层保管**（2026-09-29 起）：上传的字节先封存进 L0 原始层
（`server/rawstore.py`，只追加、按内容寻址），本层只保存 `raw_id` 指针。
波形读取一律从 L0 取正文，`raw_path` 退化为路径缓存 —— 这样「原始数据」
全局只有一份，任何一版识别算法都能拿它重算。

与微信登录 / 会话同步相关的接口在 `prod_api.py`（同为 :8787 下的子路由）。

**页面路由 `/` 不再由本模块提供** —— 标注工作台改由 pages.py 渲染 Jinja 模板并套用
AceMate 外壳（见 templates/pages/annotation.html），本模块只管数据接口。
"""
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .converter import MATCH_TOL, build_dataset, compute_consistency, export_annotation_json
from .db import DATA_DIR, VIDEO_DIR, get_conn
from .schemas import AnnotationSave, SessionMeta
from .. import rawstore

router = APIRouter()


def _utcnow_iso() -> str:
    """UTC 时间戳，**不带时区后缀**。

    与原先 `datetime.utcnow().isoformat()` 的输出格式完全一致 —— 库里已有数据是
    这种 naive 形式，换格式会让新旧记录无法直接字符串比较。`utcnow()` 在 3.12 已废弃，
    故改用 `now(timezone.utc)` 再剥掉 tzinfo。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _session_row(conn, sid):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()


def _anno_count(conn, sid):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM annotations WHERE session_id=?", (sid,)
    ).fetchone()["c"]


@router.post("/api/sessions")
async def create_session(
    id: str = Form(...),
    wrist: str = Form("right"),
    player_id: str = Form(None),
    raw: UploadFile = File(...),
    video: UploadFile = File(None),
):
    if not id:
        raise HTTPException(400, "session id required")

    # 1) 先封存进 L0 原始层（只追加、按内容寻址、sha256 幂等）。
    #    标注层从此只保存一个 `raw_id` 指针，不再自己保管原始文件 ——
    #    「原始数据」始终只有一份，重算识别结果时以它为准。
    data = await raw.read()
    try:
        archived = rawstore.archive(data, session_id=id,
                                    source="annotation_workbench",
                                    filename=raw.filename)
    except ValueError as e:
        raise HTTPException(400, str(e))
    raw_id = archived["raw_id"]
    raw_path = archived["file_path"]

    video_path = None
    if video:
        ext = os.path.splitext(video.filename or "mp4")[1] or ".mp4"
        video_path = os.path.join(VIDEO_DIR, f"{id}{ext}")
        with open(video_path, "wb") as f:
            f.write(await video.read())

    # 整个请求用同一连接：先写会话(满足外键) + 预标注，一次提交，避免 WAL 锁竞争
    conn = get_conn()
    try:
        # 解析 raw json：会话元数据 + Watch 启发式 swings（用于预标注）。
        # 直接从**内存字节**解析，不再回读文件 —— 既少一次 IO，也避免
        # 「落盘后再读」之间文件被换掉的可能。
        started_at = ended_at = None
        duration = None
        prefilled = 0
        try:
            raw_obj = json.loads(data.decode("utf-8"))
            meta = raw_obj.get("session", {})
            started_at = meta.get("startedAt")
            ended_at = meta.get("endedAt")
            duration = meta.get("duration")
            if meta.get("wrist"):
                wrist = meta["wrist"]
        except Exception:
            # 元数据解析失败不影响会话创建（原始字节已安全封存在 L0）
            meta = {}

        # 先插入会话行，再插预标注，满足外键约束
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (id, wrist, started_at, ended_at, duration, player_id,
                raw_id, raw_path, video_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (id, wrist, started_at, ended_at, duration, player_id,
             raw_id, raw_path, video_path, _utcnow_iso()),
        )

        # 预标注：Watch 启发式挥拍作为初始标注（source='auto', status='proposed'）
        try:
            swings = meta.get("swings", []) or []
            now = _utcnow_iso()
            for sw in swings:
                ty = sw.get("type")
                if ty in (None, "unknown"):
                    continue  # 不确定的启发式不预填，留给人工
                it = sw.get("impactTime", sw.get("impact_time"))
                if it is None:
                    continue
                conn.execute(
                    "INSERT INTO annotations(session_id, impact_time, label, confidence, "
                    "annotator, source, status, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (id, float(it), ty, sw.get("confidence"),
                     "heuristic", "auto", "proposed", now, now),
                )
                prefilled += 1
        except Exception:
            # 预标注失败不影响会话创建本身
            pass

        conn.commit()
    finally:
        conn.close()
    return {"id": id, "status": "created", "has_video": bool(video_path),
            "prefilled": prefilled, "raw_id": raw_id,
            "raw_revision": archived["revision"],
            "raw_deduped": archived["status"] == "duplicate"}


@router.get("/api/sessions", response_model=list[SessionMeta])
def list_sessions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        out.append(SessionMeta(
            id=r["id"], wrist=r["wrist"], started_at=r["started_at"],
            ended_at=r["ended_at"], duration=r["duration"], player_id=r["player_id"],
            raw_id=r["raw_id"],
            has_video=bool(r["video_path"]), annotation_count=_anno_count(conn, r["id"]),
        ))
    conn.close()
    return out


@router.get("/api/sessions/{sid}")
def get_session(sid: str):
    conn = get_conn()
    r = _session_row(conn, sid)
    if not r:
        conn.close()
        raise HTTPException(404, "session not found")
    meta = SessionMeta(
        id=r["id"], wrist=r["wrist"], started_at=r["started_at"],
        ended_at=r["ended_at"], duration=r["duration"], player_id=r["player_id"],
        raw_id=r["raw_id"],
        has_video=bool(r["video_path"]), annotation_count=_anno_count(conn, sid),
    )
    conn.close()
    return meta


@router.get("/api/sessions/{sid}/samples")
def get_samples(sid: str, downsample: int = Query(60, ge=10, le=400)):
    conn = get_conn()
    r = _session_row(conn, sid)
    conn.close()
    if not r:
        raise HTTPException(404, "session not found")

    # 正文一律从 L0 原始层取（`raw_id` 是权威指针，`raw_path` 只是路径缓存）。
    # 显式指定 shape='raw_package' —— 该形态才带 samples 波形；若不指定，
    # 同一会话若也被 App 以 match_session 形态上传过，可能取到没有波形的那一份。
    # 旧数据没有 raw_id 时回落到 raw_path，保证迁移期不中断。
    raw = rawstore.payload_of(sid, shape=rawstore.SHAPE_RAW_PACKAGE) if r["raw_id"] else None
    if raw is None:
        raw_path = r["raw_path"]
        if not raw_path or not os.path.exists(raw_path):
            raise HTTPException(404, "raw file missing")
        with open(raw_path) as f:
            raw = json.load(f)
    samples = raw.get("samples", [])
    if not samples:
        return {"count": 0, "duration": 0, "channels": []}
    approx_hz = 800
    step = max(1, round(approx_hz / downsample))
    picked = samples[::step]
    channels = [
        {"t": s["t"], "ax": s["ax"], "ay": s["ay"], "az": s["az"],
         "rx": s["rx"], "ry": s["ry"], "rz": s["rz"]}
        for s in picked
    ]
    duration = samples[-1]["t"] - samples[0]["t"]
    return {"count": len(channels), "duration": duration, "channels": channels}


@router.get("/api/sessions/{sid}/video")
def get_video(sid: str):
    conn = get_conn()
    r = _session_row(conn, sid)
    conn.close()
    if not r or not r["video_path"] or not os.path.exists(r["video_path"]):
        raise HTTPException(404, "video not found")
    return FileResponse(r["video_path"], media_type="video/mp4")


@router.get("/api/sessions/{sid}/annotations")
def get_annotations(sid: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT impact_time, label, confidence, note, annotator, source, status "
        "FROM annotations WHERE session_id=? ORDER BY impact_time",
        (sid,),
    ).fetchall()
    conn.close()
    return [
        {"impact_time": r["impact_time"], "label": r["label"],
         "confidence": r["confidence"], "note": r["note"],
         "annotator": r["annotator"], "source": r["source"], "status": r["status"]}
        for r in rows
    ]


@router.put("/api/sessions/{sid}/annotations")
def put_annotations(sid: str, payload: AnnotationSave):
    conn = get_conn()
    if not _session_row(conn, sid):
        conn.close()
        raise HTTPException(404, "session not found")

    ann = (payload.annotator or "human").strip() or "human"
    now = _utcnow_iso()

    # 1) 替换该标注者自己的上一轮（按 annotator 维度，不影响他人）
    conn.execute("DELETE FROM annotations WHERE session_id=? AND annotator=?",
                 (sid, ann))

    # 2) 写入本轮人类标注（纠错项 + 新增项），统一 source='human'/status='confirmed'
    for it in payload.items:
        conn.execute(
            "INSERT INTO annotations(session_id, impact_time, label, confidence, note, "
            "annotator, source, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, it.impact_time, it.label, it.confidence, it.note,
             ann, "human", "confirmed", now, now),
        )

    # 3) 难例挖掘：把与该标注者 impact 重叠的、未审阅启发式标记
    #    - 同标签 → confirmed（标注者认可）
    #    - 异标签 → rejected（标注者纠错，即硬例）
    human_map = {it.impact_time: it.label for it in payload.items}
    for t, lbl in human_map.items():
        row = conn.execute(
            "SELECT id, label FROM annotations WHERE session_id=? "
            "AND annotator='heuristic' AND source='auto' AND status='proposed' "
            "AND abs(impact_time-?)<=?",
            (sid, t, MATCH_TOL),
        ).fetchone()
        if row:
            new_status = "confirmed" if row["label"] == lbl else "rejected"
            conn.execute("UPDATE annotations SET status=?, updated_at=? WHERE id=?",
                         (new_status, now, row["id"]))

    # 4) 显式删除的启发式误检
    for t in payload.rejected_heuristic:
        conn.execute(
            "UPDATE annotations SET status='rejected', updated_at=? "
            "WHERE session_id=? AND annotator='heuristic' AND source='auto' "
            "AND status='proposed' AND abs(impact_time-?)<=?",
            (now, sid, t, MATCH_TOL),
        )

    conn.commit()
    conn.close()
    return {"status": "saved", "annotator": ann, "count": len(payload.items)}


@router.get("/api/sessions/{sid}/consistency")
def session_consistency(sid: str):
    conn = get_conn()
    if not _session_row(conn, sid):
        conn.close()
        raise HTTPException(404, "session not found")
    data = compute_consistency(conn, sid)
    conn.close()
    return data


@router.get("/api/consistency")
def all_consistency():
    conn = get_conn()
    data = compute_consistency(conn, None)
    conn.close()
    return data


@router.get("/api/export/annotation.json")
def export_all():
    conn = get_conn()
    data = export_annotation_json(conn)
    conn.close()
    return JSONResponse(data)


@router.get("/api/sessions/{sid}/export/annotation.json")
def export_one(sid: str):
    conn = get_conn()
    data = export_annotation_json(conn, sid)
    conn.close()
    if not data:
        raise HTTPException(404, "no annotations")
    return JSONResponse(data)


@router.get("/api/export/dataset.csv")
def export_dataset():
    out = os.path.join(DATA_DIR, "dataset.csv")
    conn = get_conn()
    count = build_dataset(conn, out)
    conn.close()
    if count == 0:
        raise HTTPException(404, "no annotated samples to export")
    return FileResponse(out, media_type="text/csv", filename="dataset.csv")
