#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 belum terpasang. Instal melalui https://www.python.org/downloads/macos/'
  read -r -p 'Tekan Enter untuk menutup.'
  exit 1
fi
if [ -x .venv/bin/python ]; then
  .venv/bin/python app.py
else
  python3 app.py
fi
