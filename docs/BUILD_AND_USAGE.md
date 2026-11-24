# AIS3 Junior 作業繳交系統 — 建置與使用說明書

本文件說明如何從原始碼建置、啟動、初始化資料，以及各角色的操作流程。內網測試請另見 [LAN_TESTING.md](LAN_TESTING.md)。

---

## 1. 系統需求

| 項目 | 測試／開發 | 正式部署 |
|---|---|---|
| 作業系統 | Windows 10/11、macOS、Linux | Linux（Ubuntu 22.04+ 建議） |
| Python | 3.12（3.11 以上） | 由 Docker 映像提供 |
| 資料庫 | SQLite（內建，免安裝） | PostgreSQL 16（docker compose 內建） |
| 反向代理 | 不需要 | Caddy 2（docker compose 內建，負責 HTTPS） |
| 網路 | 不需連外 | WireGuard 內網；只有建置時需下載套件／映像 |
| 硬碟 | 1 GB | 依規格 §10.2 估算：每份個人作業最壞 7.2 GB（20 MB 額度）／14.4 GB（40 MB），另加備份空間 |

不需要 GPU。

---

## 2. 專案結構

```text
aams/                 主程式
  main.py             ASGI app（主站 + 預覽來源）、安全 header、上傳大小限制 middleware
  config.py           設定（讀取 .env／環境變數）
  models.py           資料表（規格 §7）
  acl.py              物件層授權（規格 §3.3）
  services.py         提交 finalize、分數模型、發布、統計
  storage.py          私有儲存、檔案檢查 worker、5 版淘汰
  inspector.py        隔離檔案檢查程式（PDF/PNG/JPG/TXT）
  security.py         Argon2id、TOTP、簽章 token、限速、密碼政策
  routes/             auth / student / staff / grading / files / admin
  templates/          繁體中文頁面
  static/             CSS、JS；local/ 可放本機圖片（不進 Git）
manage.py             管理指令（init、seed-demo、create-admin、backup、reset-instance…）
run.py                同時啟動主站（:8000）與預覽來源（:8001）
tests/                自動化測試（安全案例＋全頁面爬測）
Dockerfile, docker-compose.yml, deploy/   正式部署
```

官方背景與 logo 的取得管道、檔名和無圖片時的預設外觀，請見 [README 的「本機視覺素材」](../README.md#本機視覺素材不包含在-git-儲存庫)。從 Git clone 後不需要這兩張圖片也能啟動；如需在 Docker 映像中使用，請先放入 `aams/static/local/` 再 build。

---

## 3. 本機建置（Windows PowerShell）

> 以下指令在專案根目錄 `D:\AIS3J-homework-system` 執行。macOS／Linux 把 `.\.venv\Scripts\python` 換成 `.venv/bin/python`。

### 3.1 建立虛擬環境並安裝套件

```powershell
python --version                      # 需 3.11 以上
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
```

- `requirements.lock.txt`：完整鎖定版本（建議，符合 SEC-019 供應鏈要求）
- `requirements.txt`：直接相依套件
- `requirements-dev.txt`：額外加上 pytest／httpx

> **完全離線的機器**：在可連網的機器執行 `pip download -r requirements.lock.txt -d wheels`，把 `wheels/` 複製過去後執行 `pip install --no-index --find-links wheels -r requirements.lock.txt`。

### 3.2 產生設定檔

```powershell
.\.venv\Scripts\python manage.py init          # 只給本機使用（127.0.0.1）
# 或
.\.venv\Scripts\python manage.py init --lan    # 讓同網段其他電腦連線（0.0.0.0）
```

會產生 `.env`（含隨機 `AAMS_SECRET_KEY`，**不可提交到 Git**）。主要設定：

| 變數 | 預設 | 說明 |
|---|---|---|
| `AAMS_SECRET_KEY` | 隨機 | Session／TOTP 加密／預覽 token 簽章金鑰。更換會讓所有 Session 與 MFA 綁定失效 |
| `AAMS_HOST` | `127.0.0.1` | `0.0.0.0` = 接受其他電腦連線 |
| `AAMS_PORT` / `AAMS_PREVIEW_PORT` | 8000 / 8001 | 主站與隔離預覽來源 |
| `AAMS_PREVIEW_URL` | 空 | 空白時自動用「使用者連線的主機名稱 + 8001」 |
| `AAMS_COOKIE_SECURE` | `false` | **正式 HTTPS 環境必須 `true`** |
| `AAMS_DATABASE_URL` | SQLite `data/aams.db` | PostgreSQL：`postgresql+psycopg://user:pw@host:5432/aams` |
| `AAMS_SYSTEM_ALLOWED_TYPES` | `pdf,png,jpg,txt` | 系統層允許類型上限，講師只能在此範圍內勾選 |
| `AAMS_EVENT_NAME` | `AIS3 Junior 2026` | 顯示於頁首 |
| `AAMS_EVENT_END_AT` | 空 | 營隊結束日（台北時間，例 `2026-07-10`；只寫日期視為當天 23:59）。作業「最終收件期限」預設 = 此日 + 7 天；未設定時改用「截止時間 + 7 天」 |
| `AAMS_DATA_DIR` | `./data` | 資料庫、上傳檔、備份目錄 |
| `AAMS_DISCORD_STAFF_WEBHOOK` | 空 | Discord **staff 頻道** Webhook：監控警示、新的成績申覆。**建議留空，改由管理員在「管理後台 › 系統」設定**（加密存放）；填了就以 .env 為準、網頁鎖住 |
| `AAMS_DISCORD_STUDENT_WEBHOOK` | 空 | Discord **學員公告頻道** Webhook：新公告、成績發布、截止提醒（不含分數或個資）。規則同上 |
| `AAMS_REMINDER_HOURS` | 空（= 網頁設定，未設定時 `2,1`） | 截止前提醒的系統預設（小時，逗號分隔）。建議留空，由管理員在「管理後台 › 系統」修改（立即生效）；填了就鎖住網頁設定。詳見 `discord_setting_readme.md` |
| `AAMS_ALERT_DISK_FREE_PCT` | 15 | 磁碟剩餘低於此百分比時警示 |
| `AAMS_ALERT_QUEUE_MINUTES` | 5 | 檔案檢查佇列有檔案等待超過此分鐘數時警示 |
| `AAMS_ALERT_ERRORS_5MIN` | 10 | 5 分鐘內伺服器錯誤達此次數時警示 |
| `AAMS_ALERT_LOGIN_FAIL_IP_5MIN` / `AAMS_ALERT_LOGIN_FAIL_ACCOUNT_5MIN` | 20 / 10 | 5 分鐘內同一 IP／同一帳號登入失敗達此次數時警示 |
| `AAMS_ALERT_REPEAT_MINUTES` | 30 | 同一種警示在此分鐘數內只發一次 |
| `AAMS_DOWNLOAD_MAX_CONCURRENT` | 2 | 批次下載同時產生 ZIP 的數量上限（超過時請使用者稍後再試） |

> **Discord Webhook 怎麼拿：** Discord 頻道 → 編輯頻道 → 整合 → Webhook → 新增 → 複製網址（`https://discord.com/api/webhooks/…`）。設定後重新啟動，到「管理後台 › 系統」按「送出測試訊息」確認。伺服器需能對外連到 `discord.com`（防火牆只開這個目的地即可）。內網測試請保持空白。

### 3.3 初始化資料（擇一）

**A. 示範資料（測試用）**

```powershell
.\.venv\Scripts\python manage.py seed-demo            # 帳號為「待改密碼」，初始密碼 10007038907
.\.venv\Scripts\python manage.py seed-demo --active   # 帳號已啟用，密碼 Ais3-Demo-2026（測試較快）
```

建立 102 個帳號（72 學員／24 助教／3 講師／3 工作人員）、G01–G12、3 門課程與 3 份作業：

| 帳號 | 說明 |
|---|---|
| `s011@demo.ais3.local` … `s126@demo.ais3.local` | 學員；`s` + 組別 2 碼 + 序號 1–6（`s051` = 第 5 組第 1 位） |
| `ta011@` / `ta012@` … `ta122@demo.ais3.local` | 助教；`ta` + 組別 + 1/2，帶領該組 |
| `instructor1@` / `2@` / `3@demo.ais3.local` | 講師，依序負責「Web Security 入門」「Reverse Engineering」「最終專題」 |
| `staff1@` / `2@` / `3@demo.ais3.local` | 管理員（首次登入須綁定 TOTP） |

**B. 正式資料**

```powershell
.\.venv\Scripts\python manage.py initdb
.\.venv\Scripts\python manage.py create-admin --email admin@example.org --name "主辦單位"
```

之後登入網頁，到「管理後台 › 分組」建立 G01–G12，再用「CSV 批次匯入」建立學員／助教／講師。

### 3.4 啟動

```powershell
.\.venv\Scripts\python run.py
```

畫面會顯示：

```text
  AAMS 主站     : http://127.0.0.1:8000
  預覽來源       : http://127.0.0.1:8001  (只服務短效簽章連結)
```

`Ctrl + C` 停止。檔案檢查 worker 會隨主站一起在背景執行。

### 3.5 執行自動化測試

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

測試使用暫存資料夾與獨立資料庫，不會動到 `data/`，也不會連到 Discord（測試中攔截）。共 45 個測試，涵蓋：個資告知同意（含所有角色）、公告權限與未讀、最終收件期限預設與拒收、遲交核准註記、成績申覆（期限、重複、助教不能處理）、批次下載（資料夾／README／強制命名／權限）、相似度比對（演算法與端到端）、Discord payload 與 Webhook 驗證、監控警示與節流、舊資料庫自動補欄位、初始密碼流程、登入防枚舉、Admin MFA、電話欄位只給 Admin、20 MB／40 MB 整次總量（12+9 MB 拒絕、18+17 MB 接受、41 MB 拒絕、偽造 Content-Length）、`.php4`／`.pdf.php`／`.PHP`／偽造 PDF／HTML 拒絕、含 JavaScript 的 PDF 被 worker 拒絕、5 版淘汰與 LATE 判定、跨組讀檔拒絕、TA 需任務才能改分（含撤銷）、同題 409、統一發布與發布後補交、CSV 公式注入、XSS、預覽 token 偽造，以及所有角色的全頁面爬測。

---

## 4. 使用流程

### 4.1 首次登入（所有人）

1. 以**報名 Email** + 初始密碼登入。初始密碼格式：民國年 3 碼 + 月 2 碼 + 日 2 碼 + 身分證末 4 碼（例：民國 100 年 7 月 3 日、末四碼 8907 → `10007038907`）。
2. **個資告知與同意**（所有角色都要）：把告知內容捲到底，逐項勾選同意後才能繼續。之後可在「個人設定 › 查看個資告知」或頁尾連結重看。告知文字在 `aams/templates/_privacy_text.html`，目前是**預留文字**，請主辦單位改成正式版本。
3. 系統強制導向「設定新密碼」：至少 10 字元、不可只有數字、不可與初始密碼相同。
4. 完成後初始密碼立即失效，需以新密碼重新登入。
5. **管理員**首次登入後會被要求以驗證器 App（Google／Microsoft Authenticator）掃描 QR Code 綁定 TOTP；之後每次登入都要輸入 6 位數驗證碼。講師／助教可在「個人設定」自行啟用。

### 4.2 管理員（工作人員）

| 工作 | 位置 |
|---|---|
| 建立 G01–G12 | 管理後台 › 分組 › 建立組別 |
| 批次建帳 | 管理後台 › CSV 批次匯入：上傳 → 預覽（新增／重複／錯誤）→ 確認匯入。**只新增，不覆寫既有帳號**；預覽 10 分鐘內有效，只能確認一次 |
| 逐人建帳 | 管理後台 › 帳號管理 › 新增單一使用者（課程進行中建議用這個） |
| 指派帶組助教 | 管理後台 › 分組 › 每組「新增助教」 |
| 建立課程、指派講師、發布課程 | 管理後台 › 課程 |
| 忘記密碼 | 帳號詳細頁 › 填寫核身紀錄 › 產生臨時密碼（只顯示一次，當面交付；舊 Session 全部失效） |
| 遺失 MFA 裝置 | 由**另一位**管理員在帳號詳細頁「重設 MFA」 |
| 停用帳號 | 帳號詳細頁 › 停用 |
| 學員進度 | 管理後台 › 學員進度（跨課程矩陣） |
| 匯出 | 管理後台 › 匯出：成績 CSV、繳交紀錄 CSV、帳號 CSV（電話需明確勾選）；附件用上方「批次下載」 |
| 全營公告 | 上方「公告」›「＋ 發布公告」› 範圍選「全營公告」（顯示在首頁，可置頂；設定了學員 Discord 頻道會推送一次） |
| 稽核 | 管理後台 › 稽核紀錄（登入、個資同意、改分、發布、申覆、下載、設定變更） |
| 遲交重新分類 | 提交檢視頁 › 「管理員：重新分類遲交狀態」（需原因） |
| 系統監控 | 管理後台 › 系統：目前狀態檢查、最近警示、Discord 設定與測試訊息（**本版尚無自動備份**） |
| 課程英文名稱 | 管理後台 › 課程：每門課的「英文名稱」（批次下載檔名用） |

CSV 格式（UTF-8，欄位順序固定）：

```csv
email,name,role,school,grade,group_code,initial_password,phone
student@example.org,王小明,STUDENT,範例高中,10,G01,10007038907,0912345678
ta@example.org,李助教,TA,範例大學,,G01,10101018888,
```

> 用 Excel 編輯時請把 `initial_password` 欄設為「文字」，否則前導零會遺失。含明文密碼的 CSV 匯入後請立即刪除，不要放進共用雲端或 Git。

### 4.3 講師

1. 「工作台」→ 自己負責的課程 →「管理此課程」：編輯課程說明、上傳 PDF 教材（可隱藏／公開）。
2. 「＋ 新增作業」：標題、**英文名稱**（下載檔名用，只能英數字、底線、連字號）、說明、個人／團體、截止時間（台北時間）、收件開關、是否允許遲交、**最終收件期限**（預設營隊結束日 + 7 天；超過一律拒收；不可早於截止時間；只有講師與管理員能改）、允許檔案類型、是否多檔與每次檔案數、**單次提交總容量（1–40 MB，預設 20）**、評分模式、學員端成績是否顯示、是否公開平均、狀態（草稿／發布）。
3. 「題目／Rubric」：新增題目與滿分、每題的快捷評分按鈕（標籤、分數、預設回饋）。
4. 「批改任務」：選助教 × 組別（可多選）× 題目（可多選），可另外授予總分權限；可隨時撤銷。
5. 批改：作業總覽 → 某組／某學員「開啟」→ 左側版本與檔案預覽，右側逐題分數（Rubric 一鍵填分）、評語、人工最終分數（需原因）、整體回饋；團體作業可對每位組員個別調分。
6. 「統一發布成績」：系統列出阻擋項目（檔案檢查中、未完成評分）。全部清除後勾選確認即發布。之後若改分或收到補交，需填原因「重新發布」。
7. 「統計」：繳交數、準時／遲交、各題得分率、分數分布、重交次數、批改負擔。
8. **課程公告**：課程頁 →「＋ 發布課程公告」（可置頂／隱藏）。只能發自己課程的公告。
9. **遲交核准註記**：開啟遲交的提交 →「遲交核准註記」填原因。LATE 標記保留，只多記錄「已核准」，批改頁與下載 README 都看得到，並寫入稽核紀錄。
10. **成績申覆**：「工作台 › 成績申覆」列出待處理的申覆（學員在第一次發布後 3 天內提出）。要改分時先到提交頁改分（會留下改分紀錄），再回來選「已改分」並回覆學員，最後到「統一發布成績」重新發布。助教不能處理申覆。
11. **批次下載**：上方「批次下載」→ 選作業 → 勾選學員／組別 → 選「只要最新版」或「所有版本」，可勾「強制統一命名」（`Group01_姓名_課程英文名_作業英文名[_LATE].pdf`）。ZIP 內每人一個資料夾，遲交資料夾加 `_LATE`，有狀況時附 `README.txt`（遲交多久、核准註記、未通過檢查或已淘汰的版本），最外層有 `_SUMMARY.txt` 與 `MANIFEST.csv`。
12. **相似度比對**：作業總覽 →「相似度比對」→（可上傳題目範本 PDF 排除題目文字）→ 開始比對。在背景執行，完成後列出文字重疊比例高的組合與重疊段落，**只列可疑組合、不判定抄襲**。截圖／掃描 PDF 會標示「無法比對」。

### 4.4 助教

- 「工作台 › 我帶領的組別」：可看本組繳交狀態與檔案預覽，**但沒有批改任務時不能改分，也看不到未發布分數與內部備註**。
- 「我的批改任務」：只能修改被指派的題目；可新增內部備註（學生永遠看不到）。
- 有新版本提交時，通知中心與工作台會出現紅色「新」標記；批改頁面若不是最新版會顯示紅色警示。
- 「批次下載」只能勾選自己帶的組與被指派批改的組。
- 助教**不能**修改作業設定（含最終收件期限）、發布公告、處理成績申覆或使用相似度比對。

### 4.5 學員

1. 首頁顯示全營公告、所有已發布課程與「我的作業」截止時間、倒數與狀態。上方「公告」有新公告時會顯示數字。
2. 課程頁：課程公告、課程說明、投影片預覽／下載、作業列表。
3. 作業頁：選擇檔案（畫面即時顯示合計大小與剩餘額度）→ 送出 → 取得**收據**（submission_id、版本、收件時間、每個檔案 SHA-256、ON_TIME/LATE、檢查狀態），可一鍵複製。
4. 可無限次重交，系統保留最近 5 版檔案。團體作業任一組員都能代全組提交，所有組員看到同一份紀錄與每版提交者。
5. 作業頁顯示**伺服器時間**與截止倒數（以伺服器時間為準，剩 1 小時內變紅）。截止後（若允許遲交）或成績已發布後提交，必須勾選「我了解這次提交屬於補交」，版本會標記紅色 LATE，不會自動扣分。超過**最終收件期限**後一律拒收。
6. 成績在講師統一發布後才會出現（含各題分數與評語）；最終專題可能設定為不公開。
7. **成績申覆**：成績第一次發布後 3 天內，可在作業頁針對某題或總分提出申覆，講師回覆後會顯示在同一頁。
8. **批次下載**：上方「批次下載」可一次下載自己（或自己小組）交過的檔案。

---

## 5. 管理指令（manage.py）

| 指令 | 用途 |
|---|---|
| `manage.py init [--lan] [--force]` | 產生 `.env` |
| `manage.py initdb` | 建立資料表 |
| `manage.py create-admin [--email ..] [--name ..]` | 建立管理員（互動輸入密碼） |
| `manage.py seed-demo [--active] [--renew]` | 示範資料；`--renew`：已存在時補齊缺少的示範帳號／課程／作業，並重設示範帳號密碼、狀態、角色（不動真實帳號與既有提交、分數） |
| `manage.py totp-code EMAIL` | **僅限測試**：印出該帳號目前的 TOTP 碼（免手機） |
| `manage.py backup` | SQLite 線上一致性備份 + 已通過檢查的附件與教材 → `data/backups/aams-時間.tar.gz` |
| `manage.py reset-instance` | 期末重置：需**兩位不同管理員**輸入帳密 + 確認字串；不會刪除 `data/backups/` |

還原（SQLite）：停止服務 → 解壓備份 → 將 `aams.db` 放回 `data/aams.db`、`accepted/`、`materials/` 放回 `data/` → 啟動。請定期演練並把備份複製到另一台機器。

---

## 6. 正式部署（Docker + PostgreSQL + Caddy + WireGuard）

> 此部分的 compose 檔已通過 `docker compose config` 語法驗證，但在開發機上**尚未實際 build 與啟動**（Docker daemon 未啟動），首次部署請依下列步驟逐項確認。

### 6.1 架構

```text
學員/TA/講師 ──WireGuard(peer 各自金鑰)──> 伺服器 wg0: 10.66.0.1
   └─ https://aams.ais3.lan        (443)  → Caddy → app:8000（主站）
   └─ https://aams.ais3.lan:8443   (8443) → Caddy → app:8001（隔離預覽來源）
app ──(internal network)──> PostgreSQL（不開 port）
app ──(spool volume)──> inspector 容器（network_mode: none、唯讀、cap_drop ALL、記憶體/CPU/PID 限制）
CTF 靶機：放在不同 VM／VLAN，不可共用 Docker host、volume、憑證
```

### 6.2 步驟

```bash
# 1. 在伺服器上（需先安裝 Docker Engine + compose plugin、WireGuard）
git clone <repo> aams && cd aams
cp deploy/env.example .env.docker
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # 填入 AAMS_SECRET_KEY
python3 -c "import secrets;print(secrets.token_urlsafe(24))"   # 填入 POSTGRES_PASSWORD
vi .env.docker   # AAMS_DOMAIN、AAMS_BIND_IP（WireGuard 介面 IP）

# 2. 啟動
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps

# 3. 建立第一位管理員
docker compose --env-file .env.docker exec app python manage.py create-admin

# 4. 取得 Caddy 內部 CA 根憑證，安全發給使用者匯入「受信任的根憑證」
docker compose --env-file .env.docker exec caddy cat /data/caddy/pki/authorities/local/root.crt > aams-root-ca.crt
```

DNS：在 WireGuard 用戶端設定 `DNS =` 指向內部 DNS，或於使用者 hosts 檔加入 `10.66.0.1 aams.ais3.lan`。

若有公有網域，可改用 DNS-01 取得公開可信憑證（不需對外開放服務），參考 Caddy 文件：<https://caddyserver.com/docs/automatic-https>。

### 6.3 WireGuard 與防火牆重點（規格 §10.1、SEC-014~016）

- 每位使用者／每台裝置各自一組 peer 金鑰，離營即從 `wg0.conf` 移除並 `wg syncconf`。
- 伺服器防火牆只允許 `wg0` 網段連 443/8443，**封鎖 peer 之間互連**與其他 port（例如 `iptables -A FORWARD -i wg0 -o wg0 -j DROP`）。
- PostgreSQL 不對外開 port（compose 已使用 internal network）。
- 不要把 AAMS 與刻意脆弱的靶機放在同一個 Docker host／bridge。

### 6.4 PostgreSQL 備份

```bash
docker compose --env-file .env.docker exec -T db pg_dump -U aams -Fc aams > aams-$(date +%F).dump
docker run --rm -v aams_appdata:/data -v "$PWD":/out alpine tar czf /out/aams-files-$(date +%F).tgz -C /data accepted materials
```

還原：`pg_restore -U aams -d aams --clean <dump>`，並把檔案解回 `appdata` volume。備份頻率、異地保存、RPO/RTO 仍是規格 OPEN-OPS-001，正式收件前至少要做一次還原演練。

---

## 7. 上線前檢查清單（規格 §12.2 No-Go 摘要）

- [ ] 全程 HTTPS，`AAMS_COOKIE_SECURE=true`，瀏覽器信任鏈正確
- [ ] 未連 WireGuard 時無法連到 AAMS；連上後 peer 無法連到 DB、管理介面、其他 peer
- [ ] 所有管理員已綁定 TOTP；`manage.py totp-code` 只在測試環境使用
- [ ] 以真實名單匯入後，測試帳號（`@demo.ais3.local`）已不存在（重新部署或 reset-instance）
- [ ] `pytest` 全數通過；已在目標硬體做 72 人同時交件壓測（見 LAN_TESTING.md §5）
- [ ] **（本版尚無自動備份）** 已決定備份頻率、存放位置與負責人；已完成一次「備份 → 還原」演練，備份存放在另一台機器
- [ ] 主辦方已書面接受「初始密碼可被知情者搶先啟用」的風險（規格 §2.2）
