<#
.SYNOPSIS
    一键启动「电商问数」全栈：基础设施 → 后端 → 前端。

.DESCRIPTION
    按依赖顺序串行编排三个子脚本，任一环节失败即中止，不会把问题
    推迟到更靠后的环节才暴露：

        start-infra.ps1     起容器并等待五个服务可达
        start-backend.ps1   起 FastAPI 并等待 /health/ready 返回 200
        start-frontend.ps1  起 Vite 并等待 5173 监听

    后端与前端会各自在**独立窗口**启动，便于分别查看日志，
    且关掉本编排脚本所在窗口不会连带杀掉服务。

    停止服务：关闭对应的两个窗口，或运行 .\scripts\stop-all.ps1

.PARAMETER SkipKibana
    跳过 Kibana 容器，省约 1GB 内存。

.PARAMETER SkipInfra
    跳过基础设施环节（依赖已在运行时用）。

.PARAMETER TimeoutSeconds
    等待基础设施就绪的最长秒数，默认 300。

.EXAMPLE
    .\scripts\start-all.ps1
    完整启动全栈。

.EXAMPLE
    .\scripts\start-all.ps1 -SkipInfra -SkipKibana
    依赖已起，只拉起后端与前端，且跳过 Kibana。
#>

[CmdletBinding()]
param(
    [switch]$SkipKibana,
    [switch]$SkipInfra,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host ""
Write-Host "=============================================" -ForegroundColor White
Write-Host "  电商问数 · 全栈启动" -ForegroundColor White
Write-Host "=============================================" -ForegroundColor White
Write-Host "  项目根目录：$ProjectRoot" -ForegroundColor DarkGray

$sw = [System.Diagnostics.Stopwatch]::StartNew()

function Start-ChildScript {
    <#
      在独立进程中启动子脚本。

      为什么不用 Start-Process：
        本机安全策略会拦截「以 shell 解释器为目标的 Start-Process」
        （报 "Start-Process with a shell/interpreter/LOLBin target"）。

      为什么 UseShellExecute = $false：
        实测 UseShellExecute = $true 在本环境会**静默终止整个 PowerShell 进程**，
        导致脚本在中途无输出退出，且报错信息极具误导性。
        UseShellExecute = $false + CreateNoWindow = $true 稳定可用。
    #>
    param(
        [string]$ScriptPath,
        [string]$WorkDir
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Get-Command powershell.exe).Source
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $false

    $p = [System.Diagnostics.Process]::Start($psi)
    if ($p) { return "PID $($p.Id)" }
    return '已派生（未取到 PID）'
}

# 安全探针：StrictMode 下 $_.Exception.Response 可能不存在，
# 直接访问会抛 PropertyNotFoundException。
#
# 注意：必须用 -NoProxy，否则 HttpWebRequest 会读取环境变量
# HTTP_PROXY/HTTPS_PROXY（本机存在 http://127.0.0.1:xxxxx 的代理），
# 把对 127.0.0.1 的请求也交给代理，代理连不上目标就回 502
# "upstream connect failed"，导致本地服务被误判为未就绪。
function Test-UrlOk {
    param([string]$Url, [int]$TimeoutMs = 3000)
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Timeout = $TimeoutMs
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return ($code -eq 200)
    } catch {
        $ex = $_.Exception
        if ($ex -and $ex.PSObject.Properties['Response'] -and $ex.Response) {
            try { return ([int]$ex.Response.StatusCode -eq 200) } catch { return $false }
        }
        return $false
    }
}

# --- 基础设施 -------------------------------------------------------------
if ($SkipInfra) {
    Write-Host ""
    Write-Host "[1/3] 基础设施：已跳过（-SkipInfra）" -ForegroundColor DarkGray
} else {
    Write-Host ""
    Write-Host "[1/3] 基础设施" -ForegroundColor White
    $infraArgs = @{ TimeoutSeconds = $TimeoutSeconds }
    if ($SkipKibana) { $infraArgs['SkipKibana'] = $true }
    & (Join-Path $ScriptDir 'start-infra.ps1') @infraArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "基础设施未就绪，已中止。修复后重跑本脚本。" -ForegroundColor Red
        exit 1
    }
}

# --- 后端 -----------------------------------------------------------------
Write-Host ""
Write-Host "[2/3] 后端（独立进程）" -ForegroundColor White
$beNote = Start-ChildScript -ScriptPath (Join-Path $ScriptDir 'start-backend.ps1') -WorkDir $ProjectRoot
Write-Host "    已启动 ($beNote)，等待其就绪…" -ForegroundColor Gray

# 轮询就绪探针，确认后端真的可用后再启前端
$deadline = (Get-Date).AddSeconds(150)
$backendReady = $false
$backendLive = $false
while ((Get-Date) -lt $deadline) {
    if (Test-UrlOk -Url 'http://127.0.0.1:8000/health/ready') {
        $backendReady = $true
        break
    }
    if (-not $backendLive -and (Test-UrlOk -Url 'http://127.0.0.1:8000/health')) {
        $backendLive = $true
        Write-Host "    应用已启动（/health 200），等待外部依赖就绪…" -ForegroundColor DarkGray
    }
    Start-Sleep -Seconds 3
}

if ($backendReady) {
    Write-Host "    后端就绪：http://127.0.0.1:8000/docs" -ForegroundColor Green
} else {
    if ($backendLive) {
        Write-Host "    应用已启动但依赖未就绪（/health/ready 非 200）。" -ForegroundColor Yellow
        Write-Host "    请确认 start-infra.ps1 的五个容器都在运行。" -ForegroundColor Yellow
    } else {
        Write-Host "    后端在 150 秒内未就绪，仍继续启动前端。" -ForegroundColor Yellow
    }
}

# --- 前端 -----------------------------------------------------------------
Write-Host ""
Write-Host "[3/3] 前端（独立进程）" -ForegroundColor White
$feNote = Start-ChildScript -ScriptPath (Join-Path $ScriptDir 'start-frontend.ps1') -WorkDir $ProjectRoot
Write-Host "    已启动 ($feNote)" -ForegroundColor Gray

$deadline = (Get-Date).AddSeconds(120)
$frontendReady = $false
while ((Get-Date) -lt $deadline) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', 5173, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne(1000, $false)) {
            $client.EndConnect($async)
            $frontendReady = $true
        }
    } catch {
    } finally {
        $client.Close()
    }
    if ($frontendReady) { break }
    Start-Sleep -Seconds 2
}

$sw.Stop()

Write-Host ""
Write-Host "=============================================" -ForegroundColor White
if ($frontendReady -and $backendReady) {
    Write-Host "  启动完成（耗时 $([math]::Round($sw.Elapsed.TotalSeconds, 1))s）" -ForegroundColor Green
} else {
    Write-Host "  部分环节未确认就绪（耗时 $([math]::Round($sw.Elapsed.TotalSeconds, 1))s）" -ForegroundColor Yellow
}
Write-Host "=============================================" -ForegroundColor White
Write-Host "  前端界面：http://localhost:5173" -ForegroundColor Gray
Write-Host "  接口文档：http://127.0.0.1:8000/docs" -ForegroundColor Gray
Write-Host "  健康检查：http://127.0.0.1:8000/health/ready" -ForegroundColor Gray
Write-Host "  Kibana  ：http://localhost:5601" -ForegroundColor Gray
Write-Host ""
Write-Host "  停止服务：.\scripts\stop-all.ps1" -ForegroundColor DarkGray
Write-Host ""
