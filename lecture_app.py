# 上課筆記網頁的後端：匯入音檔 → TEA-ASR-1.1（NPU 或 llama.cpp，自帶標點）或 Breeze-ASR-25（whisper.cpp）逐字稿 →（Breeze 才需要）Gemma 加標點 → Gemma 整理筆記
# 另外會讀 Windows 的 Energy Meter，顯示即時功耗、記錄每個步驟用了多少電
# usage: python lecture_app.py   （然後開 http://127.0.0.1:18484/）
# 資料都放在 課堂\<日期_時間_課名>\：錄音檔、逐字稿.txt、逐字稿_標點.txt、筆記.md、meta.json
# live_lecture.py 即時錄的課也在同一個資料夾，網頁裡一樣看得到、可以再加標點和整理筆記。
import datetime, json, math, mimetypes, os, queue, re, subprocess, sys, threading, time, traceback, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(ROOT, 'runtime', 'pylib')): sys.path.insert(0, os.path.join(ROOT, 'runtime', 'pylib'))   # 安裝程式放的 OpenCC
# 設定檔（可攜版由 安裝.bat 產生）；沒有設定檔就用這台電腦原本的路徑和 GenieX
CFG = json.loads(open(os.path.join(ROOT, 'config.json'), encoding='utf-8-sig').read()) if os.path.exists(os.path.join(ROOT, 'config.json')) else {}
def rel(v): return v if os.path.isabs(v) else os.path.join(ROOT, v)      # 設定檔裡的相對路徑都以程式所在資料夾為準
def cfg_path(key, default): return rel(CFG.get(key) or default)
# whisper.cpp 讀模型時不支援含中文的路徑（例如資料夾叫「上課筆記可攜版」會直接當掉）：
# 子程式一律在 ROOT 執行，ROOT 底下的檔案改傳相對路徑（models\…）
def arg(p): return os.path.relpath(p, ROOT) if os.path.abspath(p).lower().startswith(ROOT.lower() + os.sep) else p
LECT = os.environ.get('LECTURE_DIR') or cfg_path('lectures_dir', '課堂')     # 測試時可以換資料夾
WHISPER_DIR = cfg_path('whisper_dir', os.path.join('cpu', 'Release'))
EXE = '.exe' if os.name == 'nt' else ''          # Linux、Termux 的執行檔沒有副檔名
CLI = os.path.join(WHISPER_DIR, 'whisper-cli' + EXE)
MODEL = cfg_path('asr_model', os.path.join('models', 'ggml-breeze-asr-25-q8_0.bin'))
VAD_MODEL = cfg_path('vad_model', os.path.join('models', 'ggml-silero-v5.1.2.bin'))
FFMPEG = cfg_path('ffmpeg', 'ffmpeg') if CFG.get('ffmpeg') else 'ffmpeg'
FFPROBE = cfg_path('ffprobe', 'ffprobe') if CFG.get('ffprobe') else 'ffprobe'
THREADS = str(CFG.get('threads') or max(4, min(12, (os.cpu_count() or 8) - 2)))
PORT = int(os.environ.get('LECTURE_PORT') or CFG.get('port') or 18484)
# AI 模型：geniex（Snapdragon NPU）｜llama-server（可攜版自帶）｜openai（Ollama、LM Studio 等已經在跑的伺服器）｜none（只做逐字稿）
LLM_CFG = CFG.get('llm') or {'mode': 'geniex'}
LLM_MODE = LLM_CFG.get('mode', 'geniex')
LLM_MODEL = LLM_CFG.get('model') or ('unsloth/gemma-4-E4B-it-GGUF:Q4_0' if LLM_MODE == 'geniex' else '')
GENIEX_PORTS = [18181, 18282]           # 先用使用者自己的伺服器；沒開才自己開 18282
# 語音辨識：geniex = TEA-ASR-1.1 跑在 Snapdragon NPU（GenieX）｜llama-server = TEA-ASR-1.1 用 llama.cpp 跑（CPU／GPU，任何電腦）
#           whisper = Breeze-ASR-25（whisper.cpp，CPU）
# 這台（沒有 config.json）預設 NPU；可攜版的 config.json 沒寫就維持 whisper。環境變數 LECTURE_ASR 可以臨時切換。
ASR_CFG = CFG.get('asr') or {}
ASR_ENGINE = os.environ.get('LECTURE_ASR') or ASR_CFG.get('engine') or ('whisper' if CFG else 'geniex')
ASR_MODEL_ID = ASR_CFG.get('model') or 'mradermacher/TEA-ASR-1.1-GGUF:Q8_0'
ASR_GX_PORT = int(ASR_CFG.get('port') or 18282)   # 固定用自己開的伺服器：它有加 --vit-compute CPU（語音編碼器放 NPU 會當掉）
TEA = ASR_ENGINE in ('geniex', 'llama-server')
ASR_TAG = {'geniex': '（NPU）', 'llama-server': '（TEA）'}.get(ASR_ENGINE, '')
PIPELINE = ['asr', 'notes'] if TEA else ['asr', 'punct', 'notes']      # TEA-ASR 自己會加標點，不用再跑「加標點」
                                                                        # 有分辨講者時，筆記前面再插一步 spk（見 pipeline()）
AUDIO_EXT = {'.m4a', '.mp3', '.wav', '.ogg', '.opus', '.webm', '.flac', '.aac', '.mp4', '.mkv', '.mov', '.wma'}
LINE_RX = re.compile(r'^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.*)$')
os.makedirs(LECT, exist_ok=True)

# ---------- 小工具 ----------
def hms(sec):
    sec = int(sec); return f'{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}'

def read(path, default=''):
    try: return open(path, encoding='utf-8').read()
    except OSError: return default

def write(path, text):
    tmp = path + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(text); os.replace(tmp, path)

def meta_of(lid):
    m = json.loads(read(os.path.join(LECT, lid, 'meta.json'), '{}') or '{}')
    m.setdefault('name', lid.split('_', 2)[2] if lid.count('_') >= 2 else '')
    m.setdefault('keywords', '')
    return m

def save_meta(lid, m): write(os.path.join(LECT, lid, 'meta.json'), json.dumps(m, ensure_ascii=False, indent=1))

def audio_of(lid):
    d = os.path.join(LECT, lid)
    for f in sorted(os.listdir(d)):
        if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith('_'): return f
    return None

def safe_id(lid):
    return lid if lid and '/' not in lid and '\\' not in lid and '..' not in lid and os.path.isdir(os.path.join(LECT, lid)) else None

# ---------- 功耗（Windows 的 Energy Meter 計數器，單位 mW；Snapdragon 會分 CPU／GPU／NPU／記憶體…） ----------
class PowerMeter:
    RAILS = ('system', 'soc', 'cpu_cluster_0', 'cpu_cluster_1', 'cpu_cluster_2', 'gpu', 'npu', 'memory', 'infra', 'multimedia')
    # system（整機）＝ soc（晶片）＋ 晶片外（螢幕、SSD、Wi-Fi、電源轉換…）；soc ＝ CPU＋NPU＋GPU＋記憶體＋infra（晶片內部匯流排、快取等）＋multimedia

    def __init__(self):
        self.ok, self.now, self.energy, self.err = False, {}, {}, ''      # now：瓦；energy：從程式啟動累積的焦耳
        try:
            import ctypes
            from ctypes import c_void_p, c_ulong, c_double, c_wchar_p, byref, POINTER, Structure
            class Val(Structure): _fields_ = [('status', c_ulong), ('value', c_double)]
            pdh = ctypes.WinDLL('pdh.dll')
            pdh.PdhOpenQueryW.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p)]
            pdh.PdhAddEnglishCounterW.argtypes = [c_void_p, c_wchar_p, c_void_p, POINTER(c_void_p)]
            pdh.PdhCollectQueryData.argtypes = [c_void_p]
            pdh.PdhGetFormattedCounterValue.argtypes = [c_void_p, c_ulong, c_void_p, POINTER(Val)]
            q = c_void_p()
            if pdh.PdhOpenQueryW(None, None, byref(q)): raise OSError('PdhOpenQuery 失敗')
            self.h = {}
            for r in self.RAILS:
                h = c_void_p()
                if pdh.PdhAddEnglishCounterW(q, f'\\Energy Meter({r})\\Power', None, byref(h)) == 0: self.h[r] = h
            if 'system' not in self.h: raise OSError('這台電腦沒有 Energy Meter 計數器')
            self.pdh, self.q, self.Val, self.byref = pdh, q, Val, byref
            pdh.PdhCollectQueryData(q)
            self.energy = {r: 0.0 for r in self.h}; self.ok = True
            threading.Thread(target=self.run, daemon=True).start()
        except Exception as e:
            self.err = str(e)

    def run(self):
        last = time.time()
        while True:
            time.sleep(1)
            try:
                self.pdh.PdhCollectQueryData(self.q)
                t = time.time(); dt, last = t - last, t
                for r, h in self.h.items():
                    v = self.Val()
                    if self.pdh.PdhGetFormattedCounterValue(h, 0x200 | 0x8000, None, self.byref(v)) == 0:   # DOUBLE | NOCAP100
                        w = v.value / 1000; self.now[r] = w; self.energy[r] += w * dt
            except Exception as e:
                self.err = str(e)

    def watts(self):
        n = self.now
        if not n: return None
        soc = n.get('soc'); cpu = sum(n.get(f'cpu_cluster_{i}', 0) for i in range(3))
        known = cpu + sum(n.get(k, 0) for k in ('npu', 'gpu', 'memory'))
        return {'system': n.get('system'), 'soc': soc, 'npu': n.get('npu'), 'gpu': n.get('gpu'), 'memory': n.get('memory'), 'cpu': cpu,
                'soc_other': max(0, soc - known) if soc is not None else None,                  # 晶片內其他（infra、多媒體）
                'board': max(0, n['system'] - soc) if soc is not None and n.get('system') is not None else None}   # 晶片外

    def mark(self): return (time.time(), dict(self.energy))

    def since(self, mark):
        """從 mark() 到現在：秒數、整機平均瓦數、各部分用電（Wh）"""
        t0, e0 = mark; sec = time.time() - t0
        d = {r: (self.energy.get(r, 0) - e0.get(r, 0)) / 3600 for r in self.energy}
        wh = {'system': d.get('system', 0), 'soc': d.get('soc', 0), 'npu': d.get('npu', 0), 'gpu': d.get('gpu', 0),
              'cpu': sum(d.get(f'cpu_cluster_{i}', 0) for i in range(3)), 'memory': d.get('memory', 0)}
        return {'sec': round(sec), 'avg_w': round(wh['system'] * 3600 / sec, 1) if sec > 0 else 0, 'wh': {k: round(v, 3) for k, v in wh.items()}}

POWER = PowerMeter()
STEP_NAME = {'asr': '語音辨識', 'punct': '加標點', 'spk': '分辨講者', 'notes': '整理筆記', 'live': '即時錄音'}

def record_energy(lid, step, mark):
    if not POWER.ok: return
    e = POWER.since(mark); e['time'] = datetime.datetime.now().isoformat(timespec='seconds'); e['engine'] = ASR_ENGINE if step in ('asr', 'live') else LLM_MODE
    m = meta_of(lid); m.setdefault('energy', {})[step] = e; save_meta(lid, m)

# ---------- 工作佇列（一次只跑一個，CPU／NPU 不會互搶） ----------
JOBS = {}          # lid -> {'stage','progress','message','error','done'}
QUEUE, QLOCK = [], threading.Condition()

def set_job(lid, **kw):
    j = JOBS.setdefault(lid, {'stage': '', 'progress': 0, 'message': '', 'error': '', 'done': False})
    j.update(kw)

def enqueue(lid, steps):
    set_job(lid, stage='排隊中', progress=0, message='', error='', done=False)
    with QLOCK: QUEUE.append((lid, steps)); QLOCK.notify()

def worker():
    while True:
        with QLOCK:
            while not QUEUE: QLOCK.wait()
            lid, steps = QUEUE.pop(0)
        try:
            if 'asr' in steps:                # 即時錄音時 CPU 已經在跑辨識，匯入的音檔等錄完再處理
                while LIVE['sess']:
                    set_job(lid, stage='排隊中', message='等即時錄音結束'); time.sleep(2)
            if LLM_MODE == 'none': steps = [s for s in steps if s in ('asr', 'spk')]      # 沒有 AI 模型就只做逐字稿
            for s in steps:
                mark = POWER.mark()
                try: {'asr': run_asr_tea if TEA else run_asr, 'punct': run_punct, 'spk': run_spk, 'notes': run_notes}[s](lid)
                except Exception as e:
                    if s != 'spk': raise
                    traceback.print_exc(); print('分辨講者失敗，略過：', e, flush=True); continue     # 分不出講者也不影響筆記
                record_energy(lid, s, mark)
            set_job(lid, stage='完成', progress=100, done=True)
        except Exception as e:
            traceback.print_exc()
            set_job(lid, stage='失敗', error=str(e), done=True)

# ---------- 1. 語音辨識（whisper-cli，邊辨識邊寫進逐字稿.txt） ----------
def run_asr(lid):
    d = os.path.join(LECT, lid); m = meta_of(lid); src = audio_of(lid)
    if not src: raise RuntimeError('找不到音檔')
    set_job(lid, stage='轉檔', progress=0, message='把音檔轉成 16 kHz')
    wav = os.path.join(ROOT, 'tmp', 'asr_16k.wav'); os.makedirs(os.path.dirname(wav), exist_ok=True)   # 英文路徑，whisper 才讀得到
    subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', os.path.join(d, src), '-ac', '1', '-ar', '16000', wav], check=True)
    dur = float(subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', wav], capture_output=True, text=True).stdout.strip() or 0)
    m['duration'] = dur; save_meta(lid, m)
    set_job(lid, stage='語音辨識', progress=0, message=f'音檔長 {hms(dur)}')
    cmd = [CLI, '-m', arg(MODEL), '-f', arg(wav), '-l', 'zh', '-t', THREADS, '-sns', '--vad', '-vm', arg(VAD_MODEL)]
    if m['keywords'].strip(): cmd += ['--prompt', m['keywords'].strip()]
    out_path = os.path.join(d, '逐字稿.txt'); lines = []
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT)
    seg_rx = re.compile(r'^\[(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)\]\s*(.*)$')
    t0 = time.time(); last_end, cur_len = -99.0, 0
    for raw in p.stdout:
        s = raw.decode('utf-8', 'replace').strip()
        mm = seg_rx.match(s)
        if not mm: continue
        start = int(mm[1]) * 3600 + int(mm[2]) * 60 + float(mm[3]); end = int(mm[4]) * 3600 + int(mm[5]) * 60 + float(mm[6])
        text = mm[7].strip()
        if not text: continue
        # VAD 會把一句話切得很碎：停頓不到 1.5 秒、而且這行還不到 45 字，就接在同一行
        if lines and start - last_end < 1.5 and cur_len + len(text) <= 45:
            lines[-1] += ' ' + text; cur_len += len(text)
        else:
            lines.append(f'[{hms(start)}] {text}'); cur_len = len(text)
        last_end = end
        write(out_path, '\n'.join(lines) + '\n')
        pct = min(99, int(start / dur * 100)) if dur else 0
        el = time.time() - t0; eta = el / max(start, 1) * (dur - start)
        set_job(lid, progress=pct, message=f'已辨識到 {hms(start)} / {hms(dur)}，約剩 {int(eta // 60)} 分 {int(eta % 60)} 秒')
    if p.wait() != 0: raise RuntimeError('whisper 辨識失敗')
    try: os.remove(wav)
    except OSError: pass
    if not lines: raise RuntimeError('沒有辨識出任何文字（音檔可能沒有人聲）')

# ---------- 1b. 語音辨識（TEA-ASR-1.1 在 NPU：依停頓切段，每段送 GenieX） ----------
RATE, FRAME = 16000, 480            # 30 ms 一個 frame
try:
    import warnings; warnings.filterwarnings('ignore', category=DeprecationWarning)
    import audioop
    def rms16(b): return audioop.rms(b, 2)
except ImportError:                  # Python 3.13 以後沒有 audioop
    from array import array
    def rms16(b): a = array('h', b); return (sum(x * x for x in a) / max(1, len(a))) ** 0.5
try:
    from opencc import OpenCC
    to_trad = OpenCC('s2tw').convert       # TEA 偶爾會冒出簡體字（转、号）
except Exception:
    _S2T = str.maketrans('转号这们说时间问题会过还进对应该发现实际开关学习练级数据库网络设计机软处线储盘图书馆课报试讲师为个么样种从两长门见话语读写记让给结构链节点树层选择递归输错误认识准确动态环务质资讯统测验证执运编译码变类体档页队栈键标规划优势杂',
                         '轉號這們說時間問題會過還進對應該發現實際開關學習練級數據庫網絡設計機軟處線儲盤圖書館課報試講師為個麼樣種從兩長門見話語讀寫記讓給結構鏈節點樹層選擇遞歸輸錯誤認識準確動態環務質資訊統測驗證執運編譯碼變類體檔頁隊棧鍵標規劃優勢雜')
    def to_trad(s): return s.translate(_S2T)

PUA = re.compile('[-]')     # 這個 GGUF 會在某些字後面多吐一個私用區字元，直接濾掉

REPEAT = re.compile(r'(.{1,4}?)(?:\s*\1){5,}', re.S)

def asr_clean(t):
    t = PUA.sub('', t or '').replace('\r', ' ').replace('\n', ' ')
    # 前綴「language Chinese<asr_text>」；有時候標籤是中文（language中文），沒聲音時是 language None
    t = re.sub(r'^\s*language\s*(?:[A-Za-z]+|中文|英文|英語|粵語|日文|韓文)?\s*', '', t).replace('<asr_text>', '')
    t = REPEAT.sub(r'\1', t)          # 偶爾會卡住一直重複同一個字（你你你你…），留一個就好
    return to_trad(re.sub(r'\s+', ' ', t).strip().lstrip('。，,. '))

def wav_bytes(pcm):
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    return buf.getvalue()

def _tea_call(url, pcm, context, prefill=False):
    import base64
    msgs = [{'role': 'system', 'content': context or ''},
            {'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {'data': base64.b64encode(wav_bytes(pcm)).decode(), 'format': 'wav'}}]}]
    if prefill: msgs.append({'role': 'assistant', 'content': 'language Chinese<asr_text>'})
    sec = len(pcm) / 2 / RATE      # 一秒講不到 10 個字，限制長度，卡住重複時才不會一直吐
    body = {'model': ASR_MODEL_ID, 'temperature': 0, 'top_k': 1, 'max_tokens': min(600, int(sec * 12) + 40), 'stream': False, 'messages': msgs}
    req = urllib.request.Request(url + '/v1/chat/completions', json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=300))['choices'][0]['message'].get('content') or ''

LANG_ONLY = re.compile(r'^\s*language\s*(?!None)\S')

def _quietest(pcm):
    """中間三分之一裡最安靜的地方（切成兩半用），回傳 byte 位置"""
    n = len(pcm) // 2; best, at = None, n // 2
    for i in range(n // 3, 2 * n // 3 - FRAME, FRAME):
        v = rms16(pcm[i * 2:(i + FRAME) * 2])
        if best is None or v < best: best, at = v, i
    return at * 2

def tea_asr(pcm, context='', _depth=0):
    """一段 16 kHz 單聲道 PCM（最好 ≤ 30 秒）→ 文字。context 放關鍵字當熱詞（system 一定要有，不然模型只會吐出 language Chinese 就停）"""
    for attempt in range(3):          # NPU 偶爾會掛（休眠、過熱後），伺服器死掉就重開再試
        try:
            url = asr_server(); raw = _tea_call(url, pcm, context); break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if attempt == 2: raise RuntimeError(f'語音辨識失敗：{e}')
            time.sleep(3)
    text = asr_clean(raw)
    stuck = bool(REPEAT.search(PUA.sub('', raw)))        # 卡住一直重複同一個字（你你你你…），後面的內容會不見
    if (text and not stuck) or (not text and not LANG_ONLY.match(raw)): return text
    first = text
    # 有些段落只吐出「language Chinese」就結束（CPU、NPU 都會）。在 llama-server 上幫它把 <asr_text> 接上去再跑一次就好；
    # GenieX 不支援這樣接（會回 500），所以改用 CPU 的 llama-server 補這一段
    try:
        cpu = url if ASR_ENGINE == 'llama-server' else asr_cpu_server()
        if cpu:
            raw2 = _tea_call(cpu, pcm, context, prefill=True); text = asr_clean(raw2)
            if text and not REPEAT.search(PUA.sub('', raw2)): return text
    except Exception as e:
        print('CPU 補辨識失敗：', e, flush=True)
    if _depth < 1 and len(pcm) > RATE * 2 * 4:      # 還是不行：從中間最安靜的地方切兩半各自辨識
        cut = _quietest(pcm)
        return ' '.join(t for t in (tea_asr(pcm[:cut], context, _depth + 1), tea_asr(pcm[cut:], context, _depth + 1)) if t) or first
    return first

class Segmenter:
    """用音量切段：講到 min_seg 秒以上、遇到 pause 秒的停頓就切；講太久沒停頓，18 秒後只要短短停一下就切，max_seg 秒強制切。
    force 是換人講話的時間點（秒），到了就切，一段才不會混到兩個人。"""
    def __init__(self, min_seg, max_seg, pause, force=()):
        self.min_seg, self.max_seg, self.pause, self.force = min_seg, max_seg, pause, list(force)
        self.seg, self.start, self.silent, self.voiced, self.noise, self.pos, self.level = bytearray(), 0.0, 0, 0, 300.0, 0, 0.0

    def feed(self, chunk):
        rms = rms16(chunk)
        self.level = self.level * 0.8 + min(1.0, rms / 3000) * 0.2
        speech = rms > max(self.noise * 2.5, 150)
        if not speech: self.noise = self.noise * 0.995 + rms * 0.005
        if not self.seg: self.start = self.pos / RATE
        self.pos += len(chunk) // 2
        self.seg += chunk; self.silent = 0 if speech else self.silent + 1; self.voiced += speech
        dur = len(self.seg) / 2 / RATE; quiet = self.silent * FRAME / RATE
        if self.force and self.pos / RATE >= self.force[0]:
            while self.force and self.pos / RATE >= self.force[0]: self.force.pop(0)
            if dur >= 0.8: return self.cut(keep=True)     # 分辨講者確定這段有人講話，聲音小也不要丟
        if (dur >= self.min_seg and quiet >= (self.pause if dur < 18 else 0.2)) or dur >= self.max_seg:
            return self.cut()

    def cut(self, keep=False):
        out = (self.start, bytes(self.seg)) if keep or self.voiced * FRAME / RATE >= 0.5 else None
        self.seg, self.silent, self.voiced = bytearray(), 0, 0
        return out

# ---------- 分辨講者（sherpa-onnx：pyannote 切出誰在講話，3D-Speaker 算聲紋，再把聲音像的歸成同一人；都在 CPU） ----------
DIAR_DIR = cfg_path('diar_dir', os.path.join('models', 'diar'))
DIAR_SEG = os.path.join(DIAR_DIR, 'sherpa-onnx-pyannote-segmentation-3-0', 'model.onnx')
DIAR_EMB = os.path.join(DIAR_DIR, '3dspeaker_eres2net_base_zh.onnx')
SPK_FILE = '講者.json'          # {'turns': [[開始, 結束, 編號]...], 'names': {編號: 名字}}
SPK_RX = re.compile(r'^(\[\d{2}:\d{2}:\d{2}\]\s*)【([^】]{1,12})】\s*')

def diar_available():
    if not (os.path.exists(DIAR_SEG) and os.path.exists(DIAR_EMB)): return False
    try: import sherpa_onnx; return True
    except Exception: return False
DIAR_OK = diar_available()           # 元件和模型有裝：網頁上可以手動按「分辨講者」
# 預設不自動分辨（真實教室錄音不一定分得準，還會多花時間）；config.json 寫 "diarize": true 才會每堂課自動做
DIAR = DIAR_OK and (os.environ.get('LECTURE_DIARIZE') == '1' or (CFG.get('diarize') is True and os.environ.get('LECTURE_DIARIZE') != '0'))
# 門檻越低分得越細。實際的教室錄音（遠、有回音）用 0.5 會把老師拆成十幾個人，0.8 比較剛好
DIAR_THR = float(CFG.get('diar_threshold') or 0.8)
DIAR_MIN_SEC = float(CFG.get('diar_min_sec') or 3)        # 整堂課講不到這麼多秒的「講者」多半是雜音、笑聲，併給前一個人

def diarize(wav, lid=None):
    """16 kHz 單聲道 wav → [[開始, 結束, 講者編號]]，編號 0 是講最多的人（通常是老師）"""
    import wave, sherpa_onnx as so
    from array import array
    sd = so.OfflineSpeakerDiarization(so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=DIAR_SEG), num_threads=4),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=DIAR_EMB, num_threads=4),
        clustering=so.FastClusteringConfig(num_clusters=-1, threshold=DIAR_THR),   # 不指定人數，聲紋距離超過門檻就算另一個人
        min_duration_on=0.3, min_duration_off=0.5))
    with wave.open(wav, 'rb') as w: pcm = array('h', w.readframes(w.getnframes()))
    x = array('f', (v / 32768 for v in pcm)); del pcm
    t0 = time.time()
    def cb(n, total):
        if lid and total:
            el = time.time() - t0; eta = el / n * (total - n)
            set_job(lid, stage='分辨講者', progress=int(n / total * 100), message=f'約剩 {int(eta // 60)} 分 {int(eta % 60)} 秒')
        return 0
    turns = [[round(s.start, 2), round(s.end, 2), s.speaker] for s in sd.process(x, callback=cb).sort_by_start_time()]
    # 講太少的「講者」多半是咳嗽、雜音，併給前一個人
    total = {}
    for s, e, k in turns: total[k] = total.get(k, 0) + e - s
    tiny = {k for k, v in total.items() if v < DIAR_MIN_SEC}
    for i, t in enumerate(turns):
        if t[2] in tiny: t[2] = next((u[2] for u in reversed(turns[:i]) if u[2] not in tiny), next((u[2] for u in turns if u[2] not in tiny), t[2]))
    # 重新編號：講最多的是 0，其他依第一次出現的順序
    total = {}
    for s, e, k in turns: total[k] = total.get(k, 0) + e - s
    main = max(total, key=total.get) if total else 0
    order = [main] + [k for k in dict.fromkeys(t[2] for t in turns) if k != main]
    remap = {k: i for i, k in enumerate(order)}
    for t in turns: t[2] = remap[t[2]]
    return turns

def speaker_cuts(turns):
    """換人講話的時間點：兩個人中間有空檔就切在空檔中間（不到 1 秒的插話不切，段落太短辨識會變差）"""
    turns = [t for t in turns if t[1] - t[0] >= 1.0]
    cuts = []
    for a, b in zip(turns, turns[1:]):
        if a[2] != b[2]: cuts.append((a[1] + b[0]) / 2 if b[0] > a[1] else b[0])
    return cuts

def spk_info(lid):
    try: return json.loads(read(os.path.join(LECT, lid, SPK_FILE)) or 'null')
    except ValueError: return None

def spk_default_name(k): return '老師' if k == 0 else f'學生{k}'

def label_lines(lines, info, dur=None):
    """每行逐字稿標上講者：從這行開始到下一行開始，和誰講話的時間重疊最多就是誰。只有一個人時不標。"""
    turns, names = info['turns'], info.get('names') or {}
    plain = [SPK_RX.sub(r'\1', l) for l in lines]
    if len({t[2] for t in turns}) < 2: return plain
    stamps = [(i, hms_sec(m)) for i, l in enumerate(plain) if (m := LINE_RX.match(l))]
    out, prev = list(plain), None
    for j, (i, s) in enumerate(stamps):
        e = stamps[j + 1][1] if j + 1 < len(stamps) else (dur or s + 30)
        ov = {}
        for ts, te, k in turns:
            o = min(e, te) - max(s, ts)
            if o > 0: ov[k] = ov.get(k, 0) + o
        k = max(ov, key=ov.get) if ov else prev
        if k is None: continue
        prev = k
        m = LINE_RX.match(plain[i])
        out[i] = f'[{m[1]}:{m[2]}:{m[3]}] 【{names.get(str(k)) or spk_default_name(k)}】{m[4]}'
    return out

def hms_sec(m): return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])

def to_wav16(lid):
    d = os.path.join(LECT, lid); src = audio_of(lid)
    if not src: raise RuntimeError('找不到音檔')
    wav = os.path.join(ROOT, 'tmp', 'asr_16k.wav'); os.makedirs(os.path.dirname(wav), exist_ok=True)
    subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', os.path.join(d, src), '-ac', '1', '-ar', str(RATE), wav], check=True)
    return wav

def ensure_turns(lid, wav=None):
    """講者分段存在 講者.json，重做語音辨識時不用再算一次"""
    info = spk_info(lid)
    if info and 'turns' in info: return info
    own = wav is None
    if own: set_job(lid, stage='分辨講者', progress=0, message='把音檔轉成 16 kHz'); wav = to_wav16(lid)
    set_job(lid, stage='分辨講者', progress=0, message='分析聲紋…')
    info = {'turns': diarize(wav, lid), 'names': (info or {}).get('names') or {}}
    write(os.path.join(LECT, lid, SPK_FILE), json.dumps(info, ensure_ascii=False))
    if own:
        try: os.remove(wav)
        except OSError: pass
    return info

def run_spk(lid):
    """幫逐字稿標上講者（匯入時語音辨識前就分好了，這裡只是標；即時錄音、Breeze 在這一步才分）"""
    d = os.path.join(LECT, lid); info = ensure_turns(lid); dur = meta_of(lid).get('duration')
    for fn in ('逐字稿.txt', '逐字稿_標點.txt'):
        p = os.path.join(d, fn)
        if os.path.exists(p): write(p, '\n'.join(label_lines(read(p).splitlines(), info, dur)) + '\n')

def rename_speaker(lid, old, new):
    info = spk_info(lid)
    if not info: raise RuntimeError('這堂課還沒分辨講者')
    names = info.setdefault('names', {})
    ks = {t[2] for t in info['turns']}
    k = next((k for k in ks if (names.get(str(k)) or spk_default_name(k)) == old), None)
    if k is None: raise RuntimeError(f'找不到講者「{old}」')
    if any((names.get(str(j)) or spk_default_name(j)) == new for j in ks if j != k): raise RuntimeError(f'已經有講者叫「{new}」')
    names[str(k)] = new
    write(os.path.join(LECT, lid, SPK_FILE), json.dumps(info, ensure_ascii=False))
    d = os.path.join(LECT, lid)
    for fn in ('逐字稿.txt', '逐字稿_標點.txt'):
        p = os.path.join(d, fn)
        if os.path.exists(p): write(p, '\n'.join(SPK_RX.sub(lambda m: f'{m[1]}【{new}】' if m[2] == old else m[0], l) for l in read(p).splitlines()) + '\n')

def pipeline(steps, force=False):
    """有分辨講者時，在整理筆記前插一步 spk（標講者）"""
    if not (DIAR or force) or 'spk' in steps: return list(steps)
    i = steps.index('notes') if 'notes' in steps else len(steps)
    return list(steps[:i]) + ['spk'] + list(steps[i:])

def speakers_of(lid):
    """[{name, sec}]，講最多的排前面"""
    info = spk_info(lid)
    if not info or 'turns' not in info: return []
    names, tot = info.get('names') or {}, {}
    for s, e, k in info['turns']: tot[k] = tot.get(k, 0) + e - s
    if len(tot) < 2: return []
    return [{'name': names.get(str(k)) or spk_default_name(k), 'sec': round(v)} for k, v in sorted(tot.items(), key=lambda kv: -kv[1])]

def run_asr_tea(lid):
    import wave
    d = os.path.join(LECT, lid); m = meta_of(lid)
    set_job(lid, stage='轉檔', progress=0, message='把音檔轉成 16 kHz')
    wav = to_wav16(lid)
    cuts = []
    if DIAR:                          # 先分好誰在講話，換人的地方強制切段，一段逐字稿只會有一個人
        try: cuts = speaker_cuts(ensure_turns(lid, wav)['turns'])
        except Exception as e: traceback.print_exc(); print('分辨講者失敗，略過：', e, flush=True)
    set_job(lid, stage='語音辨識', progress=0, message='載入語音模型…')
    asr_server()
    out_path = os.path.join(d, '逐字稿.txt'); lines = []; ctx = m['keywords'].strip()
    with wave.open(wav, 'rb') as w:
        dur = w.getnframes() / RATE
        m['duration'] = dur; save_meta(lid, m)
        seg = Segmenter(15, 28, 0.5, cuts); t0 = time.time()     # 匯入的音檔不急，段落長一點上下文比較多（太短時 O(1) 會被聽成「哦一」）

        def do(item):
            start, pcm = item
            text = tea_asr(pcm, ctx)          # 試過把上一段文字也放進來，反而會稀釋關鍵字（期中→其中），所以只放關鍵字
            if text and not HALLU.match(text):
                lines.append(f'[{hms(start)}] {text}'); write(out_path, '\n'.join(lines) + '\n')
            done = start + len(pcm) / 2 / RATE; el = time.time() - t0; eta = el / max(done, 1) * (dur - done)
            set_job(lid, progress=min(99, int(done / dur * 100)) if dur else 0,
                    message=f'已辨識到 {hms(done)} / {hms(dur)}，約剩 {int(eta // 60)} 分 {int(eta % 60)} 秒{ASR_TAG}')
        while True:
            chunk = w.readframes(FRAME)
            if len(chunk) < FRAME * 2: break
            item = seg.feed(chunk)
            if item: do(item)
        if len(seg.seg) / 2 / RATE > 1:
            item = seg.cut()
            if item: do(item)
    try: os.remove(wav)
    except OSError: pass
    stop_cpu_server()
    if not lines: raise RuntimeError('沒有辨識出任何文字（音檔可能沒有人聲）')
    write(os.path.join(d, '逐字稿_標點.txt'), '\n'.join(lines) + '\n')     # TEA 的輸出已經有標點，直接當「逐字稿」

# ---------- AI 模型伺服器（OpenAI 相容 API） ----------
_llm = {'url': None, 'proc': None, 'lock': threading.Lock()}

def _alive(url):
    # llama-server 在模型還沒載入完時 /v1/models 就會回應，要看 /health（載入中回 503）；沒有 /health 的伺服器（GenieX、Ollama）才看 /v1/models
    try: urllib.request.urlopen(url + '/health', timeout=2); return True
    except urllib.error.HTTPError as e:
        if e.code == 503: return False
    except Exception: return False
    try: urllib.request.urlopen(url + '/v1/models', timeout=2); return True
    except Exception: return False

def _start_and_wait(cmd, url, secs, what):
    _llm['proc'] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
    for _ in range(secs):
        time.sleep(1)
        if _llm['proc'].poll() is not None: raise RuntimeError(f'{what} 啟動失敗')
        if _alive(url): _llm['url'] = url; return url
    raise RuntimeError(f'{what} 開不起來')

_gx = {'proc': None, 'lock': threading.Lock()}

def geniex_server(port=18282):
    """確定自己的 GenieX 伺服器（只在 127.0.0.1）有開；沒開或當掉了就重開。"""
    url = f'http://127.0.0.1:{port}'
    with _gx['lock']:
        for _ in range(3):                    # GenieX 在載入／卸載模型時可能幾秒不回應，不要馬上當成沒開
            if _alive(url): return url
            time.sleep(1)
        if _gx['proc'] and _gx['proc'].poll() is None: _gx['proc'].kill(); _gx['proc'].wait()
        _gx['proc'] = subprocess.Popen(['geniex', 'serve', '-c', 'npu', '--vit-compute', 'CPU', '--host', f'127.0.0.1:{port}',
                                        '--keepalive', '600', '--skip-update', '--log', 'warn'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        for _ in range(60):
            time.sleep(1)
            if _alive(url): return url
            if _gx['proc'].poll() is not None and _ > 20: break   # 新開的退出了（多半是埠被原本那個占著）：再等一下原本那個
        raise RuntimeError('GenieX 開不起來（NPU 可能卡住了，可以重開機再試）')

_asr = {'proc': None, 'url': None, 'lock': threading.Lock()}

def asr_server():
    """TEA-ASR 的伺服器網址：geniex → 自己開的 GenieX（NPU）；llama-server → 另外開一個 llama-server 專門跑 TEA-ASR。"""
    if ASR_ENGINE == 'geniex': return geniex_server(ASR_GX_PORT)
    port = int(ASR_CFG.get('port') or 18487); url = f'http://127.0.0.1:{port}'
    with _asr['lock']:
        if _alive(url): return url
        server = rel(ASR_CFG.get('server') or LLM_CFG.get('server') or 'llama-server')
        cmd = [server, '-m', arg(rel(ASR_CFG['gguf'])), '--mmproj', arg(rel(ASR_CFG['mmproj'])), '--host', '127.0.0.1', '--port', str(port),
               '-c', '4096']
        def start(ngl):
            if _asr['proc'] and _asr['proc'].poll() is None: _asr['proc'].kill(); _asr['proc'].wait()
            _asr['proc'] = subprocess.Popen(cmd + ['-ngl', str(ngl)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
            for _ in range(180):
                time.sleep(1)
                if _asr['proc'].poll() is not None: raise RuntimeError('語音辨識伺服器（llama-server）啟動失敗')
                if _alive(url): return
            raise RuntimeError('語音辨識伺服器（llama-server）開不起來')
        gpu = int(ASR_CFG.get('gpu_layers', 0))
        if gpu:
            # 先試 GPU：用一小段靜音測一次，失敗（有些顯示卡驅動不支援）就改成只用 CPU
            try:
                start(gpu)
                body = {'model': 'x', 'temperature': 0, 'max_tokens': 16, 'messages': [{'role': 'system', 'content': ''}, {'role': 'user', 'content': [
                    {'type': 'input_audio', 'input_audio': {'data': __import__('base64').b64encode(wav_bytes(b'\0' * RATE)).decode(), 'format': 'wav'}}]}]}
                urllib.request.urlopen(urllib.request.Request(url + '/v1/chat/completions', json.dumps(body).encode(), {'Content-Type': 'application/json'}), timeout=120)
                return url
            except Exception:
                print('語音辨識用 GPU 跑不正常，改成只用 CPU', flush=True)
        start(0)
        return url

# GenieX 有時辨識不出某一段時，用 CPU 的 llama-server 補（同一個模型檔，用到才開，辨識完就關）
_cpu = {'proc': None, 'lock': threading.Lock()}

def _find_cpu_server():
    exe = ASR_CFG.get('fallback_server')
    if exe: return rel(exe) if os.path.exists(rel(exe)) else None
    for dp, _, fs in os.walk(os.path.join(ROOT, 'runtime', 'llama')):
        if 'llama-server' + EXE in fs: return os.path.join(dp, 'llama-server' + EXE)

def _tea_files():
    if ASR_CFG.get('gguf') and ASR_CFG.get('mmproj'): return rel(ASR_CFG['gguf']), rel(ASR_CFG['mmproj'])
    d = os.path.join(os.environ.get('GENIEX_DATADIR') or os.path.join(os.path.expanduser('~'), '.cache', 'geniex'), 'models', *ASR_MODEL_ID.split(':')[0].split('/'))
    g = [f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith('.gguf')]
    mm = next((f for f in g if 'mmproj' in f), None); main = next((f for f in g if 'mmproj' not in f), None)
    return (os.path.join(d, main), os.path.join(d, mm)) if mm and main else (None, None)

def asr_cpu_server():
    port = int(ASR_CFG.get('fallback_port') or 18488); url = f'http://127.0.0.1:{port}'
    with _cpu['lock']:
        if _cpu['proc'] and _cpu['proc'].poll() is None and _alive(url): return url
        exe = _find_cpu_server(); gguf, mmproj = _tea_files()
        if not (exe and gguf): return None
        print('有一段 NPU 辨識不出來，開 CPU 的 llama-server 補辨識', flush=True)
        _cpu['proc'] = subprocess.Popen([exe, '-m', gguf, '--mmproj', mmproj, '--host', '127.0.0.1', '--port', str(port), '-c', '4096', '-ngl', '0'],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        for _ in range(120):
            time.sleep(1)
            if _cpu['proc'].poll() is not None: return None
            if _alive(url): return url
        _cpu['proc'].kill(); return None

def stop_cpu_server():
    with _cpu['lock']:
        if _cpu['proc'] and _cpu['proc'].poll() is None: _cpu['proc'].terminate()
        _cpu['proc'] = None

def llm_url():
    if LLM_MODE == 'none': raise RuntimeError('沒有設定 AI 模型（只能做逐字稿）；要加標點、整理筆記或提問，請重新執行安裝程式')
    with _llm['lock']:
        if _llm['url'] and _alive(_llm['url']): return _llm['url']
        if LLM_MODE == 'geniex':
            for port in GENIEX_PORTS[:-1]:
                if _alive(f'http://127.0.0.1:{port}'): _llm['url'] = f'http://127.0.0.1:{port}'; return _llm['url']
            _llm['url'] = geniex_server(GENIEX_PORTS[-1]); return _llm['url']     # 沒開：用自己開的那個（跟語音辨識共用）
        if LLM_MODE == 'llama-server':
            port = LLM_CFG.get('port', 18486); url = f'http://127.0.0.1:{port}'
            if _alive(url): _llm['url'] = url; return url
            cmd = [rel(LLM_CFG['server']), '-m', arg(rel(LLM_CFG['gguf'])), '--host', '127.0.0.1', '--port', str(port),
                   '-c', str(LLM_CFG.get('ctx', 8192)), '--jinja', '--reasoning-format', 'deepseek']
            gpu = int(LLM_CFG.get('gpu_layers', 0))
            if gpu:
                # 先用 GPU（Vulkan）；開不起來、或試問一題答不出來／答得亂七八糟，就改成只用 CPU（有些顯示卡驅動對 Vulkan 支援不好）
                try:
                    _start_and_wait(cmd + ['-ngl', str(gpu)], url, 300, 'AI 模型（llama-server）')   # 第一次載入 5 GB 模型可能要一兩分鐘
                    probe = {'model': 'x', 'messages': [{'role': 'user', 'content': '請只回答一個字：好'}], 'max_tokens': 16, 'temperature': 0,
                             'chat_template_kwargs': {'enable_thinking': False}}
                    r = json.load(urllib.request.urlopen(urllib.request.Request(url + '/v1/chat/completions', json.dumps(probe).encode(),
                                                                                {'Content-Type': 'application/json'}), timeout=90))
                    if '好' in (r['choices'][0]['message'].get('content') or ''): return url
                except Exception: pass
                print('AI 模型用 GPU 跑不正常，改成只用 CPU', flush=True)
                if _llm['proc'] and _llm['proc'].poll() is None: _llm['proc'].kill(); _llm['proc'].wait()
                _llm['url'] = None
            return _start_and_wait(cmd + ['-ngl', '0'], url, 300, 'AI 模型（llama-server）')
        url = LLM_CFG.get('url', 'http://127.0.0.1:11434').rstrip('/')               # openai：Ollama、LM Studio…
        if not _alive(url): raise RuntimeError(f'連不到 AI 模型伺服器 {url}，請先打開 Ollama 或 LM Studio')
        _llm['url'] = url; return url

def llm_body(messages, max_tokens, temperature, stream=False):
    model = LLM_MODEL
    if not model:                                   # 沒指定就用伺服器上的第一個模型
        model = json.load(urllib.request.urlopen(llm_url() + '/v1/models', timeout=5))['data'][0]['id']
    body = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': temperature, 'stream': stream}
    # 關掉「先思考」：各家伺服器的參數不同，不認得的欄位會被忽略
    if LLM_MODE == 'geniex': body.update(nctx=8192, enable_think=False, reasoning_effort='none')
    elif LLM_MODE == 'llama-server': body['chat_template_kwargs'] = {'enable_thinking': False}
    return body

def llm(system, user, max_tokens=3000):
    body = llm_body([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], max_tokens, 0.2)
    req = urllib.request.Request(llm_url() + '/v1/chat/completions', json.dumps(body).encode(), {'Content-Type': 'application/json'})
    r = json.load(urllib.request.urlopen(req, timeout=3600))   # 慢的電腦寫一段筆記可能要好幾分鐘
    text = (r['choices'][0]['message'].get('content') or '').strip()
    return re.sub(r'<think>[\s\S]*?</think>', '', text).strip()

def chunks(lines, limit):
    out, cur, n = [], [], 0
    for l in lines:
        if cur and n + len(l) > limit: out.append(cur); cur, n = [], 0
        cur.append(l); n += len(l)
    if cur: out.append(cur)
    return out

TOPIC_RX = re.compile(r'^\[\d{2}:\d{2}:\d{2}\]\s*(?:【[^】]*】)?(好[，,。 ]|好了|那接下來|接下來|那我們|我們來|我們再|另外|最後|再來|下面|下一個|OK|okay)', re.I)

def topic_chunks(lines, limit):
    """跟 chunks() 一樣切段，但會往回找老師換話題的那一行（「好，」「接下來」…）再切，例子比較不會被切成兩半。"""
    out, cur, n = [], [], 0
    for l in lines:
        if cur and n + len(l) > limit:
            cut = next((k for k in range(len(cur) - 1, max(0, len(cur) - 10), -1) if TOPIC_RX.match(cur[k])), None)
            if cut: out.append(cur[:cut]); cur = cur[cut:]
            else: out.append(cur); cur = []
            n = sum(map(len, cur))
        cur.append(l); n += len(l)
    if cur: out.append(cur)
    return out

def unlatex(s):
    for a, b in [('\\rightarrow', '→'), ('\\Rightarrow', '⇒'), ('\\leftarrow', '←'), ('\\to', '→'), ('\\times', '×'), ('\\div', '÷'),
                 ('\\cdot', '·'), ('\\leq', '≤'), ('\\geq', '≥'), ('\\le', '≤'), ('\\ge', '≥'), ('\\neq', '≠'), ('\\approx', '≈'), ('\\log', 'log')]:
        s = s.replace(a, b)
    s = re.sub(r'\\(?:text|mathrm|mathbf|textbf)\{([^{}]*)\}', r'\1', s)
    s = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'(\1)/(\2)', s)
    s = re.sub(r'\$+([^$\n]*?)\$+', r'\1', s)          # 拿掉 $…$
    return s

# ---------- 2. 加標點 ----------
PUNCT_SYS = ('你是上課逐字稿的校對助手。輸入是語音辨識的逐字稿，每行開頭是 [時:分:秒]。\n'
             '請：1. 加上全形標點符號；2. 修正語音辨識常見的同音錯字，要依上課情境判斷（例如作業是「交」不是「教」：「教作業」→「交作業」；「其中考」→「期中考」），'
             '不確定就保留原字；3. 英文術語保持原樣；4. 不要增加、刪除或改寫內容；5. 不要用 LaTeX 或 $ 符號，O(n) 就寫 O(n)。\n'
             '輸出：行數和時間戳都跟輸入完全一樣，每行「[時:分:秒] 文字」，不要任何開場白或說明。')

def run_punct(lid):
    d = os.path.join(LECT, lid); m = meta_of(lid)
    lines = [l for l in read(os.path.join(d, '逐字稿.txt')).splitlines() if LINE_RX.match(l)]
    if not lines: raise RuntimeError('還沒有逐字稿')
    parts = chunks(lines, 1200); done = []
    kw = f'\n這堂課的關鍵字：{m["keywords"]}' if m['keywords'].strip() else ''
    for i, part in enumerate(parts):
        set_job(lid, stage='加標點', progress=int(i / len(parts) * 100), message=f'第 {i + 1} / {len(parts)} 段')
        got = {}
        for l in unlatex(llm(PUNCT_SYS + kw, '\n'.join(part))).splitlines():
            mm = LINE_RX.match(l.strip())
            if mm: got.setdefault(f'{mm[1]}:{mm[2]}:{mm[3]}', mm[4].strip())
        for l in part:                       # 模型漏掉的行就用原文
            mm = LINE_RX.match(l); key = f'{mm[1]}:{mm[2]}:{mm[3]}'
            done.append(f'[{key}] {got.get(key) or mm[4]}')
        write(os.path.join(d, '逐字稿_標點.txt'), '\n'.join(done) + '\n')

# ---------- 3. 整理筆記 ----------
# 筆記分兩步：每段逐字稿各自寫成「詳細筆記」直接放進「內容整理」（不再二次濃縮，細節才不會被壓掉）；
# 再根據這些詳細筆記寫總覽（本堂重點、名詞解釋、作業考試、待確認）。
NOTE_PART_SYS = ('你是很會做筆記的學生，要把一段課堂逐字稿（每行有 [時:分:秒]）整理成「詳細的」上課筆記，讓沒來上課的同學看筆記就能學會。繁體中文，Markdown，不要用 LaTeX。\n'
                 '寫法：\n'
                 '1. 依主題分成「### 小標題」，標題要具體（例如「### 碰撞處理：Chaining」，不要寫「### 第一部分」）。\n'
                 '2. 每個概念都要寫清楚：是什麼（定義）、為什麼需要、怎麼運作（步驟或流程用編號條列）、老師舉的例子或比喻、優缺點或和其他方法的比較、時間或空間複雜度、公式或數字。逐字稿有講到的都要寫進去，不要只寫一句話帶過。\n'
                 '3. 保留老師的說明方式和舉的例子；老師說「很重要」「會考」「要注意」「常錯」的地方，在句首加「★」。\n'
                 '4. 每個條列最後標時間，例如（00:12:30）。\n'
                 '5. 交代的事項（作業、報告、考試、日期、繳交方式、要預習的章節）也要寫進對應的地方。\n'
                 '6. 只根據逐字稿，不要補充逐字稿沒講的課本知識；可能是語音辨識錯字或聽不懂的地方標「（？）」。\n'
                 '7. 不要開場白和結語，直接從 ### 開始；不要用課名當標題；不要寫「逐字稿中斷」之類的註記。\n'
                 '8. 逐字稿的行首可能有【老師】【學生1】這類講者標記（電腦自動分辨，偶爾會標錯）。學生提問和老師的回答，整理在相關主題最後的「**課堂問答**」：「- Q：學生的問題 → A：老師的回答（00:12:30）」；其他內容不用寫是誰講的。')
NOTE_FINAL_SYS = ('你是很會做筆記的學生。下面是一堂課的詳細筆記，請寫這堂課的「總覽」，繁體中文，Markdown，不要用 LaTeX。\n'
                  '只輸出下面這五個部分，標題文字照抄、不要加括號說明，也不要重寫詳細內容：\n'
                  '# <課名>：<這堂課的主題>\n'
                  '## 本堂重點\n（條列 4～8 點，每點一兩句話，涵蓋整堂課的主要概念和結論，後面標時間如（00:12:30））\n'
                  '## 名詞解釋\n（用表格：| 名詞 | 英文 | 說明 |，列出筆記中所有重要術語，說明用一兩句話，只根據筆記內容）\n'
                  '## 作業／考試／重要日期\n（條列：作業、報告、考試的內容、時間、範圍、繳交方式、遲交規定；老師說「會考」「必考」的主題（筆記中標★的）；要預習的章節。每點標時間；都沒有就寫「無」）\n'
                  '## 待確認\n（列出筆記中標「（？）」或意思不清楚、可能是辨識錯字的地方；沒有就寫「無」）\n'
                  '只根據提供的筆記，不要編造。')

def run_notes(lid):
    d = os.path.join(LECT, lid); m = meta_of(lid)
    src = read(os.path.join(d, '逐字稿_標點.txt')) or read(os.path.join(d, '逐字稿.txt'))
    lines = [l for l in src.splitlines() if LINE_RX.match(l)]
    if not lines: raise RuntimeError('還沒有逐字稿')
    head = f'課名：{m["name"] or "（未命名）"}；日期：{lid[:10]}' + (f'；關鍵字：{m["keywords"]}' if m['keywords'].strip() else '')
    parts = topic_chunks(lines, 2400); detail = []
    title_rx = re.compile(r'^###\s*' + re.escape(m['name']) + r'\s*([：:].*)?$', re.M) if m['name'] else None
    for i, part in enumerate(parts):
        set_job(lid, stage='整理筆記', progress=int(i / (len(parts) + 1) * 100), message=f'寫第 {i + 1} / {len(parts)} 段的詳細筆記')
        body = unlatex(llm(NOTE_PART_SYS, head + '\n\n逐字稿：\n' + '\n'.join(part), 3500)).strip()
        body = re.sub(r'^#{1,2}\s', '### ', body, flags=re.M)          # 小節一律用 ###，才不會跟總覽的 ## 混在一起
        if title_rx: body = title_rx.sub('', body).strip()              # 拿掉用課名當的標題
        detail.append(body)
        write(os.path.join(d, '_詳細筆記.md'), '\n\n'.join(detail))   # 寫到一半也看得到進度
    set_job(lid, stage='整理筆記', progress=int(len(parts) / (len(parts) + 1) * 100), message='寫總覽（重點、名詞、作業考試）')
    overview = unlatex(llm(NOTE_FINAL_SYS, head + '\n\n詳細筆記：\n' + '\n\n'.join(detail), 3000)).strip()
    overview = re.sub(r'^##\s*內容整理[\s\S]*?(?=^##\s)', '', overview, flags=re.M)   # 模型如果自己寫了內容整理就拿掉
    # 把「內容整理」（詳細筆記）插在「本堂重點」後面
    content = '## 內容整理\n\n' + '\n\n'.join(detail) + '\n\n'
    mm = re.search(r'^##\s*名詞解釋', overview, flags=re.M)
    notes = overview[:mm.start()] + content + overview[mm.start():] if mm else overview + '\n\n' + content
    write(os.path.join(d, '筆記.md'), notes.strip() + '\n')
    try: os.remove(os.path.join(d, '_詳細筆記.md'))
    except OSError: pass

# ---------- 4. 提問（RAG：BM25 從逐字稿／筆記找出相關片段，再請 Gemma 根據片段回答） ----------
# 中文用「單字＋相鄰兩字」當詞，英文用整個單字，不需要另外裝斷詞或向量模型
def tokens(s):
    s = s.lower(); out = re.findall(r'[a-z][a-z0-9+#.]*|\d+', s)
    for run in re.findall(r'[㐀-鿿]+', s):
        out += list(run) + [run[i:i + 2] for i in range(len(run) - 1)]
    return out

def passages(lid):
    """把一堂課切成約 350 字的片段（前後重疊一行），每段記住開始時間；筆記也按小節切進來。"""
    d = os.path.join(LECT, lid); name = meta_of(lid)['name'] or lid[:10]
    src = read(os.path.join(d, '逐字稿_標點.txt')) or read(os.path.join(d, '逐字稿.txt'))
    lines = [l for l in src.splitlines() if LINE_RX.match(l)]; out, i = [], 0
    while i < len(lines):
        j, n = i, 0
        while j < len(lines) and (n < 350 or j == i): n += len(lines[j]); j += 1
        seg = lines[i:j]
        out.append({'lid': lid, 'name': name, 't': seg[0][1:9], 'kind': '逐字稿', 'text': '\n'.join(seg)})   # 保留每行時間，模型才能標出精確出處
        i = j - 1 if j - i > 1 and j < len(lines) else j
    for sec in re.split(r'\n(?=##\s)', read(os.path.join(d, '筆記.md'))):
        if sec.strip():
            mm = re.search(r'[（(](\d{2}:\d{2}:\d{2})', sec)
            out.append({'lid': lid, 'name': name, 't': mm[1] if mm else '', 'kind': '筆記', 'text': sec.strip()[:900]})
    return out

def retrieve(question, lids, k=6):
    docs = [p for lid in lids for p in passages(lid)]
    if not docs: return []
    toks = [tokens(p['text']) for p in docs]; N = len(docs); avg = sum(map(len, toks)) / N or 1
    df = {}
    for t in toks:
        for w in set(t): df[w] = df.get(w, 0) + 1
    q = set(tokens(question)); scored = []
    for p, t in zip(docs, toks):
        tf = {}
        for w in t:
            if w in q: tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w, f in tf.items():
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            weight = 0.4 if len(w) == 1 and '㐀' <= w <= '鿿' else 1.0     # 單一個中文字權重低一點
            s += weight * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if s > 0: scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    return [dict(p, score=round(s, 2)) for s, p in scored[:k]]

ASK_SYS = ('你是助教，根據「課堂片段」回答學生的問題，用繁體中文，簡潔清楚，不要用 LaTeX。\n'
           '規則：1. 只根據課堂片段回答；片段裡沒有提到就說「這幾堂課的逐字稿裡沒有提到」，可以再補充一般知識，但要明確標示「（一般知識，非上課內容）」。'
           '2. 每個重點後面標出處，格式是（課名 時:分:秒），時間用片段裡那句話開頭的 [時:分:秒]，例如（資料結構 00:12:30）。'
           '3. 逐字稿是語音辨識的，可能有同音錯字，請依上下文理解。'
           '4. 行首的【老師】【學生1】是電腦自動分辨的講者，偶爾會標錯。')

def qa_path(lid): return os.path.join(LECT, lid, '提問.json')

# ---------- 即時錄音（後端用 ffmpeg 收音，停頓時把那段送 whisper-server，文字邊錄邊寫進逐字稿.txt） ----------
SERVER = os.path.join(WHISPER_DIR, 'whisper-server' + EXE)
ASR_PORT, RATE, FRAME = 18383, 16000, 480            # 30 ms 一個 frame
MIN_SEG, MAX_SEG, PAUSE = 4.0, 20.0, 0.6
HALLU = re.compile(r'^(謝謝(大家)?(觀看|收看|收聽)|請不吝點贊|訂閱|字幕|多謝|thank you|thanks for watching)', re.I)
LIVE = {'sess': None}

def mic_input():
    """ffmpeg 收麥克風的參數。Windows 用 DirectShow 的第一支麥克風；Linux 預設用 PulseAudio／PipeWire 的預設輸入。
    config.json 的 "mic" 可以自己指定，例如 ["-f", "alsa", "-i", "hw:0"]"""
    if CFG.get('mic'): return list(CFG['mic'])
    if os.name == 'nt': return ['-f', 'dshow', '-i', 'audio=' + mic_device()]
    return ['-f', 'pulse', '-i', 'default']

def mic_device():
    r = subprocess.run([FFMPEG, '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'], capture_output=True)
    for l in r.stderr.decode('utf-8', 'replace').splitlines():
        mm = re.search(r'"([^"]+)" \(audio\)', l)
        if mm: return mm[1]
    raise RuntimeError('找不到麥克風')

class Live:
    def __init__(self, name, keywords):
        self.lid = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S') + ('_' + name if name else '')
        self.dir = os.path.join(LECT, self.lid); os.makedirs(self.dir)
        save_meta(self.lid, {'name': name, 'keywords': keywords, 'source': '即時錄音'})
        self.t0, self.lines, self.pending, self.keywords, self.srv = time.time(), [], 0, keywords, None
        self.segm = Segmenter(4, 20, 0.6); self.mark = POWER.mark()
        set_job(self.lid, stage='即時錄音', progress=0, message='啟動中…', error='', done=False)
        if TEA:
            asr_server()
            tea_asr(b'\0' * RATE * 2, keywords)     # 先用 1 秒靜音把模型載進 NPU，第一句才不會等
        else:
            self.srv = subprocess.Popen([SERVER, '-m', arg(MODEL), '-l', 'zh', '-t', THREADS, '--port', str(ASR_PORT), '-nt', '-sns',
                                         '--vad', '-vm', arg(VAD_MODEL)] + (['--prompt', keywords] if keywords else []),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
            for _ in range(120):
                try: urllib.request.urlopen(f'http://127.0.0.1:{ASR_PORT}/', timeout=1); break
                except Exception: time.sleep(0.5)
            else:
                self.srv.kill(); raise RuntimeError('辨識伺服器沒有啟動')
        # 錄音存成 fragmented m4a：就算程式意外結束，已錄的部分也能播放
        src = ['-re', '-i', os.environ['LIVE_TEST_FILE']] if os.environ.get('LIVE_TEST_FILE') else mic_input()   # 測試用：拿音檔當麥克風
        self.ff = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y'] + src + [
                                    '-ac', '1', '-c:a', 'aac', '-b:a', '64k', '-movflags', '+frag_keyframe+empty_moov', os.path.join(self.dir, '錄音.m4a'),
                                    '-ac', '1', '-ar', str(RATE), '-f', 's16le', '-'], stdout=subprocess.PIPE, stdin=subprocess.PIPE)
        self.jobs = queue.Queue()
        self.tw = threading.Thread(target=self.transcriber, daemon=True); self.tw.start()
        self.tr = threading.Thread(target=self.reader, daemon=True); self.tr.start()

    @property
    def level(self): return self.segm.level

    def post(self, pcm):
        if TEA: return tea_asr(pcm, self.keywords)
        b = '----live' + str(time.time_ns())
        body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="seg.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + wav_bytes(pcm) + \
               (f'\r\n--{b}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n--{b}--\r\n').encode()
        req = urllib.request.Request(f'http://127.0.0.1:{ASR_PORT}/inference', body, {'Content-Type': f'multipart/form-data; boundary={b}'})
        return json.load(urllib.request.urlopen(req, timeout=120)).get('text', '').strip()

    def transcriber(self):
        while True:
            item = self.jobs.get()
            if item is None: return
            start, pcm = item
            try: text = re.sub(r'\s+', ' ', self.post(pcm)).strip()
            except Exception as e: text = f'（這段辨識失敗：{e}）'
            self.pending -= 1
            if text and not HALLU.match(text):
                self.lines.append(f'[{hms(start)}] {text}')
                write(os.path.join(self.dir, '逐字稿.txt'), '\n'.join(self.lines) + '\n')

    def reader(self):
        sg = self.segm
        while True:
            chunk = self.ff.stdout.read(FRAME * 2)
            if len(chunk) < FRAME * 2: break
            item = sg.feed(chunk)
            if item: self.pending += 1; self.jobs.put(item)
            if sg.pos % (RATE // 2) < FRAME:
                set_job(self.lid, message=f'已錄 {hms(sg.pos / RATE)}' + (f'，{self.pending} 段辨識中' if self.pending else '') + ASR_TAG)
        if len(sg.seg) / 2 / RATE > 1:
            item = sg.cut()
            if item: self.pending += 1; self.jobs.put(item)
        self.recorded = sg.pos / RATE

    def stop(self, then_notes):
        set_job(self.lid, message='停止錄音，等最後幾段辨識完…')
        try: self.ff.stdin.write(b'q'); self.ff.stdin.flush()
        except Exception: pass
        try: self.ff.wait(timeout=15)
        except Exception: self.ff.kill()
        self.tr.join(timeout=30); self.jobs.put(None); self.tw.join(timeout=300)
        if self.srv: self.srv.terminate()
        m = meta_of(self.lid); m['duration'] = getattr(self, 'recorded', time.time() - self.t0); save_meta(self.lid, m)
        record_energy(self.lid, 'live', self.mark)
        stop_cpu_server()
        if TEA and self.lines: write(os.path.join(self.dir, '逐字稿_標點.txt'), '\n'.join(self.lines) + '\n')
        LIVE['sess'] = None
        steps = ([] if TEA else ['punct']) + ['notes'] if then_notes else []
        if self.lines and (steps or DIAR): enqueue(self.lid, pipeline(steps))
        else: set_job(self.lid, stage='完成', progress=100, message='', done=True)

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); path = urllib.parse.unquote(u.path)
        if path in ('/', '/index.html'):
            return self.send(200, read(os.path.join(ROOT, 'lecture.html')), 'text/html; charset=utf-8')
        if path == '/api/lectures':
            out = []
            for lid in sorted(os.listdir(LECT), reverse=True):
                d = os.path.join(LECT, lid)
                if not os.path.isdir(d): continue
                m = meta_of(lid)
                out.append({'id': lid, 'name': m['name'], 'duration': m.get('duration'),
                            'has': {k: os.path.exists(os.path.join(d, f)) for k, f in [('raw', '逐字稿.txt'), ('punct', '逐字稿_標點.txt'), ('notes', '筆記.md')]},
                            'job': JOBS.get(lid)})
            return self.send(200, out)
        if path == '/api/config':
            return self.send(200, {'llm': LLM_MODE != 'none', 'llm_mode': LLM_MODE, 'asr': ASR_ENGINE, 'tea': TEA, 'power': POWER.ok, 'diarize': DIAR_OK, 'diarize_auto': DIAR})
        if path == '/api/power':
            return self.send(200, {'ok': POWER.ok, 'w': POWER.watts() if POWER.ok else None, 'error': POWER.err})
        if path == '/api/live':
            s = LIVE['sess']
            return self.send(200, {'active': bool(s), 'id': s and s.lid, 'elapsed': s and time.time() - s.t0, 'level': s and round(s.level, 3)})
        if path.startswith('/api/lecture/'):
            lid = safe_id(path.split('/', 3)[3])
            if not lid: return self.send(404, {'error': '找不到'})
            d = os.path.join(LECT, lid); m = meta_of(lid)
            return self.send(200, {'id': lid, 'name': m['name'], 'keywords': m['keywords'], 'duration': m.get('duration'), 'audio': audio_of(lid),
                                   'raw': read(os.path.join(d, '逐字稿.txt')), 'punct': read(os.path.join(d, '逐字稿_標點.txt')),
                                   'notes': read(os.path.join(d, '筆記.md')), 'job': JOBS.get(lid), 'energy': m.get('energy') or {},
                                   'speakers': speakers_of(lid),
                                   'qa': json.loads(read(qa_path(lid), '[]') or '[]')})
        if path.startswith('/media/'):
            lid = safe_id(path.split('/', 2)[2]); f = lid and audio_of(lid)
            if not f: return self.send(404, b'', 'text/plain')
            return self.send_file(os.path.join(LECT, lid, f))
        self.send(404, {'error': 'not found'})

    def send_file(self, fp):
        size = os.path.getsize(fp); ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        if fp.lower().endswith('.m4a'): ctype = 'audio/mp4'
        rng = self.headers.get('Range'); start, end = 0, size - 1
        if rng and (mm := re.match(r'bytes=(\d*)-(\d*)', rng)):
            if mm[1]: start = int(mm[1])
            if mm[2]: end = min(int(mm[2]), size - 1)
            elif not mm[1]: start = max(0, size - int(mm[2] or 0))
        self.send_response(206 if rng else 200)
        self.send_header('Content-Type', ctype); self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if rng: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with open(fp, 'rb') as f:
            f.seek(start); left = end - start + 1
            try:
                while left > 0:
                    b = f.read(min(1 << 20, left))
                    if not b: break
                    self.wfile.write(b); left -= len(b)
            except (ConnectionError, OSError): pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path); path = urllib.parse.unquote(u.path); q = dict(urllib.parse.parse_qsl(u.query))
        if path == '/api/upload':
            # 音檔直接放在 body，檔名、課名、關鍵字放在 query string
            fname = os.path.basename(q.get('filename', 'audio.m4a')); ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXT: return self.send(400, {'error': f'不支援的格式 {ext}'})
            name = re.sub(r'[\\/:*?"<>|]', '', q.get('name', '')).strip()[:40]
            lid = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S') + ('_' + name if name else '')
            d = os.path.join(LECT, lid); os.makedirs(d)
            n = int(self.headers.get('Content-Length', 0))
            with open(os.path.join(d, '錄音' + ext), 'wb') as f:
                while n > 0:
                    b = self.rfile.read(min(1 << 20, n))
                    if not b: break
                    f.write(b); n -= len(b)
            save_meta(lid, {'name': name, 'keywords': q.get('keywords', ''), 'source': fname})
            enqueue(lid, pipeline(PIPELINE))
            return self.send(200, {'id': lid})
        if path.startswith('/api/ask/'):
            lid = safe_id(path.split('/', 3)[3])
            if not lid: return self.send(404, {'error': '找不到'})
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            question = (req.get('q') or '').strip(); scope_all = bool(req.get('all'))
            if not question: return self.send(400, {'error': '請輸入問題'})
            history = json.loads(read(qa_path(lid), '[]') or '[]')
            lids = [x for x in sorted(os.listdir(LECT)) if os.path.isdir(os.path.join(LECT, x))] if scope_all else [lid]
            # 追問時把上一題也拿來檢索，「那它的缺點呢？」才找得到東西
            hits = retrieve(question + ' ' + (history[-1]['q'] if history else ''), lids)
            ctx = '\n\n'.join(f'[{i + 1}]（{h["name"]} {h["t"] or "筆記"}，{h["kind"]}）\n{h["text"]}' for i, h in enumerate(hits)) or '（沒有找到相關片段）'
            msgs = [{'role': 'system', 'content': ASK_SYS}]
            for h in history[-2:]: msgs += [{'role': 'user', 'content': h['q']}, {'role': 'assistant', 'content': h['a']}]
            msgs.append({'role': 'user', 'content': f'課堂片段：\n{ctx}\n\n問題：{question}'})
            # 用 NDJSON 一行一行串流給瀏覽器：先送出處，再一段段送回答
            self.send_response(200); self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8'); self.end_headers()
            emit = lambda o: (self.wfile.write((json.dumps(o, ensure_ascii=False) + '\n').encode()), self.wfile.flush())
            sources = [{k: h[k] for k in ('lid', 'name', 't', 'kind', 'score')} | {'text': re.sub(r'\[\d{2}:\d{2}:\d{2}\]\s*|\n', ' ', h['text']).strip()[:160]} for h in hits]
            answer = ''
            try:
                emit({'sources': sources})
                body = llm_body(msgs, 1500, 0.3, stream=True)
                r = urllib.request.urlopen(urllib.request.Request(llm_url() + '/v1/chat/completions', json.dumps(body).encode(),
                                                                  {'Content-Type': 'application/json'}), timeout=600)
                for raw in r:
                    s = raw.decode('utf-8', 'replace').strip()
                    if not s.startswith('data:') or s == 'data: [DONE]': continue
                    try: delta = json.loads(s[5:])['choices'][0]['delta'].get('content') or ''
                    except Exception: continue
                    if delta: answer += delta; emit({'delta': delta})
                answer = unlatex(re.sub(r'<think>[\s\S]*?</think>', '', answer)).strip()
                history.append({'q': question, 'a': answer, 'all': scope_all, 'sources': sources, 'time': datetime.datetime.now().isoformat(timespec='seconds')})
                write(qa_path(lid), json.dumps(history, ensure_ascii=False, indent=1))
                emit({'done': True, 'answer': answer})
            except (ConnectionError, OSError): pass            # 瀏覽器中途關掉
            except Exception as e:
                try: emit({'error': str(e)})
                except Exception: pass
            return
        if path.startswith('/api/qa_clear/'):
            lid = safe_id(path.split('/', 3)[3])
            if lid and os.path.exists(qa_path(lid)): os.remove(qa_path(lid))
            return self.send(200, {'ok': True})
        if path == '/api/live/start':
            if LIVE['sess']: return self.send(409, {'error': '已經在錄音了'})
            name = re.sub(r'[\\/:*?"<>|]', '', q.get('name', '')).strip()[:40]
            try: LIVE['sess'] = Live(name, q.get('keywords', '').strip())
            except Exception as e:
                LIVE['sess'] = None
                for lid, j in JOBS.items():
                    if j['stage'] == '即時錄音' and not j['done']: set_job(lid, stage='失敗', error=str(e), done=True)
                return self.send(500, {'error': str(e)})
            return self.send(200, {'id': LIVE['sess'].lid})
        if path == '/api/live/stop':
            s = LIVE['sess']
            if not s: return self.send(409, {'error': '沒有在錄音'})
            threading.Thread(target=s.stop, args=(q.get('notes') == '1',), daemon=True).start()
            return self.send(200, {'id': s.lid})
        if path.startswith('/api/run/'):
            lid = safe_id(path.split('/', 3)[3]); step = q.get('step')
            if not lid or step not in ('asr', 'punct', 'spk', 'notes', 'all'): return self.send(400, {'error': '參數錯誤'})
            if JOBS.get(lid) and not JOBS[lid]['done']: return self.send(409, {'error': '這堂課已經在處理中'})
            if 'keywords' in q: m = meta_of(lid); m['keywords'] = q['keywords']; save_meta(lid, m)
            if step == 'spk' and not DIAR_OK: return self.send(400, {'error': '沒有安裝分辨講者的元件，請重新執行安裝程式'})
            if step == 'spk':             # 重新分辨講者：丟掉舊的分段（名字保留），標完再重寫筆記
                info = spk_info(lid)
                if info: info.pop('turns', None); write(os.path.join(LECT, lid, SPK_FILE), json.dumps(info, ensure_ascii=False))
            enqueue(lid, pipeline({'asr': PIPELINE, 'punct': ['punct', 'notes'], 'spk': ['notes'], 'notes': ['notes'], 'all': PIPELINE}[step], force=step == 'spk'))
            return self.send(200, {'ok': True})
        if path.startswith('/api/speaker/'):
            lid = safe_id(path.split('/', 3)[3])
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            new = re.sub(r'[【】\[\]\s]', '', req.get('new') or '')[:12]
            if not lid or not new: return self.send(400, {'error': '請輸入名字'})
            try: rename_speaker(lid, req.get('old') or '', new)
            except RuntimeError as e: return self.send(400, {'error': str(e)})
            return self.send(200, {'ok': True})
        if path.startswith('/api/save/'):
            lid = safe_id(path.split('/', 3)[3]); kind = q.get('kind')
            fn = {'notes': '筆記.md', 'punct': '逐字稿_標點.txt'}.get(kind)
            if not lid or not fn: return self.send(400, {'error': '參數錯誤'})
            body = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8')
            write(os.path.join(LECT, lid, fn), body); return self.send(200, {'ok': True})
        self.send(404, {'error': 'not found'})

if __name__ == '__main__':
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer.allow_reuse_address = False      # Windows 上允許重複綁定會讓兩個程式搶同一個埠
    try: srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    except OSError:
        print(f'埠 {PORT} 已經被占用：上課筆記可能已經開著了，直接開 http://127.0.0.1:{PORT}/ 即可。', flush=True)
        if '--no-browser' not in sys.argv:
            import webbrowser; webbrowser.open(f'http://127.0.0.1:{PORT}/')
        sys.exit(1)
    print(f'上課筆記：http://127.0.0.1:{PORT}/  （關掉這個視窗就會停止）', flush=True)
    if '--no-browser' not in sys.argv:
        import webbrowser; webbrowser.open(f'http://127.0.0.1:{PORT}/')
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        if LIVE['sess']: LIVE['sess'].stop(False)        # 關掉程式時把錄音好好收尾
        if _llm['proc']: _llm['proc'].terminate()
