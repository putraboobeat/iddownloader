# Menjalankan di VPS Ubuntu/Debian

Mode VPS memakai port tetap 6666, hanya mendengarkan 127.0.0.1, dan mengabaikan folder dari browser. Jalankan satu instance aplikasi untuk setiap folder data.

```bash
sudo apt update
sudo apt install python3-venv ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py --vps --port 6666 --download-dir /srv/video-downloader-data
```

Folder data harus bisa ditulis oleh pengguna yang menjalankan aplikasi. Gunakan pengguna Linux khusus, bukan root. Script `.command` ditujukan untuk macOS; di Linux gunakan Python langsung.

## Penghapusan otomatis

- Hanya folder pekerjaan baru di folder data VPS yang dikelola. Nama video dan subtitle tetap sama, dengan ekstensi berbeda.
- Dua jam dihitung setelah pekerjaan selesai, gagal, atau dihentikan. File parsial juga dibersihkan.
- Pengecekan setiap 15 detik: penghapusan normal terjadi antara 2 jam dan 2 jam 15 detik.
- Unduhan yang masih aktif tidak dihapus, sehingga bisa menggunakan disk lebih dari dua jam.
- Jadwal tersimpan di `.expiry.json` dalam folder pekerjaan. Saat aplikasi restart, pekerjaan yang terputus menggunakan tenggat awal pembuatan + 2 jam; yang sudah lewat tenggat dibersihkan.
- Jika aplikasi/VPS mati, pembersihan baru berjalan saat aplikasi hidup kembali. Jika penghapusan gagal karena izin, jadwal dipertahankan dan dicoba kembali.
- File lama di luar folder pekerjaan tidak ikut dihapus. Mode lokal tanpa `--vps` tidak menghapus hasil otomatis.
- Retensi bukan kuota disk: gunakan volume dengan kuota atau pantau ruang kosong untuk unduhan besar.

## Service agar tetap hidup

Buat `/etc/systemd/system/video-downloader.service`. Sesuaikan pengguna dan lokasi proyek berikut dengan VPS Anda:

```ini
[Unit]
Description=Video Downloader
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=downloader
WorkingDirectory=/opt/iddownloader
ExecStart=/opt/iddownloader/.venv/bin/python app.py --vps --port 6666 --download-dir /srv/video-downloader-data
Restart=on-failure
RestartSec=5
KillMode=control-group

[Install]
WantedBy=multi-user.target
```

Pengguna `downloader` serta folder proyek/data harus sudah tersedia dan dimiliki/diizinkan untuk pengguna tersebut. Kemudian:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now video-downloader
sudo journalctl -u video-downloader -n 50 --no-pager
```

## Cloudflare Tunnel

Pada tunnel akun yang memiliki domain, arahkan hostname ke HTTP `127.0.0.1:6666`, dengan HTTP Host Header `127.0.0.1:6666`. Lindungi seluruh hostname dengan Cloudflare Access sebelum dipublikasikan. Jangan buka port 6666 ke Internet. Token aplikasi bukan login pengguna: halaman utama memberikan token kepada pengunjung yang bisa mengaksesnya.

## Fitur & Pembaruan Terbaru

- **Submenu Terpisah "Universal YT-DLP":**
  - Mengunduh dari YouTube, Instagram Reels/Post, TikTok, Twitter/X, Facebook, SoundCloud, dan 1000+ situs lainnya.
  - Mode Video (MP4, MKV, WebM) dengan pilihan resolusi dari 360p hingga 4K UHD.
  - Mode Audio Saja (MP3, M4A, FLAC Lossless, WAV, OPUS) dengan pilihan bitrate (128k - 320k).
  - Batch Download: Masukkan banyak URL baris demi baris dalam satu kali proses unduh.
  - Fitur andalan yt-dlp: Embed metadata, thumbnail, chapters, subtitle, auto-captions, ignore errors, playlist range, dan multi-thread fragment download.
- **Unduh Langsung ke Browser / PC:**
  - Setelah 100% selesai di VPS, file muncul dengan tombol unduh langsung ke browser.
  - Opsi *Otomatis simpan ke PC* otomatis memicu dialog download browser saat selesai tanpa perlu SFTP manual.

## Cara Memperbarui di VPS

Jika Anda memakai Git dan PM2/Systemd:

```bash
# Masuk ke folder aplikasi
cd ~/iddownloader  # sesuaikan lokasi folder di VPS Anda

# Tarik perubahan terbaru dari GitHub
git pull origin main

# Pastikan yt-dlp selalu versi paling baru agar tidak dicekal YouTube/medsos
.venv/bin/pip install -U yt-dlp

# Restart aplikasi
pm2 restart iddownloader   # jika menggunakan PM2
# atau jika menggunakan systemd:
# sudo systemctl restart video-downloader
```

## Batasan & Catatan Penggunaan

- Ekstensi Chrome Companion tetap terhubung untuk streaming film web.
- Aplikasi memproses satu antrean unduhan aktif dalam satu waktu.
- File di VPS otomatis dibersihkan dan dihapus permanen setelah 2 jam untuk menghemat kapasitas disk server.

