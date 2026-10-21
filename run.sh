#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
# yt-dlp часто меняется из-за обновлений YouTube, поэтому обновляем его при каждом запуске (без сети пропускаем)
.venv/bin/pip install -q -U "yt-dlp[default]" >/dev/null 2>&1
.venv/bin/python main.py "$@"
