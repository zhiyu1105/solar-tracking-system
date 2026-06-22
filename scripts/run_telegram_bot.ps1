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
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

$Today = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDir "telegram_bot_$Today.log"
$Script = Join-Path $ProjectRoot "scripts\telegram_bot.py"

& $Python -u -X utf8 $Script poll 2>&1 | ForEach-Object {
    $line = "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $_"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

exit $LASTEXITCODE
