# 上課筆記

把上課錄音變成逐字稿，再整理成詳細筆記，還可以對課堂內容提問。全部在自己的電腦上跑，錄音和筆記不會上傳到網路。

- **匯入音檔**（m4a、mp3、wav、影片檔都可以）：自動轉逐字稿、整理筆記
- **即時錄音**：邊錄邊出逐字稿，錄音檔也一起存下來
- **筆記**：本堂重點、詳細內容整理、名詞解釋、作業／考試／重要日期、待確認
- **提問**：問課堂內容，回答會附出處時間，點一下就能聽那一段
- **功耗**：Snapdragon 電腦會顯示即時功耗（整機／CPU／NPU／GPU），並記錄每個步驟用了多少電

## 安裝

需要 Windows 10／11，x64（Intel、AMD）或 ARM64（Snapdragon）都可以。建議 16 GB 記憶體，8 GB 請選輕量的 AI 模型。硬碟需要約 8 GB。

1. 下載這個專案：按 GitHub 頁面上的 **Code → Download ZIP** 解壓縮，或用 `git clone`。路徑可以有中文。
2. 點兩下 **安裝.bat**，照畫面選擇：
   - **語音辨識**：
     1. TEA-ASR-1.1（推薦）：台灣華語、中英夾雜最準，自己會加標點。有裝 GenieX 的 Snapdragon 電腦會跑在 NPU 上，其他電腦用 llama.cpp 跑，有顯示卡就用 GPU。
     2. Breeze-ASR-25：whisper 系的模型。
   - **AI 模型**（整理筆記、提問）：Gemma 4 E4B（推薦）、Gemma 4 E2B（輕量）、已經裝好的 Ollama 或 LM Studio、GenieX，或不用 AI、只做逐字稿。
3. 安裝完會問要不要在桌面建立捷徑。

安裝程式只會把東西放在這個資料夾裡的 `runtime`、`models`，不會改系統設定。只有選 NPU 時，TEA-ASR 模型會放進 GenieX 的模型資料夾。要移除，就把整個資料夾和桌面捷徑刪掉。

第一次安裝要上網下載，之後可以完全離線。

## 使用

點兩下 **上課筆記.bat**（或桌面捷徑），瀏覽器會打開 http://127.0.0.1:18484/ 。使用時黑色視窗不能關，用完直接關掉黑色視窗即可。

- **匯入音檔**：在左邊選檔案或拖進去，填課名和關鍵字（例如「hash table, 期中考」），按「開始處理」。關鍵字會當熱詞，專有名詞會辨識得比較準。
- **即時錄音**：按左上的「開始錄音」。如果收不到聲音，到 Windows 設定 → 隱私權 → 麥克風，打開「讓桌面應用程式存取您的麥克風」。
- **資料**：都存在「課堂」資料夾，每堂課一個資料夾，裡面有錄音、逐字稿、筆記和提問紀錄。

## 速度與耗電（參考）

以下是在 Snapdragon X2 Elite（ProArt PZ14）上，用 58 秒的中英夾雜測試音檔量到的：

| 語音辨識 | 錯字率 | 轉完 58 秒音檔 |
|---|---|---|
| TEA-ASR-1.1，NPU（GenieX） | 2.4% | 約 11 秒 |
| TEA-ASR-1.1，llama.cpp CPU | 2.4% | 約 17 秒（含啟動） |
| Breeze-ASR-25，whisper.cpp CPU | 4.9% | 約 18 秒 |

1 小時的課：語音辨識大約 10～20 分鐘，整理筆記 20～30 分鐘。電腦越快越短，處理時記得插電。

## 注意

- 逐字稿是語音辨識的，專有名詞和同音字可能會錯，筆記和回答也可能跟著錯。重要的地方請點時間戳回去聽。
- 筆記的「名詞解釋」有時會補充上課沒講的內容，請自行確認。
- 如果 whisper 或 llama.cpp 無法執行，可能是缺少 Microsoft Visual C++ 執行階段：
  - [x64](https://aka.ms/vs/17/release/vc_redist.x64.exe)
  - [ARM64](https://aka.ms/vs/17/release/vc_redist.arm64.exe)

## 開發用的電腦

沒有 `config.json` 時，程式會用系統的 Python 和 GenieX，語音辨識和 AI 模型都走 NPU，Breeze 的檔案則在 `cpu\Release`、`models`。環境變數可以臨時調整：

| 環境變數 | 用途 |
|---|---|
| `LECTURE_ASR=whisper` | 臨時改回 Breeze |
| `LECTURE_DIR` | 換資料夾，測試時用 |
| `LECTURE_PORT` | 換埠，測試時用 |

## 使用的開源專案與模型

- whisper.cpp、llama.cpp（MIT）
- TEA-ASR-1.1（JacobLinCool，MIT；基於 Qwen3-ASR，Apache 2.0），GGUF 由 mradermacher 轉換
- Breeze-ASR-25（聯發科 MediaTek Research，Apache 2.0；ggml 格式由社群轉換）
- Silero VAD（MIT）
- Gemma 4（Google，Gemma 使用條款）；E4B 選項是社群的 SDFT Heretic RP 微調版
- FFmpeg（GPL 版本，BtbN 編譯）
- OpenCC（opencc-python-reimplemented，Apache 2.0）
- Python（PSF License）
