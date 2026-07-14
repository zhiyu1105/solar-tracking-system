# Z3A 手動更新資料指令

最後確認日期：2026-06-16
專案位置：`C:\projects\solar-tracking-dashboard`
主 CSV：`C:\projects\solar-tracking-dashboard\data\combined_solar_data_20250301_20260406_processed.csv`

這份文件給「只能手動貼指令」時使用。指令請在 PowerShell 執行。不要把 `.env.dev`、token、App cache 內容貼到 GitHub、群組或簡報。

## 0. 先進專案資料夾

```powershell
cd C:\projects\solar-tracking-dashboard
```

## 1. 看這週排程有沒有成功

查 Windows 排程最後一次執行狀態：

```powershell
Get-ScheduledTaskInfo -TaskName 'SolarWeeklyMaintenance' |
  Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns
```

判讀：

- `LastTaskResult = 0`：排程程序有跑完。
- 仍要看 `logs\latest_report.txt`，因為排程跑完不代表真的有新增資料。

看最新週報：

```powershell
Get-Content C:\projects\solar-tracking-dashboard\logs\latest_report.txt -Tail 80
```

重點看：

- `Data collection: OK new=... total=...`：Z3A 資料有合併成功。
- `Data collection: WARN exit=0 but 0 new rows merged`：腳本有跑完，但沒有新資料進 CSV。
- `Cache reload: OK reloaded (...)`：dashboard 已重新載入 CSV。
- `Cache reload: WARN failed`：CSV 可能已更新，但 Docker/backend 沒開，dashboard 還沒 reload。

## 2. 直接檢查 CSV 是否真的有新資料

```powershell
@'
import pandas as pd
from pathlib import Path

p = Path(r"C:\projects\solar-tracking-dashboard\data\combined_solar_data_20250301_20260406_processed.csv")
df = pd.read_csv(p, usecols=["timestamp", "date", "panel_id"], dtype=str, low_memory=False)
ts = pd.to_datetime(df["timestamp"], errors="coerce")

print("rows=", len(df))
print("timestamp_min=", ts.min())
print("timestamp_max=", ts.max())
print("rows_2026_06_09_to_2026_06_15=", int(((ts >= "2026-06-09") & (ts < "2026-06-16")).sum()))
print("rows_2026_06_15=", int(((ts >= "2026-06-15") & (ts < "2026-06-16")).sum()))
print("panels_total=", df["panel_id"].nunique())
'@ | C:\01_CODE\python311\python.exe -X utf8 -
```

2026-06-16 實測結果：

```text
rows= 1041474
timestamp_max= 2026-06-14 16:40:00
rows_2026_06_09_to_2026_06_15= 12177
rows_2026_06_15= 0
panels_total= 28
```

解讀：2026-06-15 週排程有成功新增 12,177 筆，資料目前到 2026-06-14 16:40。

## 3. 檢查 token 與 App cache

先看 `.env.dev` 的 JWT 到期時間：

```powershell
C:\01_CODE\python311\python.exe -X utf8 z3a_check_token.py
```

2026-06-16 實測：

```text
COLLECTION_AUTH_STATUS: OK access token usable — 剩 3.1 天
Z3A_TOKEN 到期：2026-06-19 17:17:26
Z3A_REFRESH_TOKEN 到期：2026-09-17 17:17:26
```

`COLLECTION_AUTH_STATUS` 是週排程會先讀到的判斷行。2026-06-16 已做本機小修：如果 access token 過期，但 `Z3A_REFRESH_TOKEN/token2` 仍可直接呼叫資料 API，這行會顯示 token2 fallback 可用，避免週排程直接跳過收資料。

七雲物聯桌面 App 的本機登入狀態通常存在：

```text
C:\Users\USER\AppData\Roaming\iot7.cn\七云物联\shared_preferences.json
```

檢查 App cache 是否存在，以及 App token 是否和 `.env.dev` 相同：

```powershell
@'
import base64, json, os, time
from datetime import datetime
from pathlib import Path

def load_env(path):
    d = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def jwt_exp(tok):
    try:
        parts = tok.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload.encode())).get("exp"))
    except Exception:
        return None

def fmt(tok):
    exp = jwt_exp(tok or "")
    if not exp:
        return "not found / not jwt"
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S") + f" ({(exp-time.time())/86400:.1f} days left)"

def find_token(obj, key):
    if key in obj and isinstance(obj[key], str):
        return obj[key]
    cur = obj
    for part in key.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
    return cur if isinstance(cur, str) else ""

env = load_env(r"C:\projects\solar-tracking-dashboard\.env.dev")
print("ENV access exp:", fmt(env.get("Z3A_TOKEN", "")))
print("ENV refresh exp:", fmt(env.get("Z3A_REFRESH_TOKEN", "")))

root = Path(os.environ.get("APPDATA", r"C:\Users\USER\AppData\Roaming")) / "iot7.cn"
paths = sorted(root.rglob("shared_preferences.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
print("APP shared_preferences count:", len(paths))

for p in paths[:3]:
    obj = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    access = find_token(obj, "flutter.token") or find_token(obj, "token") or find_token(obj, "tokenString")
    refresh = find_token(obj, "flutter.token2") or find_token(obj, "token2") or find_token(obj, "tokenString2") or find_token(obj, "refreshToken")
    print("APP path:", str(p))
    print("APP access exp:", fmt(access))
    print("APP refresh exp:", fmt(refresh))
    print("APP access same as ENV:", bool(access and access == env.get("Z3A_TOKEN", "")))
    print("APP refresh same as ENV:", bool(refresh and refresh == env.get("Z3A_REFRESH_TOKEN", "")))
'@ | C:\01_CODE\python311\python.exe -X utf8 -
```

## 4. 測 access token / refresh token 是否能拿資料

這段不會印出 token 值，只會印 API 結果。

```powershell
@'
import base64, json, time
from datetime import datetime
from pathlib import Path
import requests
requests.packages.urllib3.disable_warnings()

def load_env(path):
    d = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def jwt_exp(tok):
    try:
        parts = tok.split(".")
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload.encode())).get("exp"))
    except Exception:
        return None

def fmt_exp(tok):
    exp = jwt_exp(tok)
    if not exp:
        return "not-jwt/no-exp"
    return datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S") + f" ({(exp-time.time())/86400:.1f} days left)"

env = load_env(r"C:\projects\solar-tracking-dashboard\.env.dev")
base = env.get("Z3A_BASE_URL", "https://server.qiyunwulian.com:12341")
access = env.get("Z3A_TOKEN", "")
refresh = env.get("Z3A_REFRESH_TOKEN", "")

print("access exp:", fmt_exp(access))
print("refresh/token2 exp:", fmt_exp(refresh))

def bind(label, token):
    r = requests.get(base + "/bind/query", headers={"auth": token}, verify=False, timeout=20)
    raw = r.json()
    data = raw.get("data")
    if isinstance(data, str):
        data = json.loads(data)
    n = len(data) if isinstance(data, list) else None
    print(f"{label}: http={r.status_code}, code={raw.get('code')}, msg={raw.get('msg')}, devices={n}")
    return data if isinstance(data, list) else []

devices = bind("access -> /bind/query", access)
bind("refresh/token2 -> /bind/query", refresh)

if devices:
    did = devices[0].get("DeviceId")
    dtype = str(devices[0].get("DeviceType", "2"))
    params = {
        "DeviceId": did,
        "DeviceType": dtype,
        "measured_fun": 1,
        "start_time": "2026-06-14 08:00:00",
        "end_time": "2026-06-14 09:00:00",
        "accuracy": "10m",
    }
    for label, token in [("access -> /history/period", access), ("refresh/token2 -> /history/period", refresh)]:
        r = requests.get(base + "/history/period", headers={"auth": token}, params=params, verify=False, timeout=20)
        raw = r.json()
        count = 0
        for item in raw.get("data") or []:
            for s in item.get("Series") or []:
                count += len(s.get("values") or [])
        print(f"{label}: http={r.status_code}, code={raw.get('code')}, msg={raw.get('msg')}, points={count}")
'@ | C:\01_CODE\python311\python.exe -X utf8 -
```

2026-06-16 實測：

```text
access -> /bind/query: code=0, devices=30
refresh/token2 -> /bind/query: code=0, devices=30
access -> /history/period: code=0, points=5
refresh/token2 -> /history/period: code=0, points=5
```

結論：目前 `Z3A_REFRESH_TOKEN` / `token2` 也可以直接拿資料。這解釋了桌面 App 為什麼可以保持登入狀態：App 把 `flutter.token` 和 `flutter.token2` 存在本機 cache；短期 access token 到期後，App 仍可用較長效的 token2 維持 session 或取資料。

注意：目前專案裡嘗試用 token2 呼叫 `/user/refreshToken` 換新 access token 的端點測試失敗，所以不要假設 token2 一定能透過已知 refresh endpoint 換新 token。比較可靠的備援是「App cache 有新 token 就同步到 `.env.dev`」，或在必要時確認 token2 直接取資料仍可用。

2026-06-16 本機程式修正：

- `z3a_collect.py`：access token 過期時，會先嘗試 refresh endpoint；如果端點失敗，會測 `Z3A_REFRESH_TOKEN/token2` 能不能直接呼叫 `/bind/query`。可以的話，本次收資料改用 token2 當 `auth`。
- `z3a_check_token.py`：新增 `COLLECTION_AUTH_STATUS` 第一行，讓週排程先判斷「收資料是否有可用 token」，而不是只看短效 access token。
- 這是本機平行運轉修正，尚未推到 GitHub。

## 5. 從 App cache 更新 `.env.dev`

如果 App cache 裡的 token 比 `.env.dev` 新，執行：

```powershell
cd C:\projects\solar-tracking-dashboard
powershell.exe -ExecutionPolicy Bypass -NoProfile -File .\scripts\update_z3a_token_from_app_cache.ps1 -Apply
```

如果 Docker/backend 有開，並且要讓 dashboard 立刻讀新 `.env.dev`：

```powershell
cd C:\projects\solar-tracking-dashboard
powershell.exe -ExecutionPolicy Bypass -NoProfile -File .\scripts\update_z3a_token_from_app_cache.ps1 -Apply -RecreateBackend -CheckStatus
```

此腳本會自動備份舊 `.env.dev`，備份檔格式：

```text
.env.dev.bak.yyyyMMdd_HHmmss
```

## 6. 手動補跑 Z3A 資料

最常用：補最近 7 天。

```powershell
cd C:\projects\solar-tracking-dashboard
C:\01_CODE\python311\python.exe -X utf8 z3a_collect.py --pipeline --days 7
```

指定日期區間：

```powershell
cd C:\projects\solar-tracking-dashboard
C:\01_CODE\python311\python.exe -X utf8 z3a_collect.py --pipeline --start 2026-06-09 --end 2026-06-15
```

先乾跑、不寫 CSV：

```powershell
cd C:\projects\solar-tracking-dashboard
C:\01_CODE\python311\python.exe -X utf8 z3a_collect.py --pipeline --start 2026-06-09 --end 2026-06-15 --dry-run
```

成功時會看到類似：

```text
新資料（pipeline 輸出）：12177 筆
合併後總筆數：1041474
已備份原始 CSV：...
已寫入主 CSV：...
```

## 7. 啟動 Docker/backend 並 reload dashboard

如果 `latest_report.txt` 顯示：

```text
Cache reload   : WARN failed (backend will mtime auto-reload next start)
```

代表 CSV 已更新，但 dashboard 沒有 reload。先啟動 Docker Desktop：

```powershell
Start-Process -FilePath 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
```

等 Docker ready：

```powershell
docker info
```

啟動專案容器：

```powershell
cd C:\projects\solar-tracking-dashboard
docker compose -f docker-compose-dev.yml up -d
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

手動 reload dashboard CSV 快取：

```powershell
$reloadResp = Invoke-RestMethod -Uri 'http://localhost:8000/api/fixed-panels/reload/' -Method POST -TimeoutSec 60
$reloadResp | ConvertTo-Json -Depth 5
```

確認 dashboard 狀態：

```powershell
$status = Invoke-RestMethod -Uri 'http://localhost:8000/api/fixed-panels/status/' -Method GET -TimeoutSec 30
$status | ConvertTo-Json -Depth 5
```

2026-06-16 reload 實測：

```text
success=true
df_rows=896404
date_range=2025-03-01 ~ 2026-06-14
reloaded_at=2026-06-16 15:42:41
```

## 8. Access token 過期時的備援判斷

如果 `z3a_check_token.py` 顯示 `Z3A_TOKEN` 已過期：

1. 先跑第 3 節，確認 App cache 是否有更新 token。
2. 如果 App cache 有更新，跑第 5 節同步 `.env.dev`。
3. 如果 App cache 沒更新，跑第 4 節確認 `Z3A_REFRESH_TOKEN/token2` 是否仍可直接取資料。
4. 如果 token2 仍可直接取資料，2026-06-16 之後的本機版本會讓 `z3a_collect.py` 自動 fallback 到 token2。可以用第 6 節手動補跑驗證。
5. 如果 `COLLECTION_AUTH_STATUS` 顯示 `FAIL`，且 App cache 也沒有新 token，就需要重新登入七雲物聯 App 或重新取得 token。

目前已知狀態：

- `Z3A_TOKEN`：到期 `2026-06-19 17:17:26`
- `Z3A_REFRESH_TOKEN/token2`：到期 `2026-09-17 17:17:26`
- token2 於 `2026-06-16` 實測可直接呼叫 `/bind/query` 與 `/history/period`
- 已知 `/user/refreshToken` 等 refresh endpoint 測試失敗，端點可能不是目前程式猜的形式

## 9. 最小手動流程

如果只想最少步驟確認並補資料：

```powershell
cd C:\projects\solar-tracking-dashboard
Get-Content .\logs\latest_report.txt -Tail 80
C:\01_CODE\python311\python.exe -X utf8 z3a_check_token.py
C:\01_CODE\python311\python.exe -X utf8 z3a_collect.py --pipeline --days 7
docker compose -f docker-compose-dev.yml up -d
$reloadResp = Invoke-RestMethod -Uri 'http://localhost:8000/api/fixed-panels/reload/' -Method POST -TimeoutSec 60
$reloadResp | ConvertTo-Json -Depth 5
```

最後再檢查 CSV：

```powershell
@'
import pandas as pd
p = r"C:\projects\solar-tracking-dashboard\data\combined_solar_data_20250301_20260406_processed.csv"
df = pd.read_csv(p, usecols=["timestamp"], dtype=str, low_memory=False)
ts = pd.to_datetime(df["timestamp"], errors="coerce")
print("rows=", len(df))
print("timestamp_max=", ts.max())
'@ | C:\01_CODE\python311\python.exe -X utf8 -
```
