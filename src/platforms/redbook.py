"""小红书(redbook / xiaohongshu)平台实现(依赖 Chrome 已登录 xiaohongshu.com)。

三条路径:
- API 搜索(默认): `opencli xiaohongshu search`,输出含 published_at 精确日期
- 浏览器"最新"排序(search_order=pubdate): 打开搜索页 → 点击"筛选→最新" →
  从 Pinia store 抽取(noteCard.cornerTagInfo 提供相对发布时间,已真机验证)
- enrich(时间戳增强): `opencli xiaohongshu note <url>` 取详情页精确发布时间

评论: `opencli xiaohongshu comments <完整笔记URL含xsec_token> --with-replies true`
"""
import re
import json
import datetime
from typing import List
from urllib.parse import quote

from ..models import VideoItem, CommentItem
from ..utils import parse_count, parse_relative_time, parse_time_with_region, first_nonempty
from .base import PlatformBase

_NOTE_ID = re.compile(r"/(?:explore|note|search_result)/([0-9a-zA-Z]{16,32})")

# 搜索页 store 抽取(已真机验证: __INITIAL_STATE__.search.feeds)
_STORE_JS = """
(() => {
  const st = window.__INITIAL_STATE__;
  const feeds = (st && st.search && st.search.feeds && st.search.feeds.value) || st.search.feeds || [];
  return JSON.stringify(feeds.map(f => ({
    id: f.id,
    xsecToken: f.xsecToken || '',
    title: (f.noteCard && f.noteCard.displayTitle) || '',
    author: (f.noteCard && f.noteCard.user && f.noteCard.user.nickname) || '',
    author_id: (f.noteCard && f.noteCard.user && f.noteCard.user.userId) || '',
    likes: (f.noteCard && f.noteCard.interactInfo && f.noteCard.interactInfo.likedCount) || '',
    corner: (f.noteCard && f.noteCard.cornerTagInfo) || []
  })).filter(x => x.id));
})()
"""


class RedbookPlatform(PlatformBase):
    name = "redbook"
    display_name = "小红书"
    supports_time_filter = True    # API 路径含 published_at;浏览器路径含相对时间
    supports_order_sort = True     # 浏览器路径点击"筛选→最新"
    supports_comments = True
    supports_enrich_detail = True
    login_hint = "请在 Chrome 打开 https://www.xiaohongshu.com 并登录,或运行 opencli xiaohongshu login"

    def search(self, keyword: str) -> List[VideoItem]:
        limit = int(self.config.get("crawl_count", 20))
        if self.config.get("search_order") == "pubdate":
            try:
                return self._search_browser_sorted(keyword)[:limit]
            except Exception as e:
                # 页面结构变化/风控时不中断任务:降级为 API 搜索 + 按时间戳排序
                self._log_fallback(e)
                items = self._search_api(keyword, fetch_limit=min(max(limit * 3, limit), 50))
                return self._sort_by_time(items)[:limit]
        return self._search_api(keyword)[:limit]

    def _log_fallback(self, err: Exception) -> None:
        self.log(f"[warn] 小红书'最新'排序失败({err}),降级为综合搜索+按时间排序")

    def _click_latest_sort(self, session: str) -> bool:
        """幂等地打开筛选面板并点击'最新';成功返回 True。"""
        probe = ("(()=>{const e=[...document.querySelectorAll('div,span,li,button,a')]"
                 ".find(e=>e.textContent.trim()==='最新'&&e.offsetParent!==null);"
                 "return e ? 'visible' : 'absent'})()")
        click_latest = ("(()=>{const e=[...document.querySelectorAll('div,span,li,button,a')]"
                        ".find(e=>e.textContent.trim()==='最新'&&e.offsetParent!==null);"
                        "if(e){e.click();return 'clicked'}return 'not-found'})()")
        toggle = ("(()=>{const f=document.querySelector('.filter');"
                  "if(!f) return 'no-filter';"
                  "f.dispatchEvent(new MouseEvent('click',{bubbles:true}));return 'toggled'})()")
        for _ in range(6):
            if "visible" in str(self.runner.run(["browser", session, "eval", probe])):
                return "clicked" in str(self.runner.run(
                    ["browser", session, "eval", click_latest]))
            if "no-filter" in str(self.runner.run(["browser", session, "eval", toggle])):
                break
            self.runner.run(["browser", session, "wait", "time", "2"])
        return False

    @staticmethod
    def _note_id_timestamp(note_id: str) -> int:
        """小红书笔记 ID 前 8 位十六进制即创建时间戳(已实测验证)。"""
        try:
            return int(str(note_id)[:8], 16)
        except (ValueError, TypeError):
            return 0

    def _sort_by_time(self, items: List[VideoItem]) -> List[VideoItem]:
        """按时间倒序,返回 (日历日, 日内精确时刻) 的二级排序键。

        - 主键 = publish_time 的日期(页面展示的发布时间,权威)
        - 次键 = publish_time 的时分秒;若 publish_time 只到日(API 路径),
          则用笔记 ID 内嵌时间戳补足日内精度

        注:实测发现笔记被编辑/推广时 ID 前缀时间会与页面显示的发布时间不一致,
        因此 ID 时间戳只用于日内细化与兜底,不作为主依据。
        """
        def key(v: VideoItem):
            try:
                dt = datetime.datetime.fromisoformat(v.publish_time)
            except (TypeError, ValueError):
                ts = self._note_id_timestamp(v.id)
                return (ts, ts)
            day = int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            intraday = dt.hour * 3600 + dt.minute * 60 + dt.second
            if intraday == 0 and self._note_id_timestamp(v.id):
                intraday = self._note_id_timestamp(v.id) % 86400
            return (day, intraday)
        return sorted(items, key=key, reverse=True)

    # ---------- 路径 1: API 搜索 ----------

    def _search_api(self, keyword: str, fetch_limit: int = None) -> List[VideoItem]:
        limit = fetch_limit or min(int(self.config.get("crawl_count", 20)), 50)
        data = self.runner.run(["xiaohongshu", "search", keyword,
                                "--limit", str(limit), "-f", "json"])
        items = []
        for row in data:
            url = first_nonempty(row, ["url", "link", "note_url"])
            nid = (_NOTE_ID.search(url or "") or ["", ""])[1] \
                or str(row.get("note_id", "") or row.get("id", ""))
            items.append(VideoItem(
                platform=self.name, id=str(nid), url=url,
                title=first_nonempty(row, ["title", "desc", "display_title"]),
                author=first_nonempty(row, ["author", "nickname", "user"]),
                publish_time=parse_relative_time(str(first_nonempty(
                    row, ["published_at", "date", "time"], ""))),
                stats={"likes": parse_count(str(first_nonempty(row, ["likes"], "")))},
                search_keyword=keyword,
            ))
        return [v for v in items if v.id]

    # ---------- 路径 2: 浏览器"最新"排序 ----------

    def _search_browser_sorted(self, keyword: str) -> List[VideoItem]:
        session = f"oc_xhs_{int(datetime.datetime.now().timestamp() * 1000) % 10**9}"
        window = self.config.get("runtime", {}).get("browser_window", "background")
        url = ("https://www.xiaohongshu.com/search_result?keyword="
               + quote(keyword) + "&source=web_explore_feed")
        self.runner.run(["browser", session, "open", url, "--window", window])
        try:
            # 等待卡片渲染(轮询)
            for _ in range(8):
                self.runner.run(["browser", session, "wait", "time", "2"])
                n = self.runner.run(["browser", session, "eval",
                                     "document.querySelectorAll('section.note-item').length"])
                try:
                    n = int(str(n).strip())
                except ValueError:
                    n = 0
                if n > 0:
                    break
            # 点击 筛选 → 最新。.filter 是开关式点击(面板已开时再点会关闭),
            # 故每轮先探测'最新'是否可见,不可见才切换面板,最多 6 轮。
            if not self._click_latest_sort(session):
                raise RuntimeError("未找到'最新'排序按钮,页面结构可能已变化")
            self.runner.run(["browser", session, "wait", "time", "4"])
            raw = self.runner.run(["browser", session, "eval", _STORE_JS])
            cards = json.loads(raw) if isinstance(raw, str) else raw
        finally:
            self.runner.run(["browser", session, "close"])
        items = []
        for c in cards:
            corner = c.get("corner") or []
            time_text = next((x.get("text") for x in corner
                              if isinstance(x, dict) and x.get("type") == "publish_time"), None)
            if not time_text and corner and isinstance(corner[0], dict):
                time_text = corner[0].get("text")
            xsec = c.get("xsecToken") or ""
            note_url = (f"https://www.xiaohongshu.com/search_result/{c['id']}"
                        + (f"?xsec_token={xsec}&xsec_source=pc_search" if xsec else ""))
            items.append(VideoItem(
                platform=self.name, id=str(c["id"]), url=note_url,
                title=c.get("title", ""), author=c.get("author", ""),
                author_id=str(c.get("author_id", "")),
                publish_time=parse_relative_time(time_text or ""),
                stats={"likes": parse_count(str(c.get("likes", "") or ""))},
                search_keyword=keyword,
                # 保留原始相对时间文本:相对时间推算的绝对时间会随采集时刻漂移,
                # 原始文本可用于追溯与复核
                extra={"publish_time_text": time_text or ""},
            ))
        return items

    # ---------- 路径 3: 详情数据增强 ----------

    # opencli xiaohongshu note 实际返回字段(已核对 adapter 源码 note.js):
    #   title / author / content / likes / collects / comments / tags
    # 其中 collects、comments、content、tags 是搜索接口拿不到的,故用于补全。
    # 注意:该命令不返回发布时间,故不做任何时间戳相关处理。
    _NOTE_STAT_FIELDS = {"likes": "likes", "collects": "collects",
                         "comments": "comments"}
    _NOTE_EXTRA_FIELDS = {"content": "content", "tags": "tags",
                          "author": "note_author", "title": "note_title"}

    def enrich_detail(self, video: VideoItem) -> VideoItem:
        if not video.url:
            return video
        try:
            data = self.runner.run(["xiaohongshu", "note", video.url, "-f", "json"])
        except Exception:
            return video  # 单条增强失败不影响整体
        fields = self._flatten_note_fields(data)
        for key, val in fields.items():
            k = str(key).strip().lower()
            if k in self._NOTE_STAT_FIELDS and val not in (None, ""):
                video.stats[self._NOTE_STAT_FIELDS[k]] = parse_count(str(val))
            elif k in self._NOTE_EXTRA_FIELDS and val not in (None, ""):
                video.extra[self._NOTE_EXTRA_FIELDS[k]] = str(val)
        return video

    @staticmethod
    def _flatten_note_fields(data) -> dict:
        """note 命令输出形如 [{field, value}](防御性映射多种可能键名)。"""
        out = {}
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                k = first_nonempty(row, ["field", "key", "name", "label"])
                v = first_nonempty(row, ["value", "val", "content"])
                if k:
                    out[k] = v
        return out

    # ---------- 评论 ----------

    # 上游输出列为 rank/author/userId/profileUrl/text/likes/time/is_reply/reply_to/images;
    # 没有评论 ID,列表是扁平的(回复紧跟其一级评论,is_reply=true),故 rpid 本地合成。
    def comments(self, video: VideoItem) -> List[CommentItem]:
        cfg_max = int(self.config.get("max_comments_per_video", 50))
        limit = min(cfg_max, 50)
        max_replies = int(self.config.get("max_replies", 0) or 0)
        args = ["xiaohongshu", "comments", video.url, "--limit", str(limit)]
        if max_replies > 0:
            args += ["--with-replies", "true"]
        args += ["-f", "json"]
        data = self.runner.run(args, allow_empty=True)
        out: List[CommentItem] = []
        last_top_rpid = None
        reply_count = {}          # 一级评论 → 已保留的回复数
        for row in data:
            is_reply = bool(row.get("is_reply"))
            rank = row.get("rank", len(out) + 1)
            rpid = f"{video.id}_{rank}"
            if not is_reply:
                last_top_rpid = rpid
            parent = last_top_rpid if is_reply else None
            if is_reply and parent is not None:
                if reply_count.get(parent, 0) >= max_replies > 0:
                    continue      # 超出楼中楼上限
                reply_count[parent] = reply_count.get(parent, 0) + 1
            raw_time = str(first_nonempty(row, ["time", "create_time", "date"], ""))
            time_iso, region = parse_time_with_region(raw_time)
            c = CommentItem(
                platform=self.name, video_id=video.id,
                video_title=video.title, video_url=video.url,
                rpid=rpid, parent_rpid=parent,
                author=first_nonempty(row, ["author", "nickname", "user_name"]),
                author_id=str(first_nonempty(row, ["userId", "user_id"], "")),
                text=first_nonempty(row, ["text", "content", "comment"]),
                likes=parse_count(str(first_nonempty(row, ["likes", "like_count"], 0))),
                replies=0,   # 上游不返回回复计数,由本地结构推导
                time=time_iso,
                extra={"profile_url": row.get("profileUrl", ""),
                       "reply_to": row.get("reply_to", ""),
                       "ip_location": region},
            )
            out.append(c)
        # 回填一级评论的实际回复数
        for c in out:
            if c.parent_rpid is None:
                c.replies = reply_count.get(c.rpid, 0)
        return out
