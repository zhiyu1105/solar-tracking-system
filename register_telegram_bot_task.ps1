# ============================================================
# register_telegram_bot_task.ps1
# Register read-only Telegram bot polling for the solar dashboard.
# Schedule: start when the current user logs on.
#
# Usage:
#   cd C:\projects\solar-tracking-dashboard
#   .\register_telegram_bot_task.ps1
#
# Unregister:
#   Unregister-ScheduledTask -TaskName 'SolarTelegramBot' -Confirm:$false
# ============================================================

param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "SolarTelegramBot"
$ProjectRoot = if ($PSScriptRoot) {
    (Get-Item $PSScriptRoot).FullName
} else {
    (Get-Item (Split-Path -Parent $MyInvocation.MyCommand.Path)).FullName
}
$ScriptPath = Join-Path $ProjectRoot "scripts\run_telegram_bot.ps1"

if (-not $Python) {
    if (Test-Path "C:\01_CODE\python311\python.exe") {
        $Python = "C:\01_CODE\python311\python.exe"
    } else {
        $Python = "python"
    }
}

Write-Host "Project root resolved to: $ProjectRoot" -ForegroundColor Cyan
Write-Host "Will register script:     $ScriptPath" -ForegroundColor Cyan
Write-Host "Python executable:        $Python" -ForegroundColor Cyan

if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: cannot find $ScriptPath" -ForegroundColor Red
    exit 1
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NoProfile -NonInteractive -WindowStyle Hidden -File `"$ScriptPath`" -Python `"$Python`"" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -Hidden `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Existing $TaskName found, removing first..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description "Solar dashboard read-only Telegram bot polling" `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal | Out-Null

    Write-Host "OK: registered task: $TaskName" -ForegroundColor Green
    Write-Host ""
    Write-Host "Manual test run:" -ForegroundColor Cyan
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "View status:" -ForegroundColor Cyan
    Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
    Write-Host "Stop running bot:" -ForegroundColor Cyan
    Write-Host "  Stop-ScheduledTask -TaskName '$TaskName'"
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Possible causes:" -ForegroundColor Yellow
    Write-Host "  1. Task Scheduler permission issue"
    Write-Host "  2. Telegram env values are not set yet"
    Write-Host "  3. Python path is wrong"
    exit 1
}
