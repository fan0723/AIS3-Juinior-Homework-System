# AIS3 Junior 作業繳交系統（程式代號 AAMS）

依據 [AIS3_Assignment_Management_Implementation_Spec_v0.6.md](AIS3_Assignment_Management_Implementation_Spec_v0.6.md) 實作的資安營隊作業管理系統，介面為繁體中文。官方視覺素材不隨原始碼提供。

> **狀態：Prototype。** 規格明定這是「需求基線／待安全設計審查」，本實作同樣**不是安全認證**。正式上線前請完成規格 §12.2 No-Go 檢查、WireGuard 部署、HTTPS 與備份演練。

> [!CAUTION]
> **⚠️ 這個版本還沒做自動備份！**
> 系統目前**不會自動備份**資料庫與學員上傳的檔案，伺服器硬碟損壞或誤刪就會**永久遺失**所有作業與成績。
> - 現在只有**手動**指令：Windows 用 `.\.venv\Scripts\python manage.py backup`，macOS／Linux 用 `.venv/bin/python manage.py backup`（SQLite + 已上傳檔案打包到 `data/backups/`）。PostgreSQL 需另外用 `pg_dump`。
> - **正式收件前**一定要決定：備份頻率、備份存放位置（伺服器位置確定後再定）、誰負責，並**實際演練一次還原**。
> - 在自動備份完成前，營隊期間請至少每天（建議每次作業截止後）手動備份一次，並把備份檔複製到另一台機器。

## 本機視覺素材（不包含在 Git 儲存庫）

主辦方工作人員可從 **AIS3 Junior 內部素材庫**、活動網站維護／視覺設計窗口取得經核准使用的圖片，或使用自己已保存且有權使用的原始檔。活動網站 <https://ais3.org/junior> 可供核對視覺版本；網站上能看到圖片不代表任何人都取得再散布權限。請勿將官方圖片加入公開 Git 儲存庫。

將圖片以以下檔名放入 `aams/static/local/`（資料夾不存在時自行建立）：

| 檔案 | 用途 |
|---|---|
| `bg.jpg` | 頁面背景 |
| `logo.png` | 頁首、登入頁及網站圖示 |

Windows PowerShell 範例（把來源路徑換成你自己的檔案）：

```powershell
New-Item -ItemType Directory -Force aams/static/local | Out-Null
Copy-Item 'C:\path\to\bg.jpg' aams/static/local/bg.jpg
Copy-Item 'C:\path\to\logo.png' aams/static/local/logo.png
```

macOS／Linux 範例：

```bash
mkdir -p aams/static/local
cp /path/to/bg.jpg aams/static/local/bg.jpg
cp /path/to/logo.png aams/static/local/logo.png
```

此目錄已由 `.gitignore` 排除。缺少圖片時仍可啟動，背景會顯示預設暖橘色，介面以文字顯示系統名稱；兩張圖片可分別選用。新增或更換圖片後重新啟動服務。Docker 本機建置會將當時放在目錄內的圖片複製進映像；若要提供映像給其他人，請先確認圖片的散布授權，或在沒有圖片的環境重新建置。

## 授權

本儲存庫的程式碼以 [GNU General Public License v3.0（GPL-3.0-only）](LICENSE) 授權。AIS3 Junior 的官方圖片與標誌不包含在此授權內，也不隨 Git 儲存庫提供。

如需要圖片請聯絡 [ais3moe@gmail.com](mailto:ais3moe@gmail.com)。

## 文件

| 文件 | 內容 |
|---|---|
| [docs/BUILD_AND_USAGE.md](docs/BUILD_AND_USAGE.md) | 安裝、建置、啟動、各角色操作流程、管理指令、正式部署（Docker + PostgreSQL + Caddy HTTPS + WireGuard） |
| [docs/LAN_TESTING.md](docs/LAN_TESTING.md) | **不連外網**的內網測試：單機、同網段多台電腦、測試劇本與驗收清單 |
| [discord_setting_readme.md](discord_setting_readme.md) | Discord 頻道通知：會發哪些訊息、建立 Webhook、填入設定、測試與疑難排解 |

## 快速開始（Windows、macOS、Linux）

程式碼設計為可在 Windows、macOS、Linux 執行；正式部署的 Docker 映像使用 Linux。每次推送會透過 [GitHub Actions](.github/workflows/tests.yml) 在三種系統上執行測試。以下是在**各自的電腦上建立** Python 虛擬環境的指令；不要把另一個作業系統建立的 `.venv/` 複製過來。需要 Python 3.11 以上（Docker 使用 3.12）。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python manage.py init
.\.venv\Scripts\python manage.py seed-demo --active
.\.venv\Scripts\python run.py
```

### macOS／Linux（終端機）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python manage.py init
.venv/bin/python manage.py seed-demo --active
.venv/bin/python run.py
```

兩種環境都在專案根目錄執行。`.env`、`data/` 和 `.venv/` 是本機產生的內容，不會隨 Git clone 提供；首次啟動需要在新機器執行 `manage.py init`。完整步驟見 [建置與使用說明](docs/BUILD_AND_USAGE.md)。

瀏覽器開啟 <http://127.0.0.1:8000>，以 `s011@demo.ais3.local` / `Ais3-Demo-2026` 登入（測試帳號，**正式上線前必須刪除**，詳見下一節）。

---

## 🧪 測試帳號與各角色測試方法（僅限測試環境）

> [!CAUTION]
> **⚠️ 正式上線時務必刪除！**
> 本節所列的帳號、密碼都是 `manage.py seed-demo` 產生的**公開示範資料**，任何讀過這份 README 的人都知道密碼。
> - 正式環境**不可執行** `manage.py seed-demo`，也**不可沿用**測試用的資料庫與 `data/` 資料夾。
> - 正式環境**不可使用** `manage.py totp-code`（它會直接印出帳號的兩步驟驗證碼）。
> - 上線前請以管理員帳號到「使用者」頁搜尋 `demo.ais3.local`，確認**查無任何帳號**。
> - 正式上線前建議把本節從 README 刪除，或移到不對外公開的內部文件。

### 準備示範資料

```powershell
cd D:\AIS3J-homework-system
.\.venv\Scripts\python manage.py seed-demo --active   # 建立示範帳號（密碼直接可用）
.\.venv\Scripts\python run.py                         # 此視窗保持開啟
```

- 不加 `--active` 時，所有帳號使用初始密碼 `10007038907`，第一次登入會被要求改密碼（可用來測「初始密碼強制更換」流程）。
- 若出現「**示範資料已存在**」（例如資料是舊版產生的，缺少新的測試帳號），改用 `--renew`：

  ```powershell
  .\.venv\Scripts\python manage.py seed-demo --active --renew
  ```

  | `--renew` 會做的事 | `--renew` **不會**動的東西 |
  |---|---|
  | 補建缺少的示範帳號、課程、作業 | 非 `@demo.ais3.local` 的帳號（真實帳號完全不碰） |
  | 把所有示範帳號的密碼、狀態、角色重設回預設值，並登出這些帳號 | 已存在的作業設定、提交、分數、已發布成績 |
  | 被移到別組的示範學員回到原本的組別 | 管理員已綁定的兩步驟驗證 |

  可以重複執行。想要完全乾淨地重來，請用下方「重置測試環境」（刪除 `data` 資料夾）。
- 其他指令（例如取 TOTP 驗證碼）請另開一個 PowerShell 視窗執行。

### 測試帳號（⚠️ 正式上線前刪除）

密碼一律為 **`Ais3-Demo-2026`**（使用 `--active` 時）。

| 角色 | 帳號 | 數量 | 負責範圍／說明 |
|---|---|---|---|
| 學員 | `s{組別2碼}{1-6}@demo.ais3.local` | 72 | 12 組 × 6 人。例：`s011@demo.ais3.local`＝第 1 組第 1 位、`s126@…`＝第 12 組第 6 位 |
| 助教 | `ta{組別2碼}{1-2}@demo.ais3.local` | 24 | 每組 2 位。例：`ta011@…`、`ta012@…` 帶第 1 組 |
| 講師 1 | `instructor1@demo.ais3.local` | 1 | 課程「Web Security 入門」→ 作業「Lab 1：XSS 解題報告」（個人、PDF、20 MB） |
| 講師 2 | `instructor2@demo.ais3.local` | 1 | 課程「Reverse Engineering」→ 作業「Crackme 小組作業」（小組、PDF/PNG/JPG、最多 5 檔、40 MB） |
| 講師 3 | `instructor3@demo.ais3.local` | 1 | 課程「最終專題」→ 作業「最終專題報告」（小組、直接給總分、成績不對學員公開） |
| 管理員 | `staff1@demo.ais3.local` ~ `staff3@…` | 3 | 全部權限；**首次登入必須綁定兩步驟驗證（TOTP）** |

> **注意：** 示範資料**沒有預先建立批改任務**，助教剛登入時什麼都不能改。必須先由講師替助教建立任務（見下方步驟 2-5），這是規格要求的權限設計，不是故障。

### 同時登入多個角色的技巧

登入狀態依網址分開保存，可以這樣同時開多個角色：

- 一般視窗開 `http://127.0.0.1:8000` → 學員
- 無痕視窗開 `http://127.0.0.1:8000` → 講師
- 另一個瀏覽器，或改開 `http://localhost:8000` → 助教／管理員

### 步驟 0：每個帳號第一次登入都要同意個資告知

所有角色（學員、助教、講師、管理員）第一次登入時，會先看到「個人資料蒐集告知與同意」：要把內容**捲到底**才能勾選，**逐項勾選**後才能繼續（新帳號接著設定密碼）。之後可在右上角姓名 →「個人設定」→「查看個資告知」重看；頁尾也有「個資告知」連結。管理員可在使用者詳細頁看到每個人同意的時間與 IP。

> 告知內容目前是**預留文字**，請主辦單位修改 `aams/templates/_privacy_text.html`（不需要改程式）。

### 步驟 1：學員交作業（後面批改要用）

1. 以 `s011` 登入 → 進「Lab 1：XSS 解題報告」→ 上傳一個 PDF。
2. 等幾秒重新整理，狀態變成「已通過檢查」才能被批改。
3. 可順便測：超過容量會被拒絕、`.exe`／改副檔名的檔案會被拒絕、重交會產生新版本（只保留最近 5 版）、收據有 SHA-256。
4. 作業頁會顯示**伺服器時間**與**截止倒數**（剩 1 小時內變紅色）；允許遲交的作業還會顯示**最終收件期限**，超過後一律拒收。
5. 首頁與上方「公告」可以看到全營公告，課程頁看得到課程公告；有新公告時「公告」旁邊會出現數字。

### 步驟 2：講師功能（`instructor1`）

1. 上方進「工作台」（`/staff`），只會看到自己負責的課程。
2. **課程管理**：修改課程說明、上傳教材 PDF、切換教材公開／隱藏。
3. **作業設定**：編輯 Lab 1（截止時間、**最終收件期限**、**英文名稱**（下載檔名用）、容量 1–40 MB、允許檔案類型），或新增一份作業。最終收件期限預設為營隊結束日（`.env` 的 `AAMS_EVENT_END_AT`）+ 7 天；未設定結束日時為截止時間 + 7 天。只有講師與管理員能改，助教不行。
4. **題目與 Rubric**：新增／修改題目與配分。已有分數後配分與刪除會被鎖住。
5. **建立批改任務（重要）**：Lab 1 →「批改任務」（`/staff/assignments/1/tasks`）→ 選「第 1 組」、指派 `ta011`、勾選題目 → 建立。
6. **批改**：作業總覽 → 點 s011 的提交 → 預覽 PDF、逐題打分或點 Rubric 按鈕帶入、寫內部備註。
7. **統計**：查看繳交率與分數分布。
8. **課程公告**：課程頁 →「＋ 發布課程公告」（可置頂、可隱藏）。講師只能發自己課程的公告，全營公告只有管理員能發。
9. **遲交核准註記**：打開一份遲交的提交 →「遲交核准註記」填原因。LATE 標記**不會**消失，只是多一行「已核准」，批改頁與下載的 README 都看得到。
10. **成績申覆**：成績發布後，學員可在第一次發布後 3 天內針對某題或總分申覆；講師在「工作台 › 成績申覆」回覆（維持原分／已改分）。需要改分時先到提交頁改分，再重新發布。助教不能處理申覆。
11. **批次下載**：上方「批次下載」→ 選作業 → 勾選學員 → 可選「所有版本」與「強制統一命名」。ZIP 內每人一個資料夾（`Group01_姓名`），遲交加 `_LATE` 並附 `README.txt`，最外層有 `_SUMMARY.txt` 總表。
12. **相似度比對**：作業總覽 →「相似度比對」→（可上傳題目範本 PDF）→ 開始比對，列出文字重疊比例高的組合。只列可疑組合，不自動判定抄襲。
13. **應被擋下**：用 instructor1 開 `/staff/assignments/2`（Crackme，屬講師 2）→ 應顯示 404。

### 步驟 3：助教功能（`ta011`）

1. 登入後進「工作台」，看到被指派的任務。
2. 打開 s011 的提交，**只能改被指派的題目**。
3. **衝突測試**：講師與助教同時開同一份提交、改同一題 → 先存的成功，後存的看到「已被他人修改」提示。
4. **權限測試**：
   - 用 `ta021`（第 2 組助教）登入 → 打不開第 1 組的提交。
   - 講師撤銷任務後，ta011 重新整理 → 也打不開了。
   - 助教**不能**改作業設定（含最終收件期限）、不能發公告、不能處理成績申覆、不能看相似度比對。
5. **批次下載**：只能勾到自己帶的組與被指派批改的組。

### 步驟 4：發布成績（講師）

1. 所有提交打完分後，到 Lab 1「發布成績」頁。
2. 若有未改完或仍在檢查中的提交，頁面會列出原因並禁止發布。
3. 勾選確認 → 發布 → 切回 `s011` 查看成績與回饋。
4. 學員平均分數需**至少 5 人**有成績才顯示（排除 0 分），只改一份時看不到平均屬正常。
5. 發布後讓學員重交一版 → 已發布的成績不會被自動改動。

### 步驟 5：管理員功能（`staff1`）

**首次登入綁定兩步驟驗證**，二選一：

- **用手機**：以 Google Authenticator／Microsoft Authenticator 掃描畫面上的 QR code，輸入 6 位數。
- **不用手機（⚠️ 僅限測試，正式環境禁止使用）**：停在綁定頁，另開 PowerShell 執行：

  ```powershell
  .\.venv\Scripts\python manage.py totp-code staff1@demo.ais3.local
  ```

  將印出的 6 位數填入。同一組驗證碼不能重複使用，剛用過請等 30 秒換新的一組。

從「管理」選單（`/admin`）可測試：

| 頁面 | 測試項目 |
|---|---|
| 使用者 | 搜尋、詳細資料（電話只有管理員看得到）、改角色、停用帳號、重設密碼（顯示一次性密碼）、重設**他人**的兩步驟驗證（不能重設自己的） |
| CSV 匯入 | 上傳 → 預覽 → 確認；只新增、不覆蓋既有帳號（格式見 [BUILD_AND_USAGE.md](docs/BUILD_AND_USAGE.md)） |
| 分組 | 學員換組、替某組加入／移除助教 |
| 課程 | 新增課程、指派講師 |
| 進度矩陣 | 所有學員 × 所有作業的繳交狀況 |
| 公告 | 上方「公告」→ 發布**全營公告**（顯示在首頁，可置頂） |
| 匯出 | 成績 CSV、提交 CSV、使用者 CSV（勾選才含電話）；附件請用上方「批次下載」 |
| 稽核紀錄 | 確認登入、個資同意、改分、發布、申覆、下載都有紀錄 |
| 系統 | 目前狀態檢查（磁碟、檔案檢查佇列、伺服器錯誤、登入失敗）、最近的警示、**密碼錯誤鎖定的名單與解除**、Discord 頻道與截止提醒預設 |

> 登入密碼錯誤：同一帳號在同一個 IP 連續錯 5 次鎖 5 分鐘，解鎖後再錯 3 次鎖 15 分鐘（登入成功或 1 小時沒再錯就歸零）。學員急用時，管理員確認是本人後可在「系統 › 密碼錯誤鎖定」直接解除。

> 講師／管理員可在「工作台 › Discord 設定」登記可 @ 的身分組、預覽截止提醒；作業設定可自訂提醒時間（預設截止前 2、1 小時）。
> Discord 通知預設**關閉**。內網測試不需要設定；要試的話由管理員到「管理後台 › 系統 › Discord 頻道通知」貼上 Webhook 網址並輸入自己的密碼（網址加密存放，存入後只顯示「✓ 已設定」，更換時有警示並通知舊頻道）。截止提醒的系統預設（2、1 小時）也在同一頁修改。`.env` 有填 `AAMS_DISCORD_*`／`AAMS_REMINDER_HOURS` 時以 `.env` 為準、網頁鎖住。

### 重置測試環境

```powershell
# 先關閉 run.py（Ctrl+C）
.\.venv\Scripts\python manage.py reset-demo
.\.venv\Scripts\python run.py
```

`reset-demo` 會清空資料庫與上傳檔案、保留 `.env`（SECRET 不變），再重建 `seed-demo --active` 的示範資料。資料庫有非 `@demo.ais3.local` 帳號時會拒絕執行，確定要刪請加 `--force`；腳本自動化可加 `--yes` 跳過確認。

> [!NOTE]
> 不要用 `manage.py init --force` 來重置：它會更換 SECRET，已綁定的 TOTP 會永久解不開。重置後管理員要重新綁定 TOTP（刪除手機上的舊項目再掃新 QR，或用 `manage.py totp-code`）。

多台電腦一起測、壓力測試與完整驗收清單請見 [docs/LAN_TESTING.md](docs/LAN_TESTING.md)。

> [!CAUTION]
> **⚠️ 再次提醒：正式上線時務必刪除上述所有測試帳號與測試資料。**
> 正式環境請從**全新的資料庫**開始，只用 `manage.py create-admin` 建立真實管理員、以 CSV 匯入真實學員，並在上線前確認系統中查無任何 `@demo.ais3.local` 帳號。

## 技術架構

```text
瀏覽器 ──HTTPS(Caddy)──> FastAPI 主站 :8000（Jinja2 伺服器端渲染、繁中介面）
       └─HTTPS(Caddy)──> 預覽來源 :8001（獨立 origin，只認 5 分鐘簽章 token）
FastAPI ──> SQLite（測試）／PostgreSQL（正式）
        ──> data/quarantine → 檔案檢查 worker（無網路、無 DB 憑證）→ data/accepted
```

- Python 3.12、FastAPI、SQLAlchemy 2、Argon2id、TOTP（pyotp）、pypdf
- 測試：安裝 `requirements-dev.txt` 後執行 `python -m pytest -q`（先使用對應平台的虛擬環境 Python，例如 Windows 的 `.\.venv\Scripts\python` 或 macOS／Linux 的 `.venv/bin/python`；49 個測試涵蓋規格 §9.3 的主要安全案例與新功能）

## 規格對應與已知限制

已實作：Email 帳號／初始密碼強制更換、CSV insert-only 匯入（預覽→確認）、Admin TOTP、固定分組、課程／教材、個人與團體作業、多檔＋整次提交總容量（預設 20 MB、可調至 40 MB，十進位 MB）、副檔名白名單＋內容檢查＋隔離 worker、5 版保留、收據＋SHA-256、遲交標記（不扣分）、新版本通知、批改任務（作業×組別×題目）、逐題分數＋Rubric、人工最終分、團體組員個別調分、樂觀鎖 409、內部備註、單向回饋、整份作業統一發布（不可變快照）／republish、學生平均（排除 0 分、n<5 隱藏）、繳交率、工作人員統計、CSV（防公式注入）、稽核紀錄、備份與期末重置指令。

新增功能：個資告知與同意（所有角色首次登入）、公告欄（全營／課程、置頂、未讀數）、截止倒數與伺服器時間、最終收件期限（預設營隊結束日 + 7 天，超過一律拒收）、遲交核准註記（保留 LATE）、Discord 頻道通知（Webhook，staff／學員兩個頻道；截止前提醒預設 2、1 小時，可每份作業自訂；公告與提醒可 @ 指定身分組）、監控與警示（磁碟、佇列、錯誤、登入失敗）、成績申覆（第一次發布後 3 天內，講師處理）、批次下載（所有角色，依權限；每人一個資料夾、遲交標記與 README、可強制統一命名）、PDF 相似度比對（工作人員）、新欄位自動升級（舊資料庫啟動時自動補欄位）。

尚未實作或簡化（請在審查時注意）：

| 項目 | 現況 |
|---|---|
| 英文介面（i18n `en`） | 目前只有繁體中文；字串集中在模板中，可後續抽出 |
| 管理員「特別代交」 | 未實作（規格矩陣中的選配操作） |
| 匯出排程工作（export_jobs） | 批次下載以同步請求產生，設總量上限與同時產生數量上限（預設 2）；大量下載時請縮小範圍 |
| **自動備份** | **尚未實作**（見最上方警告），目前只有手動 `manage.py backup` |
| Discord 私訊 | 只做頻道訊息（Webhook）；不做個人私訊機器人 |
| 相似度比對 | 只比對 PDF 文字；截圖／掃描檔抽不到文字；只抓得到大段照抄，抓不到改寫 |
| MFA 救援碼 | 以「另一位管理員核身後重設 MFA」取代 |
| 頻率限制 | 單一行程記憶體內計數；多行程部署需改用 Redis 等共享儲存 |
| 不可變表 | 應用層阻擋 UPDATE/DELETE；PostgreSQL 可再加 trigger 或權限分離 |
| 防毒掃描（AV） | 規格標為可選，未整合 |
| DB migration | `create_all` + 啟動時自動補上新欄位（只會新增、不改名不刪除）；正式改版前應導入 Alembic |
| 災難復原（OPEN-OPS-001） | 提供 `manage.py backup`，頻率／異地備份仍待主辦方決定 |
