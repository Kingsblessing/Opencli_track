"""小红书(redbook / xiaohongshu)平台实现(依赖 Chrome 已登录 xiaohongshu.com)。

- search: `opencli xiaohongshu search`
- comments: `opencli xiaohongshu comments <完整笔记URL含xsec_token> --with-replies true`
  注意上游输出列名可能随版本变化,此处做了防御性键名映射;登录验证通过后
  如有偏差仅需调整 _map_comment_row 的候选键列表。
"""
import re
from typing import List

from ..models import VideoItem, CommentItem
from ..utils import parse_count, parse_relative_time, first_nonempty
from .base import PlatformBase

_NOTE_ID = re.compile(r"/(?:explore|note|search_result)/([0-9a-zA-Z]{16,32})")


class RedbookPlatform(PlatformBase):
    name = "redbook"
    display_name = "小红书"
    supports_time_filter = False   # 搜索结果列不含标准发布时间
    supports_comments = True
    login_hint = "请在 Chrome 打开 https://www.xiaohongshu.com 并登录,或运行 opencli xiaohongshu login"

    def search(self, keyword: str) -> List[VideoItem]:
        limit = int(self.config.get("crawl_count", 20))
        data = self.runner.run(["xiaohongshu", "search", keyword,
                                "--limit", str(limit), "-f", "json"])
        items = []
        for row in data:
            url = first_nonempty(row, ["url", "link", "note_url"])
            nid = (_NOTE_ID.search(url or "") or ["", ""])[1] or str(row.get("note_id", "") or row.get("id", ""))
            items.append(VideoItem(
                platform=self.name, id=str(nid), url=url,
                title=first_nonempty(row, ["title", "desc", "display_title"]),
                author=first_nonempty(row, ["author", "nickname", "user"]),
                publish_time=parse_relative_time(str(row.get("date", "") or row.get("time", "") or "")),
                stats={"likes": parse_count(str(row.get("likes", "") or ""))},
                search_keyword=keyword,
            ))
        return [v for v in items if v.id]

    def comments(self, video: VideoItem) -> List[CommentItem]:
        cfg_max = int(self.config.get("max_comments_per_video", 50))
        limit = min(cfg_max, 50)
        max_replies = int(self.config.get("max_replies", 0) or 0)
        args = ["xiaohongshu", "comments", video.url,
                "--limit", str(limit)]
        if max_replies > 0:
            args += ["--with-replies", "true"]
        args += ["-f", "json"]
        data = self.runner.run(args)
        out: List[CommentItem] = []
        for row in data:
            out.append(self._map_comment_row(row, video, parent=None))
            if max_replies > 0:
                for sub in (row.get("replies") or row.get("sub_comments") or []):
                    out.append(self._map_comment_row(sub, video, parent=out[-1].rpid))
        return out

    def _map_comment_row(self, row: dict, video: VideoItem, parent) -> CommentItem:
        return CommentItem(
            platform=self.name, video_id=video.id,
            video_title=video.title, video_url=video.url,
            rpid=str(first_nonempty(row, ["rpid", "id", "comment_id"])),
            parent_rpid=parent,
            author=first_nonempty(row, ["author", "nickname", "user_name"]),
            text=first_nonempty(row, ["text", "content", "comment"]),
            likes=parse_count(str(first_nonempty(row, ["likes", "like_count"], 0))),
            replies=parse_count(str(first_nonempty(row, ["replies", "reply_count", "sub_comment_count"], 0))),
            time=parse_relative_time(str(first_nonempty(row, ["time", "create_time", "date"], ""))),
        )
