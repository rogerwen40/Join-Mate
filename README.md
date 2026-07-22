# JoinMate

JoinMate 是讓朋友、社群與不同組織一起建立及參加活動的揪團系統。

## 開發環境

- Python 3.10.11
- 專案虛擬環境：`.venv-joinmate`

在 VS Code 開啟 `D:\BenQ\JoinMate` 後，專案設定會自動指定以下直譯器：

```text
.venv-joinmate\Scripts\python.exe
```

如果終端機沒有自動啟用環境，可在 PowerShell 執行：

```powershell
.\.venv-joinmate\Scripts\Activate.ps1
```

成功後，終端機提示字元前方會出現 `(.venv-joinmate)`。

也可以不啟用環境，直接使用專屬 Python：

```powershell
.\.venv-joinmate\Scripts\python.exe --version
```

## 安裝與執行

第一次取得專案時安裝套件：

```powershell
.\.venv-joinmate\Scripts\python.exe -m pip install -r requirements.txt
```

啟動開發伺服器：

```powershell
.\.venv-joinmate\Scripts\python.exe -m uvicorn app.main:app --reload
```

接著開啟：

- 首頁：<http://127.0.0.1:8000>
- API 文件：<http://127.0.0.1:8000/docs>
- 健康檢查：<http://127.0.0.1:8000/api/health>

## 部署到 Render（固定網址、不用開著 Terminal）

專案根目錄的 `render.yaml` 會建立：

- 一個 FastAPI Web Service
- 一個由 Render 私密環境變數連接的外部 PostgreSQL 資料庫
- 自動產生的 Session 密鑰與 HTTPS Cookie 設定

`.python-version` 將 Render 的執行環境固定在 Python 3.13，避免套件與
平台預設 Python 版本不相容。

將專案推送到 GitHub 後，在 Render 選擇 **New > Blueprint**，連接該
GitHub repository 並套用 `render.yaml`。首次建立時，將 Neon 的連線字串
填入 `JOINMATE_DATABASE_URL`。部署完成後會取得固定的
`https://joinmate-....onrender.com` 網址。

本機執行仍使用 `joinmate.db`；Render 則由 `JOINMATE_DATABASE_URL`
切換至 Neon PostgreSQL，因此本機與雲端資料彼此獨立。

## Gmail Email 通知

`google_apps_script/Code.gs` 是以網站管理者 Gmail 寄信的 Apps Script
橋接程式。部署為 Web App 後，在 Render 設定：

- `JOINMATE_EMAIL_WEBHOOK_URL`：Apps Script 的 `/exec` 網址
- `JOINMATE_EMAIL_SECRET`：執行 `setupJoinMate` 產生的共用密鑰
- `JOINMATE_PUBLIC_URL`：公開的 JoinMate 網址

`setupJoinMate` 也會建立每 5 分鐘執行一次的排程，用 HTTPS 喚醒免費
Render 服務並檢查活動提醒。Email 使用資料庫 outbox，寄送成功前最多
重試五次；站內通知不受 Email 寄送失敗影響。

本機活動資料會儲存在專案根目錄的 `joinmate.db`。這個檔案已列入
`.gitignore`，不會被提交到版本控制。

## 第一版範圍

第一版提供以下核心功能：

1. 成員登入
2. 建立活動
3. 瀏覽活動
4. 報名與取消
5. 候補與自動遞補
6. 達到最低人數後自動成團
7. 站內通知

每位使用者可自行建立帳號並替自己報名。額滿後會自動排入候補，正式參加者
取消時會依報名時間自動遞補。

站內通知會記錄報名成功、進入候補、取消、候補轉正、活動成團，以及成團後
人數再次不足等事件。登入後，每位使用者只會看到自己的通知。

開發伺服器運行時，每 30 秒會檢查一次即將開始的活動。正式名單會在活動前
24 小時及 1 小時收到站內提醒；同一位成員在同一提醒階段只會收到一次。
若伺服器暫時關閉，重新啟動後仍會補送尚未開始活動的當前階段提醒。

每位使用者可在「我的紀錄」設定 Email 通知。報名結果、活動修改或取消、
候補轉正式及活動成團預設開啟；活動前一天與前一小時提醒預設關閉，可自行
勾選。關閉 Email 不會刪除站內通知。

建立活動時可選擇公開或邀請碼。公開活動顯示於首頁；邀請碼活動不會出現在
首頁，參加者需輸入建立者設定的 4～20 字元代碼後才能報名。既有活動維持
公開，先前建立的只限連結活動也會自動改為公開。

活動建立者可在活動頁複製完整分享文字，內容包含名稱、日期、星期、時間、
地點、費用、目前名額狀態及報名連結。Email 主旨會標示通知原因，內文也會
附上完整活動資訊，不會只顯示活動名稱。

活動圖示會依名稱與說明自動判斷，例如羽球、籃球、排球、足球、桌球、網球、
棒球及保齡球會使用各自圖示；無法判斷的運動使用通用獎牌圖示。首頁、活動頁、
分享文字及 Email 會顯示一致的圖示。

## 個人登入

第一次使用時，到 `/account/setup` 輸入姓名、Email，並設定剛好 4 個數字的登入碼。
系統不要求使用者預先存在於固定成員名單。密碼使用 Argon2 雜湊儲存，原始密碼
不會寫入資料庫；同一個 Email 不能重複註冊。4 位數登入碼只適合封閉內部環境，
若系統公開部署，應改用較強密碼並限制登入嘗試次數。

新建立的活動會記錄建立者。只有建立者能編輯或取消活動；取消後，正式與候補
報名會一併取消、時間提醒停止，所有受影響成員會收到站內通知。升級前已存在的
舊活動沒有建立者紀錄，因此只能查看，不能由一般成員修改或取消。
登入後，成員只能替自己報名、取消自己的報名，以及查看自己的通知。

目前是公開測試版本，因此尚未寄送 Email 驗證信。正式部署前，應設定
強隨機的 `JOINMATE_SESSION_SECRET`、啟用 HTTPS，並補上 Email 驗證與忘記密碼流程。

AI 推薦與成團預測會等系統累積足夠資料後再加入。

## 公開測試網址

執行 `start_public_test.ps1` 會同時啟動 JoinMate 與 Cloudflare Quick Tunnel，並在
終端機顯示可分享的 `https://...trycloudflare.com` 網址。這個方式只供短期測試；
電腦及終端機必須保持運行，每次重啟取得的網址可能不同。

為保護 4 位數登入碼，同一 Email 或來源在 15 分鐘內錯誤 5 次後會暫停登入。

SQLite 已啟用 WAL、外鍵及 30 秒忙碌等待。報名、取消與候補遞補會先鎖定活動
交易，避免多人同時搶最後名額時超額報名。

活動建立者可在活動頁選擇「完成與出席」，逐一記錄正式參加者有出席或未出席。
完成後會停止報名與提醒、結束尚未轉正的候補，並向每位成員發送個人結果通知。

登入後可從 `/me` 查看個人活動頁，包括即將參加、自己建立、歷史報名、出席率、
有出席與未出席次數，以及取消或候補未轉正的紀錄。

首頁支援活動名稱／說明／地點關鍵字、類型、指定日期、地點、活動狀態及只看
可報名等條件。活動卡片會顯示目前正式人數、上限與招募中／已成團／額滿狀態。

活動完成後，只有被記錄為有出席的正式參加者能留下 1～5 分與 500 字內回饋。
每人每場限一筆並可修改；活動頁及首頁卡片會顯示平均評分。

第一位註冊使用者會成為平台管理者。只有管理者能查看 `/admin` 統計後台及包含
Email 的使用者列表；一般使用者無法存取。後台顯示活動、成團、取消、出席、
評分與活動類型等整體資料。
