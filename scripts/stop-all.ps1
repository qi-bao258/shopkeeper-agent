<#
.SYNOPSIS
    停止「电商问数」全栈服务。

.DESCRIPTION
    按与启动相反的顺序停止：前端 → 后端 → 容器。

    默认**保留数据卷**，容器下次 `up` 即可恢复，不需要重建知识库。
    只有显式加 -RemoveVolumes 才会删除数据卷（会清空 MySQL/ES/Qdrant 数据，
    之后必须重跑 build_meta_knowledge 重建知识库）。

.PARAMETER RemoveVolumes
    危险操作：连数据卷一并删除。执行前会二次确认。

.PARAMETER KeepInfra
    只停前端与后端，保留容器运行。

.EXAMPLE
    .\scripts\stop-all.ps1
    停止前后端并停掉容器，保留数据。

.EXAMPLE
    .\scripts\stop-all.ps1 -KeepInfra
    只停前后端，容器继续跑。
#>

[CmdletBinding()]
param(
    [switch]$RemoveVolumes,
    [switch]$KeepInfra
)

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker\docker-compose.yaml'

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ">>> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    [OK]   $Message" -ForegroundColor Green
}

function Stop-PortProcess {
    <#
      按监听端口定位并结束进程。
      仅在「该端口确实是本项目服务」时才有意义，因此额外校验进程名，
      避免误杀恰好占用同端口的其它程序。
    #>
    param(
        [int]$Port,
        [string]$ExpectedName
    )

    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host "    端口 $Port 未监听，跳过" -ForegroundColor DarkGray
        return
    }

    $killed = @()
    foreach ($c in $conns) {
        $pid_ = $c.OwningProcess
        $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
        if (-not $proc) { continue }

        $name = $proc.ProcessName
        if ($ExpectedName -and ($name -notmatch $ExpectedName)) {
            Write-Host "    端口 $Port 由 $name (PID $pid_) 占用，与本项目预期进程（$ExpectedName）不符，跳过" -ForegroundColor Yellow
            continue
        }

        try {
            Stop-Process -Id $pid_ -Force -ErrorAction Stop
            $killed += "$name (PID $pid_)"
        } catch {
            Write-Host "    结束 $name (PID $pid_) 失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    if ($killed.Count -gt 0) {
        Write-Ok "已停止：$($killed -join ', ')"
    }
}

# --- 前端 -----------------------------------------------------------------
Write-Step "步骤 1/3 · 停止前端 (5173)"
Stop-PortProcess -Port 5173 -ExpectedName 'node'

# --- 后端 -----------------------------------------------------------------
Write-Step "步骤 2/3 · 停止后端 (8000)"
Stop-PortProcess -Port 8000 -ExpectedName '.+'

# --- 容器 -----------------------------------------------------------------
if ($KeepInfra) {
    Write-Step "步骤 3/3 · 容器：已跳过（-KeepInfra）"
} else {
    if ($RemoveVolumes) {
        Write-Step "步骤 3/3 · 停止容器并删除数据卷"
        Write-Host ""
        Write-Host "    ⚠️  警告：此操作会删除 MySQL / Elasticsearch / Qdrant 的数据卷。" -ForegroundColor Red
        Write-Host "       删除后必须重跑以下命令重建知识库：" -ForegroundColor Red
        Write-Host "       uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml" -ForegroundColor Red
        Write-Host ""
        $answer = Read-Host "    确认删除数据卷？输入 yes 继续"
        if ($answer -ne 'yes') {
            Write-Host "    已取消，改为仅停止容器（保留数据卷）" -ForegroundColor Yellow
            & docker compose -f $ComposeFile stop
        } else {
            & docker compose -f $ComposeFile down -v
        }
    } else {
        Write-Step "步骤 3/3 · 停止容器（保留数据卷）"
        & docker compose -f $ComposeFile stop
    }
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "容器已停止，数据卷保留，下次 start-infra 可直接恢复"
    } else {
        Write-Host "    docker compose 返回退出码 $LASTEXITCODE" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "停止完成。" -ForegroundColor Green
Write-Host ""
