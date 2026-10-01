"""
schemas.py — API 请求/响应模型。

标签集：基线 5 类（与 SwingType 一致）+ 扩展 3 类（smash/lob/drop），标注不校验可任意扩展。
⚠️ 这是**训练用的标签空间**，刻意与展示层（小程序 SHOT_TYPES / Swift SwingType 的 6 类）
   不同 —— 标注阶段要保留富标签以便后续难例分析，训练时再按需归并。
"""
from typing import List, Optional

from pydantic import BaseModel

# 与 TRAINING_SPEC 第 9 节一致；前 5 类是 CoreML 基线标签
LABELS_5 = ["forehand", "backhand", "volley", "slice", "serve"]
LABELS_ALL = LABELS_5 + ["smash", "lob", "drop"]

# 前端展示用中文名（仅 UI，不影响存储）
LABEL_CN = {
    "forehand": "正手",
    "backhand": "反手",
    "serve": "发球",
    "volley": "截击",
    "slice": "切削",
    "smash": "高压",
    "lob": "挑高",
    "drop": "放小球",
}


class SessionMeta(BaseModel):
    id: str
    wrist: Optional[str] = "right"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration: Optional[float] = None
    player_id: Optional[str] = None
    # 指向 L0 原始层 raw_sessions.raw_id —— 原始数据的权威指针
    # （raw_path 仅是该文件的路径缓存，不再是真源）
    raw_id: Optional[str] = None
    has_video: bool = False
    annotation_count: int = 0


class AnnotationItem(BaseModel):
    impact_time: float
    label: str
    confidence: Optional[float] = None
    note: Optional[str] = None
    # 前端回传时携带来源信息（后端据此区分 human / heuristic）
    annotator: Optional[str] = None
    source: Optional[str] = None


class AnnotationSave(BaseModel):
    # 当前标注者身份（协作与一致性评分用）；缺省 "human" 兼容单标注者模式
    annotator: str = "human"
    # 该标注者本轮的全部击球（含纠错项与新增项）
    items: List[AnnotationItem]
    # 被标注者判为误检、显式删除的启发式预标注 impact_time 列表
    rejected_heuristic: List[float] = []
