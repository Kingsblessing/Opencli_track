"""步骤编排:读配置 → ① 采集视频 → ② 评论采集 + 正则匹配 → 落盘(json/csv)。

所有函数接受可选的 JobCtx(ctx):WebUI 运行时提供日志/进度/暂停停止检查点;
CLI 直接运行时用 _NullCtx(仅打印)。每个 opencli 调用前都有检查点,暂停/停止即时生效。
"""
import csv
import datetime
import glob
import json
import os
import re
from typing import List, Dict, Optional

import yaml

from .models import VideoItem, CommentItem
from .platforms import get_platform
from .intent import IntentEngine
from .utils import Runner, OpencliAuthError, UnsupportedFeature

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SEEN_FILE = os.path.join(DATA_DIR, "seen.json")
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config.yaml")


class _NullCtx:
    """CLI 直接运行时的最小上下文:日志走 print,无进度、无暂停检查点。"""

    def log(self, msg: str):
        print(msg)

    def progress(self, *args, **kwargs):
        pass

    def checkpoint(self):
        pass


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(config: dict, path: str = DEFAULT_CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def _validate_rule_groups(match: dict, errors: List[str]) -> None:
    """递归校验 match.recall / match.negative 的分组规则(正则 + 权重)。"""
    for group_key in ("recall", "negative"):
        groups = match.get(group_key)
        if groups is None:
            continue
        if not isinstance(groups, dict):
            errors.append(f"match.{group_key} 必须是 分组字典")
            continue
        for name, spec in groups.items():
            if not isinstance(spec, dict):
                errors.append(f"match.{group_key}.{name} 必须是字典(含 weight/patterns)")
                continue
            if "weight" in spec:
                try:
                    float(spec["weight"])
                except (TypeError, ValueError):
                    errors.append(f"match.{group_key}.{name}.weight 必须是数字")
            for p in spec.get("patterns", []) or []:
                try:
                    re.compile(p)
                except re.error as e:
                    errors.append(f"非法正则 match.{group_key}.{name} '{p}': {e}")
    dir_cfg = match.get("direction")
    if isinstance(dir_cfg, dict):
        for side in ("seller", "buyer"):
            for p in (dir_cfg.get(side) or {}).get("patterns", []) or []:
                try:
                    re.compile(p)
                except re.error as e:
                    errors.append(f"非法正则 match.direction.{side} '{p}': {e}")


def validate_config(config: dict) -> List[str]:
    """返回错误列表;保存配置前调用,保证非法配置进不了磁盘。"""
    errors = []
    platforms = config.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        errors.append("platforms 不能为空")
    if not config.get("keywords"):
        errors.append("keywords 不能为空")
    for k in ("publish_window_hours", "crawl_count", "max_comments_per_video", "max_replies"):
        try:
            if int(config.get(k, 0)) < 0:
                errors.append(f"{k} 不能为负数")
        except (TypeError, ValueError):
            errors.append(f"{k} 必须是整数")
    if config.get("search_order") not in ("relevance", "pubdate", "views"):
        errors.append("search_order 必须是 relevance/pubdate/views")
    match = config.get("match") or {}
    if match.get("mode", "intent") not in ("intent", "keyword"):
        errors.append("match.mode 必须是 intent/keyword")
    for p in match.get("patterns", []) + match.get("exclude_patterns", []):
        try:
            re.compile(p)
        except re.error as e:
            errors.append(f"非法正则 '{p}': {e}")
    _validate_rule_groups(match, errors)
    th = match.get("thresholds") or {}
    try:
        high, medium, low = (float(th.get(k, d)) for k, d in
                             (("high", 6), ("medium", 3), ("low", 1)))
        if not (low <= medium <= high):
            errors.append("match.thresholds 需满足 low ≤ medium ≤ high")
    except (TypeError, ValueError):
        errors.append("match.thresholds 的 high/medium/low 必须是数字")
    for k, dflt in (("similarity", 0.85),):
        dd = match.get("dedup") or {}
        if k in dd:
            try:
                if not 0 < float(dd[k]) <= 1:
                    errors.append(f"match.dedup.{k} 必须在 (0, 1] 之间")
            except (TypeError, ValueError):
                errors.append(f"match.dedup.{k} 必须是数字")
    return errors


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_json(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _write_csv(path: str, rows: List[dict], columns: List[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([_cell(r.get(c, "")) for c in columns])


def _cell(v):
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    return v


def _make_runner(config: dict, ctx) -> Runner:
    return Runner(
        interval=config.get("runtime", {}).get("request_interval_seconds", 3),
        retry=config.get("runtime", {}).get("retry", 2),
        timeout=config.get("runtime", {}).get("timeout", 180),
        checkpoint=(ctx.checkpoint if ctx else None),
    )


def _load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return {tuple(x) for x in json.load(f)}
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def _save_seen(seen: set) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)


# ---------------- ① 视频采集 ----------------

def step_collect(config: dict, config_path: str, ctx=None) -> Dict[str, str]:
    ctx = ctx or _NullCtx()
    runner = _make_runner(config, ctx)
    keywords = config.get("keywords", [])
    if not keywords:
        raise ValueError("config.keywords 为空")
    outputs: Dict[str, str] = {}
    platforms = config.get("platforms", [])
    window_hours = int(config.get("publish_window_hours", 0) or 0)
    search_order = config.get("search_order", "relevance")
    dedup = bool(config.get("dedup_across_runs", False))
    seen = _load_seen() if dedup else set()
    new_seen = set()

    for pname in platforms:
        try:
            platform = get_platform(pname, config)
        except KeyError as e:
            ctx.log(f"[skip] {e}")
            continue
        platform.log = ctx.log
        # 平台能力提示
        if search_order == "pubdate" and not platform.supports_order_sort:
            ctx.log(f"[warn] {platform.display_name} 不支持按时间排序,退回平台默认排序")
        if search_order == "views" and pname != "bilibili":
            ctx.log(f"[warn] {platform.display_name} 不支持按播放排序,退回平台默认排序")
        if window_hours > 0 and not platform.supports_time_filter:
            ctx.log(f"[warn] {platform.display_name} 搜索结果不含发布时间,"
                    f"publish_window_hours={window_hours} 无法生效,将照常采集")

        videos: List[VideoItem] = []
        try:
            for ki, kw in enumerate(keywords, 1):
                ctx.checkpoint()
                ctx.progress("collect", ki - 1, len(keywords),
                             f"{platform.display_name} 搜索 '{kw}'")
                found = platform.search(kw)
                ctx.log(f"[collect] {platform.display_name} '{kw}': {len(found)} 条")
                videos.extend(found)
        except OpencliAuthError as e:
            ctx.log(f"[auth] {platform.display_name} 未登录,跳过该平台: {e} ({platform.login_hint})")
            continue
        ctx.progress("collect", len(keywords), len(keywords),
                     f"{platform.display_name} 过滤去重中")

        # 去重 + 时间窗兜底过滤 + 跨运行去重
        seen_ids, uniq = set(), []
        cutoff = (datetime.datetime.now().astimezone()
                  - datetime.timedelta(hours=window_hours)) if window_hours > 0 else None
        for v in videos:
            if (v.platform, v.id) in seen_ids:
                continue
            seen_ids.add((v.platform, v.id))
            if dedup:
                if (v.platform, v.id) in seen:
                    continue
                new_seen.add((v.platform, v.id))
            if cutoff and v.publish_time and platform.supports_time_filter:
                try:
                    if datetime.datetime.fromisoformat(v.publish_time) < cutoff:
                        continue
                except ValueError:
                    pass
            uniq.append(v)
        # 详情数据增强(逐条调详情命令,只对过滤后的候选)
        if config.get("enrich_detail") and platform.supports_enrich_detail and uniq:
            ctx.progress("collect", 0, len(uniq), f"{platform.display_name} 补充详情数据")
            for i, v in enumerate(uniq):
                ctx.checkpoint()
                ctx.progress("collect", i, len(uniq),
                             f"{platform.display_name} 详情 {i + 1}/{len(uniq)}")
                platform.enrich_detail(v)
            ctx.log(f"[collect] {platform.display_name} 已补充 {len(uniq)} 条详情数据")
        if not uniq:
            ctx.log(f"[collect] {platform.display_name} 无可用结果")
            continue

        stem = f"videos_{platform.name}_{_stamp()}"
        out_json = os.path.join(DATA_DIR, "videos", f"{stem}.json")
        _write_json(out_json, [v.to_dict() for v in uniq])
        out_csv = os.path.join((config.get("output") or {}).get("dir", "output"),
                               f"{stem}.csv")
        video_columns = ["platform", "id", "url", "title", "author",
                         "publish_time", "search_keyword", "views", "likes", "fetched_at"]
        csv_rows = [dict(v.to_dict(),
                         views=(v.stats or {}).get("views", (v.stats or {}).get("score", "")),
                         likes=(v.stats or {}).get("likes", "")) for v in uniq]
        _write_csv(out_csv, csv_rows, video_columns)
        ctx.log(f"[collect] {platform.display_name} 去重后 {len(uniq)} 条 → {out_json}")
        ctx.log(f"[collect] {platform.display_name} 表格 → {out_csv}")
        outputs[platform.name] = out_json
    if dedup and new_seen:
        _save_seen(seen | new_seen)
        ctx.log(f"[collect] 跨运行去重: 新增 {len(new_seen)} 个已见 id")
    return outputs


def latest_videos_file(platform_name: str) -> str:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "videos",
                                           f"videos_{platform_name}_*.json")))
    if not files:
        raise FileNotFoundError(
            f"未找到 {platform_name} 的视频数据,请先运行采集步骤")
    return files[-1]


# ---------------- ② 评论采集 + 意图识别 ----------------

def compile_patterns(config: dict):
    match = config.get("match", {}) or {}
    includes = [re.compile(p) for p in match.get("patterns", [])]
    excludes = [re.compile(p) for p in match.get("exclude_patterns", [])]
    return includes, excludes


def match_comment(comment: CommentItem, includes, excludes) -> List[str]:
    """旧行为(mode=keyword):返回命中的模式串列表。

    与旧实现的区别:排除命中时返回空列表,但调用方通过返回值语义区分
    "被排除"与"未命中"——见 judge_comment。
    """
    if any(p.search(comment.text) for p in excludes):
        return []
    return [p.pattern for p in includes if p.search(comment.text)]


def judge_comment(comment: CommentItem, config: dict, includes=None, excludes=None):
    """旧行为(mode=keyword)的完整判定,返回 (kept, excluded)。

    修复:旧实现在 keep_all=True 时无法区分"被排除"与"未命中",
    导致 exclude_patterns 实际失效。此处用独立布尔值区分。
    """
    if includes is None or excludes is None:
        includes, excludes = compile_patterns(config)
    excluded = any(p.search(comment.text) for p in excludes)
    hits = [] if excluded else [p.pattern for p in includes if p.search(comment.text)]
    comment.matched_patterns = hits
    comment.positive_patterns = hits
    comment.negative_patterns = []
    comment.intent = "customer" if hits else "irrelevant"
    comment.lead_level = "MATCH" if hits else ""
    comment.reason = "命中 " + "、".join(hits) if hits else ("被排除" if excluded else "未命中")
    return hits, excluded


def collect_comments(config: dict, videos_files: Dict[str, str], ctx=None) -> List[CommentItem]:
    """采集阶段:全量收进内存,不做任何过滤(作者画像与去重需要全量语料)。"""
    ctx = ctx or _NullCtx()
    all_comments: List[CommentItem] = []
    for pname, path in videos_files.items():
        platform = get_platform(pname, config)
        if not platform.supports_comments:
            ctx.log(f"[skip] {platform.display_name} 暂不支持评论采集")
            continue
        with open(path, "r", encoding="utf-8") as f:
            video_rows = json.load(f)
        ctx.log(f"[comments] {platform.display_name}: {len(video_rows)} 个帖子待处理")
        for i, vr in enumerate(video_rows, 1):
            ctx.checkpoint()
            ctx.progress("comments", i - 1, len(video_rows),
                         f"{platform.display_name} {vr.get('id', '')}")
            video = VideoItem(**{k: vr.get(k) for k in
                                 ("platform", "id", "url", "title", "author",
                                  "author_id", "publish_time", "search_keyword")})
            try:
                comments = platform.comments(video)
            except (OpencliAuthError, UnsupportedFeature) as e:
                ctx.log(f"[comments]   帖子 {video.id} 跳过: {e}")
                continue
            except Exception as e:
                ctx.log(f"[comments]   帖子 {video.id} 失败: {e}")
                continue
            all_comments.extend(comments)
            ctx.log(f"[comments]   ({i}/{len(video_rows)}) {video.id} "
                    f"采集 {len(comments)} 条")
    ctx.progress("comments", len(videos_files), len(videos_files), "采集完成")
    return all_comments


def write_raw_comments(config: dict, comments: List[CommentItem], ctx=None) -> List[str]:
    """落原始语料(未过滤),供 --step match 离线重跑判定、调参而不必重爬。"""
    ctx = ctx or _NullCtx()
    if not comments:
        return []
    by_platform: Dict[str, list] = {}
    for c in comments:
        by_platform.setdefault(c.platform, []).append(c.to_dict())
    stamp = _stamp()
    paths = []
    for pname, rows in by_platform.items():
        p = os.path.join(DATA_DIR, "comments", f"raw_comments_{pname}_{stamp}.json")
        _write_json(p, rows)
        paths.append(p)
        ctx.log(f"[raw] {p} ({len(rows)} 条,未过滤)")
    return paths


def latest_raw_comments_file(platform_name: str) -> str:
    files = sorted(glob.glob(os.path.join(
        DATA_DIR, "comments", f"raw_comments_{platform_name}_*.json")))
    if not files:
        raise FileNotFoundError(
            f"未找到 {platform_name} 的原始评论语料,请先运行评论采集步骤")
    return files[-1]


def apply_intent(config: dict, comments: List[CommentItem], ctx=None) -> List[dict]:
    """判定阶段:mode=intent 走新算法,mode=keyword 走旧行为。"""
    ctx = ctx or _NullCtx()
    match_cfg = config.get("match", {}) or {}
    mode = match_cfg.get("mode", "intent")
    keep_all = bool(match_cfg.get("keep_all", False))
    only_leads = bool(match_cfg.get("only_leads", False))
    rows: List[dict] = []

    if mode == "keyword":
        includes, excludes = compile_patterns(config)
        excluded_n = 0
        for c in comments:
            hits, excluded = judge_comment(c, config, includes, excludes)
            if excluded:
                excluded_n += 1
            # 修复:排除的评论即使 keep_all 也不再保留,避免与"未命中"混淆
            if excluded:
                continue
            if hits or keep_all:
                rows.append(c.to_dict())
        ctx.log(f"[match] 旧关键词模式: {len(comments)} 条评论 → 保留 {len(rows)} 条"
                f"(排除 {excluded_n} 条)")
        return rows

    engine = IntentEngine(match_cfg)
    stat = engine.run(comments, ctx)
    lead_levels = {"HIGH", "MEDIUM", "LOW"}
    for c in comments:
        if only_leads and c.lead_level not in lead_levels:
            continue
        if not keep_all and not only_leads and c.lead_level == "EXCLUDE":
            continue
        rows.append(c.to_dict())
    lv = stat.get("levels", {})
    ctx.log(f"[match] 意图模式: {stat.get('total', 0)} 条 → "
            f"HIGH {lv.get('HIGH', 0)} / MEDIUM {lv.get('MEDIUM', 0)} / "
            f"LOW {lv.get('LOW', 0)} / EXCLUDE {lv.get('EXCLUDE', 0)};"
            f"其中 customer {stat.get('intents', {}).get('customer', 0)}、"
            f"provider {stat.get('intents', {}).get('provider', 0)}")
    dd = stat.get("dedup") or {}
    if dd:
        ctx.log(f"[match] 去重: {dd.get('groups', 0)} 个文本组,"
                f"重复 {dd.get('duplicates', 0)} 条,刷屏组 {dd.get('spam_groups', 0)} 个;"
                f"输出 {len(rows)} 条")
    return rows


def step_comments(config: dict, videos_files: Dict[str, str], ctx=None) -> List[dict]:
    """采集 → 落原始语料 → 判定 → 过滤,返回待输出的行。"""
    ctx = ctx or _NullCtx()
    raw = collect_comments(config, videos_files, ctx)
    write_raw_comments(config, raw, ctx)
    return apply_intent(config, raw, ctx)


def step_match(config: dict, ctx=None) -> List[dict]:
    """对已有原始语料重跑判定,不联网——调规则时无需重爬。"""
    ctx = ctx or _NullCtx()
    comments: List[CommentItem] = []
    for pname in config.get("platforms", []):
        try:
            path = latest_raw_comments_file(pname)
        except FileNotFoundError as e:
            ctx.log(f"[warn] {e}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        ctx.log(f"[match] 载入 {path} ({len(rows)} 条)")
        for r in rows:
            comments.append(_comment_from_dict(r))
    if not comments:
        raise FileNotFoundError("没有可用的原始评论语料,请先运行 --step comments")
    ctx.log(f"[match] 共 {len(comments)} 条评论待判定")
    return apply_intent(config, comments, ctx)


def _comment_from_dict(d: dict) -> CommentItem:
    """从原始语料还原 CommentItem(忽略不认识的键,兼容旧文件)。"""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(CommentItem)}
    kwargs = {k: v for k, v in d.items() if k in fields}
    kwargs.setdefault("platform", d.get("platform", "unknown"))
    kwargs.setdefault("video_id", d.get("video_id", ""))
    return CommentItem(**kwargs)


def save_comment_outputs(config: dict, rows: List[dict], ctx=None) -> Dict[str, str]:
    ctx = ctx or _NullCtx()
    if not rows:
        ctx.log("[output] 无符合条件的评论,未生成文件")
        return {}
    out_cfg = config.get("output", {}) or {}
    formats = out_cfg.get("formats", ["json", "csv"])
    stamp = _stamp()
    by_platform: Dict[str, list] = {}
    for r in rows:
        by_platform.setdefault(r.get("platform", "unknown"), []).append(r)
    outputs: Dict[str, str] = {}
    for pname, prows in by_platform.items():
        if "json" in formats:
            p = os.path.join(DATA_DIR, "comments", f"comments_{pname}_{stamp}.json")
            _write_json(p, prows)
            outputs["json"] = outputs.get("json", "") + f"{p} "
            ctx.log(f"[output] {p} ({len(prows)} 条)")
        if "csv" in formats:
            p = os.path.join(out_cfg.get("dir", "output"),
                             f"comments_{pname}_{stamp}.csv")
            _write_csv(p, prows, out_cfg.get("table_columns", []))
            outputs["csv"] = outputs.get("csv", "") + f"{p} "
            ctx.log(f"[output] {p}")
    return outputs
