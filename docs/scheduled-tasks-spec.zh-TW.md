# 太陽能監控 Windows 排程規格

最後更新：2026-06-23

這份文件說明本機平行運轉時使用的 Windows Scheduled Tasks，以及如何判斷排程是否正常啟動。

## 總覽

目前有兩個彼此獨立的 Windows 排程：

| 排程名稱 | 類型 | 用途 | 正常狀態 |
| --- | --- | --- | --- |
| `SolarTelegramBot` | 常駐 polling | 接收 Telegram 指令，回覆狀態查詢與人工操作審核。 | `Running` |
| `SolarWeeklyMaintenance` | 每週排程 | 執行週資料維護、檢查、收資料、reload、週報與告警。 | 週期外為 `Ready` |

`SolarTelegramBot` 和 `SolarWeeklyMaintenance` 不是同一個排程。前者負責 Telegram long polling，後者負責每週維護。兩者的關係是：週排程完成後可以透過 Telegram bot 推送週報與 token 告警。

## 排程一：SolarTelegramBot

### 目的

`SolarTelegramBot` 讓 Telegram bot 持續在線，底層會執行：

```powershell
C:\01_CODE\python311\python.exe -u -X utf8 scripts\telegram_bot.py poll
```

目前註冊的 Scheduled Task action 應該長這樣：

```powershell
powershell.exe -ExecutionPolicy Bypass -NoProfile -NonInteractive -WindowStyle Hidden -File "C:\projects\solar-tracking-dashboard\scripts\run_telegram_bot.ps1" -Python "C:\01_CODE\python311\python.exe"
```

重點是要有：

- `-WindowStyle Hidden`
- `-NonInteractive`
- `WorkingDirectory = C:\projects\solar-tracking-dashboard`
- `ExecutionTimeLimit = PT0S`

這樣 bot 才會背景執行，避免跳出容易誤觸的前景終端機，且不會被 Windows Task Scheduler 在預設 72 小時後強制停止。

### 負責功能

`SolarTelegramBot` 負責：

- 接收 `/status`、`/token`、`/csv`、`/docker`、`/weekly`、`/allstatus` 等查詢指令。
- 只在 `05-手動操作審核` topic 接受操作型指令。
- 操作型指令包含 `/collect`、`/update_token`、`/reload`、`/restart_backend`、`/run_weekly`。
- 每個操作都必須先用 `/confirm <code>` 二階段確認。
- 寫入 bot runtime log：

```text
C:\projects\solar-tracking-dashboard\logs\telegram_bot_YYYY-MM-DD.log
```

### 如何確認是否正常啟動

執行：

```powershell
Get-ScheduledTask -TaskName SolarTelegramBot
Get-ScheduledTask -TaskName SolarTelegramBot | Get-ScheduledTaskInfo
```

判讀方式：

| 欄位 | 正常值 | 意義 |
| --- | --- | --- |
| `State` | `Running` | Telegram bot polling 正在執行。 |
| `LastTaskResult` | `267009` | 常駐 task 執行中，這是正常值。 |
| `Actions.Execute` | `powershell.exe` | 透過 PowerShell wrapper 啟動。 |
| `Actions.Arguments` | 含 `-WindowStyle Hidden -NonInteractive` | 背景執行，不應跳前景終端機。 |
| `Actions.WorkingDirectory` | `C:\projects\solar-tracking-dashboard` | 必須指向正式專案路徑。 |
| `Settings.ExecutionTimeLimit` | `PT0S` | 無執行時間上限，才能長期常駐。 |

最直接的活性測試：到 Telegram 群組輸入 `/status` 或 `/whoami`。有回覆就代表 polling 正常。

如果 `State = Ready`，代表 Telegram bot 目前沒有在 long polling。請執行 `Start-ScheduledTask -TaskName SolarTelegramBot`，再確認是否變成 `Running`。

### 手動啟動

```powershell
Start-ScheduledTask -TaskName SolarTelegramBot
```

### 查看 log

不用手動改日期，直接抓最新 bot log：

```powershell
Get-ChildItem C:\projects\solar-tracking-dashboard\logs\telegram_bot_*.log |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content -Tail 20
```

正常會看到類似：

```text
Telegram polling started. Press Ctrl+C to stop.
```

### 重新註冊

如果排程不見、路徑錯誤、或會跳前景終端機，重新註冊：

```powershell
cd C:\projects\solar-tracking-dashboard
powershell.exe -ExecutionPolicy Bypass -NoProfile -File .\register_telegram_bot_task.ps1 -Python "C:\01_CODE\python311\python.exe"
Start-ScheduledTask -TaskName SolarTelegramBot
```

重新註冊後確認設定：

```powershell
Get-ScheduledTask -TaskName SolarTelegramBot |
  Select-Object -ExpandProperty Settings |
  Select-Object ExecutionTimeLimit,RestartCount,RestartInterval,Hidden
```

正常應看到：

```text
ExecutionTimeLimit : PT0S
RestartCount       : 3
RestartInterval    : PT2M
Hidden             : True
```

## 排程二：SolarWeeklyMaintenance

### 目的

`SolarWeeklyMaintenance` 執行每週資料維護腳本：

```text
C:\projects\solar-tracking-dashboard\solar_weekly_run.ps1
```

目前註冊的 action 應該是：

```powershell
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "C:\projects\solar-tracking-dashboard\solar_weekly_run.ps1"
```

### 排程時間

目前預期是：

```text
每週一 02:00
```

週排程不是常駐程式，所以平常 `State = Ready` 是正常的。只有正在跑的時候才會是 `Running`。

### 週排程包含哪些檢查

`SolarWeeklyMaintenance` 有包含多個檢查與維護步驟，不只是單純 collect。

| 階段 | 名稱 | 做什麼 | 異常時怎麼處理 |
| --- | --- | --- | --- |
| Stage 1 | Backend health check | 檢查 `solar_backend` Docker container。若沒跑，嘗試用 `docker-compose -f docker-compose-dev.yml up -d backend` 啟動。接著檢查 Django API `http://localhost:8000/api/fixed-panels/status/`。 | 寫入 `WARN` 或 `FAIL`。 |
| Stage 2 | Token status check | 執行 `python -X utf8 z3a_check_token.py`，解析 access token 剩餘時間。 | token 過期就跳過 Z3A collection；若剩餘天數低則仍收資料但記錄警告。 |
| Stage 2.5 | Illumination CSV merge | 檢查 `data\illumination_inbox` 是否有新的照度 CSV。若有，執行 `merge_illumination_csv.py --csv <file>`，成功後移到 `data\illumination_archive`。 | merge 失敗的檔案留在 inbox，下次可重試。 |
| Stage 3 | Z3A data collection | token 可用時執行 `python -X utf8 z3a_collect.py --pipeline --days 7`。 | exit code 非 0 是 `FAIL`；若 exit 0 但新資料 0 筆，記為 `WARN`。 |
| Stage 3.5 | Backend cache reload | 呼叫 `POST http://localhost:8000/api/fixed-panels/reload/`，讓 dashboard 重新載入資料快取。 | reload 失敗記為 `WARN`，不會抹掉 collection 結果。 |
| Stage 4 | Backup listing | 列出最近的 `data\*.bak.*.csv` 備份檔。 | 失敗記為 `WARN`。 |
| Final | Report generation | 寫出完整 log 與摘要週報。 | 產出 `logs\latest_report.txt`。 |
| Stage 5 | Telegram notification | 執行 `telegram_bot.py send-report --report-file logs\latest_report.txt`，再執行 `telegram_bot.py check-token-alert`。 | Telegram 推送失敗只記 `WARN`，不讓整個週排程失敗。 |

### 週排程輸出

完整 log：

```text
C:\projects\solar-tracking-dashboard\logs\solar_weekly_YYYY-MM-DD.log
```

最新摘要週報：

```text
C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

查看最新 weekly log：

```powershell
Get-ChildItem C:\projects\solar-tracking-dashboard\logs\solar_weekly_*.log |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content -Tail 60
```

查看最新週報：

```powershell
Get-Content C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

### 如何確認是否正常

執行：

```powershell
Get-ScheduledTask -TaskName SolarWeeklyMaintenance
Get-ScheduledTask -TaskName SolarWeeklyMaintenance | Get-ScheduledTaskInfo
```

判讀方式：

| 欄位 | 正常值 | 意義 |
| --- | --- | --- |
| `State` | `Ready` | 排程已註冊，正在等待下次週一 02:00。 |
| `LastTaskResult` | `0` | 上次執行成功。 |
| `LastRunTime` | 最近一次週一 02:00 | 上次實際執行時間。 |
| `NextRunTime` | 下次週一 02:00 | 下次排程時間。 |
| `Actions.WorkingDirectory` | `C:\projects\solar-tracking-dashboard` | 必須指向正式專案路徑。 |

如果正在手動或自動執行中，`State = Running` 是正常的。

### 手動觸發週排程

```powershell
Start-ScheduledTask -TaskName SolarWeeklyMaintenance
```

觸發後檢查：

```powershell
Get-ScheduledTask -TaskName SolarWeeklyMaintenance | Get-ScheduledTaskInfo
Get-Content C:\projects\solar-tracking-dashboard\logs\latest_report.txt
```

## 一次檢查兩個排程

可直接貼上以下指令：

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

## 目前本機觀察狀態

2026-06-23 實際觀察：

| 排程 | 狀態 | LastTaskResult | 判讀 |
| --- | --- | --- | --- |
| `SolarTelegramBot` | `Running` | `267009` | 正常，bot polling 正在執行。 |
| `SolarWeeklyMaintenance` | `Ready` | `0` | 正常，上次週排程成功，正在等待下次執行。 |

觀察到的下次週排程時間：

```text
2026-06-29 02:00
```

## 注意事項

- Docker Desktop 只能管理 container，不等於 Telegram bot 或週排程有啟動。
- Telegram bot 與 weekly maintenance 都由 Windows Scheduled Task 控制。
- `SolarTelegramBot` 應該背景執行；如果一直跳終端機，請重新註冊 task。
- `SolarWeeklyMaintenance` 目前腳本內固定收最近 7 天。Telegram `/collect` 則較聰明，會從 CSV 最新 timestamp 補抓到今天。
- token 更新主流程是七云物聯 App cache / Telegram `/update_token`，Fiddler 只作備援。
- 不要把 `.env.dev`、Telegram bot token、chat id、admin id、topic id、Z3A token commit 到 GitHub。
