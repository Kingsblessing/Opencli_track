"""抖音平台实现(依赖 Chrome 已登录 douyin.com)。

- search: `opencli douyin search`(官方列: rank/desc/author/url/plays/likes/comments/shares)
- comments: opencli 未提供任意视频的评论命令,标记为不支持,pipeline 会跳过
"""
import re
from typing import List

from ..models import VideoItem, CommentItem
from ..utils import parse_count, first_nonempty, UnsupportedFeature
from .base import PlatformBase


class DouyinPlatform(PlatformBase):
    name = "douyin"
    display_name = "抖音"
    supports_time_filter = False   # 搜索结果不含发布时间
    supports_order_sort = False    # opencli douyin search 无排序参数
    supports_comments = False      # opencli douyin 无通用评论命令
    login_hint = "请在 Chrome 打开 https://www.douyin.com 并登录,或运行 opencli douyin login"

    def search(self, keyword: str) -> List[VideoItem]:
        limit = min(int(self.config.get("crawl_count", 20)), 30)  # 官方单次上限 30
        data = self.runner.run(["douyin", "search", keyword,
                                "--limit", str(limit), "-f", "json"])
        items = []
        for row in data:
            url = row.get("url", "") or ""
            vid = (re.search(r"/video/(\d+)", url) or ["", ""])[1]
            if not vid:
                vid = row.get("aweme_id", "") or row.get("id", "")
            items.append(VideoItem(
                platform=self.name, id=str(vid), url=url,
                title=first_nonempty(row, ["desc", "title"]),
                author=first_nonempty(row, ["author", "nickname", "author_name"]),
                stats={"plays": parse_count(str(row.get("plays", "") or "")),
                       "likes": parse_count(str(row.get("likes", "") or "")),
                       "comments": parse_count(str(row.get("comments", "") or "")),
                       "shares": parse_count(str(row.get("shares", "") or ""))},
                search_keyword=keyword,
            ))
        return [v for v in items if v.id]

    def comments(self, video: VideoItem) -> List[CommentItem]:
        raise UnsupportedFeature(
            "opencli douyin 暂无通用评论命令;可用 opencli browser 手动扩展或等待上游支持")
