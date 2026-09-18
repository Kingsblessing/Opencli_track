# 项目内 Node + opencli。只写仓库 .tools\,不改系统 PATH,不 npm install -g。
$ErrorActionPreference = "Stop"

$script:NodeVersion = "22.23.2"

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Write-ToolLog($msg) {
    Write-Host "`n==> $msg" -ForegroundColor Blue
}

function Ensure-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-ToolLog "安装 uv (Python 环境管理器,仅此一步写入用户目录)"
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv 安装失败,请手动安装: https://docs.astral.sh/uv/"
    }
}

function Ensure-Venv {
    $root = Get-RepoRoot
    Set-Location $root
    $py = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Write-ToolLog "创建 Python 虚拟环境 (uv 自动下载所需 Python)"
        uv venv .venv --python 3.11
    }
    Write-ToolLog "安装 Python 依赖"
    uv pip install --python $py -q -r requirements.txt
}

function Get-NodeArch {
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { return "win-arm64" }
    return "win-x64"
}

function Ensure-ProjectNode {
    $root = Get-RepoRoot
    $nodeDir = Join-Path $root ".tools\node"
    $nodeExe = Join-Path $nodeDir "node.exe"
    if (Test-Path $nodeExe) { return $nodeDir }

    $arch = Get-NodeArch
    $ver = $script:NodeVersion
    $zipName = "node-v$ver-$arch.zip"
    $url = "https://nodejs.org/dist/v$ver/$zipName"
    $tmp = Join-Path $env:TEMP $zipName
    Write-ToolLog "下载便携 Node $ver ($arch) 到项目 .tools\node"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    $extract = Join-Path $env:TEMP "opencli-track-node-$ver"
    if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
    Expand-Archive -Path $tmp -DestinationPath $extract -Force
    $inner = Join-Path $extract "node-v$ver-$arch"
    if (-not (Test-Path $inner)) { throw "Node 压缩包结构异常: $inner" }
    New-Item -ItemType Directory -Force -Path (Split-Path $nodeDir) | Out-Null
    if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
    Move-Item $inner $nodeDir
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $nodeExe)) { throw "Node 解压后未找到 node.exe" }
    return $nodeDir
}

function Ensure-ProjectOpencli {
    $root = Get-RepoRoot
    $nodeDir = Ensure-ProjectNode
    $prefix = Join-Path $root ".tools\opencli"
    $main = Join-Path $prefix "node_modules\@jackwener\opencli\dist\src\main.js"
    if (Test-Path $main) { return $main }

    Write-ToolLog "安装 opencli 到项目 .tools\opencli (非全局)"
    New-Item -ItemType Directory -Force -Path $prefix | Out-Null
    $cache = Join-Path $root ".tools\npm-cache"
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $npmCli = Join-Path $nodeDir "node_modules\npm\bin\npm-cli.js"
    $nodeExe = Join-Path $nodeDir "node.exe"
    $env:npm_config_cache = $cache
    & $nodeExe $npmCli install --prefix $prefix @jackwener/opencli
    if ($LASTEXITCODE -ne 0) { throw "npm install @jackwener/opencli 失败 (exit $LASTEXITCODE)" }
    if (-not (Test-Path $main)) { throw "opencli 已安装但未找到 $main" }
    return $main
}

function Get-ProjectOpencliArgv {
    $root = Get-RepoRoot
    $node = Join-Path $root ".tools\node\node.exe"
    $main = Join-Path $root ".tools\opencli\node_modules\@jackwener\opencli\dist\src\main.js"
    if (-not (Test-Path $node) -or -not (Test-Path $main)) {
        throw "项目内 opencli 未就绪,请先运行 Ensure-ProjectOpencli"
    }
    return @($node, $main)
}
