"""统一数据模型:平台无关的视频/帖子与评论结构,序列化为统一 JSON。"""
import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, List


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class VideoItem:
    """视频/帖子统一模型(平台无关字段名)。"""
    platform: str
    id: str
    url: str
    title: str
    author: str = ""
    author_id: str = ""
    publish_time: Optional[str] = None      # ISO8601;平台不提供时为 None
    search_keyword: str = ""
    stats: dict = field(default_factory=dict)
    fetched_at: str = field(default_factory=now_iso)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CommentItem:
    """评论统一模型;parent_rpid 为空表示一级评论,否则为楼中楼回复。"""
    platform: str
    video_id: str
    video_title: str = ""
    video_url: str = ""
    rpid: str = ""
    parent_rpid: Optional[str] = None
    author: str = ""
    author_id: str = ""
    text: str = ""
    likes: int = 0
    replies: int = 0
    time: Optional[str] = None
    matched_patterns: List[str] = field(default_factory=list)
    # ---- 意图识别判定字段(见 src/intent.py) ----
    positive_patterns: List[str] = field(default_factory=list)
    negative_patterns: List[str] = field(default_factory=list)
    intent: str = ""              # customer / provider / irrelevant
    intent_score: float = 0.0
    lead_level: str = ""          # HIGH / MEDIUM / LOW / EXCLUDE
    reason: str = ""              # 得分构成,便于人工复核
    post_type: str = ""           # 主贴类型:service_selling / user_seeking / discussion / irrelevant
    author_role: str = ""         # 作者角色:customer / provider / unknown
    author_comment_count: int = 0
    author_seller_ratio: float = 0.0
    dup_group: str = ""           # 近似重复分组
    dup_count: int = 1
    is_representative: bool = True
    is_spam: bool = False         # 跨作者刷屏
    fetched_at: str = field(default_factory=now_iso)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
