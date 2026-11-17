#!/usr/bin/env bash
# 上課筆記 安裝程式（Linux）：把 llama.cpp、模型下載到這個資料夾，寫出 config.json。
# 只會動這個資料夾裡的 runtime/、models/，不會改系統設定。Windows 請用 安裝.bat。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$ROOT"
RT=runtime; MD=models; DL=$RT/_download
mkdir -p "$RT" "$MD" "$DL"

LLAMA_TAG=b11976
TEA_REPO=https://huggingface.co/mradermacher/TEA-ASR-1.1-GGUF/resolve/main
TEA_GGUF=TEA-ASR-1.1.Q8_0.gguf; TEA_MMPROJ=TEA-ASR-1.1.mmproj-f16.gguf
SHERPA_VER=2.0.1

say()  { printf '%b\n' "$1"; }
head_() { printf '\n\033[36m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
# 測試用：環境變數可以預先回答問題（SETUP_GPU、SETUP_DIAR、SETUP_LLM）
ask() { local v="${!2:-}"; if [ -n "$v" ]; then say "$1 $v" >&2; echo "$v"; else read -r -p "$1 " v || true; echo "$v"; fi; }
get() {   # get <url> <dest>：已經有就跳過，可續傳
    if [ -s "$2" ]; then say "  已經有 $(basename "$2")，跳過"; return; fi
    say "  下載 $(basename "$2")"
    curl -L --fail --retry 3 -C - -o "$2.part" "$1"
    mv -f "$2.part" "$2"
}

head_ '=== 上課筆記 安裝（Linux） ==='

# ---------- 偵測電腦、檢查需要的程式 ----------
case "$(uname -m)" in
    x86_64|amd64) ARCH=x64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) warn "不支援的 CPU：$(uname -m)（只支援 x86_64 和 arm64）"; exit 1 ;;
esac
RAM_GB=$(awk '/MemTotal/ { printf "%d", $2 / 1048576 + 0.5 }' /proc/meminfo 2>/dev/null || echo 0)
say "電腦：$ARCH，記憶體 ${RAM_GB} GB"
[ "$RAM_GB" -gt 0 ] && [ "$RAM_GB" -lt 8 ] && warn '注意：記憶體少於 8 GB，語音辨識和 AI 模型可能跑不動。'

missing=()
for c in python3 ffmpeg ffprobe curl tar; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
    warn "缺少：${missing[*]}，請先安裝再重新執行："
    say '  Debian／Ubuntu：sudo apt install python3 ffmpeg curl tar'
    say '  Fedora：        sudo dnf install python3 ffmpeg curl tar'
    say '  Arch：          sudo pacman -S python ffmpeg curl tar'
    exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { warn '需要 Python 3.9 以上'; exit 1; }

# ---------- 1. llama.cpp（語音辨識和 AI 模型都用它） ----------
head_ '[1/4] llama.cpp'
GPU_LAYERS=0; BUILD="ubuntu-$ARCH"
a=$(ask '  有支援 Vulkan 的顯示卡，要用 GPU 加速嗎？（跑不起來程式會自動改用 CPU）(y/N)' SETUP_GPU)
if [ "$a" = y ] || [ "$a" = Y ]; then BUILD="ubuntu-vulkan-$ARCH"; GPU_LAYERS=99; fi
LNAME="llama-$LLAMA_TAG-bin-$BUILD.tar.gz"
get "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/$LNAME" "$DL/$LNAME"
if [ ! -d "$RT/llama" ] || [ -z "$(find "$RT/llama" -name llama-server -type f 2>/dev/null | head -1)" ]; then
    rm -rf "$RT/llama.tmp"; mkdir -p "$RT/llama.tmp"
    tar -xzf "$DL/$LNAME" -C "$RT/llama.tmp"
    rm -rf "$RT/llama"; mv "$RT/llama.tmp" "$RT/llama"
fi
LLAMA_SERVER=$(find "$RT/llama" -name llama-server -type f | head -1)
[ -n "$LLAMA_SERVER" ] || { warn '解壓縮後找不到 llama-server'; exit 1; }
chmod +x "$LLAMA_SERVER"

# ---------- 2. OpenCC（簡轉繁；純 Python，直接解壓縮 wheel） ----------
head_ '[2/4] OpenCC'
python3 - "$RT/pylib" <<'PY' || warn '  OpenCC 下載失敗（沒關係，會用內建的簡易簡轉繁）'
import sys, os, json, io, zipfile, urllib.request
dest = sys.argv[1]
if os.path.isdir(os.path.join(dest, 'opencc')): print('  已經有 OpenCC，跳過'); sys.exit(0)
urls = json.load(urllib.request.urlopen('https://pypi.org/pypi/opencc-python-reimplemented/json', timeout=20))['urls']
whl = next(u for u in urls if u['packagetype'] == 'bdist_wheel')
print('  下載', whl['filename'])
zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(whl['url'], timeout=120).read())).extractall(dest)
PY

# ---------- 3. 語音辨識（TEA-ASR-1.1，約 2.5 GB） ----------
head_ '[3/4] 語音辨識模型 TEA-ASR-1.1（約 2.5 GB）'
get "$TEA_REPO/$TEA_GGUF" "$MD/$TEA_GGUF"
get "$TEA_REPO/$TEA_MMPROJ" "$MD/$TEA_MMPROJ"

# ---------- 3b. 分辨講者（選用） ----------
USE_DIAR=false
a=$(ask $'\n  要自動分辨講者嗎？（逐字稿會標出老師、學生；教室收音不好時不一定準；約 60 MB）(y/N)' SETUP_DIAR)
if [ "$a" = y ] || [ "$a" = Y ]; then
    if python3 - "$RT/pylib" "$MD/diar" "$SHERPA_VER" <<'PY'
import sys, os, json, io, zipfile, tarfile, platform, shutil, urllib.request
dest, mdir, ver = sys.argv[1:4]
def fetch(url):
    print('  下載', url.rsplit('/', 1)[-1], flush=True)
    return urllib.request.urlopen(url, timeout=600).read()
if not os.path.isdir(os.path.join(dest, 'sherpa_onnx', 'lib')):
    arch = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}[platform.machine().lower()]
    cp = f'cp{sys.version_info[0]}{sys.version_info[1]}'
    for pkg in ('sherpa-onnx', 'sherpa-onnx-core'):
        urls = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{pkg}/{ver}/json', timeout=20))['urls']
        ok = [u for u in urls if u['filename'].endswith('.whl') and 'manylinux' in u['filename'] and arch in u['filename']
              and (f'-{cp}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not ok: raise SystemExit(f'  找不到 {pkg} 給 Python {cp}／{arch} 的版本')
        zipfile.ZipFile(io.BytesIO(fetch(ok[0]['url']))).extractall(dest)
    for d in os.listdir(dest):                      # wheel 的 .data 目錄裡是重複的函式庫，不需要
        if d.startswith('sherpa_onnx_core-') and d.endswith('.data'): shutil.rmtree(os.path.join(dest, d), ignore_errors=True)
os.makedirs(mdir, exist_ok=True)
if not os.path.exists(os.path.join(mdir, 'sherpa-onnx-pyannote-segmentation-3-0', 'model.onnx')):
    data = fetch('https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2')
    tarfile.open(fileobj=io.BytesIO(data), mode='r:bz2').extractall(mdir)
emb = os.path.join(mdir, '3dspeaker_eres2net_base_zh.onnx')
if not os.path.exists(emb):
    data = fetch('https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx')
    open(emb, 'wb').write(data)
sys.path.insert(0, dest)
import sherpa_onnx
print('  分辨講者 OK')
PY
    then USE_DIAR=true; else warn '  分辨講者的元件裝不起來，先略過（之後可以重新執行安裝）'; fi
fi

# ---------- 4. AI 模型（整理筆記、提問） ----------
head_ '[4/4] AI 模型（整理筆記、提問用）'
say '  1. Gemma 4 E4B（推薦；約 4.8 GB，建議 16 GB 記憶體）'
say '  2. Gemma 4 E2B（輕量；約 3 GB，8 GB 記憶體也能用，筆記品質差一點）'
say '  3. 用電腦上已經在跑的 Ollama 或 LM Studio'
say '  4. 不用 AI，只做逐字稿'
DEF=1; [ "$RAM_GB" -gt 0 ] && [ "$RAM_GB" -lt 12 ] && DEF=2
choice=$(ask "選擇（直接按 Enter = $DEF）" SETUP_LLM); choice=${choice:-$DEF}
LLM_MODE=none; LLM_GGUF=; LLM_URL=; LLM_MODEL=
case "$choice" in
    1) LLM_GGUF="$MD/gemma-4-E4B-it-Q4_0.gguf"; LLM_MODE=llama-server
       get 'https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_0.gguf' "$LLM_GGUF" ;;
    2) LLM_GGUF="$MD/gemma-4-E2B-it-Q4_0.gguf"; LLM_MODE=llama-server
       get 'https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_0.gguf' "$LLM_GGUF" ;;
    3) LLM_MODE=openai; LLM_URL=http://127.0.0.1:11434
       for u in http://127.0.0.1:11434 http://127.0.0.1:1234; do
           if curl -s -m 2 "$u/v1/models" >/dev/null 2>&1; then LLM_URL=$u; say "  找到 $u"; break; fi
       done
       u2=$(ask "  伺服器網址（Enter = $LLM_URL）" SETUP_LLM_URL); [ -n "$u2" ] && LLM_URL=${u2%/}
       LLM_MODEL=$(ask '  要用的模型名稱（Enter = 伺服器上的第一個）' SETUP_LLM_MODEL) ;;
    *) LLM_MODE=none ;;
esac

# ---------- 寫設定檔 ----------
LLAMA_SERVER="$LLAMA_SERVER" TEA_GGUF="$MD/$TEA_GGUF" TEA_MMPROJ="$MD/$TEA_MMPROJ" GPU_LAYERS="$GPU_LAYERS" USE_DIAR="$USE_DIAR" \
LLM_MODE="$LLM_MODE" LLM_GGUF="$LLM_GGUF" LLM_URL="$LLM_URL" LLM_MODEL="$LLM_MODEL" python3 - <<'PY'
import os, json
e = os.environ; gpu = int(e['GPU_LAYERS'])
llm = {'mode': e['LLM_MODE']}
if llm['mode'] == 'llama-server': llm.update(server=e['LLAMA_SERVER'], gguf=e['LLM_GGUF'], port=18486, ctx=8192, gpu_layers=gpu)
if llm['mode'] == 'openai': llm.update(url=e['LLM_URL'], model=e['LLM_MODEL'])
cfg = {'lectures_dir': '課堂', 'port': 18484,
       'asr': {'engine': 'llama-server', 'server': e['LLAMA_SERVER'], 'gguf': e['TEA_GGUF'], 'mmproj': e['TEA_MMPROJ'], 'port': 18487, 'gpu_layers': gpu},
       'llm': llm, 'diarize': e['USE_DIAR'] == 'true'}
json.dump(cfg, open('config.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=4)
PY

# ---------- 測試程式能不能跑 ----------
head_ '測試程式…'
if "$LLAMA_SERVER" --version >/dev/null 2>&1; then say '  llama.cpp OK'
else warn '  llama.cpp 無法執行：系統的 glibc 可能太舊（官方版本是在 Ubuntu 22.04 上編譯的），或缺少 libgomp1（sudo apt install libgomp1）'; fi
chmod +x "$ROOT/lecture.sh" 2>/dev/null || true

say $'\n\033[32m安裝完成！執行 ./lecture.sh 就能開始用（瀏覽器開 http://127.0.0.1:18484/）。\033[0m\n'
