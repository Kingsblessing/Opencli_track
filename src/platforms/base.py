"""平台抽象层:所有平台实现必须继承 PlatformBase 并实现 search()/comments()。

约定:
- 统一入参: keyword / config 视图 + Runner(限速重试已内建)
- 统一出参: List[VideoItem] / List[CommentItem],字段平台无关
- 登录态问题抛 OpencliAuthError,能力缺失抛 UnsupportedFeature,pipeline 负责优雅处理
"""
import abc
from typing import List, Optional

from ..models import VideoItem, CommentItem
from ..utils import Runner


class PlatformBase(abc.ABC):
    # 平台标识,与 config.platforms 中的名称对应
    name: str = ""
    # 平台展示名
    display_name: str = ""
    # 该平台搜索结果是否带发布时间(决定 publish_window 过滤能否生效)
    supports_time_filter: bool = False
    # 该平台是否支持抓取评论
    supports_comments: bool = True
    # 登录提示
    login_hint: str = ""

    def __init__(self, config: dict, runner: Optional[Runner] = None):
        self.config = config
        self.runner = runner or Runner(
            interval=config.get("runtime", {}).get("request_interval_seconds", 3),
            retry=config.get("runtime", {}).get("retry", 2),
            timeout=config.get("runtime", {}).get("timeout", 180),
        )

    @abc.abstractmethod
    def search(self, keyword: str) -> List[VideoItem]:
        """按关键词搜索,返回统一 VideoItem 列表(时间过滤/条数截断由子类或 pipeline 处理)。"""

    @abc.abstractmethod
    def comments(self, video: VideoItem) -> List[CommentItem]:
        """抓取单个视频/帖子下的评论(含楼中楼),返回统一 CommentItem 列表。"""
