#!/usr/bin/env bash
# 上課筆記（Linux）：啟動後用瀏覽器開 http://127.0.0.1:18484/ 。處理音檔或錄音時不要關掉；用完按 Ctrl+C。
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ ! -f config.json ]; then echo '還沒安裝，請先執行 ./install.sh'; exit 1; fi
echo '上課筆記網頁：http://127.0.0.1:18484/'
exec python3 -u -X utf8 lecture_app.py "$@"
