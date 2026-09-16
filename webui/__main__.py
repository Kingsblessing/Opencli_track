"""WebUI 入口: python -m webui.server"""
from .server import start, load_config
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
    cfg = load_config().get("webui", {}) or {}
    start(host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 8765)),
          open_browser=bool(cfg.get("open_browser", True)))
