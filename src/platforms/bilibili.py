"""B 站平台实现。

- search: 有时间窗口时走浏览器搜索页(官方 pubtime_begin_s/end 参数 + DOM 抽取);
          无时间窗口时走官方 API `opencli bilibili search`
- comments: 官方 API `opencli bilibili comments`,楼中楼用 --parent <rpid> 逐条展开
"""
import datetime
import json
import re
import time
from typing import List
from urllib.parse import quote

from ..models import VideoItem, CommentItem
from ..utils import Runner, parse_count, parse_relative_time, first_nonempty
from .base import PlatformBase

# 搜索页卡片抽取脚本(已在真实页面上验证过选择器)
_CARD_JS = """
(() => {
  const cards = [...document.querySelectorAll('.bili-video-card')];
  const out = cards.map(c => {
    const a = c.querySelector('a[href*="/video/BV"]');
    const title = c.querySelector('h3')?.innerText?.trim() || '';
    const author = c.querySelector('.bili-video-card__info--author')?.innerText?.trim() || '';
    const date = c.querySelector('.bili-video-card__info--date')?.innerText?.trim() || '';
    const authorLink = c.querySelector('a[href*="/space/"]')?.getAttribute('href') || '';
    const stats = [...c.querySelectorAll('.bili-video-card__stats--item')].map(s => s.innerText.trim());
    const dur = c.querySelector('.bili-video-card__stats__duration')?.innerText?.trim() || '';
    return {url: a ? new URL(a.href, location.origin).href : '',
            author_id: (authorLink.match(/space\\/(\\d+)/) || [])[1] || '',
            title, author, date, dur, stats};
  }).filter(v => v.url);
  return JSON.stringify(out);
})()
"""


class BilibiliPlatform(PlatformBase):
    name = "bilibili"
    display_name = "哔哩哔哩"
    supports_time_filter = True
    supports_comments = True
    login_hint = "请在 Chrome 打开 https://www.bilibili.com 并登录,或运行 opencli bilibili login"

    def search(self, keyword: str) -> List[VideoItem]:
        window_hours = int(self.config.get("publish_window_hours", 0) or 0)
        if window_hours > 0:
            items = self._search_browser(keyword, window_hours)
        else:
            items = self._search_api(keyword)
        limit = int(self.config.get("crawl_count", 20))
        return items[:limit]

    def _search_api(self, keyword: str) -> List[VideoItem]:
        data = self.runner.run(["bilibili", "search", keyword,
                                "--limit", "50", "-f", "json"])
        items = []
        for row in data:
            url = row.get("url", "")
            bvid = (re.search(r"(BV\w+)", url) or ["", ""])[1]
            items.append(VideoItem(
                platform=self.name, id=bvid, url=url,
                title=first_nonempty(row, ["title", "desc"]),
                author=first_nonempty(row, ["author", "up"]),
                stats={"score": parse_count(str(row.get("score", "") or ""))},
                search_keyword=keyword,
            ))
        return [v for v in items if v.id]

    def _search_browser(self, keyword: str, window_hours: int) -> List[VideoItem]:
        end = int(time.time())
        begin = end - window_hours * 3600
        url = ("https://search.bilibili.com/all?keyword=" + quote(keyword)
               + f"&pubtime_begin_s={begin}&pubtime_end_s={end}")
        session = f"oc_bili_{int(time.time() * 1000) % 10**9}"
        window = self.config.get("runtime", {}).get("browser_window", "background")
        self.runner.run(["browser", session, "open", url, "--window", window])
        try:
            # 搜索页渲染耗时波动大(实测 4~6s+),轮询直到卡片出现或超时
            cards = []
            for _ in range(8):
                self.runner.run(["browser", session, "wait", "time", "2"])
                n = self.runner.run(["browser", session, "eval",
                                     "document.querySelectorAll('.bili-video-card').length"])
                try:
                    n = int(str(n).strip())
                except ValueError:
                    n = 0
                if n > 0:
                    raw = self.runner.run(["browser", session, "eval", _CARD_JS])
                    cards = json.loads(raw) if isinstance(raw, str) else raw
                    break
        finally:
            self.runner.run(["browser", session, "close"])
        items = []
        for c in cards:
            bvid = (re.search(r"(BV\w+)", c.get("url", "")) or ["", ""])[1]
            stats = c.get("stats") or []
            items.append(VideoItem(
                platform=self.name, id=bvid, url=c.get("url", ""),
                title=c.get("title", ""), author=c.get("author", ""),
                author_id=c.get("author_id", ""),
                publish_time=parse_relative_time(c.get("date", "")),
                search_keyword=keyword,
                stats={"views": parse_count(stats[0] if len(stats) > 0 else ""),
                       "danmaku": parse_count(stats[1] if len(stats) > 1 else "")},
                extra={"duration": c.get("dur", "")},
            ))
        return [v for v in items if v.id]

    def comments(self, video: VideoItem) -> List[CommentItem]:
        cfg_max = int(self.config.get("max_comments_per_video", 50))
        limit = min(cfg_max, 50)  # 官方 API 单次上限 50
        data = self.runner.run(["bilibili", "comments", video.id,
                                "--limit", str(limit), "-f", "json"])
        out = []
        for row in data:
            out.append(CommentItem(
                platform=self.name, video_id=video.id,
                video_title=video.title, video_url=video.url,
                rpid=str(row.get("rpid", "")),
                author=first_nonempty(row, ["author", "uname"]),
                text=first_nonempty(row, ["text", "content", "message"]),
                likes=parse_count(str(row.get("likes", 0) or 0)),
                replies=parse_count(str(row.get("replies", 0) or 0)),
                time=parse_relative_time(str(row.get("time", "") or "")),
            ))
        out.sort(key=lambda c: -c.likes)
        self._expand_replies(video, out)
        return out

    def _expand_replies(self, video: VideoItem, top: List[CommentItem]) -> None:
        """按点赞序对有回复的一级评论展开楼中楼,每条最多 max_replies 条。"""
        max_replies = int(self.config.get("max_replies", 0) or 0)
        if max_replies <= 0:
            return
        for c in top:
            if c.replies <= 0:
                continue
            try:
                data = self.runner.run(["bilibili", "comments", video.id,
                                        "--parent", c.rpid,
                                        "--limit", str(max_replies), "-f", "json"])
            except Exception:
                continue  # 单条楼中楼失败不影响整体
            for row in data:
                top.append(CommentItem(
                    platform=self.name, video_id=video.id,
                    video_title=video.title, video_url=video.url,
                    rpid=str(row.get("rpid", "")), parent_rpid=c.rpid,
                    author=first_nonempty(row, ["author", "uname"]),
                    text=first_nonempty(row, ["text", "content", "message"]),
                    likes=parse_count(str(row.get("likes", 0) or 0)),
                    time=parse_relative_time(str(row.get("time", "") or "")),
                ))
