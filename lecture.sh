#!/usr/bin/env bash
# 上課筆記（Linux、Termux）：啟動後用瀏覽器開 http://127.0.0.1:18484/ 。處理音檔或錄音時不要關掉；用完按 Ctrl+C。
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ ! -f config.json ]; then echo '還沒安裝，請先執行 bash install.sh'; exit 1; fi
# Termux：不讓 Android 在處理到一半時把程式停掉，並自動打開瀏覽器
if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock; trap 'termux-wake-unlock' EXIT; fi
if command -v termux-open-url >/dev/null 2>&1; then (sleep 2; termux-open-url http://127.0.0.1:18484/) >/dev/null 2>&1 & fi
echo '上課筆記網頁：http://127.0.0.1:18484/'
python3 -u -X utf8 lecture_app.py "$@"
