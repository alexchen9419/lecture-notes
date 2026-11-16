# 上課筆記安裝程式：下載 Python（免安裝版）、ffmpeg、語音辨識模型（TEA-ASR 或 Breeze），以及選擇的 AI 模型
# 全部放在這個資料夾裡（GenieX 的模型除外），不會改動系統設定；要移除就整個資料夾刪掉。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$RT = Join-Path $Root 'runtime'; $MD = Join-Path $Root 'models'; $DL = Join-Path $Root 'runtime\_download'
New-Item -ItemType Directory -Force $RT, $MD, $DL | Out-Null

$PY_VER = '3.12.10'
$WHISPER_TAG = 'b5454'
$LLAMA_TAG = 'b11976'
$TEA_REPO = 'https://huggingface.co/mradermacher/TEA-ASR-1.1-GGUF/resolve/main'
$TEA_GGUF = 'TEA-ASR-1.1.Q8_0.gguf'; $TEA_MMPROJ = 'TEA-ASR-1.1.mmproj-f16.gguf'
$SHERPA_VER = '2.0.1'

function Say($msg, $color = 'White') { Write-Host $msg -ForegroundColor $color }

# 下載（已經下載過就跳過；用 Windows 內建的 curl.exe，有進度條、可以續傳）
function Get-File($url, $dest) {
    if ((Test-Path $dest) -and (Get-Item $dest).Length -gt 0) { Say "  已經有 $(Split-Path -Leaf $dest)，跳過" 'DarkGray'; return }
    Say "  下載 $(Split-Path -Leaf $dest)"
    & curl.exe -L --fail --retry 3 -C - -o "$dest.part" $url
    if ($LASTEXITCODE -ne 0) { throw "下載失敗：$url" }
    Move-Item -Force "$dest.part" $dest
}

function Expand-To($zip, $dir) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    Expand-Archive -Path $zip -DestinationPath $dir -Force
}

# 在資料夾裡找某個 exe，回傳它所在的資料夾（各家 zip 的資料夾結構不一樣）
function Find-ExeDir($dir, $exe) {
    $f = Get-ChildItem -Path $dir -Recurse -Filter $exe -File | Select-Object -First 1
    if (-not $f) { throw "在 $dir 找不到 $exe" }
    return $f.DirectoryName
}

# 測試用：環境變數可以預先回答問題（SETUP_ASR、SETUP_NPU、SETUP_LLM、SETUP_SHORTCUT、SETUP_CLEAN）
function Ask($prompt, $envName) { $v = [Environment]::GetEnvironmentVariable($envName); if ($v) { Say "$prompt $v"; return $v }; return Read-Host $prompt }

function Rel($path) { return $path.Substring($Root.Length).TrimStart('\') }

Say "`n=== 上課筆記 安裝 ===`n" 'Cyan'

# ---------- 偵測電腦 ----------
$cpuArch = (Get-CimInstance Win32_Processor | Select-Object -First 1).Architecture   # 12 = ARM64，9 = x64
$arm = ($cpuArch -eq 12)
if ($env:SETUP_ARCH -eq 'x64') { $arm = $false }          # 測試用：在 ARM 電腦上模擬 x64 安裝
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$gpus = (Get-CimInstance Win32_VideoController | ForEach-Object Name) -join '、'
$hasGeniex = [bool](Get-Command geniex -ErrorAction SilentlyContinue)
Say ("電腦：{0}，記憶體 {1} GB，顯示卡：{2}{3}" -f ($(if ($arm) { 'ARM64（Snapdragon 等）' } else { 'x64（Intel／AMD）' })), $ramGB, $gpus, $(if ($hasGeniex) { '，有裝 GenieX（可以用 NPU）' } else { '' }))
if ($ramGB -lt 8) { Say '注意：記憶體少於 8 GB，語音辨識和 AI 模型可能跑不動。' 'Yellow' }

# llama.cpp：TEA-ASR（不用 NPU 時）和 AI 模型 1、2 都用它；x64 用 Vulkan 版（有顯示卡就用 GPU，不行程式會自動改 CPU）
$script:lDir = $null
function Get-Llama {
    if ($script:lDir) { return $script:lDir }
    $lName = "llama-$LLAMA_TAG-bin-win-$(if ($arm) { 'cpu-arm64' } else { 'vulkan-x64' }).zip"
    $lZip = Join-Path $DL $lName
    Get-File "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/$lName" $lZip
    if (-not (Test-Path "$RT\llama")) { Expand-To $lZip "$RT\llama" }
    $script:lDir = Find-ExeDir "$RT\llama" 'llama-server.exe'
    return $script:lDir
}
$gpuLayers = $(if ($arm) { 0 } else { 99 })

# ---------- 1. Python（免安裝版）與 OpenCC（簡轉繁） ----------
Say "`n[1/4] Python" 'Cyan'
$pyZip = Join-Path $DL "python-$PY_VER-embed-$(if ($arm) { 'arm64' } else { 'amd64' }).zip"
Get-File "https://www.python.org/ftp/python/$PY_VER/$(Split-Path -Leaf $pyZip)" $pyZip
if (-not (Test-Path "$RT\python\python.exe")) { Expand-To $pyZip "$RT\python" }
if (-not (Test-Path "$RT\pylib\opencc")) {
    try {
        $whl = (Invoke-RestMethod 'https://pypi.org/pypi/opencc-python-reimplemented/json' -TimeoutSec 20).urls | Where-Object packagetype -eq 'bdist_wheel' | Select-Object -First 1
        $wZipO = Join-Path $DL 'opencc.zip'                      # wheel 就是 zip，純 Python
        Get-File $whl.url $wZipO
        Expand-Archive -Path $wZipO -DestinationPath "$RT\pylib" -Force
    } catch { Say '  OpenCC 下載失敗（沒關係，會用內建的簡易簡轉繁）' 'Yellow' }
}

# ---------- 2. ffmpeg（轉檔、錄音） ----------
Say "`n[2/4] ffmpeg" 'Cyan'
$ffName = "ffmpeg-master-latest-$(if ($arm) { 'winarm64' } else { 'win64' })-gpl.zip"
$ffZip = Join-Path $DL $ffName
Get-File "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/$ffName" $ffZip
if (-not (Test-Path "$RT\ffmpeg")) { Expand-To $ffZip "$RT\ffmpeg" }
$ffDir = Find-ExeDir "$RT\ffmpeg" 'ffmpeg.exe'

# ---------- 3. 語音辨識 ----------
Say "`n[3/4] 語音辨識模型" 'Cyan'
Say '  1. TEA-ASR-1.1（推薦；台灣華語、中英夾雜最準，自己會加標點；約 2.5 GB）'
Say '  2. Breeze-ASR-25（whisper 系；約 1.7 GB，需要另外用 AI 加標點）'
$asrChoice = Ask '選擇（直接按 Enter = 1）' 'SETUP_ASR'
if (-not $asrChoice) { $asrChoice = '1' }
$cfgWhisper = @{}
if ($asrChoice -eq '2') {
    $wName = $(if ($arm) { 'whisper-bin-win-cpu-arm64.zip' } else { 'whisper-bin-x64.zip' })
    $wZip = Join-Path $DL $wName
    Get-File "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/$wName" $wZip
    if (-not (Test-Path "$RT\whisper")) { Expand-To $wZip "$RT\whisper" }
    $wDir = Find-ExeDir "$RT\whisper" 'whisper-cli.exe'
    Get-File 'https://huggingface.co/BRlin/Breeze-ASR-25-ggml/resolve/main/ggml-breeze-q8_0.bin' "$MD\ggml-breeze-asr-25-q8_0.bin"
    Get-File 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin' "$MD\ggml-silero-v5.1.2.bin"
    $cfgWhisper = @{ whisper_dir = (Rel $wDir); asr_model = 'models\ggml-breeze-asr-25-q8_0.bin'; vad_model = 'models\ggml-silero-v5.1.2.bin' }
    $asr = @{ engine = 'whisper' }
} else {
    $useNpu = $false
    if ($hasGeniex) {
        $a = Ask '  這台有 GenieX，語音辨識要跑在 NPU 上嗎？（比較快、省電）(Y/n)' 'SETUP_NPU'
        $useNpu = ($a -ne 'n' -and $a -ne 'N')
    }
    if ($useNpu) {
        # 放進 GenieX 的模型資料夾，並寫 geniex.json，GenieX 才認得（語音編碼器跑 CPU、解碼器跑 NPU）
        $gxRoot = $(if ($env:GENIEX_DATADIR) { $env:GENIEX_DATADIR } else { Join-Path $HOME '.cache\geniex' })
        $gxDir = Join-Path $gxRoot 'models\mradermacher\TEA-ASR-1.1-GGUF'
        New-Item -ItemType Directory -Force $gxDir | Out-Null
        Get-File "$TEA_REPO/$TEA_GGUF" (Join-Path $gxDir $TEA_GGUF)
        Get-File "$TEA_REPO/$TEA_MMPROJ" (Join-Path $gxDir $TEA_MMPROJ)
        $manifest = [ordered]@{
            Name = 'mradermacher/TEA-ASR-1.1-GGUF'; ModelName = 'TEA-ASR-1.1'; ModelType = 'vlm'; PluginId = 'llama_cpp'
            ModelFile = @{ Q8_0 = @{ Name = $TEA_GGUF; Downloaded = $true; Size = (Get-Item (Join-Path $gxDir $TEA_GGUF)).Length } }
            MMProjFile = @{ Name = $TEA_MMPROJ; Downloaded = $true; Size = (Get-Item (Join-Path $gxDir $TEA_MMPROJ)).Length }
            TokenizerFile = @{ Name = ''; Downloaded = $false; Size = 0 }; ExtraFiles = @()
        }
        $mf = Join-Path $gxDir 'geniex.json'
        if (-not (Test-Path $mf)) { [IO.File]::WriteAllText($mf, ($manifest | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding $false)) }
        # NPU 偶爾會有一段辨識不出來，用 CPU 的 llama.cpp 補（約 30 MB）
        $asr = @{ engine = 'geniex'; model = 'mradermacher/TEA-ASR-1.1-GGUF:Q8_0' }
        try { $asr.fallback_server = (Rel (Join-Path (Get-Llama) 'llama-server.exe')) } catch { Say '  llama.cpp 下載失敗，略過 CPU 補辨識' 'Yellow' }
    } else {
        $lDir = Get-Llama
        Get-File "$TEA_REPO/$TEA_GGUF" "$MD\$TEA_GGUF"
        Get-File "$TEA_REPO/$TEA_MMPROJ" "$MD\$TEA_MMPROJ"
        $asr = @{ engine = 'llama-server'; server = (Rel (Join-Path $lDir 'llama-server.exe')); gguf = "models\$TEA_GGUF"; mmproj = "models\$TEA_MMPROJ"
                  port = 18487; gpu_layers = $gpuLayers }
    }
}

# ---------- 3b. 分辨講者（sherpa-onnx，CPU；約 60 MB） ----------
$diar = Ask "`n  要自動分辨講者嗎？（逐字稿會標出老師、學生；教室收音不好時不一定準；約 60 MB）(y/N)" 'SETUP_DIAR'
$useDiar = ($diar -eq 'y' -or $diar -eq 'Y')
if ($useDiar) {
    try {
        $plat = $(if ($arm) { 'win_arm64' } else { 'win_amd64' })
        if (-not (Test-Path "$RT\pylib\sherpa_onnx\lib")) {
            foreach ($pkg in @('sherpa-onnx', 'sherpa-onnx-core')) {
                $urls = (Invoke-RestMethod "https://pypi.org/pypi/$pkg/$SHERPA_VER/json" -TimeoutSec 20).urls
                $whl = $urls | Where-Object { $_.filename -like "*-$plat.whl" -and ($_.filename -like '*-cp312-*' -or $_.filename -like '*-py3-none-*') } | Select-Object -First 1
                if (-not $whl) { throw "找不到 $pkg 的 $plat 版本" }
                $wz = Join-Path $DL "$pkg-$SHERPA_VER-$plat.zip"          # wheel 就是 zip，裡面已經有編好的 DLL
                Get-File $whl.url $wz
                Expand-Archive -Path $wz -DestinationPath "$RT\pylib" -Force
            }
            Remove-Item "$RT\pylib\sherpa_onnx_core-*.data" -Recurse -Force -ErrorAction SilentlyContinue   # 同樣的 DLL 已經在 sherpa_onnx\lib
        }
        $dd = "$MD\diar"; New-Item -ItemType Directory -Force $dd | Out-Null
        if (-not (Test-Path "$dd\sherpa-onnx-pyannote-segmentation-3-0\model.onnx")) {
            $sz = Join-Path $DL 'pyannote-seg.tar.bz2'
            Get-File 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2' $sz
            & "$env:SystemRoot\System32\tar.exe" -xjf $sz -C $dd          # Windows 10 以後內建 tar（bsdtar），會解 .tar.bz2
            if ($LASTEXITCODE -ne 0) { throw '解壓縮 pyannote 模型失敗' }
        }
        Get-File 'https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx' "$dd\3dspeaker_eres2net_base_zh.onnx"
    } catch {
        Say "  分辨講者的元件下載失敗，先略過（之後可以重新執行安裝）：$_" 'Yellow'
        $useDiar = $false
    }
}

# ---------- 4. AI 模型（整理筆記、提問；Breeze 還要加標點） ----------
Say "`n[4/4] AI 模型（整理筆記、提問用）" 'Cyan'
Say '  1. Gemma 4 E4B（推薦；約 4.8 GB，建議 16 GB 記憶體）'
Say '  2. Gemma 4 E2B（輕量；約 3 GB，8 GB 記憶體也能用，筆記品質差一點）'
Say '  3. 用電腦上已經裝好的 Ollama 或 LM Studio'
if ($hasGeniex) { Say '  4. 用 GenieX（這台有裝，Snapdragon NPU 最省電）' }
Say '  5. 不用 AI，只做逐字稿'
$default = $(if ($hasGeniex) { '4' } elseif ($ramGB -lt 12) { '2' } else { '1' })
$choice = Ask "選擇（直接按 Enter = $default）" 'SETUP_LLM'
if (-not $choice) { $choice = $default }

$llm = @{ mode = 'none' }
switch ($choice) {
    { $_ -in '1', '2' } {
        $lDir = Get-Llama
        if ($_ -eq '1') {
            $gguf = "$MD\gemma-4-E4B-it-Q4_0.gguf"
            Get-File 'https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_0.gguf' $gguf
        } else {
            $gguf = "$MD\gemma-4-E2B-it-Q4_0.gguf"
            Get-File 'https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_0.gguf' $gguf
        }
        $llm = @{ mode = 'llama-server'; server = (Rel (Join-Path $lDir 'llama-server.exe')); gguf = (Rel $gguf); port = 18486; ctx = 8192; gpu_layers = $gpuLayers }
    }
    '3' {
        $url = ''
        foreach ($u in 'http://127.0.0.1:11434', 'http://127.0.0.1:1234') {
            try { Invoke-RestMethod "$u/v1/models" -TimeoutSec 2 | Out-Null; $url = $u; break } catch {}
        }
        if ($url) { Say "  找到 $url" 'Green' } else { Say '  沒有偵測到 Ollama（11434）或 LM Studio（1234），請先打開它們。' 'Yellow'; $url = 'http://127.0.0.1:11434' }
        $u2 = Read-Host "  伺服器網址（Enter = $url）"; if ($u2) { $url = $u2.TrimEnd('/') }
        try { (Invoke-RestMethod "$url/v1/models" -TimeoutSec 3).data | ForEach-Object { Say "    - $($_.id)" 'DarkGray' } } catch {}
        $m = Read-Host '  要用的模型名稱（Enter = 伺服器上的第一個）'
        $llm = @{ mode = 'openai'; url = $url; model = $m }
    }
    '4' {
        Say '  （模型要先用 geniex pull 下載，例如：geniex pull unsloth/gemma-4-E4B-it-GGUF）' 'DarkGray'
        $m = Read-Host '  GenieX 模型 id（Enter = unsloth/gemma-4-E4B-it-GGUF:Q4_0）'
        $llm = @{ mode = 'geniex'; model = $(if ($m) { $m } else { 'unsloth/gemma-4-E4B-it-GGUF:Q4_0' }) }
    }
    default { $llm = @{ mode = 'none' } }
}

# ---------- 寫設定檔 ----------
$cfg = [ordered]@{
    ffmpeg       = (Rel (Join-Path $ffDir 'ffmpeg.exe'))
    ffprobe      = (Rel (Join-Path $ffDir 'ffprobe.exe'))
    lectures_dir = '課堂'
    port         = 18484
    asr          = $asr
    llm          = $llm
    diarize      = $useDiar
}
foreach ($k in $cfgWhisper.Keys) { $cfg[$k] = $cfgWhisper[$k] }
[IO.File]::WriteAllText((Join-Path $Root 'config.json'), ($cfg | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))

# ---------- 測試程式能不能跑 ----------
Say "`n測試程式…" 'Cyan'
$vc = "https://aka.ms/vs/17/release/vc_redist.$(if ($arm) { 'arm64' } else { 'x64' }).exe"
$ErrorActionPreference = 'Continue'                        # 5.1 版會把程式寫到 stderr 的文字當成錯誤
$tests = @()
if ($asr.engine -eq 'whisper') { $tests += , @('whisper.cpp', (Join-Path $wDir 'whisper-cli.exe'), '--help') }
if ($script:lDir) { $tests += , @('llama.cpp', (Join-Path $script:lDir 'llama-server.exe'), '--version') }
foreach ($t in $tests) {
    & $t[1] $t[2] 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) { Say "  $($t[0]) 無法執行（可能缺少 Visual C++ 執行階段），請安裝：$vc" 'Yellow' } else { Say "  $($t[0]) OK" 'Green' }
}
if ($useDiar) {
    & "$RT\python\python.exe" -c "import sys; sys.path.insert(0, sys.argv[1]); import sherpa_onnx" "$RT\pylib" 2>$null
    if ($LASTEXITCODE -ne 0) { Say "  分辨講者 無法執行（可能缺少 Visual C++ 執行階段），請安裝：$vc" 'Yellow' } else { Say '  分辨講者 OK' 'Green' }
}
$ErrorActionPreference = 'Stop'

$ans = Ask "`n要在桌面建立「上課筆記」捷徑嗎？(Y/n)" 'SETUP_SHORTCUT'
if ($ans -ne 'n' -and $ans -ne 'N') {
    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '上課筆記.lnk'))
    $lnk.TargetPath = Join-Path $Root '上課筆記.bat'; $lnk.WorkingDirectory = $Root; $lnk.Save()
}

$ans = Ask "`n刪除下載的安裝檔（zip）來省空間嗎？模型不會刪 (Y/n)" 'SETUP_CLEAN'
if ($ans -ne 'n' -and $ans -ne 'N') { Get-ChildItem $DL -Include *.zip, *.bz2 -Recurse | Remove-Item -Force }

Say "`n安裝完成！點兩下「上課筆記.bat」（或桌面捷徑）就能開始用。`n" 'Green'
