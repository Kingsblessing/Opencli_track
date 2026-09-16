"""opencli 子进程封装、错误分类、限速重试、数值/时间解析等通用工具。"""
import json
import re
import subprocess
import time
import datetime
from typing import List, Optional


class OpencliError(Exception):
    """opencli 调用失败(非登录原因)。"""


class OpencliAuthError(OpencliError):
    """平台需要登录(Chrome 会话未登录对应站点)。"""


class UnsupportedFeature(Exception):
    """平台未实现该能力。"""


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

    def run(self, args: List[str], allow_empty: bool = False) -> object:
        """执行 opencli 子命令并返回解析后的 JSON;失败抛 OpencliError/AuthError。

        allow_empty=True 时,EMPTY_RESULT(例如视频没有评论)返回 [] 而非报错。
        """
        for attempt in range(self.retry + 1):
            if self.checkpoint:
                self.checkpoint()
            self._throttle()
            proc = subprocess.run(
                ["opencli"] + args, capture_output=True, text=True,
                timeout=self.timeout,
            )
            out = proc.stdout.strip()
            if proc.returncode == 0:
                return self._parse_json(out)
            code, message, help_ = self._parse_error(out + "\n" + proc.stderr)
            if code == "AUTH_REQUIRED":
                raise OpencliAuthError(message)
            if code == "EMPTY_RESULT" and allow_empty:
                return []
            if attempt < self.retry:
                time.sleep(self.interval)
                continue
            raise OpencliError(f"opencli {' '.join(args[:4])} failed: [{code}] {message} {help_}")
        raise OpencliError("unreachable")

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
