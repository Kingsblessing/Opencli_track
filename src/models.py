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
    fetched_at: str = field(default_factory=now_iso)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
