# scripts/docker_toggle.ps1
# Shows WINS stack status, then offers: Start/Restart, Stop, or Quit.
# Secrets are pulled from Doppler before any docker compose up.

function Show-Status {
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
    Write-Host "  WINS Docker Status" -ForegroundColor Cyan
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan

    $psOutput = docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>&1
    if ($psOutput -match "no configuration file") {
        Write-Host "  No running stack found." -ForegroundColor Yellow
        Write-Host ""
        return $false
    }

    foreach ($line in $psOutput) {
        if ($line -match "running") {
            Write-Host "  $line" -ForegroundColor Green
        } elseif ($line -match "exited|unhealthy") {
            Write-Host "  $line" -ForegroundColor Red
        } else {
            Write-Host "  $line" -ForegroundColor Gray
        }
    }
    Write-Host ""

    $running = docker compose ps --status running -q 2>&1
    return ($running -ne $null -and $running -ne "")
}

function Sync-Doppler {
    Write-Host "  Syncing secrets from Doppler..." -ForegroundColor DarkCyan
    doppler secrets download --no-file --format env | Set-Content .env.doppler
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Doppler sync failed. Is 'doppler setup' run in this directory?" -ForegroundColor Red
        exit 1
    }
    Write-Host "  Secrets synced." -ForegroundColor DarkGreen
}

$isRunning = Show-Status

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
if ($isRunning) {
    Write-Host "  [S] Start / Restart    [X] Stop    [Q] Quit" -ForegroundColor Yellow
} else {
    Write-Host "  [S] Start              [Q] Quit" -ForegroundColor Yellow
}
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

$choice = Read-Host "Choice"

switch ($choice.ToUpper()) {
    "S" {
        Write-Host ""
        Sync-Doppler
        Write-Host "Starting stack..." -ForegroundColor Cyan
        docker compose up -d
        Write-Host ""
        Show-Status | Out-Null
        Write-Host "Done." -ForegroundColor Green
    }
    "X" {
        if ($isRunning) {
            Write-Host ""
            Write-Host "Stopping stack..." -ForegroundColor Yellow
            docker compose down
            Write-Host "Stack stopped." -ForegroundColor Red
        } else {
            Write-Host "Stack is not running." -ForegroundColor Gray
        }
    }
    default {
        Write-Host "Cancelled." -ForegroundColor Gray
    }
}
