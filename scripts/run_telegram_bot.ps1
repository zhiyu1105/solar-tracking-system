param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = if ($PSScriptRoot) {
    (Get-Item (Join-Path $PSScriptRoot "..")).FullName
} else {
    (Get-Location).Path
}
Set-Location $ProjectRoot

if (-not $Python) {
    if (Test-Path "C:\01_CODE\python311\python.exe") {
        $Python = "C:\01_CODE\python311\python.exe"
    } else {
        $Python = "python"
    }
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
try { chcp 65001 | Out-Null } catch { }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) {
    try { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null } catch { }
}

$Script = Join-Path $ProjectRoot "scripts\telegram_bot.py"
$RestartDelaySeconds = 15

function Write-BotLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $SafeMessage = $Message `
        -replace '(?i)/bot[0-9]+:[A-Za-z0-9_-]+/', '/bot<redacted>/' `
        -replace '(?i)bot[0-9]+:[A-Za-z0-9_-]+', 'bot<redacted>'

    $Today = Get-Date -Format "yyyy-MM-dd"
    $LogFile = Join-Path $LogDir "telegram_bot_$Today.log"
    $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $SafeMessage"
    try { Write-Host $line } catch { }
    try {
        if (-not (Test-Path $LogDir)) {
            New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    } catch {
        try {
            Write-Host "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] LOG_WRITE_FAILED: $($_.Exception.Message)"
        } catch { }
    }
}

$CommandForLog = "`"$Python`" -u -X utf8 `"$Script`" poll"
$RunCount = 0

Write-BotLog "Telegram polling supervisor starting. PowerShellPID=$PID ProjectRoot=$ProjectRoot RestartDelaySeconds=$RestartDelaySeconds"

$PreviousErrorActionPreference = $ErrorActionPreference
while ($true) {
    $RunCount += 1
    $ExitCode = 1
    try {
        Write-BotLog "Launching poll run #${RunCount}: $CommandForLog"

        # telegram_bot.py intentionally logs transient polling/network errors to stderr
        # and then retries. Do not let PowerShell convert those stderr lines into a
        # terminating supervisor exception.
        $ErrorActionPreference = "Continue"
        & $Python -u -X utf8 $Script poll 2>&1 | ForEach-Object {
            Write-BotLog ([string]$_)
        }

        if ($null -ne $LASTEXITCODE) {
            $ExitCode = [int]$LASTEXITCODE
        } else {
            $ExitCode = 0
        }
        Write-BotLog "Telegram polling process exited. run=$RunCount exit_code=$ExitCode"
    } catch {
        $ExitCode = 1
        Write-BotLog "ERROR run_telegram_bot supervisor caught exception. run=$RunCount $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }

    Write-BotLog "Restarting Telegram polling after ${RestartDelaySeconds}s. previous_exit_code=$ExitCode"
    Start-Sleep -Seconds $RestartDelaySeconds
}
