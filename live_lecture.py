# 上課即時逐字稿：麥克風一邊錄音（存成 m4a），一邊在講話停頓時把那一段送給 whisper-server（Breeze-ASR-25）辨識，文字即時印出並寫進 txt。
# usage:
#   python live_lecture.py [課名] [--prompt "關鍵字, 專有名詞"] [--file 測試用音檔]
# 結果存在 C:\Users\d4467\whisper\課堂\<日期_時間_課名>\（錄音.m4a、逐字稿.txt）
import argparse, datetime, io, json, os, queue, re, subprocess, sys, threading, time, urllib.request, wave
from array import array

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, 'cpu', 'Release', 'whisper-server.exe')
MODEL = os.path.join(ROOT, 'models', 'ggml-breeze-asr-25-q8_0.bin')
VAD_MODEL = os.path.join(ROOT, 'models', 'ggml-silero-v5.1.2.bin')
MIC = 'audio=Microphone Array (Qualcomm(R) Aqstic(TM) ACX Static Endpoints Audio Device)'
PORT = 18383
RATE, FRAME = 16000, 480                      # 30 ms 一個 frame
MIN_SEG, MAX_SEG, PAUSE = 4.0, 20.0, 0.6      # 每段至少 4 秒、最多 20 秒；停頓 0.6 秒就切
# Whisper 在沒人講話時常會自己編的句子
HALLU = re.compile(r'^(謝謝(大家)?(觀看|收看|收聽)|請不吝點贊|訂閱|字幕|多謝|thank you|thanks for watching)', re.I)

ap = argparse.ArgumentParser()
ap.add_argument('name', nargs='?', default='')
ap.add_argument('--prompt', default='', help='課程關鍵字、專有名詞，用逗號分隔，可提高辨識率')
ap.add_argument('--file', help='用音檔模擬麥克風（測試用）')
ap.add_argument('--threads', type=int, default=12)
args = ap.parse_args()

stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M')
out_dir = os.path.join(ROOT, '課堂', stamp + ('_' + args.name if args.name else ''))
os.makedirs(out_dir, exist_ok=True)
txt_path = os.path.join(out_dir, '逐字稿.txt')

# 1. 啟動辨識伺服器（模型只載入一次）
srv = subprocess.Popen([SERVER, '-m', MODEL, '-l', 'zh', '-t', str(args.threads), '--port', str(PORT), '-nt', '-sns',
                        '--vad', '-vm', VAD_MODEL] + (['--prompt', args.prompt] if args.prompt else []),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(120):
    try: urllib.request.urlopen(f'http://127.0.0.1:{PORT}/', timeout=1); break
    except Exception: time.sleep(0.5)
else:
    sys.exit('辨識伺服器沒有啟動')

# 2. ffmpeg：同時存 m4a 完整錄音，並把 16 kHz 單聲道 PCM 從 stdout 給我們
src = ['-re', '-i', args.file] if args.file else ['-f', 'dshow', '-i', MIC]
ff = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + src +
                      ['-ac', '1', '-c:a', 'aac', '-b:a', '64k', os.path.join(out_dir, '錄音.m4a'),
                       '-ac', '1', '-ar', str(RATE), '-f', 's16le', '-'],
                      stdout=subprocess.PIPE, stdin=subprocess.PIPE)

def transcribe(pcm):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    b = '----lecture' + str(time.time_ns())
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="seg.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode() + buf.getvalue() + \
           (f'\r\n--{b}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n--{b}--\r\n').encode()
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/inference', body, {'Content-Type': f'multipart/form-data; boundary={b}'})
    return json.load(urllib.request.urlopen(req, timeout=120)).get('text', '').strip()

# 3. 辨識在另一條執行緒做，錄音不會被卡住
jobs = queue.Queue()
def worker():
    with open(txt_path, 'a', encoding='utf-8') as f:
        while True:
            item = jobs.get()
            if item is None: return
            t0, pcm = item
            try: text = transcribe(pcm)
            except Exception as e: text = f'（辨識失敗：{e}）'
            text = re.sub(r'\s+', ' ', text).strip()
            if not text or HALLU.match(text): continue
            line = f'[{int(t0 // 3600):02d}:{int(t0 % 3600 // 60):02d}:{int(t0 % 60):02d}] {text}'
            print(line, flush=True); f.write(line + '\n'); f.flush()
th = threading.Thread(target=worker, daemon=True); th.start()

# 4. 用音量判斷停頓來切段（背景噪音的基準會自動調整）
print(f'開始錄音，存到 {out_dir}\n按 Ctrl+C 結束。\n', flush=True)
seg, seg_start, silent, noise, pos, voiced = bytearray(), 0.0, 0, 300.0, 0, 0
try:
    while True:
        chunk = ff.stdout.read(FRAME * 2)
        if len(chunk) < FRAME * 2: break
        a = array('h', chunk); rms = (sum(x * x for x in a) / len(a)) ** 0.5
        pos += FRAME
        speech = rms > max(noise * 2.5, 150)
        if not speech: noise = noise * 0.995 + rms * 0.005
        if not seg: seg_start = (pos - FRAME) / RATE
        seg += chunk; silent = 0 if speech else silent + 1; voiced += speech
        dur = len(seg) / 2 / RATE
        if (dur >= MIN_SEG and silent * FRAME / RATE >= PAUSE) or dur >= MAX_SEG:
            if voiced * FRAME / RATE >= 0.5: jobs.put((seg_start, bytes(seg)))   # 幾乎沒聲音的段落不送
            seg, silent, voiced = bytearray(), 0, 0
except KeyboardInterrupt:
    pass
finally:
    if len(seg) / 2 / RATE > 1 and voiced: jobs.put((seg_start, bytes(seg)))
    try: ff.stdin.write(b'q'); ff.stdin.flush()       # 讓 ffmpeg 正常收尾，m4a 才不會壞
    except Exception: pass
    try: ff.wait(timeout=10)
    except Exception: ff.kill()
    print('\n錄音結束，等最後幾段辨識完…', flush=True)
    jobs.put(None); th.join()
    srv.terminate()
    print(f'完成：{txt_path}', flush=True)
