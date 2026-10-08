<#
.SYNOPSIS
    启动「电商问数」基础设施容器（MySQL / ES / Kibana / Qdrant / Embedding）。

.DESCRIPTION
    负责三件事：
      1. 确认 Docker 引擎已就绪（未就绪则尝试拉起 Docker Desktop 并轮询等待）
      2. 拉起 docker-compose 定义的 5 个容器
      3. 轮询各服务健康状态，直到全部可达或超时

    本脚本只做「起依赖」，不启动后端与前端。理由：容器冷启动（尤其
    Elasticsearch 与 Embedding 的模型加载）往往需要数十秒到数分钟，
    与后端进程的生命周期解耦后，排错时能明确区分「依赖没好」还是
    「应用起不来」。

.PARAMETER SkipKibana
    跳过 Kibana。Kibana 仅用于可视化排查，16GB 内存吃紧时可省下约 1GB。

.PARAMETER TimeoutSeconds
    等待全部服务就绪的最长秒数，默认 300。

.EXAMPLE
    .\scripts\start-infra.ps1
    启动全部基础设施并等待就绪。

.EXAMPLE
    .\scripts\start-infra.ps1 -SkipKibana -TimeoutSeconds 600
    跳过 Kibana，最多等待 10 分钟。
#>

[CmdletBinding()]
param(
    [switch]$SkipKibana,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# 项目根目录取脚本所在目录的上一级，避免硬编码盘符路径
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker\docker-compose.yaml'

if (-not (Test-Path $ComposeFile)) {
    throw "找不到 compose 文件：$ComposeFile"
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ">>> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    [OK]   $Message" -ForegroundColor Green
}

function Write-Warn2([string]$Message) {
    Write-Host "    [WARN] $Message" -ForegroundColor Yellow
}

function Test-TcpPort {
    <#
      快速 TCP 连通性探测。
      不用 Test-NetConnection：它会先发 ICMP 再建 TCP，每次约 1 秒，
      且进度输出会污染终端。这里直接 TcpClient 建连，超时可控。
    #>
    param(
        [string]$TargetHost = '127.0.0.1',
        [int]$Port,
        [int]$TimeoutMs = 1500
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-DockerEngine {
    <#
      判定 Docker 引擎是否可用。

      注意两点：
      1. docker CLI 存在不代表引擎在跑，必须以 `docker info` 的退出码为准。
      2. 引擎未跑时 docker 会往 stderr 写错误信息，在 $ErrorActionPreference='Stop'
         下会被 PowerShell 包装成 NativeCommandError 并中断脚本。这里临时把
         错误策略降为 Continue——探测失败是预期分支，不该抛异常。
    #>
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = docker info --format '{{.ServerVersion}}' 2>&1
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Start-DockerDesktopIfNeeded {
    <#
      引擎未就绪时尝试拉起 Docker Desktop。
      安装路径不固定（本机在 %LOCALAPPDATA%），因此按候选列表逐个探测。
    #>
    if (Test-DockerEngine) {
        Write-Ok "Docker 引擎已就绪"
        return $true
    }

    Write-Warn2 "Docker 引擎未运行，尝试启动 Docker Desktop…"

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        'C:\Program Files\Docker\Docker\Docker Desktop.exe',
        'C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe'
    )

    $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $exe) {
        Write-Warn2 "未找到 Docker Desktop 可执行文件，请手动启动后重跑本脚本"
        return $false
    }

    Start-Process -FilePath $exe | Out-Null
    Write-Host "    已启动：$exe" -ForegroundColor Gray

    # 轮询等待引擎就绪；冷启动通常 30~90 秒
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        if (Test-DockerEngine) {
            Write-Ok "Docker 引擎已就绪"
            return $true
        }
        Write-Host "." -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }

    Write-Warn2 "等待引擎就绪超时（180s）"
    return $false
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

Write-Step "步骤 1/3 · 检查 Docker 引擎"
if (-not (Start-DockerDesktopIfNeeded)) {
    throw "Docker 引擎不可用。请手动启动 Docker Desktop，等待托盘图标变绿后重跑本脚本。"
}

Write-Step "步骤 2/3 · 启动基础设施容器"
$services = @('mysql', 'elasticsearch', 'qdrant', 'embedding')
if (-not $SkipKibana) { $services += 'kibana' }

$composeArgs = @('compose', '-f', $ComposeFile, 'up', '-d') + $services
Write-Host "    docker $($composeArgs -join ' ')" -ForegroundColor Gray

# 同 Test-DockerEngine：compose 的报错走 stderr，需临时放宽错误策略，
# 否则会被包装成 NativeCommandError 而跳过下方基于退出码的判定
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & docker @composeArgs
    $composeExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
}
if ($composeExit -ne 0) {
    throw "docker compose up 失败（退出码 $composeExit）"
}
Write-Ok "容器已创建/启动"

Write-Step "步骤 3/3 · 等待服务就绪（最长 $TimeoutSeconds 秒）"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

# 每项服务用「最能代表其可用性」的端点判定，而非仅看容器 Up
$targets = @(
    @{ Name = 'MySQL (3307)';         Port = 3307 }
    @{ Name = 'Elasticsearch (9200)'; Port = 9200 }
    @{ Name = 'Qdrant (6333)';        Port = 6333 }
    @{ Name = 'Embedding (8081)';     Port = 8081 }
)
if (-not $SkipKibana) {
    $targets += @{ Name = 'Kibana (5601)'; Port = 5601 }
}

$allReady = $true
foreach ($t in $targets) {
    Write-Host "    等待 $($t.Name) …" -NoNewline
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -Port $t.Port) { $ready = $true; break }
        Write-Host "." -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Seconds 3
    }
    if ($ready) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " 超时" -ForegroundColor Yellow
        $allReady = $false
    }
}

Write-Host ""
if ($allReady) {
    Write-Host "全部基础设施已就绪。" -ForegroundColor Green
    Write-Host "下一步：.\scripts\start-backend.ps1  （新开终端）" -ForegroundColor Gray
    Write-Host "        .\scripts\start-frontend.ps1 （另开终端）" -ForegroundColor Gray
    exit 0
} else {
    Write-Host "部分服务未在超时内就绪，可用以下命令排查：" -ForegroundColor Yellow
    Write-Host "  docker ps -a" -ForegroundColor Gray
    Write-Host "  docker compose -f docker/docker-compose.yaml logs --tail=50 <服务名>" -ForegroundColor Gray
    exit 1
}
