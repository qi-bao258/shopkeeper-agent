<#
.SYNOPSIS
    启动「电商问数」前端（Vite dev server）。

.DESCRIPTION
    启动前做三件事：
      1. 自愈 node_modules 里退化为空目录的 pnpm 链接
      2. 校验 vite 可用
      3. 启动 Vite 并轮询端口监听

    关于包管理器：本项目 frontend/package.json 声明
    `packageManager: pnpm@10.33.0`，而本机 pnpm 为 12.x，pnpm 会尝试下载
    声明版本并失败（@pnpm/exe did not materialize）。因此本脚本**直接调用
    node_modules 里的 vite**（用 node 运行），不依赖 pnpm 的版本协商。

    前端通过 VITE_* 变量直连后端，默认 http://127.0.0.1:8000。
    若后端换了端口，需同步修改 frontend/.env，本脚本会提示这一点。

.PARAMETER Port
    前端端口，默认 5173。

.PARAMETER RepairLinks
    启动前强制运行 scripts\repair-node-links.py 修复链接。

.PARAMETER NoRepair
    跳过链接自愈（默认会自动执行一次）。

.PARAMETER NoWait
    启动后不等待端口就绪，立即返回。

.EXAMPLE
    .\scripts\start-frontend.ps1
    常规启动并等待就绪。
#>

[CmdletBinding()]
param(
    [int]$Port = 5173,
    [switch]$RepairLinks,
    [switch]$NoRepair,
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $ProjectRoot 'frontend'

if (-not (Test-Path $FrontendRoot)) {
    throw "找不到前端目录：$FrontendRoot"
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

function Test-ViteUsable {
    param([string]$FrontendRoot)
    $viteJs = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
    return (Test-Path $viteJs)
}

function Invoke-NodeLinkRepair {
    <#
      调用 scripts\repair-node-links.py 自愈 node_modules 链接。

      项目跨磁盘/路径迁移后，pnpm 的 junction 会退化为空目录。
      典型报错：
        Cannot find module @rollup/rollup-win32-x64-msvc
        You installed esbuild for another platform than the one you're currently using
        Cannot find package '...\@vitejs\plugin-react\index.js'
        SyntaxError: ... does not provide an export named 'default'
        ERR_MODULE_NOT_FOUND ...picomatch
      这些都不是代码问题，而是链接损坏，重装依赖不是必要手段。
    #>
    param([string]$ProjectRoot)

    $repairScript = Join-Path $ProjectRoot 'scripts\repair-node-links.py'
    if (-not (Test-Path $repairScript)) {
        Write-Warn2 "未找到 scripts\repair-node-links.py，跳过链接自愈"
        return $false
    }

    # 优先用项目 venv 的 python，其次系统 python
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pyCmd) {
            Write-Warn2 "未找到 Python，跳过链接自愈"
            return $false
        }
        $python = $pyCmd.Source
    }

    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $env:PYTHONUTF8 = '1'
        & $python $repairScript
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return ($code -eq 0)
}

# ---------------------------------------------------------------------------

Write-Step "步骤 1/4 · 校验前端环境"

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    throw "未找到 Node.js。请安装 Node.js 18+ 后重试。"
}
$nodeExe = $node.Source
Write-Ok "node $(& $nodeExe --version)"

# node_modules 完全不存在才需要装；仅链接损坏则走自愈
$nodeModules = Join-Path $FrontendRoot 'node_modules'
if (-not (Test-Path $nodeModules)) {
    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $pnpm -and -not $npm) {
        throw "node_modules 不存在，且未找到 pnpm / npm。请先安装依赖。"
    }
    Write-Warn2 "node_modules 不存在，尝试安装依赖（首次较慢）…"
    Push-Location $FrontendRoot
    try {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            if ($pnpm) { & pnpm install } else { & npm install --no-fund --no-audit }
            $inst = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $prev
        }
        if ($inst -ne 0) { Write-Warn2 "依赖安装返回非零退出码 $inst，继续尝试自愈" }
    } finally {
        Pop-Location
    }
}

# 自愈链接
if (-not $NoRepair) {
    Write-Step "步骤 2/4 · 自愈 node_modules 链接"
    if ($RepairLinks -or -not (Test-ViteUsable -FrontendRoot $FrontendRoot)) {
        if ($RepairLinks) {
            Write-Host "    指定了 -RepairLinks，强制修复" -ForegroundColor Gray
        } else {
            Write-Warn2 "node_modules 中的 vite 不完整，尝试自愈链接"
        }
    } else {
        Write-Host "    运行链接自愈（无损坏则为空操作）…" -ForegroundColor Gray
    }
    $null = Invoke-NodeLinkRepair -ProjectRoot $ProjectRoot

    if (Test-ViteUsable -FrontendRoot $FrontendRoot) {
        Write-Ok "前端依赖就绪"
    } else {
        Write-Warn2 "自愈后仍未找到 node_modules\vite\bin\vite.js，请手动检查依赖"
    }
} else {
    Write-Step "步骤 2/4 · 自愈 node_modules 链接（已跳过）"
}

if (-not (Test-ViteUsable -FrontendRoot $FrontendRoot)) {
    throw "vite 入口不可用：node_modules\vite\bin\vite.js 不存在。请检查依赖或运行 -RepairLinks。"
}

Write-Step "步骤 3/4 · 检查后端代理配置"

$feEnv = Join-Path $FrontendRoot '.env'
$apiBase = $null
if (Test-Path $feEnv) {
    $line = Get-Content $feEnv -Encoding utf8 |
            Where-Object { $_ -match 'VITE_.*API' } |
            Select-Object -First 1
    if ($line) {
        $apiBase = $line.Trim()
        Write-Ok $apiBase
    }
} else {
    Write-Warn2 "frontend/.env 不存在，前端将使用默认代理目标 http://127.0.0.1:8000"
}

if ($apiBase -and ($apiBase -match ':(\d+)')) {
    $backendPort = [int]$Matches[1]
    if (Test-TcpPort -Port $backendPort) {
        Write-Ok "后端端口 $backendPort 可达"
    } else {
        Write-Warn2 "后端端口 $backendPort 不可达 —— 请先运行 start-backend.ps1"
    }
}

Write-Step "步骤 4/4 · 启动 Vite"

if (Test-TcpPort -Port $Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    $pidInfo = if ($conn) { " (PID $($conn[0].OwningProcess))" } else { "" }
    throw "端口 $Port 已被占用$pidInfo。请先释放，或用 -Port 指定其它端口。"
}
Write-Ok "端口 $Port 空闲"

$viteJs = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
$viteArgs = @($viteJs, '--port', "$Port", '--host', '127.0.0.1')

Write-Host "    命令：node node_modules\vite\bin\vite.js --port $Port" -ForegroundColor Gray
Write-Host "    提示：日志输出在下方，Ctrl+C 停止。" -ForegroundColor DarkGray
Write-Host ""

# 用 System.Diagnostics.Process 直接启动，比 Start-Process -PassThru 更可靠。
# 注意 UseShellExecute 必须为 $false —— 本环境下设为 $true 会静默终止父进程。
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $nodeExe
$psi.Arguments = ($viteArgs | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
$psi.WorkingDirectory = $FrontendRoot
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $false
$psi.RedirectStandardError = $false

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi

$started = $proc.Start()
if (-not $started) { throw "无法启动 node 进程。请确认 node 在 PATH 中。" }
Write-Host "    node 进程已启动 (PID $($proc.Id))" -ForegroundColor DarkGray

if ($NoWait) {
    Write-Ok "已在后台启动（未等待就绪）"
    exit 0
}

$deadline = (Get-Date).AddSeconds(90)
$ready = $false

Write-Host "    等待 Vite 监听 $Port …" -NoNewline
while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Write-Host ""
        throw "前端进程已退出（退出码 $($proc.ExitCode)）。请检查上方日志。"
    }
    if (Test-TcpPort -Port $Port) {
        Write-Host " OK" -ForegroundColor Green
        $ready = $true
        break
    }
    Write-Host "." -NoNewline -ForegroundColor DarkGray
    Start-Sleep -Seconds 2
}

Write-Host ""
if ($ready) {
    Write-Host "前端已就绪。" -ForegroundColor Green
    Write-Host "  访问地址：http://localhost:$Port" -ForegroundColor Gray
    Write-Host "  后端地址：$(if ($apiBase) { $apiBase } else { 'http://127.0.0.1:8000' })" -ForegroundColor Gray
    Write-Host "  按 Ctrl+C 停止前端。" -ForegroundColor DarkGray
    Write-Host ""
    try {
        $proc.WaitForExit()
    } finally {
        if (-not $proc.HasExited) { $proc.Kill() }
    }
    exit 0
} else {
    Write-Warn2 "Vite 未在 90 秒内监听 $Port。进程仍在运行，请检查上方日志。"
    exit 1
}
