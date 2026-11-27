# AAMS Docker 測試環境說明（v1.0.0-rc.2）

這個資料夾用來在 Docker 裡跑一套「和正式環境相同設定」的測試站，給紅隊內測與部署驗證使用。

- **設定檔沿用專案根目錄的 `docker-compose.yml`**（正式環境用的那份），這裡只放測試用的 `.env` 與本說明。這樣測到的就是正式部署會用的設定。
- 測試資料全部放在 Docker 的 **named volume**（Docker 管理的資料區），和開發用的 `data/` 資料夾完全分開。
- 專案名稱固定用 `-p aams-test`，所以測試環境的容器與 volume 名稱都以 `aams-test_` 開頭，不會和正式環境（`aams_`）混在一起。

> [!NOTE]
> **已驗證：** 2026-11-27 在 Windows 10 + Docker Desktop（Engine 29.9.3、Compose v5.7.1）上，以 `v1.0.0-rc.2` 實際跑過本文件第 2～5 節的指令，並以腳本跑完第 6 節驗證清單中標 ✅ 的項目。標 ⬜ 的項目需要人工或另一台電腦確認。
>
> **舊版 Docker Desktop 注意：** 24.0.5 版在這台機器上 build 時引擎會卡死（`Bad Gateway`），升級後正常。遇到 `docker ps` 沒有回應時請先升級 Docker Desktop。

---

## 1. 需要什麼

- Docker Desktop（Windows／macOS）或 Docker Engine + Compose v2（Linux）
- 指令都在**專案根目錄**執行（`docker-compose.yml` 所在的地方）
- 測試版本：`git checkout v1.0.0-rc.2`（回報問題時請註明版本）

為了少打字，先定義一個簡寫（PowerShell，每開一個新視窗要重新貼一次）：

```powershell
function dc { docker compose -p aams-test --env-file docker-test/.env -f docker-compose.yml @args }
```

macOS／Linux（bash/zsh）：

```bash
alias dc='docker compose -p aams-test --env-file docker-test/.env -f docker-compose.yml'
```

---

## 2. 第一次啟動

### 2-1 建立 `.env`

```powershell
Copy-Item docker-test\env.example docker-test\.env
python -c "import secrets;print(secrets.token_urlsafe(48))"   # 貼到 AAMS_SECRET_KEY
python -c "import secrets;print(secrets.token_urlsafe(24))"   # 貼到 POSTGRES_PASSWORD
```

| 變數 | 單機測試 | 給區網其他電腦連 |
|---|---|---|
| `AAMS_DOMAIN` | `localhost` | 本機區網 IP，例如 `192.168.1.50` |
| `AAMS_BIND_IP` | `127.0.0.1` | 同一個區網 IP |

> [!IMPORTANT]
> - `docker-test/.env` 已被 `.gitignore` 排除，**不要提交**。
> - `AAMS_SECRET_KEY` 建立之後**不要再換**。換掉之後，已綁定的 TOTP 會全部解不開（和 `manage.py init --force` 同一個問題）。要重來請用第 5 節的方法。

### 2-2 建置並啟動

```powershell
dc up -d --build
dc ps                     # 四個服務：db、app、inspector、caddy 都應該是 running
dc logs -f app            # 看到 Uvicorn running on http://0.0.0.0:8000 即可 Ctrl+C
```

第一次 build 約需數分鐘（下載 Python image、安裝套件），之後會用快取。

啟動順序：`db` 有健康檢查（`pg_isready`），`app` 會等 `db` 顯示 `(healthy)` 才啟動。如果之後 app 啟動時還是連不上資料庫（例如手動 `dc restart app` 時 db 沒開），app 會印出「AAMS 啟動失敗」並結束，Docker 會自動重新啟動它，db 恢復後網站就會自己恢復，不需要手動處理。

### 2-3 建立示範資料

```powershell
dc exec app python manage.py seed-demo --active
```

帳號與密碼和本機測試相同（見專案 README「測試帳號」）：密碼一律 `Ais3-Demo-2026`。

### 2-4 開啟網站

- 主站：<https://localhost>（區網就是 `https://<AAMS_DOMAIN>`）
- 檔案預覽來源：`https://<AAMS_DOMAIN>:8443`

Caddy 用自己的內部 CA 簽發憑證，瀏覽器會警告「不安全」。二選一：

1. **快速**：主站和 `:8443` 各打開一次、手動按「繼續前往」。沒有先接受 `:8443` 的話，PDF 預覽框會是空白。
2. **建議（紅隊測試用）**：把 Caddy 的根憑證匯入**測試機**的信任清單：

   ```powershell
   dc cp caddy:/data/caddy/pki/authorities/local/root.crt docker-test/caddy-root.crt
   ```

   用 `dc cp`，不要用 `dc exec caddy cat ... > 檔案`：Windows PowerShell 的 `>` 會改動檔案內容（加 BOM、換行），憑證可能匯入失敗。

   Windows：雙擊 `caddy-root.crt` → 安裝憑證 → 本機電腦 → 「受信任的根憑證授權單位」。
   測試結束後記得移除，並刪除 `caddy-root.crt`。

### 2-5 管理員兩步驟驗證

`staff1~3@demo.ais3.local` 第一次進管理後台要綁定 TOTP。可以用手機掃 QR，或直接取驗證碼：

```powershell
dc exec app python manage.py totp-code staff1@demo.ais3.local
```

---

## 3. 資料放在哪裡、怎麼掛載

| Volume（實際名稱） | 掛在哪個容器的哪裡 | 內容 | 刪掉會怎樣 |
|---|---|---|---|
| `aams-test_pgdata` | db：`/var/lib/postgresql/data` | PostgreSQL 資料庫（帳號、作業、分數、稽核紀錄） | 所有資料不見 |
| `aams-test_appdata` | app：`/data` | 上傳檔案：`quarantine/`（檢查中）、`accepted/`（通過）、`materials/`（教材）、`tmp/`、`backups/` | 所有上傳檔案不見 |
| `aams-test_spool` | app：`/data/spool`、inspector：`/spool` | app 與檔案檢查容器交換檔案用的暫存區 | 沒影響（處理中的檢查會逾時失敗） |
| `aams-test_caddydata` | caddy：`/data` | Caddy 內部 CA 與憑證 | 憑證重新產生，要重新信任根憑證 |

設計上的隔離（測試時請一併確認沒有被破壞）：

- `db` 只在 `backend` 內部網段，**沒有對外 port**。
- `inspector`（檔案檢查）**沒有網路**（`network_mode: none`）、檔案系統唯讀、只看得到 `spool`，拿不到資料庫密碼和其他學生的檔案。
- 只有 `caddy` 對外開 443／8443，而且只綁在 `AAMS_BIND_IP`。

### 為什麼用 named volume，不用資料夾掛載（bind mount）

- 容器裡的程式以 uid `10001`（使用者 `aams`）執行。Linux 上把主機資料夾掛進去時，資料夾通常是 root 擁有，app 會寫不進去。
- Windows／macOS 上 PostgreSQL 放在 bind mount 容易遇到權限與效能問題。
- Named volume 由 Docker 管理，第一次建立時會自動帶入正確權限。

如果你一定要在主機上直接看到上傳的檔案（例如紅隊要檢查檔案落地位置），**只對 `appdata` 改用 bind mount**，資料庫維持 named volume。做法是在 `docker-test/` 新增 `compose.bind.yml`：

```yaml
services:
  app:
    volumes:
      - ./docker-test/appdata:/data
      - spool:/data/spool
```

```powershell
# 指令多加一個 -f
docker compose -p aams-test --env-file docker-test/.env -f docker-compose.yml -f docker-test/compose.bind.yml up -d
```

Linux 主機要先給權限：`mkdir -p docker-test/appdata && sudo chown -R 10001:10001 docker-test/appdata`。
`docker-test/appdata/` 裡有學員上傳的檔案，已加進 `.gitignore`，不要提交。（這個 bind mount 做法沒有實測。）

### 直接查看 volume 裡的檔案

```powershell
dc exec app ls -la /data/accepted /data/quarantine
```

---

## 4. 日常操作

| 要做的事 | 指令 |
|---|---|
| 看狀態 | `dc ps` |
| 看紀錄 | `dc logs -f app inspector caddy` |
| 停止（資料保留） | `dc stop` |
| 再啟動 | `dc start` |
| 關掉並移除容器（資料保留） | `dc down` |
| 進入 app 容器 | `dc exec app sh` |
| 進入資料庫 | `dc exec db psql -U aams aams` |
| 取 TOTP 驗證碼（僅測試） | `dc exec app python manage.py totp-code <email>` |

### 備份

```powershell
New-Item -ItemType Directory -Force docker-test\backups | Out-Null
dc exec -T db pg_dump -U aams -f /tmp/aams.sql aams
dc cp db:/tmp/aams.sql docker-test/backups/aams.sql
docker run --rm -v aams-test_appdata:/data -v ${PWD}/docker-test/backups:/backup alpine tar czf /backup/appdata.tgz -C /data accepted materials
```

- 資料庫先在容器裡輸出成檔案再 `dc cp` 出來。不要用 `pg_dump ... > 檔案`：Windows PowerShell 5.1 的 `>` 會用系統編碼重新寫入，中文可能變亂碼。
- 在 Git Bash 裡跑時，`/tmp`、`/data` 這類路徑會被自動改成 Windows 路徑而失敗；請用 PowerShell，或在指令前加 `MSYS_NO_PATHCONV=1`。
- `docker-test/backups/` 內含個資與上傳檔案，已加進 `.gitignore`，不要提交。

---

## 5. 重置

### 5-1 重置測試資料（保留 SECRET 與憑證）

每一輪紅隊測試結束後用這個。會清空資料庫與上傳檔案，重建示範資料：

```powershell
dc stop app
dc run --rm app python manage.py reset-demo --yes
dc start app
```

- 資料庫裡有非 `@demo.ais3.local` 的帳號（例如紅隊用 CSV 匯入的帳號）時會拒絕執行，確定要刪請加 `--force`。
- 重置後管理員要重新綁定 TOTP（手機上舊的項目刪掉再掃新 QR，或用 `totp-code`）。

### 5-2 完全砍掉重來

```powershell
dc down -v        # -v 會刪除上面四個 volume（資料庫、上傳檔、Caddy 憑證全部不見）
```

接著回到第 2-2 節重新啟動。Caddy 根憑證會重新產生，要重新信任。

---

## 6. 驗證清單

✅ = 2026-11-27 已用腳本（HTTPS 直接打 `https://localhost`）實測通過；⬜ = 尚待人工確認。

同一份腳本放在 `docker-test/docker_smoke.py`，可以自己重跑（需要 `pip install httpx`，在專案根目錄執行，跑之前先 `reset-demo`）：

```powershell
dc stop app; dc run --rm app python manage.py reset-demo --yes; dc start app
python docker-test/docker_smoke.py      # 最後一行應為 13/13 passed
```

腳本會留下一份提交與已發布成績，並替 `staff1` 綁定 TOTP；跑完再 `reset-demo` 一次即可回到乾淨狀態。

- ✅ `dc up -d --build` 成功，`dc ps` 四個服務都是 running
- ✅ `https://localhost/login` 回 200（compose 沒有開 80 port，所以 `http://` 連不到是正常的）
- ✅ `seed-demo --active` 成功，學員 `s011@demo.ais3.local` 能登入（第一次登入會先到個資同意頁）
- ✅ 學員上傳 PDF → 收據頁出現 SHA-256 → 幾秒內檔案檢查變成 `ACCEPTED`（`inspector` 容器透過 spool 處理）
- ✅ 上傳偽造 PDF（純文字改名 `.pdf`）回 400 被拒絕
- ✅ 預覽頁產生 `https://localhost:8443/p/...` 簽章連結，該連結回傳 `application/pdf`
- ✅ 講師逐題評分 → 統一發布成績 → 學員端有成績
- ✅ 管理員未綁 TOTP 時被導去綁定；用 `totp-code` 取得的 6 位數綁定成功後進得了管理後台
- ✅ 第 5-1 節的 `reset-demo` 流程跑完：102 個帳號、0 份提交、0 筆 TOTP 綁定，之後能正常登入
- ✅ `dc restart` 之後已發布的成績與上傳檔案都還在
- ✅ `dc up -d` 時 app 等 db `(healthy)` 才啟動
- ✅ 故意先停 db 再重啟 app：app 結束並被 Docker 自動重啟，db 恢復後網站自動回到 200（修正前會一直 502）
- ✅ 主機上只有 443／8443 有回應；`80`、`5432`、`8000`、`8001` 都連不到
- ✅ `inspector`：網路 `none`、根目錄唯讀、以 `aams` 使用者執行、環境變數裡沒有資料庫連線字串、解析不到 `db`
- ✅ `app` 以 uid `10001` 執行
- ✅ 第 2-4 節匯出 Caddy 根憑證（`dc cp`）、第 4 節備份指令都能用，備份的中文沒有亂碼
- ⬜ 瀏覽器實際操作：畫面正常、PDF 預覽框顯示正常（需先信任憑證或接受 `:8443` 警告）
- ⬜ 從另一台電腦以區網設定（`AAMS_DOMAIN`／`AAMS_BIND_IP` 改成區網 IP）連線
- ⬜ 第 3 節的 bind mount 做法

全部通過後，才把 image 推到 GHCR 並建立 GitHub Release（第 8 節）。

---

## 7. 紅隊測試注意事項

- 用獨立的測試主機與網段，不要放任何真實學員資料。
- 測試主機要和正式機、其他內網隔開（規格書 CTF 靶機隔離那段的要求同樣適用）。
- 回報問題時附上版本（`v1.0.0-rc.2`）、帳號角色、重現步驟，以及 `dc logs app` 的相關片段。
- 每一輪測完用第 5-1 節重置，確保下一輪從相同狀態開始。

---

## 8. 發布 release（驗證清單全部通過後）

```powershell
git checkout v1.0.0-rc.2
docker build -t ghcr.io/fan0723/aams:v1.0.0-rc.2 .
# 需要 GitHub Personal Access Token（權限 write:packages）
echo <TOKEN> | docker login ghcr.io -u fan0723 --password-stdin
docker push ghcr.io/fan0723/aams:v1.0.0-rc.2
```

接著在 GitHub 的 Releases 頁面，從 tag `v1.0.0-rc.2` 建立 release，勾選 **Set as a pre-release**，說明中附上 image 名稱。

推上 GHCR 後，測試機可以直接用同一份 image，不必各自 build（確保大家測的是同一份）。到時候要把 `docker-compose.yml` 的 `build: .` 改成 `image: ghcr.io/fan0723/aams:v1.0.0-rc.2`，這屬於下一個 rc 的修改。
