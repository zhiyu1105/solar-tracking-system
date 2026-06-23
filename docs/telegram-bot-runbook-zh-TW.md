# Telegram Bot Runbook

這份文件說明太陽能追蹤 dashboard 的 Telegram bot 設定、啟動、指令與維護方式。一般查詢指令是 read-only；操作型指令只允許在 `05-手動操作審核` topic 使用，且必須二階段確認。

## 1. 必要設定

`.env.dev` 需設定下列欄位；不要把 token、chat id、admin id 貼到 GitHub、PPT 或公開截圖。

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ADMIN_USER_IDS=
TELEGRAM_ALERT_ENABLED=1
TELEGRAM_MESSAGE_THREAD_ID=
TELEGRAM_WEEKLY_THREAD_ID=
TELEGRAM_CSV_THREAD_ID=
TELEGRAM_TOKEN_THREAD_ID=
TELEGRAM_DOCKER_THREAD_ID=
TELEGRAM_ALERT_THREAD_ID=
TELEGRAM_OPS_THREAD_ID=
```

Topic 對應：

| Topic | 用途 | env |
| --- | --- | --- |
| `00-週報摘要` | 週報與 compact status | `TELEGRAM_WEEKLY_THREAD_ID` |
| `01-排程與CSV` | CSV freshness 與資料更新狀態 | `TELEGRAM_CSV_THREAD_ID` |
| `02-Z3A與Token` | Z3A token 狀態 | `TELEGRAM_TOKEN_THREAD_ID` |
| `03-Docker與Dashboard` | Docker container 與 backend health | `TELEGRAM_DOCKER_THREAD_ID` |
| `04-異常告警` | token 到期、操作失敗等告警 | `TELEGRAM_ALERT_THREAD_ID` |
| `05-手動操作審核` | 操作型指令確認與執行紀錄 | `TELEGRAM_OPS_THREAD_ID` |

## 2. 啟動與檢查

手動啟動 polling：

```powershell
Start-ScheduledTask -TaskName SolarTelegramBot
```

確認狀態：

```powershell
Get-ScheduledTask -TaskName SolarTelegramBot
Get-ScheduledTask -TaskName SolarTelegramBot | Get-ScheduledTaskInfo
```

看 log：

```powershell
Get-Content C:\projects\solar-tracking-dashboard\logs\telegram_bot_YYYY-MM-DD.log -Tail 20
```

重新註冊排程：

```powershell
cd C:\projects\solar-tracking-dashboard
powershell.exe -ExecutionPolicy Bypass -NoProfile -File .\register_telegram_bot_task.ps1 -Python "C:\01_CODE\python311\python.exe"
```

`SolarTelegramBot` 與 `SolarWeeklyMaintenance` 是兩個獨立排程。前者負責 Telegram long polling；後者負責週資料維護並可推送週報與 token 告警。

## 3. 查詢指令

| 指令 | 功能 |
| --- | --- |
| `/help` | 顯示指令說明 |
| `/status` | 回覆最新 compact dashboard status |
| `/weekly` | 回覆最新週報 |
| `/allstatus` | 分流推送週報、CSV、Token、Docker 狀態到各 topic |
| `/token` | 檢查 Z3A token 狀態，輸出已遮罩 |
| `/csv` | 檢查 CSV 路徑、大小與最新 timestamp |
| `/docker` | 檢查 Docker container 與 backend API |
| `/log` | 回覆最新 weekly log 尾端，已遮罩敏感字串 |
| `/whoami` | 顯示 Telegram user id 與 chat id |
| `/ops` | 顯示操作型指令與安全規則 |

## 4. 操作型指令

操作型指令只能在 `05-手動操作審核` topic 使用。流程是先輸入操作指令，bot 回覆 60 秒有效確認碼，再輸入 `/confirm <code>` 才會執行。

| 指令 | 功能 |
| --- | --- |
| `/collect` | 讀取主 CSV 最新 timestamp，從該日期補抓到今天，成功後 reload backend cache |
| `/update_token` | 從七云物聯 Windows App cache 更新 `.env.dev`，重建 backend 並檢查狀態 |
| `/reload` | 呼叫 backend reload API |
| `/restart_backend` | 重啟 backend container |
| `/run_weekly` | 觸發 `SolarWeeklyMaintenance` |
| `/confirm <code>` | 確認 pending operation |
| `/cancel` | 取消 pending operation |

操作結果會寫入 `logs\telegram_ops_YYYY-MM-DD.log`；失敗會同步送到 `04-異常告警`。`/update_token` 不會輸出 token 值，只會回報路徑、token 長度、到期時間與狀態。

## 5. Token 更新與告警

主要更新流程已改為 App cache，不再以 Fiddler 為主：

1. 確認七云物聯 Windows App 已登入。
2. 在 `05-手動操作審核` topic 輸入 `/update_token`。
3. 依 bot 回覆輸入 `/confirm <code>`。
4. bot 會讀取 `C:\Users\USER\AppData\Roaming\iot7.cn\七云物联\shared_preferences.json`，更新 `.env.dev`，重建 backend 並檢查狀態。

Fiddler 僅保留為 App cache 失效時的備援。

Token 告警規則：

| 條件 | 告警 topic |
| --- | --- |
| Access token 剩餘 < 7 天 | `04-異常告警` |
| Refresh token/token2 剩餘 < 30 天 | `04-異常告警` warning |
| Refresh token/token2 剩餘 < 14 天 | `04-異常告警` urgent |
| Refresh token/token2 剩餘 < 7 天 | `04-異常告警` critical |

手動測試：

```powershell
cd C:\projects\solar-tracking-dashboard
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py check-token-alert --dry-run --simulate-access-days 6 --simulate-refresh-days 29
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py check-token-alert --dry-run --simulate-access-days 8 --simulate-refresh-days 13
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py check-token-alert --dry-run --simulate-access-days 8 --simulate-refresh-days 6
```

## 6. 常用 CLI

```powershell
cd C:\projects\solar-tracking-dashboard
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py status
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py send-report --dry-run
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py send-all-status --dry-run
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py send-token-status --dry-run
C:\01_CODE\python311\python.exe -X utf8 scripts\telegram_bot.py check-token-alert --dry-run
```
