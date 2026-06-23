param(
    [string]$Python = "",
    [switch]$IncludeFullReport,
    [switch]$DryRun
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
try { chcp 65001 | Out-Null } catch { }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ArgsList = @("-X", "utf8", "scripts\telegram_bot.py", "send-all-status")
if ($IncludeFullReport) {
    $ArgsList += "--include-full-report"
}
if ($DryRun) {
    $ArgsList += "--dry-run"
}

& $Python @ArgsList
exit $LASTEXITCODE
