# AIS3 Assignment Management System (AAMS)
## Implementation Specification — v0.6（單次提交總容量上限定稿版）

- 文件日期：2026-11-20
- 規格狀態：**Prototype 需求基線／待安全設計審查**；不是可直接上線的安全認證
- 前一版本：v0.5；本版依新確認的「整次提交總容量」需求修改，若內容衝突以本版為準
- 產品定位：可自行部署、外觀可替換、中英雙語的資安教育營隊作業管理系統
- 首批部署：AIS3 Junior，72 名學生（12 組 × 6 人）、24 名 TA、3 名講師、3 名工作人員，合計約 102 個帳號
- 部署原則：**每個活動獨立一套 Instance**，活動內有多門課；不可跨活動共用學生個資或資料庫
- 標記：`MUST` 必須實作；`SHOULD` 建議；`LATER` 後續擴充；`OPEN` 待決定／尚未完成
- 規格風格：每項業務規則對應後端驗證、資料庫約束與安全測試；**前端顯示限制不構成安全措施**

> **重大需求更新摘要：** 登入名稱改為報名 Email、初始密碼編碼明確；CSV 僅新增、課程可提供投影片；分組固定，團體作業由任一人代全組提交；每份作業允許多檔案／限副檔名／**整次提交所有檔案合計預設 20 MB，可由講師或 Admin 手動調整至 40 MB**；無限次重新提交但只保留最近五個實體版本；截止後允許的上傳只標記遲交、不自動扣分；後端支援逐題分數＋獨立人工最終分數；刪除對學生雙向留言／申訴；全份作業統一發布，發布後重交不得自動改動既有發布快照；學生統計只在發布後提供，排除 0 分，且可關閉公開；允許選人批次打包附件；WireGuard-only 內網自架，資料保留三個月，詳細災難復原尚待決定。

---

# 1. 範圍、角色與高階架構

## 1.1 範圍內（MVP）

1. 由管理員匯入 CSV 批次建帳或逐人建帳，並強制首次改密碼。
2. 固定分組（每組兩名 TA）及角色、課程範圍、任務／題目層級授權。
3. 活動內的課程列表、投影片下載、每門課的作業清單。
4. 個人及團體作業；受限檔案上傳、網頁 PDF 預覽、五個可用版本、收據、遲交標記與重交通知。
5. 講師／管理員出題、分派批改、設定 Rubric；TA 只評被指派的題目。
6. 每題給分、逐題加總、單獨人工最終分數與團體作業個別調整。
7. 內部批改備註、學生單向正式評語、整份作業統一發布。
8. 統計後台、學生可選公開平均、CSV 匯出、授權選人打包附件。
9. 管理員個資控管、審計、基礎 MFA、雙語與可替換 UI 主題。
10. WireGuard 限定入口、TLS、應用隔離及上傳／預覽沙箱。

## 1.2 不在 MVP 範圍

- 自行註冊、Google OAuth／SSO、跨活動帳號共用；預設不寄自動登入／重設信件。
- 即時聊天、帶組討論區、學生雙向作業留言、成績申訴流程（學生在線下找助教）。
- 自動執行學生的程式碼、Judge、AI 批改、PDF 逐行批註。
- 依遲交時長計算自動扣分、最低 60 分保底演算法；由批改者以最終分數欄位人工處理。
- 單一作業中 TA 與講師評分加權合成（保留 `LATER`）。
- 手機特製版頁面；行動裝置顯示桌面版，不保證手機操作友善。

## 1.3 技術建議

```text
學生／TA／講師／工作人員
        │ WireGuard VPN（peer-specific 訪問權）
        ▼
  VPN ACL / 防火牆 ──> HTTPS / Caddy
                            │
                   React + TypeScript Frontend
                            │
                   FastAPI Modular Monolith
                      │         │          │
               PostgreSQL   Private FS   Job Queue
                      │         │          │
                      │      Quarantine → File-inspection worker
                      │                  (無網路、資源限制)
                      └──── Audit / Export / Cleanup

[不同 VM／隔離網段]：CTF 練習靶機（不可與 AAMS 共用檔案、DB、Docker socket）
```

- 可先使用 React + Vite（或 Next.js）、FastAPI、PostgreSQL、Caddy、背景工作者；Redis 可依需要引入，不是為了小規模負載強制採購的服務。
- PostgreSQL 的 relational constraints、transaction 與 optimistic locking 比持久化 JSON 適合這個場景；JSON 僅用於受限的暫存設定或匯入資料。
- GPU 不是本系統的必要設備。i7-12700／64 GB RAM 的候選主機原則上足以供小型原型評估，仍須以 SSD 容量、磁碟 I/O、掃描負載、隔離方式及同時交件壓測決定能否上線；GX10 主機規格另行實測，不預設一定適合。

---

# 2. 身分與帳號（AUTH）

## 2.1 帳號與 CSV 欄位

**登入識別碼：學生報名時使用的 Email，亦即 `username`。** 每個活動的 Email 必須唯一，大小寫等價處理及去空白規則需在匯入預覽中確認。**不可做 Gmail 專屬的句點忽略或 `+tag` 移除**，避免錯把不同地址合併。原始 Email 供通知／展示；索引使用 canonicalized email（明定正規化方式）。不做 Email 真實所有權的隱含保證。

```csv
email,name,role,school,grade,group_code,initial_password,phone
student@example.org,Example Student,STUDENT,Example High School,10,G01,<SECRET>,0912xxxxxx
assistant@example.org,Example TA,TA,Example University,,,<SECRET>,
lecturer@example.org,Example Instructor,INSTRUCTOR,Example Univ.,,,<SECRET>,
admin@example.org,Example Staff,ADMIN,Example Organization,,,<SECRET>,
```

- `discord_id` 不需要 CSV 強制提供：使用者在個人設定中自行編輯；必須防 Stored XSS、長度濫用及不合理字元。
- `school`、`grade` 可由授權的帶組助教／講師依使用目的查看（應做欄位級授權）；`phone` **只有 `ADMIN` 可讀取或匯出**，其他角色的 API 回應不可包含此欄位，不只在 UI 隱藏。
- 支援使用者多角色（`user_roles` 關聯表），常態仍是一人一個主要角色；**管理員的最高權限只在本活動 instance 內有效**。
- 同一活動所有學生可看到全部已發布課程與其公開投影片、作業公告，**不代表可查看其他學生的私人提交**。講師只管理被指派的課程；只有 `ADMIN` 跨課程管理。

## 2.2 初始密碼編碼及啟用

- 初始密碼：**民國年 3 位 + 月 2 位 + 日 2 位 + 身分證末 4 碼**。
- 正式測試向量：民國 `100` 年 `07` 月 `03` 日、末四碼 `8907` → `10007038907`（11 位數字字串）。
- 不得以整數型別處理密碼（避免前導零損失），也不能把生日／身分證明文欄位存入 AAMS。
- 主辦單位於受控離線流程組裝含初始密碼的匯入 CSV，HTTPS 傳送至後端；Server 接收後逐筆 Argon2id hash，**不得把含明文密碼的 CSV 放入長期儲存、log、錯誤報告、備份與版控**。
- **初始帳號狀態 `PENDING_PASSWORD_CHANGE`，初始密碼只允許換密碼；成功後狀態轉為 `ACTIVE`，初始密碼立即失效、所有受限登入 Session 撤銷。**
- 風險接受：該初始密碼可預測，強制改密碼無法防止知情者搶先啟用。必須保留管理員核身重設、逐帳號與來源限速、防枚舉、異常啟用稽核，並讓營隊主辦者於上線前確認風險；不是「已安全消除冒領風險」。
- 所有授權皆檢查 `status=ACTIVE` 且具正常 Session；受限 Session 只能呼叫 onboarding/logout/change-initial-password 等白名單 API；使用者不能利用修改 Cookie／JWT claim 升級帳號。
- 新密碼須具足夠長度、阻擋常見密碼、不得與初始密碼相同；管理員重設後再走 `PENDING_PASSWORD_CHANGE`，同時作廢既有 Session；後端 password hash 只存 Argon2id。

## 2.3 CSV 匯入／人工建帳

- `AUTH-IMPORT-001 MUST`：匯入操作限定 `ADMIN` + MFA；**僅新增（insert-only）**，遇重複 Email 產生預覽錯誤／略過，**不得更新已存在使用者的密碼、角色、組別或個資**。
- `AUTH-IMPORT-002 MUST`：先 preview（顯示總筆數、新增筆數、重複、非法角色、缺欄），管理員確認後 commit；不可由惡意 CSV 升權到 `ADMIN`，只有已通過 MFA 的管理員可指定合法角色。
- `AUTH-IMPORT-003 MUST`：匯入前檢查 CSV 大小／列數／型態／每個欄位長度／重複值；transaction atomicity 和 idempotency 防重送。
- `AUTH-IMPORT-004 MUST`：介面明確告知：**課程進行中請盡量逐筆新增帳號，避免重新批次匯入**。
- `AUTH-IMPORT-005 MUST`：另提供受限的管理員手動新增單一使用者、調整非秘密欄位及停用功能，每次留下 audit。

## 2.4 忘記密碼與 Gmail 可選方案

**MVP 決策：人工核身的管理員重設。** 學生聯繫工作人員 → 工作人員確認身分 → 後台產生一次性／短效重設憑證或受限初始密碼 → 撤銷舊 Session → 學員完成強制改密碼；管理員絕不查看原密碼。

**`LATER`：Gmail 寄送單次重設連結／驗證碼**。技術上可行，不需要讓學生用 Google 登入，但需維護寄送帳號、SMTP 或 Gmail API 憑證、寄件配額、寄信失敗重試、重設 token（只存雜湊、單次使用、短期失效）、防寄信濫用及 Email 帳號所有權風險。個人 Gmail 配額和限制可能變更，不依賴它作為核心帳號可用性條件。不得因已經有報名 Email 就直接假定它已通過信箱驗證。

## 2.5 MFA／Session

- `ADMIN`：MUST 啟用 TOTP MFA，並提供安全的遺失裝置／救援碼程序；講師、TA 建議支援 TOTP，是否在正式活動全面強制為操作政策 `OPEN`。
- 使用 server-side opaque session ID；Cookie `Secure`、`HttpOnly`、適當 `SameSite`、CSRF token；敏感操作驗證 Session 和角色、課程、資源作用域。
- 受限／一般 Session 分開，密碼重設與角色重大異動撤銷相關 Session；登出和異常登入事件稽核；限速與鎖定不得讓惡意學生低成本大量封鎖同學帳號。

---

# 3. 課程、分組、投影片與權限（COURSE／GROUP／ACL）

## 3.1 固定分組

- 活動開始前由管理員建立編號 `G01` … `G12`（顯示「第 1 組」…「第 12 組」）；每組原則上 6 名學生、2 名 TA。**不需要額外組名**。
- 原則上營隊中不換組、不換帶組助教；後台仍保留 `ADMIN` 修正資料的能力及稽核，而不是以此支援常態換組流程。
- 小組關係不等於評分權限。
- 個人作業按學生建立邏輯 Submission；團體作業按組別建立邏輯 Submission，最後專題可能是主要團隊作業。

## 3.2 課程頁面

- 學生登入首頁看到所有已發布課程，進入課程後看到課程說明、投影片／教材下載、課程作業卡片（各自截止、遲交、繳交、成績發布狀態）。
- 講師可以在**自己被指派的課程**上傳教材（預設 PDF，其他檔案須經管理員允許，均不得在伺服器上執行）；只有 `ADMIN` 跨課程管理教材／作業。
- 投影片檔與學生不可信任提交檔要分不同 storage class、授權規則與上傳管線，避免誤將學生檔案當官方教材發布。

## 3.3 正式權限矩陣

| 資源操作 | 學生 | 本組 TA 未指派 | 收到有效任務的 TA | 課程講師 | ADMIN |
|---|---|---|---|---|---|
| 閱讀公開課程與教材 | ✓ | ✓ | ✓ | 自己課程 | ✓ |
| 查看組別繳交狀態 | 自己／自己組 | ✓ 本組 | 任務指定組 | 自己課程 | ✓ |
| 看 PDF（已通過安全檢查） | 自己／自己組 | ✓ 本組 | ✓ 任務指定作業／組 | 自己課程 | ✓（稽核） |
| 上傳、重交作業 | 自己／所屬組 | ✗ | ✗ | ✗ | 特別代交操作（稽核） |
| 修改任一題分數 | ✗ | **✗** | ✓ **只限被指派題目** | 自己課程 | ✓ |
| 直接設定最終分數 | ✗ | ✗ | 需任務特別授權（預設不授予） | 自己課程 | ✓ |
| 批改者內部備註 | ✗ | 預設僅讀取授權範圍 | 任務範圍可寫 | 自己課程 | ✓ |
| 統一發布成績 | ✗ | ✗ | ✗ | 自己課程 | ✓ |
| 查看聯絡電話 | ✗ | ✗ | ✗ | ✗ | ✓ |
| 管理其他課程 | ✗ | ✗ | ✗ | ✗ | ✓ |

- **任何 TA 自己組也需獲得批改任務才能改分**。跨組 TA 在任務未生效前不可看到他組交件狀態／PDF，任務失效後再次讀取亦拒絕。
- 批改任務以**作業＋指定組別集合**建立，可再選允許的題目集合；一個請求只要包含未授權題目就整體拒絕。
- 學生／TA 不可由 URI 猜 ID、修改 JSON 欄位、前端 DevTools 或共用 PDF URL 擴張權限。Download API 每次查角色、組別、課程、任務與檔案狀態。
- 初版 PDF 通常包含整份作答。題目層級權限僅限制**分數修改**，不承諾 TA 只能閱讀 PDF 內某些題目的答案。
- 講師可以直接批改自己的課程，不需要接受額外指派；講師可覆寫 TA 分數，但改分需留理由和歷史。

---

# 4. 作業與檔案（ASSIGNMENT／UPLOAD／SUBMISSION）

## 4.1 作業設定表單（UI 與 API 必須一致）

| 欄位 | 型態／預設 | 說明 |
|---|---|---|
| `title`, `description` | 字串 | 文字需安全編碼／不接受任意 HTML |
| `course_id` | UUID | 使用者必須有該課的出題權 |
| `submission_mode` | `INDIVIDUAL` / `GROUP` | 作業發布後切換應被禁止或走明確 migration |
| `due_at` | ISO8601 UTC + UI Asia/Taipei | 可由講師／Admin 修改，留 before/after audit |
| `accepting_submissions` | Boolean | 除截止時間外的獨立收件總開關 |
| `allow_late` | Boolean | 截止後是否繼續接受 |
| `allowed_extensions` | allowlist | 講師用「允許檔案類型」的複選控制設定；Backend 有系統層上限 |
| `allow_multiple_files` | Boolean | 「允許提交多個檔案」核取框 |
| `max_files_per_revision` | 整數 | 核取框未勾必為 1；多檔時可自訂，但不得無限制 |
| `max_total_bytes_per_revision` | **預設 20 MB；可設定為 40 MB** | 核算**單次提交的全部檔案大小加總**，多檔案共享同一額度；講師（限自己課程）或 Admin 可在作業設定手動調整。初版系統允許的最高配置值為 40 MB；若未來要超過，必須另行修改部署政策、容量與安全驗收。 |
| `max_bytes_per_file` | 不另設獨立業務限額 | 單一檔案同樣受 `max_total_bytes_per_revision` 約束；不可把 20 MB 誤作「每一個檔案的限制」 |
| `allow_resubmission` | `true` | 任意次數，但保留最近 5 個檔案版本 |
| `retained_versions` | 5 | 以**一次提交的整套檔案**為一個版本 |
| `grading_mode` | `BY_QUESTION` / `DIRECT_TOTAL` | BY_QUESTION 仍可授權最終分數人工覆寫 |
| `show_student_average` | Boolean | 學生成績發布後是否顯示該作業平均；預設關閉較保守 |
| `student_grade_visibility` | `VISIBLE_AFTER_RELEASE` / `HIDDEN` | 最後團體專題可設定 HIDDEN，不在學員端顯示分數 |

- 初期安全政策可先限制支援 PDF（原始 PDF 在線預覽），系統架構允許將來開放 `TXT`, `PNG`, `JPG` 等**經過安全檢測的類型**；不要直接開放 PHP/HTML/JS/執行檔或任意類型。
- 「allowed_extensions」不是信任副檔名；必須有**伺服器層的白名單＋檔案內容格式檢查**。如 `.php4`、`.PHP`、`.pdf.php`、偽造 `Content-Type`、polyglot 與 magic-byte spoofing，須有負面測試。
- **容量單位**：本版使用十進位 **MB（1 MB = 1,000,000 bytes）**，預設 `20,000,000` bytes、可配置上限 `40,000,000` bytes；前端及 API 文件均顯示同一單位，不與 MiB 混用。
- **總量驗證**：對每個 `revision` 內全部上傳附件取實際儲存的 byte 數相加（`SUM(file_size_bytes)`），必須 `<= max_total_bytes_per_revision`；包括單檔／多檔上傳以及重試、分段上傳。前端應顯示目前總量與剩餘額度，但**不得依賴前端檢查**。變更作業額度不會改寫已完成提交版本的歷史紀錄。
- **DoS 控制**：在反向代理、上傳串流、暫存空間、finalize 交易各層限制 request/累計大小與檔案數。檔案超限應整次提交失敗，清除相關暫存，不得產生部分有效的 revision。不要只信任 `Content-Length` 或由客戶端上報的大小；需計算實際收取的 bytes。

## 4.2 交件規則

- 個人作業：學員只能建立或查看自己的提交；同組 TA 可以依帶組關係閱讀。
- 團體作業：任一目前在該組的學員都可以提交、重交，等於替整組交件；**所有組員可查看最近 5 個版本及每版提交者、時間**；以伺服器最新有效完成提交為目前版本，並行提交需使用 idempotency key、順序編號／交易。
- 不限制重交總次數。超過 5 次時將最舊版本**實體附件**列入清除，保留必要的最小 metadata、audit 與已發布分數快照。若過去發布成績所對應的 PDF 被淘汰，仍保留 `revision_id`／hash／score provenance，但不能再打開被刪除的 PDF；這是「只保留五版」的已知追溯性限制。
- **提交收據**：每次成功 finalize 產生 `submission_id`、`revision_no`、`received_at`、檔案清單與 SHA-256、`ON_TIME/LATE`、`PENDING_SCAN/ACCEPTED/REJECTED`。掃描佇列耗時不可追認成學生遲交；但收據表示已收件，**不保證檔案格式驗證已通過**。
- 截止時間、是否開放遲交可隨時改動，但僅影響**改動後的交件判定**；舊提交的原始收件時間及原始遲交標記不可默默重寫。若要追認舊版本，應由管理員執行具原因的重新分類操作。
- `is_late = finalized_at > effective_due_at`，**超過截止時間一秒即視為遲交**。若先準時交、截止後又提交新版本，**新版本標記 LATE**；舊版本仍記錄原始 ON_TIME。後端以截止規則版本及完成收件的 server-side 時間判定，不能取信前端時間。
- **不做自動遲交扣分**：只顯示紅色 `LATE/補交` 標籤，批改者自行決定分數；作業可設定完全禁止遲交（截止後 403/409，依 API 契約）。
- 發布成績後可繼續遲交（只要 `allow_late=true` 且收件仍開），學生送出前要看到「這次提交屬於補交，可能需要重新批改」的確認警告。
- 每次完成新提交，系統在**本組兩位 TA 的工作台／通知中心**顯示新版本事件（初始可標示「待檔案檢查」，檢查失敗時同步更新）；正在批改該作業的所有授權批改者也應看到顯著紅色警示「已有較新版本」，並能比較 revision/time。避免前端輪詢造成負擔，初期可以 server polling + outbox／事件旗標。
- `GRADE` 綁定特定 `revision_id`；新版本不覆蓋舊評分。**已發布的分數不會因新文件上傳而自動變動或撤銷**；後台顯示「已發布第 N 版成績／最新交件第 M 版待處理」，學生同樣看到不易誤解的狀態；更新分數後才可再次明確發布。

## 4.3 安全檔案管線

```text
Student browser
  → POST /uploads/initiate（ACL、期限、配額）
  → private quarantine storage（隨機 key，非 webroot）
  → POST /uploads/finalize（hash、大小、檔數、assignment 規則、deadline）
  → commit revision + receipt + PENDING_SCAN
  → isolated worker（擴充名／MIME／signature／parser 驗證，AV 可選）
  → ACCEPTED / REJECTED（失敗不可出現在可預覽或可下載路徑）
  → authorized PDF preview（專用受限來源與 CSP）/ download
```

- 附件不使用原始檔名當實際 filesystem path，禁止路徑穿越、symlink 跳脫、控制字元、危險雙副檔名；client `accept` 只作便利性提示。
- 嚴格處理 PDF parser：worker 不可存取 DB 憑證、主站 Cookie、Docker socket、其他學生檔案或任意外網；CPU、記憶體、解壓輸出、執行時間均設上限。PDF.js 等網頁預覽必須採獨立 Origin／sandbox／保守 CSP，禁止在主站 DOM 以危險 HTML 方式解析附件；視正式威脅模型決定是否使用渲染後圖片的高隔離預覽。
- 所有未通過掃描／格式檢查的附件必須被拒絕預覽與下載；掃描失敗採 **fail-closed**，不能因為排隊過久就放行。
- 不開放使用者藉著遠端 URL 拉取檔案（避免 SSRF）；教材與作業都由使用者經授權上傳。

---

# 5. 批改、版本、發布、內部備註（GRADING）

## 5.1 出題、Rubric、批改權限

- BY_QUESTION 模式：每題設定題號、滿分、評分標準（多個可自訂文字與分數的快捷按鈕，例如 1～5 分），按標準可一鍵填分及回饋，手動輸入也允許。
- DIRECT_TOTAL 模式：有權人員直接輸入總分，不需先填各題；**BY_QUESTION 也允許由講師／管理員另行設定 `manual_final_score`**（與各題加總分開存），可清楚顯示差異和原因。
- 只要助教沒有有效批改任務，即使是自己帶領的學生，也不能寫入任何分數；批改權可以以「一份作業 × 一個或多個組別 × 指定題目集合」指派。
- 講師可對自己課程任何提交、任何題目直接批改；只有工作人員可跨課程。
- 分組專題：系統保留團體基準分數，**可再對每位組員設定個別最終分數**，每筆調整要有原因；可設定 `student_grade_visibility=HIDDEN`，避免專題成績出現在學生頁面或公開統計。
- `LATER`：同份作業由講師與 TA 個別評分並按比例合成，與目前逐題分派完全不同，不在 MVP 設計複雜加權流程。

## 5.2 建議分數模型

```text
question_total = SUM(saved_question_scores)                 # 可能尚未完成
explicit_total = grading_mode == DIRECT_TOTAL ? value : null
base_score = explicit_total ?? question_total
manual_final_score = NULL or instructor/admin authorized override
published_score = manual_final_score ?? base_score          # 只在發布時 snapshot
late_status = ON_TIME / LATE                               # 僅狀態，不自動扣
```

- 逐題分數、人工最終分數、最終發布分數**不得只存一個欄位**。`NULL` 表示「尚未評分」，`0` 表示「確實得到零分」，這兩種在計算和發布檢查上不可混淆。
- score range 預設為 `[0, max_points]`，超上限只可透過管理員例外授權並留完整審計；使用 decimal 型別及固定 rounding policy（例如 DECIMAL(8,2)）。
- 可由講師覆寫 TA 原始分數，或覆寫最終總分，但保留原始分數、覆寫前後、操作者、原因、時間、revision ID。
- 任務可選擇 `GRADE_QUESTION`, `GRADE_TOTAL`（若確有需要）。後者對 TA 預設不授權；講師／Admin 可改最終總分。

## 5.3 批改並行控制

- 以**每份提交版本的每個評分項目**維持 `version`，`PATCH /grade-items/{id}` 搭配 `If-Match` 或 `expected_version` ；同題競態時 HTTP `409 Conflict`，後端不接受後寫覆蓋前寫。
- 不同題目由 A／B 各自修改時可並行，不應因提交層級全域版本衝突相互阻塞；人工最終分數另有版本控制。
- 修改題目必須在交易內重新驗證有效 assignment/task/course/question/submission/version；不能先驗後存時遭任務撤銷仍成功。
- 評分者介面顯示「正在編輯的 submission revision」與「學生是否已有更新版本」，不可在背景自動切到較新 PDF 後仍沿用原始欄位分數。

## 5.4 留言與回饋（修正原 v0.4）

- **刪除學生雙向留言、聊天室、分組討論、線上成績申訴模組。** 學生對題目、評分有問題直接找帶組助教／實際批改助教實體討論。
- **保留單向正式回饋**：每題 `feedback_to_student`、整份作業 `summary_feedback`，由授權批改者寫入，僅在該作業成績正式發布、且學生有權看成績時可見。
- **保留內部備註**：`staff_internal_note` 可在指定作業／組別／題目範圍內由被授權批改者或課程講師添加；永遠不能由學生端 API、匯出或統計取得。
- 帶組 TA 能否閱讀本組其他批改者的**未發布評語／內部備註**，預設**不行**，除非有有效批改任務或講師另賦予明確讀備註權。這是避免本組閱讀 PDF 自動擴張成看到機密評分討論。

## 5.5 統一發布與重交

- **以單一作業 (`assignment_id`) 為單位**，講師／Admin 在全部欲發布對象完成評分後統一發布；發布操作產生 immutable release batch（提交版號、每人最終分數、各題分數、評語、操作者、時間）。
- 應明確列出未完成評分、未通過文件檢查、最新交件未批改等阻擋項；不可被一鍵誤發布。若需例外部分發布，必須另立需求，不在 MVP。
- 只有 `student_grade_visibility=VISIBLE_AFTER_RELEASE` 的提交會在學生介面顯示成績；團體最後專題可設定 HIDDEN，即使工作人員已做內部成績結算也不公開個人／組別分數或作為學生平均。
- 發布之後修改分數／收到補交，不會直接更新既有發布資料；重新批改後由授權講師另行 `republish`，保留所有前次發布快照與新發布原因。

---

# 6. 統計與匯出（STATS／EXPORT）

## 6.1 工作人員與課程講師

- 查看繳交率（個人作業以學生計數、組別作業以組計數）、準時／遲交／補交、重交次數、各題得分率、學生跨課程進度、TA 批改負擔、分數分布。
- 講師只看自己課程，Admin 跨課程。TA 只可看其帶組或實際被指派的必要工作資料，不能因此得到全營隊私人分數表。

## 6.2 學生可公開統計

- **只有成績發布後**，且出題者在該作業勾選 `show_student_average=true`，才顯示「全營隊各題平均得分」；計算**排除 0 分，也排除 NULL／未評分**，並在 UI 明確顯示「排除 0 分」及有效樣本數，避免將結果誤稱為全體平均。
- 學生可看全營隊／各課程繳交率；繳交率與平均分數分別配置公開政策，未授權的詳細名單不得顯示。
- 若一題有效分數樣本數過少（建議閾值 `n<5`），或可由已知資訊反推特定同學分數，則不顯示；此安全門檻不得由講師任意關閉。團體最後專題 HIDDEN 成績不得進入學生統計。
- 學生繳交率預設以各作業應繳單位為分母（個人＝學生、團體＝組別），已交＝至少一個通過驗證的提交版本；截止前公開繳交率是否可能造成不必要壓力／群體推測，保留獨立公開開關（工程預設截止後才顯示）。

## 6.3 匯出與選人下載

- Admin：成績 CSV、繳交紀錄 CSV；支援「全課／整份作業／指定學生集合／指定組別」的篩選與下載，其中檔案批次下載產生 ZIP。
- 批次匯出 ZIP 必須對**每一個 submission/artifact 再次做 ACL**；每個成員的檔案數與總量需限額、排程工作需逾時／取消與短效 token，ZIP 內檔名應清理路徑、處理碰撞、禁止絕對路徑和 `../`。
- CSV/Excel 需求：MVP CSV，XLSX 為可選便利功能；若日後輸出 XLSX，使用寫入**字串**的安全 API，防止學生姓名、學校、備註等欄位成為公式。CSV 必須防 Formula Injection（`=`, `+`, `-`, `@` 等）。
- 原始聯絡電話僅 Admin 有權匯出，且由管理員**明確勾選**才包含；一般成績／繳交紀錄 CSV 預設不含 phone、password hash、internal note、credential state。
- 匯出前要顯示資料範圍、筆數、是否包含個資，記錄操作人、範圍及時間；匯出檔使用短期私有暫存與清除排程。

---

# 7. 資料庫概念 Schema（DATABASE）

以下是**設計級**資料表與必要欄位；正式 implementation 應補 Alembic migrations、FK、CHECK、UNIQUE、INDEX、刪除政策及 transaction tests。

| Table | 關鍵欄位 | 規則／約束 |
|---|---|---|
| `users` | `id, email, canonical_email, name, school, grade, phone, discord_id, status, password_hash, credential_version` | `canonical_email UNIQUE`；phone 只在 Admin schema 可見 |
| `user_roles` | `user_id, role` | user/role unique；有效角色 enum；可多角色 |
| `auth_sessions` | `id_hash, user_id, scope, expires_at, revoked_at, credential_version` | Session 可撤銷、受限 scope |
| `mfa_factors` | `user_id, type, encrypted_secret, enabled_at` | TOTP secret 必須加密，禁止回傳 |
| `courses` | `id, title, description, visibility, status` | 每活動獨立課程 |
| `course_instructors` | `course_id, instructor_id` | 講師僅管理自己課程 |
| `course_materials` | `id, course_id, title, storage_key, visibility` | 官方教材與學生附件不同 storage class |
| `groups` | `id, code, ordinal` | group code 唯一 |
| `group_memberships` | `group_id, student_id, joined_at, ended_at` | 有效分組不重疊；歷史 snapshot |
| `group_staff` | `group_id, ta_id, assigned_at, ended_at` | 僅本組 read，不授予 edit grade |
| `assignments` | `id, course_id, submission_mode, due_at, due_version, accepting_submissions, allow_late, allow_multiple_files, max_files, max_file_bytes, max_total_bytes, grading_mode, show_student_average, student_grade_visibility` | 所有設定改動 audit，不能無條件回溯 |
| `assignment_allowed_types` | `assignment_id, file_type_id` | 受系統 allowlist 上限限制 |
| `assignment_questions` | `id, assignment_id, ordinal, max_points, prompt` | 題號唯一、不可任意竄改已經有分數的題目 |
| `rubric_options` | `id, question_id, label, score, feedback_template` | 提供快速評分按鈕 |
| `submissions` | `id, assignment_id, owner_type, student_id, group_id, active_revision_id` | logical submission；個人／組別擇一，unique owner |
| `submission_revisions` | `id, submission_id, revision_no, uploader_id, finalized_at, due_version, is_late, scan_state, manifest_hash` | immutable；第六版後清掉最舊檔案但保留必要 metadata |
| `submission_artifacts` | `id, revision_id, private_key, original_filename, size, sha256, validated_type, inspection_state, purged_at` | private, no executable webroot |
| `submission_group_snapshots` | `revision_id, student_id` | 完成提交時快照組員 |
| `grading_tasks` | `id, grader_id, assignment_id, target_group_id, status, can_grade_total, can_read_file, created_by, revoked_at` | 有效任務、明確組別與作業範圍 |
| `grading_task_questions` | `task_id, question_id` | 題目必須屬於 task.assignment_id |
| `grade_items` | `id, revision_id, question_id, score, feedback_to_student, grader_id, row_version` | 逐題 optimistic locking；NULL≠0 |
| `grade_totals` | `revision_id, direct_total, manual_final_score, override_reason, row_version` | 人工最終分數獨立儲存 |
| `group_member_grade_overrides` | `revision_id, student_id, manual_final_score, reason, actor_id` | 團體作業每位組員可獨立調分 |
| `staff_internal_notes` | `id, submission_id, revision_id, question_id, author_id, content, created_at` | 不可向學生暴露 |
| `grade_release_batches` | `id, assignment_id, release_no, actor_id, created_at, reason` | 一份作業一次正式發布動作 |
| `released_grades` | `batch_id, revision_id, student_id, score, snapshot_json, visibility` | immutable；個人／組別分數快照 |
| `submission_notifications` | `event_id, recipient_id, submission_id, new_revision_id, read_at` | 只對帶組 TA 與授權批改者可見 |
| `audit_events` | `id, actor_id, action, target_type, target_id, before_hash, after_hash, created_at` | Append-only, sensitive logging minimized |
| `export_jobs` | `id, requester_id, scope_json, state, expires_at` | 短效下載、驗證範圍 |
| `cleanup_jobs` | `id, target_id, type, state, retry_count` | 五版淘汰、暫存清除、資料保留刪除 |

### DB 一致性必須測試

1. 學生不可能同時提交他不屬於的組別；團體提交者必須在 commit 當下是活躍組員。
2. `grade_items.question_id` 必須屬於 `revision.assignment_id`；`grading_task_questions` 同理，不可替換 ID 產生跨作業寫入。
3. `grade_release_batches`／`released_grades` immutable；下一次變更必須產生新 release version。
4. 提交版本與評分版本不互相覆蓋；最多五個有實體附件的版本，刪除舊附件需同步標記 metadata 及處理重試。
5. 校驗後的下載 URL 必須在每次請求重新檢查 ACL，而不是僅憑 object key 或永久 presigned URL。

---

# 8. REST API 契約初稿（API）

| Method | Endpoint | 核心規則 |
|---|---|---|
| `POST` | `/api/v1/auth/login` | 有防枚舉、防暴力猜測，首次登入只拿 restricted session |
| `POST` | `/api/v1/auth/change-initial-password` | 僅 restricted session；atomic activate |
| `POST` | `/api/v1/auth/logout` | 撤銷 session |
| `GET` | `/api/v1/me` | ACTIVE；只返回 actor 可見欄位 |
| `PATCH` | `/api/v1/me/discord` | ACTIVE；僅修改自己的 Discord ID |
| `POST` | `/api/v1/admin/users/imports/preview` | ADMIN + MFA，僅新增預覽 |
| `POST` | `/api/v1/admin/users/imports/commit` | ADMIN + MFA，僅新增且 transaction-safe |
| `POST` | `/api/v1/admin/users` | ADMIN + MFA，逐人新增 |
| `POST` | `/api/v1/admin/users/{id}/reset-password` | ADMIN + MFA + 核身記錄；撤銷舊 Sessions |
| `GET` | `/api/v1/courses` | ACTIVE；學生可看所有 published courses |
| `GET` | `/api/v1/courses/{id}/materials` | ACTIVE + published course membership |
| `POST` | `/api/v1/courses/{id}/materials` | assigned instructor / ADMIN |
| `POST` | `/api/v1/courses/{id}/assignments` | assigned instructor / ADMIN |
| `PATCH` | `/api/v1/assignments/{id}` | assigned instructor / ADMIN；期限修改 audit |
| `POST` | `/api/v1/assignments/{id}/uploads/initiate` | student/group owner + accepting + quota + time policy |
| `POST` | `/api/v1/uploads/{id}/finalize` | atomic revision + receipt + late flag + notification |
| `GET` | `/api/v1/submissions/{id}/revisions` | submission owner/group members / scoped staff |
| `GET` | `/api/v1/artifacts/{id}/preview` | safe file + per-request ACL; isolated preview origin |
| `GET` | `/api/v1/artifacts/{id}/download` | safe file + per-request ACL |
| `POST` | `/api/v1/grading/tasks` | assigned instructor / ADMIN；組別集合+題目集合 |
| `PATCH` | `/api/v1/grading/tasks/{id}` | scope check + audit + revoke semantics |
| `PATCH` | `/api/v1/grade-items/{id}` | TA explicit task question OR instructor/admin + If-Match |
| `PATCH` | `/api/v1/grades/{id}/final-score` | instructor/admin or expressly authorized total grader + If-Match + reason |
| `POST` | `/api/v1/submissions/{id}/internal-notes` | task-scoped staff; no student visibility |
| `POST` | `/api/v1/assignments/{id}/grades/publish` | assigned instructor / ADMIN；single assignment release batch |
| `GET` | `/api/v1/assignments/{id}/stats/public` | published + visibility toggle + safe sample count |
| `GET` | `/api/v1/assignments/{id}/stats/staff` | instructor/admin; scoped TA summary when allowed |
| `POST` | `/api/v1/admin/exports/grades` | Admin, filtered data + CSV formula protection |
| `POST` | `/api/v1/admin/exports/submissions` | Admin, selected student IDs + per-object ACL + ZIP job |

**重要：** API 的 `student_id`, `group_id`, `course_id`, `question_id` 永遠不是授權依據；Actor 和每一個資源的關係必須由後端從資料庫查詢，並在最終交易內再檢查。前端不可自行決定 `is_late`、`grade_published` 或 `role`。

---

# 9. 安全規格與威脅模型（SEC）

## 9.1 安全目標與威脅假設

- 對手可能是**已經合法連上 WireGuard、擁有學員帳號、知道 API、熟悉 Burp Suite 與檔案上傳繞過技巧的人**。因此只允許 VPN 進站不表示需要降低應用安全控制。
- 攻擊面：初始密碼冒領、權限越界（IDOR/BOLA/BFLA）、CSV／教材上傳、PDF 解析、Stored XSS、批次下載 ZIP、老師成績竄改、跨課程資料外洩、DoS、靶機到 AAMS 的 lateral movement。
- OWASP ASVS 5.0 Level 2 作為目標基線；檔案與分數權限加嚴；威脅建模 STRIDE；不得聲稱可抵禦所有未知攻擊。

## 9.2 必備 controls

| ID | 控制 | 可驗證需求 |
|---|---|---|
| SEC-001 | 物件層授權 | 所有提交、附件、批改項、組別、課程，逐一檢查 actor-role-scope；不可跨組／跨課程 |
| SEC-002 | 屬性層授權 | `phone` 僅 Admin；`password_hash` 永不回傳；staff note 永不返回 student |
| SEC-003 | 批改權 | 本組 TA 沒任務也不能評任何題；跨組只有有效任務指定的組、作業、題目可改 |
| SEC-004 | 成績完整性 | grade update/override/release 有 If-Match、DB transaction、immutable history、audit |
| SEC-005 | 發布前保密 | 未發布分數與隱藏專題分數不經 student API、cache、統計、匯出洩漏 |
| SEC-006 | 身分啟用 | 初始密碼取得的 Session 只能改密碼；啟用後初始密碼失效；冒領風險追蹤 |
| SEC-007 | 登入防護 | Argon2id、rate limit、account enumeration resistance、login audit、密碼重設撤銷 Session |
| SEC-008 | 管理員 MFA | ADMIN 強制 TOTP；MFA recovery 要安全人工流程 |
| SEC-009 | 安全上傳 | Backend allowlist/size/file count、非 webroot、quarantine、worker isolation、fail-closed |
| SEC-010 | PDF Preview | separate origin／sandbox／CSP／nosniff，禁止與主站 Session 同源不受限渲染 |
| SEC-011 | 注入防禦 | SQL parameters、XSS encoding、HTML 禁用、CSV formula neutralization、ZIP name sanitization |
| SEC-012 | Session／CSRF | secure cookies、CSRF token、session rotation/revocation，CORS 精確 allowlist |
| SEC-013 | 資源使用限制 | upload request streaming + quotas、worker memory/time/CPU、zip job quota、DB pool |
| SEC-014 | TLS inside VPN | WireGuard 外仍建 HTTPS，部署 CA／憑證鏈需可驗證，禁 HTTP 降級 |
| SEC-015 | VPN 最小權限 | 每位學員獨立 WireGuard peer key、可撤銷；Firewall 僅允許特定 AAMS port |
| SEC-016 | 防跨系統側移 | 靶機與 AAMS 在不同 VM／網段，限制靶機→AAMS、DB、Storage、hypervisor 路徑 |
| SEC-017 | 日誌與監控 | 認證失敗、批改越權、管理設定、下載、匯出、異常上傳可查；不記錄密碼與敏感原文 |
| SEC-018 | 機敏資訊與憑證 | Secrets 不入 Git、DB 最小權限，掃描 worker 無 DB secrets／外網 |
| SEC-019 | 套件供應鏈 | pinned dependencies、lockfile、dependency scan、patch policy、基礎映像最小化 |
| SEC-020 | 重新提交通知 | 只能通知原組兩位 TA／有效任務人員；無法由訂閱端猜測其他組的最新事件 |
| SEC-021 | 公開統計 | 只在發布後且公開開關啟用；排除 0/NULL；小樣本隱藏；不曝隱藏專題分數 |
| SEC-022 | API 競態 | deadline／finalize／group membership／grade task revocation 在交易中正確判定 |
| SEC-023 | 輸出與匯出 | CSV Injection、Zip Slip、選人權限、短效下載，避免大量下載 DoS |
| SEC-024 | 初始資料壽命 | CSV 明文密碼不留持久化；電話與教材/學員附件分級、最短保存 |

## 9.3 必要安全測試（例）

1. 學員改 query 參數／JSON `group_id` → 無法讀其他組 PDF、不能代其他組提交。
2. 本組 TA 直接 `PATCH grade item` → 403；但本組讀 PDF 仍通過。
3. 對 TA 指派 `assignment=A`, `group=G08`, `question=Q2`；讀 `assignment=B` 或改 `Q1` → 403。撤銷任務後立即不得再讀／改。
4. Student/TA 猜測作業隱藏專題的 grade URL、統計 API → 不能推知分數。
5. 一個學生把 `discord_id` 或 PDF 檔名寫成 `<script>`, `=HYPERLINK(...)` → 頁面安全編碼、CSV 無可執行公式。
6. 上傳 `.php4`, `.pdf.php`, `.PHP`, 偽造 MIME、有效 PDF 後接惡意內容；不得被執行，非核准格式被拒，受檢查 worker 無法逃至 DB／其他附件。
7. 作業預設整次提交總量 20 MB：上傳兩檔 12 MB + 9 MB（合計 21 MB）時，**整個 revision 必須失敗**，不得部分接受；單檔 21 MB 也必須拒絕。講師調整該作業上限至 40 MB 後，兩檔 18 MB + 17 MB（合計 35 MB）可進行後續格式與安全檢查，合計 41 MB 必須拒絕。不可透過拆檔、分段傳輸、偽造 `Content-Length` 或直接呼叫 API 繞過。
8. 同組兩人同時 finalize、同題兩 TA 同時儲存；提交順序可追蹤、成績衝突返回 409。
9. 已發布成績後再次補交：舊 release snapshot 不變，新版本顯示紅字待改、通知帶組 TA，不自動公開新分數。
10. 非 Admin 角色在 API response、搜尋／匯出／活動稽核頁都不能拿到別人的電話。
11. WireGuard peer 無法直接連到 PostgreSQL、監控、Hypervisor 管理埠、其他 peer 私有服務；若靶機失陷亦不可取得 AAMS 儲存憑證。
12. 受限初始登入 Session 無法訪問其他一般 endpoints；完成啟用之後原初始密碼無法登入。
13. 零分真實分數與 NULL 未評分在 DB、統計排除、發布完成度判斷上明確不同。
14. 導入 102 人、12 組，模擬截止時尖峰 72 人並行提交**總量 20 MB** 的 revision，另以**40 MB** 額度測試大附件；測量 API、磁碟、掃描佇列、暫存配額與成功收據比例；不以硬體規格推斷測試結果。

---

# 10. 部署、儲存、維運與資料壽命（OPS）

## 10.1 WireGuard-only 架構

- 服務只在 WireGuard network interface 或內部 Reverse Proxy 的 ACL 上提供，宿主防火牆限制 AAMS HTTPS port 只接受 VPN 網段流量；不可只依靠 DNS 隱藏。
- 每個人／每個裝置建獨立 peer 身分與配額，離營撤銷 key；WireGuard `AllowedIPs` 不是伺服器側的完整應用 ACL，仍須在 Router/Firewall、Proxy、API 控制授權與 peer-to-peer 封鎖。
- WireGuard 是**額外網路屏障**，不是防止已入網學員或被攻陷靶機攻擊的最終防線。**不要將正式站與刻意脆弱的靶機放在同一個具高權限的 Docker host/bridge 上**；必要時用不同 VM、VLAN、host firewall、無共享 storage/credential。
- 即使全 VPN，仍使用 HTTPS：可透過公有網域搭配 DNS-01 取得公開可信憑證而無需向外公開 AAMS 服務，也可用 Caddy 內部 CA（但必須安全散布並信任 CA cert）。具體方式由部署環境決定。

## 10.2 儲存容量估算

以 72 位學員、每人每份作業保留 5 個版本估算，**每一版本總容量上限 20 MB** 時，單一個人作業最極端情況為 `72 × 5 × 20 MB = 7,200 MB ≈ 7.2 GB`（約 6.71 GiB）；若整份作業提高到 40 MB，則為 `72 × 5 × 40 MB = 14,400 MB ≈ 14.4 GB`（約 13.41 GiB）。**同一版本內不論 1 檔或 5 檔，都只共享該 20／40 MB 額度，不因檔案數增加而倍增容量。** 這還未計算資料庫、教材、檢查暫存及備份容量。

因此：**必須另有每位使用者／每作業配額、整體 storage quota、清理暫存、備份額外空間與並行上傳限制**。本系統採自架私有磁碟，不假設需要 Cloudflare R2 等雲端儲存；成本取決於硬碟與備援。

## 10.3 保存期限與期末重置

- 使用者希望活動結束後保留資料 **3 個月**，之後匯出成績 CSV（需要時 XLSX）、繳交紀錄 CSV 與選擇性 PDF ZIP，再由工作人員確認後對該 instance 執行重置。
- 3 個月以 `event_end_at` 為計時起點（此為工程預設，UI/後台應明示）；清除前先確認匯出完整性、檔案數、SHA-256 manifest，並有至少兩位授權工作人員確認；避免按錯一鍵刪全站。
- 應包含主資料庫、附件、匯出暫存、失效 Session、暫存檔、日志和備份的到期規則；「重置資料庫」不等於所有檔案及備份都已清除。
- 需要特別確認：**預設將聯絡電話、Discord ID、密碼雜湊排除在期末成績匯出之外**；對需保留的隱藏專題分數僅輸出給管理員的內部表。

## 10.4 目前尚未決定的災難復原（明確 OPEN）

`OPEN-OPS-001`：**備份頻率、異機／離線備份、RPO/RTO、還原演練由主辦方延後決定，v0.5 不聲稱已實作。**

但系統具安全性要求：在正式收件前至少要建立基本可用的 DB／PDF 備份與手動還原流程，否則磁碟損壞或被攻擊後可能造成學生交件及成績不可恢復；正式部署 Go/No-Go 需要檢查此點。完整方案、頻率、服務中斷容忍度，可留到後續維運階段決定。

---

# 11. 頁面與使用流程（UI）

1. **登入／強制改密碼**：登入 Email + 密碼 → 初次導向 change password；Admin 首次啟用後 TOTP enrollment。
2. **學生首頁**：全部課程卡片 → 課程教材／作業 → 繳交狀態、截止／補交警示、收據、最新 5 版（個人或組別）、發布後的成績與單向回饋。
3. **助教工作台**：自己所帶組別繳交列表、PDF 預覽、紅字「新的提交版本」；另有「我的批改任務」只呈現被指派的題目分數與內部備註。
4. **講師工作台**：自己課程建立作業／教材；作業表單多檔核取框、允許檔案類型、截止／允許遲交、題目／Rubric、組別批改任務、發布成績、公開平均選項。
5. **管理員工作台**：CSV preview + insert-only commit、逐人建帳、強制重設、分組、所有課程、使用者 phone、後台統計、選人匯出、刪除／重置設定。
6. **中英文 i18n**：`zh-TW`, `en`；所有 UI 字串可替換、活動外觀由 config/theme 自訂，不能自行注入不受信任 script。
7. **桌面優先**：學生用手機開啟仍看電腦版布局（可縮放或水平捲動），不是 Responsive 手機專用版；但基本登入／安全提示不得因桌面布局而不可讀。

---

# 12. 驗收條件（Acceptance Criteria）

## 12.1 功能驗收

- 建立 102 個帳號、12 組、每組兩 TA；CSV 只新增、重複帳號不改原資料；Email 為登入 ID，密碼範例 `10007038907` 可進入受限首次改密碼。
- 學生在活動內可見全部已發布課程、教材；講師不可管理不屬於自己的課程；Admin 可跨課程。
- 團體作業任何組員都能交件／重交，組員皆可查看最新版、5 個保留版本及每版提交者。
- 作業多檔開關、可接受檔案類型與**整次提交總額度**生效：預設 20 MB，可由講師／Admin 設定為 40 MB；多個檔案的實際 bytes 加總超限時，整次提交拒絕，且無法經 API bypass。
- 若截止後仍允許上傳，顯示警告、標 LATE；不計算自動遲交扣分，學生 23:59 已交第一版、00:01 再交第二版時新版為 LATE。
- 講師延後 Deadline 只影響後續提交且有 audit；每次完成上傳可下載或複製可核對的收據。
- 新版交件通知兩位帶組 TA，並在同一份作業的實際批改者畫面顯示警示；評分維持綁定原有 revision。
- TA 只讀本組，不因帶組可給分；講師派指定組別+題目後才可改。講師任意修改自己課程分數；同題碰撞 409。
- 逐題分數可自動加總、Rubric 快速填分，講師可額外覆寫最終分數；團體專題每位組員可不同分數且設定不對學生公布。
- 統一發布指定作業：之前分數、評語不對學生可見；發布後才可見，學生平均須 toggle 且排除零分、樣本過小不公開。
- CSV 成績／繳交紀錄匯出、選人 ZIP 批次下載可運作，不得包含超出授權的檔案或 phone/internal note。
- 未提供聊天、雙向留言或申訴頁面；有單向正式回饋和 staff-only note。
- WireGuard 未連線狀態無法接觸 AAMS 服務，接上 VPN 仍需成功登入及 object-level ACL；HTTPS 有正確信任鏈。

## 12.2 安全 No-Go

任一以下情形發生，**不可正式上線**：

1. 未啟用帳號能使用正常 API，或初始密碼改過後仍可使用。
2. 學生能讀到他組 PDF、未授權 TA 能給分／跨組讀檔、講師能跨非授權課程、非 Admin 能得到電話。
3. 學生能透過其他 API／統計取得尚未發布或隱藏專題成績。
4. `.php4`／雙副檔名／偽造 PDF 直接進入可執行 webroot 或 PDF preview 能讀取主站 Session。
5. 有跨角色成績竄改、批改競態遺失更新、發布後未授權自動改分。
6. 靶機／學生 VPN peer 可直接到達 DB、host 管理服務，或共享 AAMS 的高權限憑證／目錄。
7. 無 TLS、無基礎資料備份與復原方式、無法辨識已上傳檔案的實體儲存與檢查狀態。

---

# 13. 階段、優先級與剩餘待辦

| Phase | 工作 | 交付物 |
|---|---|---|
| P0 | 威脅模型、資料表、ACL policy、事件定義 | ERD、API authorization matrix、測試清單 |
| P1 | 核心帳號／安全配置 | WireGuard/HTTPS、DB、CSV insert-only、初次改密碼、管理員 TOTP |
| P2 | 課程／分組／作業 | 固定組、教材、個人／團體、多檔安全上傳、5 版、收據與遲交標籤 |
| P3 | 批改／發布 | 指派任務、題目 ACL、PDF 預覽、Rubric、人工最終分、鎖、版本通知 |
| P4 | 統計／匯出／外觀 | 安全平均、繳交率、CSV／選人 ZIP、i18n/theme、桌面介面 |
| P5 | 安全與上線 | Auth/ACL fuzz、惡意 PDF、負載測試、網段隔離、基礎還原演練 |
| P6 | 後續可選 | Gmail reset、TA/講師比例合成評分、進階匯出、詳細 RPO/RTO |

## 13.1 仍需要回答的**精簡問題**（不妨礙 v0.5 基線定稿）

1. `RESOLVED-01`：**限制對象是「一次提交的所有檔案總量」，不是每檔大小**；預設 20 MB（20,000,000 bytes），講師／Admin 可在作業設定調整至 40 MB（40,000,000 bytes）。初版上限 40 MB。
2. `OPEN-02`：多檔作業的預設 `max_files_per_revision`（建議 5）；哪些上傳類型實際要開放（除 PDF 外）？
3. `OPEN-03`：批改者可見的「新版本通知」是否要通知兩位帶組 TA，即使他們沒被指派改分？**本版依 Q19 規格設定為是**；可在工作台內通知而非發 Email。
4. `OPEN-04`：Admin 以外的哪種人可以看到學員 Email、學校、年級？**本版暫將學校／年級視為帶組輔導資料；電話只給 ADMIN**。
5. `OPEN-05`：管理員重設初始密碼的人工傳遞方式／核身 SOP、TOTP 遺失救援 SOP。
6. `OPEN-06`：是否需要將最後專題分數完全對學生隱藏（本版採此解讀），還是僅「不公開到統計」但允許自己看到？
7. `OPEN-07`：系統是否要讓學生看到所有**公開**課程投影片，抑或包括預先排程／未發布課程？**本版採已發布才能看**。
8. `OPEN-OPS-001`：備份 RPO/RTO、頻率、備援位置、可接受資料遺失量與還原演練，**需求方暫緩決定**；部署前仍需基本備份可行。
9. `OPEN-08`：學生繳交率何時顯示（截止前／後）？目前預設截止後；平均分數只在成績發布後。

---

# 14. 參考標準與技術文件

- OWASP ASVS: https://owasp.org/www-project-application-security-verification-standard/
- OWASP Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- OWASP Password Storage Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP File Upload Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- OWASP CSV Injection: https://owasp.org/www-community/attacks/CSV_Injection
- OWASP Session Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- Caddy Automatic HTTPS: https://caddyserver.com/docs/automatic-https
- Caddy `tls internal`: https://caddyserver.com/docs/caddyfile/directives/tls
- Gmail SMTP/IMAP: https://developers.google.com/workspace/gmail/imap/imap-smtp

---

## v0.5 變更紀錄（對 v0.4）

1. 正式確定註冊 Email 為 Username、初始密碼測試向量與多角色、個資欄位／phone 權限。
2. 登入忘記密碼採管理員核身重設；Gmail 信件重設列為選配。
3. CSV 僅新增，活動期間建議逐人操作；多課程可下載投影片、講師課程範圍受限。
4. 固定分組；團體由任一成員重交並顯示每版提交者。
5. 多檔表單可配置；**每次提交所有附件合計預設 20 MB，可手動調整至 40 MB**；網頁 PDF 預覽；超過五版淘汰最舊檔案。
6. 刪除自動晚交扣分政策、60 分保底演算法；改為 `LATE` 標籤與人工分數覆寫。
7. 發布後可補交、紅字版本警示與 TA 通知；成績快照與提交版本獨立。
8. 原本學生聊天／雙向留言／申訴正式移除，改為單向評語與內部備註。
9. 發布單位固定為一份作業；團體專題可對每人獨立調分並隱藏學生分數。
10. 學生平均值排除 0 分／NULL，只有作業發布後可選公開；支援 Admin 精選對象 ZIP。
11. WireGuard-only、桌面版、中英雙語、三個月保留、備份細節 OPEN。

## v0.6 變更紀錄（對 v0.5）

1. 移除「每檔 20 MiB」的錯誤解讀，改為**整次提交（revision）所有附件合計預設 20 MB**。
2. 允許自己課程的講師與 Admin 將特定作業之單次提交額度手動調整至 **40 MB**；本次先設最高可配置額度 40 MB。
3. 明定十進位 MB 單位、上傳串流及 finalize 的後端總量檢查、超限整次拒絕。
4. 修正容量估算：多檔共用同一總額度；新增 20／40 MB 的安全與負載驗收案例。
