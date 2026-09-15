"""平台注册表:新增平台只需实现 PlatformBase 并在此注册。"""
from typing import Dict, Type, List

from .base import PlatformBase
from .bilibili import BilibiliPlatform
from .douyin import DouyinPlatform
from .redbook import RedbookPlatform

REGISTRY: Dict[str, Type[PlatformBase]] = {
    "bilibili": BilibiliPlatform,
    "douyin": DouyinPlatform,
    "redbook": RedbookPlatform,
    "xiaohongshu": RedbookPlatform,  # 别名
}


def available_platforms() -> List[str]:
    return sorted(REGISTRY.keys())


def get_platform(name: str, config: dict) -> PlatformBase:
    cls = REGISTRY.get(name.lower())
    if cls is None:
        raise KeyError(
            f"未知平台 '{name}',可选: {', '.join(available_platforms())}")
    return cls(config)
