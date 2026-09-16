#!/usr/bin/env python3
"""多平台视频/评论采集 CLI 入口。

用法:
  python3 main.py --config config.yaml --step collect    # ① 采集视频/帖子
  python3 main.py --config config.yaml --step comments   # ② 评论采集+意图识别
  python3 main.py --config config.yaml --step all        # ①+②
  python3 main.py --config config.yaml --step match      # 仅重跑意图判定(不联网)
  python3 main.py --webui                                # 启动 WebUI 控制台
  python3 main.py --list-platforms                       # 查看已注册平台
"""
import argparse
import sys

from src.pipeline import (load_config, step_collect, step_comments,
                          step_match, save_comment_outputs, latest_videos_file)
from src.platforms import available_platforms


def main():
    ap = argparse.ArgumentParser(description="多平台视频/评论采集管道")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--step", choices=["collect", "comments", "all", "match"],
                    default="all",
                    help="match=仅对已有原始语料重跑意图判定(不联网)")
    ap.add_argument("--webui", action="store_true", help="启动 WebUI 控制台")
    ap.add_argument("--list-platforms", action="store_true")
    args = ap.parse_args()

    if args.list_platforms:
        print("已注册平台:", ", ".join(available_platforms()))
        return 0

    config = load_config(args.config)

    if args.webui:
        from webui.server import start
        w = config.get("webui", {}) or {}
        start(host=w.get("host", "127.0.0.1"), port=int(w.get("port", 8765)),
              open_browser=bool(w.get("open_browser", True)))
        return 0

    # --step match:直接对已有原始语料重跑判定,不发任何网络请求
    if args.step == "match":
        rows = step_match(config)
        save_comment_outputs(config, rows)
        _summarize(rows)
        return 0

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
        _summarize(rows)
    return 0


def _summarize(rows) -> None:
    match_mode = "intent"
    if not rows:
        print("[done] 无输出记录")
        return
    if (rows[0].get("lead_level") or "") == "MATCH":
        match_mode = "keyword"
    if match_mode == "intent":
        from collections import Counter
        lv = Counter(r.get("lead_level") or "?" for r in rows)
        it = Counter(r.get("intent") or "?" for r in rows)
        print(f"[done] 输出 {len(rows)} 条 | "
              f"HIGH {lv['HIGH']} / MEDIUM {lv['MEDIUM']} / "
              f"LOW {lv['LOW']} / EXCLUDE {lv['EXCLUDE']} | "
              f"customer {it['customer']} / provider {it['provider']} / "
              f"irrelevant {it['irrelevant']}")
    else:
        print(f"[done] 输出 {len(rows)} 条")


if __name__ == "__main__":
    sys.exit(main())
