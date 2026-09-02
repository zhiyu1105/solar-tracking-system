# 樹莓派資料節點移轉紀錄（2026-09-02）

## 目標

- 舊接收端：`https://solar-dashboard.tail7c1eb9.ts.net/api`
- 新接收端：`https://solar-dashboard-zhiyu.tail7c1eb9.ts.net/api`
- 正式專案：`C:\projects\solar-tracking-dashboard`
- 原則：先備份、再匯入穩定基線、最後切換 Pi，切換後補抓當日增量。

## 已完成

1. 舊站穩定基線已下載到：
   `C:\solar-data\backups\old-dashboard-pre-redirect-20260902`
2. `manifest.csv` 內 10 個 CSV 均已核對筆數並記錄 SHA-256。
3. 穩定基線共 202,147 筆，資料截止到 2026-09-01；不包含仍持續變動的 2026-09-02 即時資料。
4. 本機 MySQL 移轉前 dump：
   `C:\solar-data\backups\pre-power-record-migration-20260902_185428\solar_tracking_db.sql`
5. dump 大小 123,253,930 bytes，SHA-256：
   `B9B54A96989D3BA28E4EE49BBA3297DE6B73781CFF11CCF023D2FD2DF2D06F04`
6. 本機 SystemGroup ID 已與舊站一致，尤其 `system_id=2` 已修正為「實驗組II (ANFIS)」。
7. 基線已匯入新站；總筆數 202,147。重跑 dry-run 時待新增筆數為 0。
8. 新站公開 API 已通過 POST、GET、DELETE 實際測試。
9. 四台 Pi 的 controller 已改指向新站；每台均先建立同目錄時間戳備份。
10. system 2、6、7 已 restart 並在新站成功寫入首筆資料；system 4 原本 inactive，移轉後刻意保持 inactive。
11. 舊站 2026-09-02 最後增量共 540 筆，已下載、驗證並回填新站。

## 系統與 Pi 對應

| system_id | 系統 | Pi | Tailscale IP | SSH 使用者 | 程式 |
|---:|---|---|---|---|---|
| 2 | 實驗組II (ANFIS) | raspberrypi-1 | 100.66.182.46 | rte | `~/solar_tracking/anfis_2/anfis_controller.py` |
| 4 | 實驗組I | raspberrypi-v4 | 100.79.66.68 | raspberrypi | `~/solar_tracking/anfis_1/anfis_controller.py` |
| 6 | 對照組I | raspberrypi | 100.96.31.110 | raspberrypi | `~/solar_tracking/traditional_1/traditional_controller.py` |
| 7 | 對照組II | raspberrypi-v3 | 100.126.13.120 | raspberrypi | `~/solar_tracking/traditional_2/traditional_controller.py` |

四台均使用 `solar_tracking.service`。Tailscale 管理頁目前顯示四台皆 Connected，且 key expiry 已停用。

## Pi 切換結果

本次只替換 controller 內的 `api_url`，沒有 scp 覆蓋整份程式，因此現場 `system_id`、硬體 pin 與校正設定均保留。

| system_id | 切換時間 | 切換前 | 切換後 | Pi 備份 |
|---:|---|---|---|---|
| 2 | 19:09:21 | active | active | `anfis_controller.py.bak.endpoint-20260902_190921` |
| 4 | 19:08:34 | inactive | inactive | `anfis_controller.py.bak.endpoint-20260902_190834` |
| 6 | 19:08:33 | active | active | `traditional_controller.py.bak.endpoint-20260902_190833` |
| 7 | 19:08:34 | active | active | `traditional_controller.py.bak.endpoint-20260902_190834` |

新站首筆切換後資料：

- system 2：2026-09-02 19:09:29
- system 6：2026-09-02 19:08:34
- system 7：2026-09-02 19:08:35

舊站最後資料分別為 19:05:34、19:01:25、19:07:18，與新站首筆之間沒有超過原上傳週期的缺口。

## 日後維護登入方式

### 1. 從本機進入 Pi 所在 tailnet

本機 Windows Tailscale 是另一個帳號，需透過 `solar_tailscale` 容器：

```powershell
docker exec solar_tailscale apk add --no-cache openssh-client
docker exec -it solar_tailscale ssh -o "ProxyCommand=tailscale nc %h %p" rte@100.66.182.46
```

其他三台將帳號與 IP 換成上表內容。

### 每台 Pi 先備份，再只改 API URL

以下以 `raspberrypi-1` 為例：

```bash
file="$HOME/solar_tracking/anfis_2/anfis_controller.py"
stamp="$(date +%Y%m%d_%H%M%S)"
cp -a "$file" "$file.bak.$stamp"
grep -n "api_url" "$file"
sed -i 's#https://solar-dashboard\.tail7c1eb9\.ts\.net/api#https://solar-dashboard-zhiyu.tail7c1eb9.ts.net/api#g' "$file"
grep -n "api_url" "$file"
curl -fsS https://solar-dashboard-zhiyu.tail7c1eb9.ts.net/api/system-groups/ >/dev/null
sudo systemctl restart solar_tracking.service
sudo systemctl is-active solar_tracking.service
sudo journalctl -u solar_tracking.service --since "5 minutes ago" --no-pager | tail -n 80
```

若驗證失敗，立即以剛建立的 `.bak.<時間>` 覆蓋回原檔並 restart service。

## 最後增量回填結果

備份位置：`C:\solar-data\backups\old-dashboard-final-delta-20260902_191229`

| system_id | 筆數 | SHA-256 |
|---:|---:|---|
| 2 | 214 | `3D79D3AEDA6F85567DADFA1A0CEEE468AD525D4DC87584B529741D50E47C8C3B` |
| 6 | 114 | `C828F286BD86EB77E6983ABE70E83BA20AF6791C2DDB9770514EAF7FB7B8C802` |
| 7 | 212 | `8162937EBAFD444FCEA8989C4AF5DE92D20038215B25EFBF519010566333A691` |

final delta 下載範圍截至舊站最後一筆，與切換後新站資料無重疊。由於新站首筆已先寫入、最大 timestamp 已超過舊站最後一筆，這次歷史回填**不能**使用 `--append-after-max`，否則 540 筆會全部被略過。實際執行的是一次性完整匯入：

```powershell
docker cp C:\solar-data\backups\old-dashboard-final-delta-20260902_191229 solar_backend:/tmp/final-delta
docker exec solar_backend python manage.py import_power_records_csv `
  --manifest /tmp/final-delta/manifest.csv
```

這份 final delta 已匯入，不得再次執行上述完整匯入，否則會重複 540 筆。

## 驗證過的匯入指令

```powershell
docker exec solar_backend python manage.py import_power_records_csv `
  --manifest /tmp/old-dashboard-import/manifest.csv `
  --systems-json /tmp/old-dashboard-import/source_systems.json `
  --append-after-max `
  --dry-run
```

正式基線匯入結果：來源 202,147、略過既有 167,854、新增 34,293。再次 dry-run：略過 202,147、新增 0。

## Git 與敏感資料

- Repo 內目前的 Pi controller/config 預設 URL 已改成新節點。
- CSV、SQL dump、`.env.dev`、密碼、token 不得加入 Git。
- 部署到 Pi 時只做 URL 取代，不直接 scp 覆蓋整份 controller。
