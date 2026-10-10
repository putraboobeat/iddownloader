#!/usr/bin/env python3
import sys
from pathlib import Path

# Tambahkan path saat ini agar modul 'backend' bisa dimuat
sys.path.insert(0, str(Path(__file__).parent.resolve()))

try:
    from backend.main import run_server
except ImportError as e:
    print(f"Error memuat modul backend: {e}")
    print("Pastikan Anda menjalankan perintah dari direktori utama aplikasi.")
    sys.exit(1)

if __name__ == '__main__':
    run_server()
