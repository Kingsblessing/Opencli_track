# Opencli_track 一键引导(Windows):
#   项目内 Node + opencli + .venv,然后启动 WebUI。不改系统 PATH,不 npm -g。
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
. (Join-Path $PSScriptRoot "ensure-tools.ps1")

function Log($msg) { Write-Host "`n==> $msg" -ForegroundColor Blue }

Ensure-Uv
Ensure-Venv
Ensure-ProjectNode | Out-Null
Ensure-ProjectOpencli | Out-Null

$root = Get-RepoRoot
$cache = Join-Path $root ".opencli-home\cache"
New-Item -ItemType Directory -Force -Path $cache | Out-Null
$env:OPENCLI_CACHE_DIR = $cache
$env:PATH = "$(Join-Path $root '.tools\node');$env:PATH"

$argv = Get-ProjectOpencliArgv
Log "opencli 体检 (失败不中断,可在 WebUI 引导页继续)"
& $argv[0] $argv[1] doctor
if ($LASTEXITCODE -ne 0) {
    Write-Warning "opencli doctor 未通过。请在打开的 WebUI 里安装 Chrome 扩展并登录站点。"
}

Write-Host "`n提示: 抖音/小红书采集需先在 Chrome 登录对应网站;浏览器采集需要本机 Chrome。"
Log "启动 WebUI"
& (Join-Path $root ".venv\Scripts\python.exe") (Join-Path $root "main.py") --webui
exit $LASTEXITCODE
