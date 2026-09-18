"""opencli 子进程封装、错误分类、限速重试、数值/时间解析等通用工具。"""
import json
import os
import re
import shutil
import subprocess
import time
import datetime
from pathlib import Path
from typing import List, Optional


class OpencliError(Exception):
    """opencli 调用失败(非登录原因)。"""


class OpencliAuthError(OpencliError):
    """平台需要登录(Chrome 会话未登录对应站点)。"""


class UnsupportedFeature(Exception):
    """平台未实现该能力。"""


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _project_opencli_argv() -> Optional[List[str]]:
    """项目内 .tools 的 node + main.js。不依赖 PATH,也不会走 .cmd。"""
    root = _project_root()
    main = root / ".tools" / "opencli" / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"
    if not main.is_file():
        return None
    for node in (
        root / ".tools" / "node" / "node.exe",
        root / ".tools" / "node" / "bin" / "node",
    ):
        if node.is_file():
            return [str(node), str(main)]
    node = shutil.which("node")
    if node and not node.lower().endswith((".cmd", ".bat")):
        return [node, str(main)]
    return None


def _node_beside(start: str) -> Optional[str]:
    """从垫片所在目录向上找 node.exe / node,避开 PATH 里的坏垫片。"""
    cur = os.path.dirname(os.path.abspath(start))
    for _ in range(6):
        for name in ("node.exe", "node"):
            cand = os.path.join(cur, name)
            if os.path.isfile(cand):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    node = shutil.which("node")
    if node and not node.lower().endswith((".cmd", ".bat")):
        return node
    return None


def _parse_cmd_node_entry(cmd_path: str) -> Optional[List[str]]:
    """从 .cmd/.bat 垫片解析出 [node, script.js]。

    覆盖官方 `node "...js"` 与 npm 垫片 `"%_prog%" "%dp0%\\node_modules\\...js"`。
    解析失败则返回 None —— 调用方不得回退执行 .cmd(cmd 会把 URL 中的 & 截断)。
    """
    try:
        text = open(cmd_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    cmd_dir = os.path.dirname(os.path.abspath(cmd_path))

    def expand(raw: str) -> str:
        s = raw.strip().strip('"')
        repl = cmd_dir + os.sep
        s = re.sub(r"%dp0%", lambda _: repl, s, flags=re.I)
        s = re.sub(r"%~dp0", lambda _: repl, s, flags=re.I)
        return os.path.normpath(s)

    scripts: List[str] = []
    for m in re.finditer(r'"([^"]+\.js)"', text, re.I):
        scripts.append(expand(m.group(1)))
    for m in re.finditer(r'((?:%dp0%|%~dp0)[\\/][^\s"]+\.js)', text, re.I):
        scripts.append(expand(m.group(1)))
    for m in re.finditer(r'\bnode(?:\.exe)?\s+"?([^\s"]+\.js)"?', text, re.I):
        scripts.append(expand(m.group(1)))

    js = next((p for p in scripts if os.path.isfile(p)), None)
    if not js:
        return None
    node = _node_beside(cmd_path) or _node_beside(js)
    if not node:
        return None
    return [node, js]


def _resolve_opencli() -> List[str]:
    """返回调用 opencli 的 argv 前缀 [node, main.js] 或 Unix 可执行文件。

    优先项目内 .tools。禁止直接执行 .cmd/.bat:Windows 上 cmd 会把 & 当成分隔符。
    """
    local = _project_opencli_argv()
    if local:
        return local

    exe = shutil.which("opencli")
    if not exe:
        raise OpencliError(
            "opencli not found。请双击 Start.bat 安装项目内工具链,"
            "或设置 PATH 中的 opencli(不要用 WindowsApps 残留垫片)。"
        )
    low = exe.lower()
    if low.endswith((".cmd", ".bat")):
        argv = _parse_cmd_node_entry(exe)
        if argv:
            return argv
        raise OpencliError(
            f"opencli 垫片无法解析为 node+js,已拒绝执行 .cmd: {exe}"
        )
    return [exe]


def doctor_report() -> dict:
    """跑项目内 opencli doctor,解析 daemon / extension / connectivity。"""
    try:
        argv = _resolve_opencli()
    except OpencliError as e:
        return {"ok": False, "daemon": False, "extension": False,
                "connectivity": False, "returncode": None, "raw": str(e), "argv": []}
    try:
        proc = subprocess.run(
            argv + ["doctor"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "daemon": False, "extension": False,
                "connectivity": False, "returncode": None, "raw": str(e), "argv": argv}
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()

    def has_ok(label: str) -> bool:
        return bool(re.search(rf"\[OK\][^\n]*{label}", text, re.I))

    daemon = has_ok("Daemon")
    extension = has_ok("Extension")
    connectivity = has_ok("Connectivity")
    return {
        "ok": proc.returncode == 0 and daemon and extension and connectivity,
        "daemon": daemon,
        "extension": extension,
        "connectivity": connectivity,
        "returncode": proc.returncode,
        "raw": text[-4000:],
        "argv": argv,
    }


class Runner:
    """带限速/重试的 opencli 调用器,输出统一解析为 JSON。

    checkpoint: 可选回调,在每次调用前触发(用于 WebUI 任务暂停/停止检查点)。
    """

    def __init__(self, interval: float = 3.0, retry: int = 2, timeout: int = 180,
                 checkpoint=None):
        self.interval = interval
        self.retry = retry
        self.timeout = timeout
        self.checkpoint = checkpoint
        self._last_call = 0.0
        self._opencli = _resolve_opencli()

    def run(self, args: List[str], allow_empty: bool = False) -> object:
        """执行 opencli 子命令并返回解析后的 JSON;失败抛 OpencliError/AuthError。

        allow_empty=True 时,EMPTY_RESULT(例如视频没有评论)返回 [] 而非报错。
        """
        daemon_restarted = False
        for attempt in range(self.retry + 1):
            if self.checkpoint:
                self.checkpoint()
            self._throttle()
            proc = subprocess.run(
                self._opencli + args, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self.timeout,
            )
            out = (proc.stdout or "").strip()
            if proc.returncode == 0:
                return self._parse_json(out)
            code, message, help_ = self._parse_error(out + "\n" + (proc.stderr or ""))
            if code == "AUTH_REQUIRED":
                raise OpencliAuthError(message)
            if code == "EMPTY_RESULT" and allow_empty:
                return []
            if (not daemon_restarted) and "navigation rejected" in (message or "").lower():
                daemon_restarted = True
                self._restart_daemon()
                continue
            if attempt < self.retry:
                time.sleep(self.interval)
                continue
            shown = " ".join(args)
            if len(shown) > 500:
                shown = shown[:500] + "…"
            raise OpencliError(f"opencli {shown} failed: [{code}] {message} {help_}")
        raise OpencliError("unreachable")

    def _restart_daemon(self):
        """浏览器桥会话脏掉时重启 daemon,再重试一次(不经 .cmd)。"""
        try:
            subprocess.run(
                self._opencli + ["daemon", "restart"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        time.sleep(3)

    def _throttle(self):
        wait = self.interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    @staticmethod
    def _parse_json(text: str) -> object:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # browser eval 等命令可能输出裸标量或纯文本
            return text

    @staticmethod
    def _parse_error(text: str):
        code = re.search(r"code:\s*(\w+)", text)
        msg = re.search(r"message:\s*['\"]?(.+?)['\"]?\s*\n", text)
        hlp = re.search(r"help:\s*['\"]?(.+?)['\"]?\s*\n", text)
        return (code.group(1) if code else f"EXIT",
                msg.group(1) if msg else text.strip()[:200],
                hlp.group(1) if hlp else "")


def parse_count(text: str) -> int:
    """'3.2万' -> 32000;'3385' -> 3385;解析失败返回 0。"""
    if text is None:
        return 0
    s = str(text).strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*(万|亿)?", s)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    if m.group(2) == "万":
        n *= 10_000
    elif m.group(2) == "亿":
        n *= 100_000_000
    return int(n)


def parse_relative_time(text: str, base: Optional[datetime.datetime] = None) -> Optional[str]:
    """相对/绝对时间文本 → ISO8601,失败返回 None。

    采用前缀匹配以兼容平台附加的后缀(小红书评论时间常带 IP 属地,
    如 '昨天 16:44中国香港' / '31分钟前重庆' / '3天前')。
    """
    iso, _ = parse_time_with_region(text, base)
    return iso


def parse_time_with_region(text: str, base: Optional[datetime.datetime] = None):
    """返回 (ISO8601 或 None, 附加后缀/属地文本)。

    支持: 刚刚 / 今天|昨天|前天[ HH:MM] / N(秒|分钟|小时|天|周|个月|月|年)前 /
    YYYY-MM-DD[ HH:MM[:SS]] / MM-DD[ HH:MM] / YYYY年MM月DD日,允许带任意后缀。
    """
    if not text:
        return None, ""
    now = base or datetime.datetime.now().astimezone()
    s = str(text).strip().lstrip("·").strip()
    s = re.sub(r"^(编辑于|发布于|修改于)\s*", "", s).strip()
    if not s:
        return None, ""
    suffix = ""
    dt = None

    m = re.match(r"^(刚刚)", s)
    if m:
        dt = now
        suffix = s[m.end():]
    if dt is None:
        m = re.match(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})?", s)
        if m:
            days = {"今天": 0, "昨天": 1, "前天": 2}[m.group(1)]
            dt = (now - datetime.timedelta(days=days))
            if m.group(2):
                dt = dt.replace(hour=int(m.group(2)),
                                minute=int(m.group(3) or 0), second=0, microsecond=0)
            suffix = s[m.end():]
    if dt is None:
        m = re.match(r"^(\d+)\s*(秒|分钟|小时|天|周|个月|月|年)前", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            deltas = {"秒": datetime.timedelta(seconds=n),
                      "分钟": datetime.timedelta(minutes=n),
                      "小时": datetime.timedelta(hours=n),
                      "天": datetime.timedelta(days=n),
                      "周": datetime.timedelta(weeks=n),
                      "个月": datetime.timedelta(days=30 * n),
                      "月": datetime.timedelta(days=30 * n),
                      "年": datetime.timedelta(days=365 * n)}
            dt = now - deltas[unit]
            suffix = s[m.end():]
    if dt is None:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s)
        if m:
            dt = now.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)),
                             hour=int(m.group(4) or 0), minute=int(m.group(5) or 0),
                             second=0, microsecond=0)
            suffix = s[m.end():]
    if dt is None:
        m = re.match(r"^(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s)
        if m:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                             hour=int(m.group(3) or 0), minute=int(m.group(4) or 0),
                             second=0, microsecond=0)
            suffix = s[m.end():]
    if dt is None:
        m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", s)
        if m:
            dt = now.replace(year=int(m.group(1)), month=int(m.group(2)),
                             day=int(m.group(3)), hour=0, minute=0, second=0, microsecond=0)
            suffix = s[m.end():]
    if dt is None:
        return None, ""
    return dt.isoformat(timespec="seconds"), suffix.strip()


def first_nonempty(d: dict, keys: List[str], default=""):
    """从候选键名中取第一个非空值——用于平台输出列名的防御性映射。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default
