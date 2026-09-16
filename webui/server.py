"""WebUI 后端:FastAPI + WebSocket,只绑定 127.0.0.1。

启动: .venv/bin/python main.py --webui  (或 python -m webui.server)
"""
import asyncio
import csv
import json
import os
import webbrowser
import yaml
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jobs import JobManager
from src.pipeline import (load_config, validate_config, save_config,
                          DEFAULT_CONFIG_PATH, ROOT)
from src.platforms import available_platforms

app = FastAPI(title="Opencli_track WebUI")
STATIC_DIR = Path(__file__).parent / "static"

# 结果目录白名单:只允许查看/下载这些目录下的文件
RESULT_DIRS = [
    os.path.join(ROOT, "data", "videos"),
    os.path.join(ROOT, "data", "comments"),
    os.path.join(ROOT, "output"),
]


def _safe_path(rel_path: str) -> str:
    """防止路径穿越:目标必须落在白名单目录内。"""
    p = os.path.realpath(os.path.join(ROOT, rel_path))
    for d in RESULT_DIRS:
        if p.startswith(os.path.realpath(d) + os.sep):
            return p
    raise HTTPException(400, f"路径不在结果目录内: {rel_path}")


# ---------------- 配置 ----------------

class ConfigBody(BaseModel):
    config: dict


@app.get("/api/config")
def get_config():
    return {"config": load_config(), "errors": validate_config(load_config()),
            "available_platforms": available_platforms()}


@app.put("/api/config")
def put_config(body: ConfigBody):
    errors = validate_config(body.config)
    if errors:
        raise HTTPException(422, detail=errors)
    save_config(body.config)
    return {"ok": True}


# ---------------- 任务控制 ----------------

class RunBody(BaseModel):
    step: str  # collect / comments / all / match


@app.post("/api/run")
def run(body: RunBody):
    if body.step not in ("collect", "comments", "all", "match"):
        raise HTTPException(422, "step 必须是 collect/comments/all/match")
    mgr = JobManager.instance()
    config = load_config()
    errors = validate_config(config)
    if errors:
        raise HTTPException(422, detail=errors)
    ok = mgr.start(body.step, config, DEFAULT_CONFIG_PATH)
    if not ok:
        raise HTTPException(409, "已有任务在运行中")
    return {"ok": True, "state": "running"}


@app.post("/api/pause")
def pause():
    JobManager.instance().pause()
    return {"ok": True}


@app.post("/api/resume")
def resume():
    JobManager.instance().resume()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    JobManager.instance().stop()
    return {"ok": True}


@app.get("/api/job")
def job_status():
    return JobManager.instance().status()


@app.websocket("/api/ws")
async def ws_progress(ws: WebSocket):
    """定时推送任务状态快照(状态 + 日志尾部),前端据此刷新进度条与日志。

    注意:这里必须用 asyncio.sleep 而不是 time.sleep——本函数是 async,
    阻塞式 sleep 会卡住整个事件循环,导致所有 HTTP 请求(如 /api/config)
    在多个 WS 连接下排队变慢。
    """
    await ws.accept()
    try:
        while True:
            await ws.send_json(JobManager.instance().status())
            # 等待客户端 ping;超时则继续推送(客户端未及时回 pong 也不断开)
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------- 结果查看 ----------------

@app.get("/api/outputs")
def list_outputs():
    files = []
    for d in RESULT_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            files.append({
                "path": os.path.relpath(p, ROOT),
                "name": name,
                "kind": "videos" if "videos" in d else
                        ("comments" if "comments" in d else "tables"),
                "size": os.path.getsize(p),
                "mtime": os.path.getmtime(p),
            })
    files.sort(key=lambda x: -x["mtime"])
    return {"files": files}


@app.get("/api/output")
def read_output(path: str, limit: int = 500):
    p = _safe_path(path)
    if not os.path.exists(p):
        raise HTTPException(404, "文件不存在")
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == ".json":
            with open(p, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, dict):
                rows = [rows]
            cols = list(rows[0].keys()) if rows else []
            return {"type": "json", "columns": cols, "rows": rows[:limit],
                    "total": len(rows)}
        if ext == ".csv":
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                all_rows = list(reader)
            cols = all_rows[0] if all_rows else []
            return {"type": "csv", "columns": cols,
                    "rows": all_rows[1:limit + 1], "total": max(len(all_rows) - 1, 0)}
        # 其他类型:按文本预览(如日志)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(limit * 200)
        return {"type": "text", "columns": [], "rows": [], "text": text,
                "total": 0}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(422, f"解析失败: {e}")


@app.get("/api/download")
def download(path: str):
    p = _safe_path(path)
    if not os.path.exists(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=os.path.basename(p))


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/app.js", response_class=FileResponse)
def app_js():
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


@app.get("/style.css", response_class=FileResponse)
def style_css():
    return FileResponse(STATIC_DIR / "style.css", media_type="text/css")


def start(host="127.0.0.1", port=8765, open_browser=True):
    if open_browser:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    start()
