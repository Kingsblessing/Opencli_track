# Opencli_track 一键引导(Windows PowerShell):
#   安装 uv + Python + 依赖 + Node/opencli,然后启动 WebUI。幂等,可重复执行。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Log($msg) { Write-Host "`n==> $msg" -ForegroundColor Blue }

# ---------- 1. uv ----------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Log "安装 uv (Python 环境管理器)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}

# ---------- 2. Python venv + 依赖 ----------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Log "创建 Python 虚拟环境 (uv 自动下载所需 Python)"
    uv venv .venv --python 3.11
}
Log "安装 Python 依赖"
uv pip install --python .venv\Scripts\python.exe -q -r requirements.txt

# ---------- 3. Node + opencli ----------
if (-not (Get-Command opencli -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Log "未检测到 Node.js,尝试 winget 安装…"
        winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("PATH", "User")
    }
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Log "安装 opencli (npm 全局)"
        npm install -g @jackwener/opencli
    } else {
        Write-Warning "无法自动安装 Node.js,请手动安装: https://nodejs.org"
        exit 1
    }
}

# ---------- 4. 体检 ----------
Log "opencli 体检"
opencli doctor
Write-Host "`n提示: 抖音/小红书采集需先在 Chrome 登录对应网站;浏览器采集需要本机 Chrome。"

# ---------- 5. 启动 WebUI ----------
Log "启动 WebUI"
& .venv\Scripts\python.exe main.py --webui
