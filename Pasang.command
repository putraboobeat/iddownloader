#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
python3 -m venv .venv || exit 1
.venv/bin/python -m pip install --upgrade -r requirements.txt || exit 1
echo 'Pendeteksi siap. Jalankan Mulai.command. Google Chrome harus sudah terpasang.'
