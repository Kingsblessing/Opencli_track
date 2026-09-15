#!/usr/bin/env python3
"""多平台视频/评论采集 CLI 入口。

用法:
  python3 main.py --config config.yaml --step collect    # ① 采集视频/帖子
  python3 main.py --config config.yaml --step comments   # ② 评论采集+正则匹配
  python3 main.py --config config.yaml --step all        # ①+②
  python3 main.py --list-platforms                       # 查看已注册平台
"""
import argparse
import sys

from src.pipeline import (load_config, step_collect, step_comments,
                          save_comment_outputs, latest_videos_file)
from src.platforms import available_platforms


def main():
    ap = argparse.ArgumentParser(description="多平台视频/评论采集管道")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--step", choices=["collect", "comments", "all"], default="all")
    ap.add_argument("--list-platforms", action="store_true")
    args = ap.parse_args()

    if args.list_platforms:
        print("已注册平台:", ", ".join(available_platforms()))
        return 0

    config = load_config(args.config)

    if args.step in ("collect", "all"):
        video_files = step_collect(config, args.config)
    else:
        # comments 单独运行时,自动取各平台最近一次的采集结果
        video_files = {}
        for pname in config.get("platforms", []):
            try:
                video_files[pname] = latest_videos_file(pname)
                print(f"[comments] 使用最近一次采集: {video_files[pname]}")
            except FileNotFoundError as e:
                print(f"[warn] {e}")

    if args.step in ("comments", "all"):
        rows = step_comments(config, video_files)
        save_comment_outputs(config, rows)
        matched = sum(1 for r in rows if r.get("matched_patterns"))
        print(f"[done] 评论总数 {len(rows)},其中命中关键词 {matched} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
