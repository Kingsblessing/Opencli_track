# 转发到项目内引导。请优先双击 Start.bat。
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "scripts\bootstrap.ps1")
exit $LASTEXITCODE
