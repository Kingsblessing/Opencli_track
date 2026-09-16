#!/usr/bin/env bash
# Opencli_track 一键引导(macOS / Linux):
#   安装 uv + Python + 依赖 + Node/opencli,然后启动 WebUI。
# 幂等:重复执行安全,已装好的部分会自动跳过。
set -e
cd "$(dirname "$0")"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ---------- 1. uv(自动管理 Python) ----------
if ! command -v uv >/dev/null 2>&1; then
  log "安装 uv (Python 环境管理器)"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    wget -qO- https://astral.sh/uv/install.sh | sh
  fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "uv 安装失败,请手动安装: https://docs.astral.sh/uv/"; exit 1; }

# ---------- 2. Python venv + 依赖 ----------
if [ ! -x .venv/bin/python ]; then
  log "创建 Python 虚拟环境 (uv 自动下载所需 Python)"
  uv venv .venv --python 3.11
fi
log "安装 Python 依赖"
uv pip install --python .venv/bin/python -q -r requirements.txt

# ---------- 3. Node + opencli ----------
if ! command -v opencli >/dev/null 2>&1; then
  if ! command -v npm >/dev/null 2>&1; then
    log "未检测到 Node.js/npm"
    if command -v brew >/dev/null 2>&1; then
      echo "尝试用 Homebrew 安装 Node LTS…"; brew install node@22 || true
    elif command -v apt-get >/dev/null 2>&1; then
      echo "尝试用 apt 安装 Node…"; (curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs) || true
    fi
  fi
  if command -v npm >/dev/null 2>&1; then
    log "安装 opencli (npm 全局)"
    npm install -g @jackwener/opencli
  else
    echo "⚠ 无法自动安装 Node.js。请手动安装后重跑本脚本:"
    echo "    macOS:   brew install node"
    echo "    Ubuntu:  sudo apt install nodejs npm"
    echo "    Windows: https://nodejs.org"
    exit 1
  fi
fi

# ---------- 4. 体检 ----------
log "opencli 体检"
opencli doctor || true
echo
echo "提示: 抖音/小红书采集需先在 Chrome 登录对应网站;浏览器采集需要本机 Chrome。"

# ---------- 5. 启动 WebUI ----------
log "启动 WebUI"
exec .venv/bin/python main.py --webui
