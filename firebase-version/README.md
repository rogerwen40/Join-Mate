# JoinMate Fire

這是 JoinMate 的 Firebase Spark（免綁信用卡）版本，與根目錄的 FastAPI / Render
版本互不影響。

## 第一版功能

- Google 帳號登入
- 公開活動首頁與篩選
- 邀請碼活動（私人活動不會出現在公開首頁）
- 建立、取消與分享活動
- 正取與候補報名、取消報名
- 我的活動
- Firestore Security Rules
- Firebase Hosting 單頁網站設定

第一版刻意不使用 Cloud Functions，才能留在免費 Spark 方案。Email 通知與候補
自動轉正需要可信任的伺服器執行，下一階段會接到 Google Apps Script，不能把寄信
密鑰直接放進瀏覽器 JavaScript。

## 免費額度怎麼計算

Firestore 免費額度主要是每天重置：

- 50,000 次文件讀取／日
- 20,000 次文件寫入／日
- 20,000 次文件刪除／日
- 1 GiB 資料儲存
- 10 GiB 對外傳輸／月

Firebase Hosting 提供 10 GB 儲存及 10 GB／月的資料傳輸免費額度。這個小型揪團
系統不會因為「放著沒人用」而持續產生資料庫運算時數；只有使用者載入或修改資料
時才會使用 Firestore 讀寫額度。

## Firebase Console 設定

1. 到 <https://console.firebase.google.com/> 建立 Firebase 專案，方案保持 `Spark`。
2. 「Authentication」→「登入方式」→啟用 **Google**。
3. 「Firestore Database」→「建立資料庫」→選擇正式環境模式。地區建議選台灣或
   亞洲附近，建立後不要直接使用測試模式。
4. 專案首頁點 `</>` 新增 Web App，名稱可填 `JoinMate Fire`。
5. 複製 Firebase 提供的 `firebaseConfig`，填入
   `public/firebase-config.js`。

Firebase 網頁設定中的 `apiKey` 是前端專案識別資訊，不是管理員密碼；真正的資料
權限由 `firestore.rules` 和使用者登入身分保護。

## VS Code 本機預覽

在 `firebase-version` 資料夾開啟 Terminal：

```powershell
npx firebase-tools@latest login
npx firebase-tools@latest use --add
npx firebase-tools@latest emulators:start --only hosting
```

第一次執行 `npx` 可能詢問是否安裝 `firebase-tools`，輸入 `y`。終端會顯示本機
網址，通常是 <http://127.0.0.1:5000>。

> 本機頁面仍會連到你設定的線上 Firestore。Firebase Emulator 的完整本機資料庫
> 隔離會在下一階段加入。

## 部署

將 `.firebaserc.example` 複製為 `.firebaserc`，把專案 ID 換成自己的 Firebase
Project ID，接著執行：

```powershell
npx firebase-tools@latest deploy --only firestore,hosting
```

部署完成後會得到固定的 `https://你的專案.web.app` 網址，不需要一直開 Terminal。

## 目前限制

- 同一瞬間大量搶最後一個名額時，純前端版本仍可能發生名額競爭；小型群組通常
  不會遇到。正式公開使用前，會把報名交易移到 Apps Script 後端。
- 候補取消後不會自動將下一位轉正。
- 尚未串接 Email 排程。
- LINE 分享已支援；LINE 帳號登入需要 LINE Login 後端驗證，會在後續版本評估。
