"""任务管理:WebUI 后台任务的进度上报、暂停/恢复/停止(协作式检查点)。"""
import threading
import time
import traceback
from collections import deque
from typing import Callable, Optional


class JobStopped(Exception):
    """任务被用户停止。"""


def _summarize(ctx, rows) -> None:
    """把判定结果汇总进日志(WebUI 与 CLI 共用口径)。"""
    if not rows:
        ctx.log("[done] 无输出记录")
        return
    if (rows[0].get("lead_level") or "") == "MATCH":
        ctx.log(f"[done] 输出 {len(rows)} 条(旧关键词模式)")
        return
    from collections import Counter
    lv = Counter(r.get("lead_level") or "?" for r in rows)
    it = Counter(r.get("intent") or "?" for r in rows)
    ctx.log(f"[done] 输出 {len(rows)} 条 | HIGH {lv['HIGH']} / MEDIUM {lv['MEDIUM']} / "
            f"LOW {lv['LOW']} / EXCLUDE {lv['EXCLUDE']} | "
            f"customer {it['customer']} / provider {it['provider']} / "
            f"irrelevant {it['irrelevant']}")


class Control:
    """协作式控制:检查点处响应暂停/停止。

    - pause 后,checkpoint() 会阻塞直到 resume 或 stop
    - stop 后,checkpoint() 抛出 JobStopped,pipeline 各层向上传播终止任务
    """

    def __init__(self):
        self._stop = threading.Event()
        self._paused = threading.Event()

    def checkpoint(self):
        if self._stop.is_set():
            raise JobStopped()
        while self._paused.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        if self._stop.is_set():
            raise JobStopped()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stop.set()
        self._paused.clear()   # 解除 pause 阻塞,让 checkpoint 立刻抛出


class JobCtx:
    """传给 pipeline 的上下文:日志 + 进度事件 + 检查点。"""

    def __init__(self, control: Control,
                 log_fn: Callable[[str], None],
                 progress_fn: Callable[..., None]):
        self.control = control
        self._log = log_fn
        self._progress = progress_fn

    def log(self, msg: str):
        self._log(msg)

    def progress(self, phase: str, current: int = 0, total: int = 0, detail: str = ""):
        self._progress(phase, current, total, detail)

    def checkpoint(self):
        self.control.checkpoint()


class _NullCtx(JobCtx):
    """CLI 直接运行时的空实现:日志走 print,无进度。"""

    def __init__(self):
        super().__init__(Control(), print, lambda *a, **k: None)

    def checkpoint(self):
        pass


class JobManager:
    """单任务管理器:同一时间只允许一个采集任务在跑。"""

    _instance: Optional["JobManager"] = None

    @classmethod
    def instance(cls) -> "JobManager":
        if cls._instance is None:
            cls._instance = JobManager()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._control = Control()
        self._logs: deque = deque(maxlen=2000)
        self._state = {
            "state": "idle",          # idle / running / paused / stopping / done / error / stopped
            "step": "", "error": "", "started_at": "", "finished_at": "",
            "phase": "", "current": 0, "total": 0, "detail": "",
        }

    # ---- 对外控制 ----

    def start(self, step: str, config: dict, config_path: str) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._control = Control()
            self._logs.clear()
            self._state.update(state="running", step=step, error="",
                               started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                               finished_at="", phase="", current=0, total=0, detail="")
            ctx = JobCtx(self._control, self.log,
                         lambda phase, current=0, total=0, detail="":
                         self._progress(phase, current, total, detail))
            self._thread = threading.Thread(
                target=self._run, args=(step, config, config_path, ctx), daemon=True)
            self._thread.start()
            return True

    def _run(self, step: str, config: dict, config_path: str, ctx: JobCtx):
        from . import pipeline  # 延迟导入避免循环
        try:
            if step == "match":
                # 仅重跑判定:读已有原始语料,不发网络请求
                rows = pipeline.step_match(config, ctx)
                pipeline.save_comment_outputs(config, rows, ctx)
                _summarize(ctx, rows)
            else:
                if step in ("collect", "all"):
                    video_files = pipeline.step_collect(config, config_path, ctx)
                else:
                    video_files = {}
                    for pname in config.get("platforms", []):
                        try:
                            video_files[pname] = pipeline.latest_videos_file(pname)
                            ctx.log(f"[comments] 使用最近一次采集: {video_files[pname]}")
                        except FileNotFoundError as e:
                            ctx.log(f"[warn] {e}")
                if step in ("comments", "all"):
                    rows = pipeline.step_comments(config, video_files, ctx)
                    pipeline.save_comment_outputs(config, rows, ctx)
                    _summarize(ctx, rows)
            self._state["state"] = "stopped" if self._control._stop.is_set() else "done"
        except JobStopped:
            self._state["state"] = "stopped"
            self.log("[stopped] 任务已停止")
        except Exception as e:
            self._state["state"] = "error"
            self._state["error"] = str(e)
            self.log(f"[error] {e}")
            self.log(traceback.format_exc()[-1500:])
        finally:
            self._state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def pause(self):
        if self._state["state"] == "running":
            self._control.pause()
            self._state["state"] = "paused"

    def resume(self):
        if self._state["state"] == "paused":
            self._control.resume()
            self._state["state"] = "running"

    def stop(self):
        if self._state["state"] in ("running", "paused"):
            self._state["state"] = "stopping"
            self._control.stop()

    # ---- 状态与事件 ----

    def log(self, msg: str):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self._logs.append(line)
        print(msg)

    def _progress(self, phase: str, current: int, total: int, detail: str):
        self._state.update(phase=phase, current=current, total=total, detail=detail)

    def status(self) -> dict:
        st = dict(self._state)
        st["logs"] = list(self._logs)[-200:]
        return st

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
