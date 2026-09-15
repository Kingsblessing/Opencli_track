"""步骤编排:读配置 → ① 采集视频 → ② 评论采集 + 正则匹配 → 落盘(json/csv)。"""
import csv
import datetime
import glob
import json
import os
import re
from typing import List, Dict

import yaml

from .models import VideoItem, CommentItem
from .platforms import get_platform, available_platforms
from .utils import Runner, OpencliAuthError, UnsupportedFeature

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DEFAULT_CONFIG_PATH = os.path.join(ROOT, "config.yaml")


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


# ---------------- ① 视频采集 ----------------

def step_collect(config: dict, config_path: str) -> Dict[str, str]:
    runner = Runner(
        interval=config.get("runtime", {}).get("request_interval_seconds", 3),
        retry=config.get("runtime", {}).get("retry", 2),
        timeout=config.get("runtime", {}).get("timeout", 180),
    )
    keywords = config.get("keywords", [])
    if not keywords:
        raise ValueError("config.keywords 为空")
    outputs: Dict[str, str] = {}
    for pname in config.get("platforms", []):
        try:
            platform = get_platform(pname, config)
        except KeyError as e:
            print(f"[skip] {e}")
            continue
        window_hours = int(config.get("publish_window_hours", 0) or 0)
        if window_hours > 0 and not platform.supports_time_filter:
            print(f"[warn] {platform.display_name} 搜索结果不含发布时间,"
                  f"publish_window_hours={window_hours} 无法生效,将照常采集")
        videos: List[VideoItem] = []
        try:
            for kw in keywords:
                found = platform.search(kw)
                print(f"[collect] {platform.display_name} '{kw}': {len(found)} 条")
                videos.extend(found)
        except OpencliAuthError as e:
            print(f"[auth] {platform.display_name} 未登录,跳过该平台: {e} ({platform.login_hint})")
            continue
        # 按 (platform, id) 去重,时间窗口兜底过滤
        seen, uniq = set(), []
        for v in videos:
            if (v.platform, v.id) in seen:
                continue
            seen.add((v.platform, v.id))
            if window_hours > 0 and v.publish_time and platform.supports_time_filter:
                try:
                    pt = datetime.datetime.fromisoformat(v.publish_time)
                    if datetime.datetime.now().astimezone() - pt > datetime.timedelta(hours=window_hours):
                        continue
                except ValueError:
                    pass
            uniq.append(v)
        if not uniq:
            print(f"[collect] {platform.display_name} 无可用结果")
            continue
        out = os.path.join(DATA_DIR, "videos", f"videos_{platform.name}_{_stamp()}.json")
        _write_json(out, [v.to_dict() for v in uniq])
        print(f"[collect] {platform.display_name} 去重后 {len(uniq)} 条 → {out}")
        outputs[platform.name] = out
    return outputs


def latest_videos_file(platform_name: str) -> str:
    files = sorted(glob.glob(os.path.join(DATA_DIR, "videos",
                                           f"videos_{platform_name}_*.json")))
    if not files:
        raise FileNotFoundError(
            f"未找到 {platform_name} 的视频数据,请先运行 --step collect")
    return files[-1]


# ---------------- ② 评论采集 + 正则匹配 ----------------

def compile_patterns(config: dict):
    match = config.get("match", {}) or {}
    includes = [re.compile(p) for p in match.get("patterns", [])]
    excludes = [re.compile(p) for p in match.get("exclude_patterns", [])]
    return includes, excludes


def match_comment(comment: CommentItem, includes, excludes) -> List[str]:
    """返回命中的模式串列表;命中排除模式返回空(即丢弃)。"""
    if any(p.search(comment.text) for p in excludes):
        return []
    return [p.pattern for p in includes if p.search(comment.text)]


def step_comments(config: dict, videos_files: Dict[str, str]) -> List[dict]:
    includes, excludes = compile_patterns(config)
    keep_all = bool((config.get("match", {}) or {}).get("keep_all", False))
    all_rows: List[dict] = []
    for pname, path in videos_files.items():
        platform = get_platform(pname, config)
        if not platform.supports_comments:
            print(f"[skip] {platform.display_name} 暂不支持评论采集")
            continue
        with open(path, "r", encoding="utf-8") as f:
            video_rows = json.load(f)
        print(f"[comments] {platform.display_name}: {len(video_rows)} 个视频待处理")
        for i, vr in enumerate(video_rows, 1):
            video = VideoItem(**{k: vr.get(k) for k in
                                 ("platform", "id", "url", "title", "author",
                                  "author_id", "publish_time", "search_keyword")})
            try:
                comments = platform.comments(video)
            except (OpencliAuthError, UnsupportedFeature) as e:
                print(f"[comments]   视频 {video.id} 跳过: {e}")
                continue
            except Exception as e:
                print(f"[comments]   视频 {video.id} 失败: {e}")
                continue
            kept = 0
            for c in comments:
                hits = match_comment(c, includes, excludes)
                if hits or keep_all:
                    c.matched_patterns = hits
                    all_rows.append(c.to_dict())
                    kept += 1
            matched = sum(1 for r in all_rows if r.get("matched_patterns"))
            print(f"[comments]   ({i}/{len(video_rows)}) {video.id} "
                  f"评论 {len(comments)} 条,保留 {kept} 条")
    return all_rows


def save_comment_outputs(config: dict, rows: List[dict]) -> Dict[str, str]:
    if not rows:
        print("[output] 无符合条件的评论,未生成文件")
        return {}
    out_cfg = config.get("output", {}) or {}
    formats = out_cfg.get("formats", ["json", "csv"])
    stamp = _stamp()
    # 按平台分组输出
    by_platform: Dict[str, list] = {}
    for r in rows:
        by_platform.setdefault(r.get("platform", "unknown"), []).append(r)
    outputs: Dict[str, str] = {}
    for pname, prows in by_platform.items():
        if "json" in formats:
            p = os.path.join(DATA_DIR, "comments", f"comments_{pname}_{stamp}.json")
            _write_json(p, prows)
            outputs["json"] = outputs.get("json", "") + f"{p} "
            print(f"[output] {p} ({len(prows)} 条)")
        if "csv" in formats:
            p = os.path.join(out_cfg.get("dir", "output"),
                             f"comments_{pname}_{stamp}.csv")
            _write_csv(p, prows, out_cfg.get("table_columns", []))
            outputs["csv"] = outputs.get("csv", "") + f"{p} "
            print(f"[output] {p}")
    return outputs
