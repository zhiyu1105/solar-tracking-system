# Solar Dashboard Windows Scheduled Tasks Spec

Last updated: 2026-06-23

This document describes the local Windows Scheduled Tasks used for parallel operation on the new machine.

## Scope

The system currently uses two independent Windows Scheduled Tasks:

| Task name | Type | Purpose | Expected normal state |
| --- | --- | --- | --- |
| `SolarTelegramBot` | long-running polling task | Receives Telegram commands and replies to status/operation requests. | `Running` |
| `SolarWeeklyMaintenance` | scheduled weekly maintenance task | Runs weekly checks, data collection, cache reload, report generation, and Telegram notifications. | `Ready` between weekly runs |

`SolarTelegramBot` and `SolarWeeklyMaintenance` are not the same task. They only interact because the weekly maintenance task can send reports/alerts through Telegram.

## Task 1: SolarTelegramBot

### Purpose

`SolarTelegramBot` keeps the Telegram bot online by running long polling:

```powershell
C:\01_CODE\python311\python.exe -u -X utf8 scripts\telegram_bot.py poll
```

The registered task launches:

```powershell
powershell.exe -ExecutionPolicy Bypass -NoProfile -NonInteractive -WindowStyle Hidden -File "C:\projects\solar-tracking-dashboard\scripts\run_telegram_bot.ps1" -Python "C:\01_CODE\python311\python.exe"
```

The task settings must include `ExecutionTimeLimit = PT0S`; otherwise Windows Task Scheduler can stop the long-running polling process after the default 72-hour limit.

### Responsibilities

- Receive Telegram commands such as `/status`, `/token`, `/csv`, `/gap30`, `/gap <days>`, `/docker`, `/weekly`, `/allstatus`.
- Accept guarded operations only in `05-手動操作審核`, such as `/collect`, `/update_token`, `/reload`, `/restart_backend`, `/run_weekly`.
- Require `/confirm <code>` before operation commands run.
- Write runtime logs to:

```text
C:\projects\solar-tracking-dashboard\logs\telegram_bot_YYYY-MM-DD.log
```

### Correct status

Check:

```powershell
Get-ScheduledTask -TaskName SolarTelegramBot
Get-ScheduledTask -TaskName SolarTelegramBot | Get-ScheduledTaskInfo
```

Expected:

| Field | Normal value | Meaning |
| --- | --- | --- |
| `State` | `Running` | Bot polling is active. |
| `LastTaskResult` | `267009` while running | Normal for a long-running task. |
| `Actions.Execute` | `powershell.exe` | Task runs the wrapper script. |
| `Actions.Arguments` | includes `-WindowStyle Hidden -NonInteractive` | Task should not leave a foreground terminal open. |
| `Actions.WorkingDirectory` | `C:\projects\solar-tracking-dashboard` | Must point to the official project path. |
| `Settings.ExecutionTimeLimit` | `PT0S` | No runtime limit, required for persistent polling. |

If `State = Ready`, the bot is not currently polling. Start it manually, then confirm it changes to `Running`.

### Manual start

```powershell
Start-ScheduledTask -TaskName SolarTelegramBot
```

### Log check

Use this command without editing the date:

```powershell
Get-ChildItem C:\projects\solar-tracking-dashboard\logs\telegram_bot_*.log |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content -Tail 20
```

Expected recent line:

```text
Telegram polling started. Press Ctrl+C to stop.
```

The strongest live check is to send `/status` or `/whoami` in the Telegram group. A reply means polling is working.

### Re-register task

```powershell
cd C:\projects\solar-tracking-dashboard
powershell.exe -ExecutionPolicy Bypass -NoProfile -File .\register_telegram_bot_task.ps1 -Python "C:\01_CODE\python311\python.exe"
Start-ScheduledTask -TaskName SolarTelegramBot
```

After re-registering, verify:

```powershell
Get-ScheduledTask -TaskName SolarTelegramBot |
  Select-Object -ExpandProperty Settings |
  Select-Object ExecutionTimeLimit,RestartCount,RestartInterval,Hidden
```

Expected:

```text
ExecutionTimeLimit : PT0S
RestartCount       : 3
RestartInterval    : PT2M
Hidden             : True
```

## Task 2: SolarWeeklyMaintenance

### Purpose

`SolarWeeklyMaintenance` runs weekly maintenance from:

```text
C:\projects\solar-tracking-dashboard\solar_weekly_run.ps1
```

Current registered action:

```powershell
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "C:\projects\solar-tracking-dashboard\solar_weekly_run.ps1"
```

### Schedule

Current expected schedule:

```text
Every Monday 02:00
```

When the task is not actively running, `State = Ready` is normal.

### Weekly stages

The weekly script does contain multiple checks and actions:

| Stage | Name | What it does | Failure behavior |
| --- | --- | --- | --- |
| 1 | Backend health check | Checks `solar_backend` Docker container. If not running, attempts `docker-compose -f docker-compose-dev.yml up -d backend`. Then checks Django API `http://localhost:8000/api/fixed-panels/status/`. | Records `WARN` or `FAIL` in log/report. |
| 2 | Token status check | Runs `python -X utf8 z3a_check_token.py`. Parses access token remaining time. | If expired, skips Z3A collection. If warning/urgent, still collects but reports warning. |
| 2.5 | Illumination CSV merge | Looks for CSV files in `data\illumination_inbox`, runs `merge_illumination_csv.py --csv <file>`, then archives successful files into `data\illumination_archive`. | Failed merge stays in inbox for retry. |
| 3 | Z3A data collection | If token is usable, runs `python -X utf8 z3a_collect.py --pipeline --days 7`. Parses new rows and total rows. | Nonzero exit is `FAIL`. `0` new rows is `WARN`. |
| 3.5 | Backend cache reload | Calls `POST http://localhost:8000/api/fixed-panels/reload/`. | Reload failure is `WARN`; collection result is still preserved. |
| 4 | Backup listing | Lists recent `data\*.bak.*.csv` files. | Failure is `WARN`. |
| Final | Report generation | Writes full log and summary report. | Writes `logs\latest_report.txt`. |
| 5 | Telegram notification | Runs `telegram_bot.py send-report --report-file logs\latest_report.txt`, then `telegram_bot.py check-token-alert`. | Telegram failure is `WARN` only; weekly task should not fail only because Telegram is unavailable. |

### Reports and logs

Full weekly log:

```text
C:\projects\solar-tracking-dashboard\logs\solar_weekly_YYYY-MM-DD.log
```

Latest summary report:

```text
C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

Check latest weekly log:

```powershell
Get-ChildItem C:\projects\solar-tracking-dashboard\logs\solar_weekly_*.log |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content -Tail 60
```

Check latest summary:

```powershell
Get-Content C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

### Correct status

Check:

```powershell
Get-ScheduledTask -TaskName SolarWeeklyMaintenance
Get-ScheduledTask -TaskName SolarWeeklyMaintenance | Get-ScheduledTaskInfo
```

Expected:

| Field | Normal value | Meaning |
| --- | --- | --- |
| `State` | `Ready` | The weekly task is registered and waiting for the next schedule. |
| `LastTaskResult` | `0` | Last run succeeded. |
| `LastRunTime` | recent Monday 02:00 after a normal weekly run | Shows when it last ran. |
| `NextRunTime` | next Monday 02:00 | Shows the next scheduled run. |
| `Actions.WorkingDirectory` | `C:\projects\solar-tracking-dashboard` | Must point to the official project path. |

During a manual or scheduled run, `State = Running` is expected.

### Manual run

Use this when you want to run the weekly maintenance immediately:

```powershell
Start-ScheduledTask -TaskName SolarWeeklyMaintenance
```

Then watch:

```powershell
Get-ScheduledTask -TaskName SolarWeeklyMaintenance | Get-ScheduledTaskInfo
Get-Content C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

## One-shot health check command

Run this to inspect both scheduled tasks and the latest bot log:

```powershell
$tasks = 'SolarTelegramBot','SolarWeeklyMaintenance'
foreach ($name in $tasks) {
  Write-Output "==== $name ===="
  $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if (-not $task) { Write-Output 'NOT FOUND'; continue }
  $info = $task | Get-ScheduledTaskInfo
  $action = $task.Actions | Select-Object -First 1
  [pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $info.NextRunTime
    Execute = $action.Execute
    Arguments = $action.Arguments
    WorkingDirectory = $action.WorkingDirectory
  } | Format-List
}

Write-Output '==== Telegram bot latest log ===='
$log = Get-ChildItem 'C:\projects\solar-tracking-dashboard\logs\telegram_bot_*.log' -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
if ($log) {
  Write-Output $log.FullName
  Get-Content $log.FullName -Tail 20
} else {
  Write-Output 'No telegram bot log found'
}
```

## Current known local state on 2026-06-23

Observed local state:

| Task | State | Last result | Interpretation |
| --- | --- | --- | --- |
| `SolarTelegramBot` | `Running` | `267009` | Normal. Bot polling is active. |
| `SolarWeeklyMaintenance` | `Ready` | `0` | Normal. Last weekly run succeeded and task is waiting for next Monday. |

Observed next weekly run:

```text
2026-06-29 02:00
```

## Important notes

- Docker Desktop does not replace these scheduled tasks. Docker Desktop can start containers, but Telegram polling and weekly maintenance are controlled by Windows Scheduled Tasks.
- `SolarTelegramBot` should be hidden/non-interactive. If a foreground terminal appears repeatedly, re-register the task.
- `SolarWeeklyMaintenance` currently collects the last 7 days by script design. Telegram `/collect` is smarter and collects from the latest CSV timestamp to today.
- Token update is App cache-first through Telegram `/update_token`. Fiddler is only a backup path.
- Do not commit `.env.dev`, Telegram bot token, chat id, admin id, topic ids, or Z3A token values.
