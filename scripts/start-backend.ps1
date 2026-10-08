<#
.SYNOPSIS
    启动「电商问数」FastAPI 后端。

.DESCRIPTION
    启动前依次完成三项前置校验，避免「进程起来了但问数不可用」这类
    难排查的状态：

      1. uv 与 .venv 是否存在（环境缺失直接给修复命令）
      2. .env 中的 LLM_API_KEY 是否为占位值
         ——占位 Key 会让指标召回步骤返回 401，是全链路唯一的功能性阻塞点
      3. 四个外部依赖端口是否可达

    校验通过后以 `uv run fastapi dev` 启动，并在后台轮询 /health/ready，
    直到四个依赖全部探活成功再返回，方便后续脚本串行编排。

.PARAMETER Host_ / Port
    监听地址与端口，默认 127.0.0.1:8000。

.PARAMETER SkipDependencyCheck
    跳过外部依赖端口校验。仅在你明确知道依赖未起、只想看接口文档时使用。

.PARAMETER NoWait
    启动后不轮询就绪探针，立即返回。

.EXAMPLE
    .\scripts\start-backend.ps1
    常规启动并等待就绪。

.EXAMPLE
    .\scripts\start-backend.ps1 -NoWait
    仅拉起进程，不等待就绪。
#>

[CmdletBinding()]
param(
    [string]$Host_ = '127.0.0.1',
    [int]$Port = 8000,
    [switch]$SkipDependencyCheck,
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

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

      不用 Test-NetConnection：它默认先发 ICMP 再建 TCP，输出冗长（进度条会
      污染终端）且每次耗时约 1 秒。这里直接以 TcpClient 建连，超时 1.5 秒，
      足够判定端口是否可服务。
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

# ---------------------------------------------------------------------------
# 前置校验
# ---------------------------------------------------------------------------

Write-Step "步骤 1/4 · 校验运行环境"

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    throw "未找到 uv。安装：powershell -c ""irm https://astral.sh/uv/install.ps1 | iex"""
}
Write-Ok "uv $((uv --version) -replace 'uv ', '')"

$venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    throw "找不到虚拟环境：$venvPython`n请先执行：uv sync"
}
Write-Ok "虚拟环境就绪"

Write-Step "步骤 2/4 · 校验 LLM API Key"

$envFile = Join-Path $ProjectRoot '.env'
if (-not (Test-Path $envFile)) {
    Write-Warn2 ".env 不存在，将从 .env.example 复制模板"
    $example = Join-Path $ProjectRoot '.env.example'
    if (Test-Path $example) {
        Copy-Item $example $envFile
        Write-Warn2 "已复制 .env.example → .env，请填入真实 LLM_API_KEY 后重跑"
    } else {
        throw "缺少 .env 且无 .env.example 模板"
    }
}

$keyLine = Get-Content $envFile -Encoding utf8 |
           Where-Object { $_ -match '^\s*LLM_API_KEY\s*=' } |
           Select-Object -First 1

if (-not $keyLine) {
    Write-Warn2 ".env 中未找到 LLM_API_KEY"
} else {
    $keyValue = ($keyLine -split '=', 2)[1].Trim()
    $isPlaceholder = ($keyValue -eq '') -or ($keyValue -match 'sk-xxx') -or ($keyValue -match 'your') -or ($keyValue -match '占位')
    if ($isPlaceholder) {
        Write-Warn2 "LLM_API_KEY 仍是占位值 —— 问数会在指标召回步骤返回 401 Token is invalid"
        Write-Warn2 "获取地址：https://siliconflow.cn ，编辑 .env 后重跑本脚本"
    } else {
        Write-Ok "LLM_API_KEY 已配置（长度 $($keyValue.Length)）"
    }
}

Write-Step "步骤 3/4 · 校验外部依赖"

if ($SkipDependencyCheck) {
    Write-Warn2 "已跳过依赖校验（-SkipDependencyCheck）"
} else {
    # 端口 → 服务名映射，与 docker-compose 的端口规划保持一致
    $deps = @(
        @{ Port = 3307; Name = 'MySQL (meta + dw)' }
        @{ Port = 9200; Name = 'Elasticsearch' }
        @{ Port = 6333; Name = 'Qdrant' }
        @{ Port = 8081; Name = 'Embedding (TEI)' }
    )
    $missing = @()
    foreach ($d in $deps) {
        $reachable = Test-TcpPort -Port $d.Port
        if ($reachable) {
            Write-Ok "$($d.Name) :$($d.Port) 可达"
        } else {
            Write-Warn2 "$($d.Name) :$($d.Port) 不可达"
            $missing += $d.Name
        }
    }
    if ($missing.Count -gt 0) {
        Write-Host ""
        Write-Warn2 "缺失依赖：$($missing -join ', ')"
        Write-Warn2 "请先运行：.\scripts\start-infra.ps1"
        throw "外部依赖未就绪，已中止启动。"
    }
}

# ---------------------------------------------------------------------------
# 启动后端
# ---------------------------------------------------------------------------

Write-Step "步骤 4/4 · 启动 FastAPI"

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    throw "端口 $Port 已被占用（PID $($existing[0].OwningProcess)）。请先释放端口或改用 -Port 指定其它端口。"
}

Write-Host "    命令：uv run fastapi dev main.py --host $Host_ --port $Port" -ForegroundColor Gray
Write-Host "    提示：日志输出在下方，Ctrl+C 停止。" -ForegroundColor DarkGray
Write-Host ""

$fastapiArgs = @('run', 'fastapi', 'dev', 'main.py', '--host', $Host_, '--port', "$Port")

# 用 System.Diagnostics.Process 直接启动，比 Start-Process -PassThru 更可靠。
# 注意 UseShellExecute 必须为 $false —— 本环境下设为 $true 会静默终止父进程。
$uvExe = (Get-Command uv).Source
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $uvExe
$psi.Arguments = ($fastapiArgs | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
$psi.WorkingDirectory = $ProjectRoot
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $false
$psi.RedirectStandardError = $false

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi

$started = $proc.Start()
if (-not $started) { throw "无法启动后端进程。请确认 uv 在 PATH 中。" }
Write-Host "    uv 进程已启动 (PID $($proc.Id))" -ForegroundColor DarkGray

if ($NoWait) {
    Write-Ok "已在后台启动（未等待就绪）"
    exit 0
}

$readyUrl = "http://$Host_`:$Port/health/ready"
$docUrl   = "http://$Host_`:$Port/docs"
$deadline = (Get-Date).AddSeconds(120)
$ready = $false

Write-Host "    等待就绪探针 $readyUrl …" -NoNewline
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Write-Host ""
        throw "后端进程已退出（退出码 $($proc.ExitCode)）。请检查上方日志。"
    }
    try {
        $req = [System.Net.HttpWebRequest]::Create($readyUrl)
        $req.Timeout = 3000
        # 必须清零代理：环境变量 HTTP_PROXY 指向本地代理时，
        # 对 127.0.0.1 的请求会被它截获并回 502，导致误判。
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
        $resp.Close()
        if ($code -eq 200) {
            Write-Host " OK" -ForegroundColor Green
            $ready = $true
            break
        } else {
            Write-Host ""
            Write-Warn2 "就绪探针返回 $code：$body"
            Write-Host "    继续等待…" -NoNewline
        }
    } catch {
        # 端口未开 / 探针超时（依赖不可达时会挂在探针里，属预期）
        Write-Host "." -NoNewline -ForegroundColor DarkGray
    }
    Start-Sleep -Seconds 3
}

Write-Host ""
if ($ready) {
    Write-Host "后端已就绪。" -ForegroundColor Green
    Write-Host "  接口文档：$docUrl" -ForegroundColor Gray
    Write-Host "  健康检查：http://$Host_`:$Port/health" -ForegroundColor Gray
    Write-Host "  按 Ctrl+C 停止后端。" -ForegroundColor DarkGray
    Write-Host ""
    try {
        $proc.WaitForExit()
    } finally {
        if (-not $proc.HasExited) { $proc.Kill() }
    }
    exit 0
} else {
    Write-Warn2 "就绪探针在 120 秒内未返回 200。进程仍在运行，可用以下方式确认："
    Write-Host "  curl http://$Host_`:$Port/health" -ForegroundColor Gray
    Write-Host "  若 /health 返回 200 但 /health/ready 非 200，说明应用已启动、" -ForegroundColor DarkGray
    Write-Host "  但外部依赖（MySQL/ES/Qdrant/Embedding）还没就绪，需要先运行 start-infra.ps1。" -ForegroundColor DarkGray
    exit 1
}
