# PANDUAN INTEGRASI PERPUSTAKAAN STREAMING PRIBADI: OMNIFETCH & JELLYFIN

Dokumen ini merupakan panduan arsitektur, instalasi, konfigurasi, operasional, dan pemeliharaan fitur **Perpustakaan Streaming Pribadi** pada proyek OmniFetch Studio.

---

## 1. PENJELASAN ARSITEKTUR

Sistem ini menghubungkan penangkapan media dari browser hingga pemutaran di layar TV melalui rantai arsitektur terintegrasi:

```
[ Browser Web: Ekstensi OmniFetch Companion ]
                      │
                      ▼ (Kirim URL Media, Subtitle, Judul, & Mode: Library)
[ Backend OmniFetch Studio ]
  ├── 1. Mesin Download (HLS/Direct/yt-dlp) ──► Folder Staging Sementara
  ├── 2. Media Validator (ffprobe)           ──► Inspeksi Container, Durasi, Codec
  ├── 3. Atomic Importer                     ──► Rename/Pindah Atomik ke Library
  └── 4. Integration Database (SQLite)       ──► Catat Metadata & Status Item
                      │
                      ▼ (Pemicu Scan via REST API Resmi: /Items/{id}/Refresh)
[ Jellyfin Media Server (Linux Host) ]
  ├── Katalog Perpustakaan Film (Movies Library)
  ├── Transcoding Engine (Opsional jika perangkat klien butuh)
  └── Akun Penonton Mandiri (Non-Admin Viewer)
                      │
                      ▼ (Streaming Direct Play / HLS)
[ Jellyfin Android TV App / Web Browser Player ]
```

### Pemisahan Konsep dan Tanggung Jawab
1. **OmniFetch Downloader**: Menangani pengunduhan video multi-thread, penggabungan segmen HLS, pemulihan koneksi, penanganan subtitle, dan sanitasi berkas awal.
2. **Folder Staging vs Permanen**: Berkas yang sedang diunduh berada di staging sementara. Hanya berkas yang lolos verifikasi `ffprobe` yang dipindahkan ke folder permanen Jellyfin.
3. **Database Integrasi (SQLite)**: Mencatat riwayat impor, pemetaan job OmniFetch ke Jellyfin Item ID, status verifikasi media, dan deduplikasi sumber.
4. **Jellyfin**: Bertindak sebagai server katalog film, poster/metadata fetcher, pemutar video hemat resource (Direct Play), pemilih subtitle, dan antarmuka Android TV.
5. **Ekstensi Browser**: Pintu masuk pengguna untuk memilih media, subtitle Indonesia, dan menetapkan target pekerjaan (`download` vs `library`).

---

## 2. TEMPLATE KONFIGURASI (TANPA SECRET)

Buat berkas konfigurasi lingkungan di server (misalnya `/etc/omnifetch/omnifetch.env` atau `.env` lokal):

```bash
# ==========================================
# KONFIGURASI SERVER OMNIFETCH
# ==========================================
PORT=6666
VPS_ROOT=/var/lib/omnifetch/data/temporary
RETENTION_SECONDS=7200
MAX_CONCURRENT_JOBS=4

# ==========================================
# PENYIMPANAN PERPUSTAKAAN MEDIA
# Untuk VPS dengan Secondary HDD (misal /mnt/hdd berkapasitas 1.8 TB):
# ==========================================
LIBRARY_ROOT=/mnt/hdd/perpustakaan_film
LIBRARY_MOVIES_DIR=/mnt/hdd/perpustakaan_film/Movies
# Catatan: Jika server mendeteksi /mnt/hdd terpasang, OmniFetch secara otomatis
# mengalihkan perpustakaan ke /mnt/hdd/perpustakaan_film agar tidak membebani SSD sistem.
DATA_DIR=/var/lib/omnifetch/data
LIBRARY_DB_PATH=/var/lib/omnifetch/data/omnifetch-library.sqlite3

# ==========================================
# INTEGRASI JELLYFIN SERVER
# ==========================================
# URL akses lokal backend ke Jellyfin (biasanya localhost:8096)
JELLYFIN_INTERNAL_URL=http://127.0.0.1:8096

# URL publik yang dibuka oleh pengguna (browser / aplikasi)
# Contoh: http://192.168.1.50:8096 atau https://jellyfin.domainpribadi.id
JELLYFIN_PUBLIC_URL=http://IP_SERVER_ATAU_DOMAIN:8096

# API Key Jellyfin yang dibuat dari Dashboard Admin Jellyfin
# PERINGATAN: Jangan pernah membocorkan API Key ini ke ekstensi atau klien publik!
JELLYFIN_API_KEY=PLACEHOLDER_API_KEY_DARI_ADMIN_JELLYFIN

# ID Library 'Movies' di Jellyfin (opsional, untuk scan terarah)
JELLYFIN_LIBRARY_ID=PLACEHOLDER_LIBRARY_ID_MOVIES

# Root path media sebagaimana dibaca oleh sistem Jellyfin
JELLYFIN_MEDIA_ROOT=/var/lib/omnifetch/data/library
```

---

## 3. PANDUAN INSTALASI JELLYFIN PADA LINUX (SYSTEMD, TANPA DOCKER)

Panduan ini mengikuti dokumentasi resmi Jellyfin untuk Debian / Ubuntu:

### Langkah 1: Pasang Kebutuhan Awal
```bash
sudo apt update
sudo apt install -y curl gnupg apt-transport-https ffmpeg
```

### Langkah 2: Tambahkan Repository Resmi Jellyfin
```bash
# Buat direktori keyring
sudo mkdir -p /etc/apt/keyrings

# Unduh GPG key resmi Jellyfin
curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg

# Tambahkan repository sesuai versi distro (Ubuntu/Debian)
# Untuk Ubuntu:
echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu $(lsb_release -c -s) main" | sudo tee /etc/apt/sources.list.d/jellyfin.list

# Untuk Debian:
# echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] https://repo.jellyfin.org/debian $(lsb_release -c -s) main" | sudo tee /etc/apt/sources.list.d/jellyfin.list
```

### Langkah 3: Pasang Paket Jellyfin
```bash
sudo apt update
sudo apt install -y jellyfin
```

### Langkah 4: Verifikasi Service Systemd Jellyfin
```bash
sudo systemctl enable jellyfin
sudo systemctl start jellyfin
sudo systemctl status jellyfin
```

Jellyfin kini berjalan pada port `8096` dan dapat diakses melalui browser: `http://IP_SERVER:8096`.

---

## 4. STRUKTUR FOLDER, PERMISSION & SYSTEMD PERPUSTAKAAN

Agar OmniFetch dapat menulis file media dan Jellyfin dapat membacanya tanpa masalah izin:

### 1. Buat Direktori Media (Pada HDD /mnt/hdd)
Untuk menyimpan perpustakaan di HDD (misal `/mnt/hdd` 1.8 TB):
```bash
# Buat folder khusus perpustakaan film di HDD
sudo mkdir -p /mnt/hdd/perpustakaan_film/Movies
sudo mkdir -p /mnt/hdd/perpustakaan_film/staging

# Beri kepemilikan ke user linux Anda (misal atrbpnaceh) dan izin akses
sudo chown -R $USER:$USER /mnt/hdd/perpustakaan_film
sudo chmod -R 775 /mnt/hdd/perpustakaan_film
```

Atau jika memakai direktori default di SSD sistem:
```bash
sudo mkdir -p /var/lib/omnifetch/data/temporary
sudo mkdir -p /var/lib/omnifetch/data/staging
sudo mkdir -p /var/lib/omnifetch/data/library/Movies
```

### 2. Atur Grup dan Permission Filesystem
Tambahkan user service OmniFetch dan user `jellyfin` ke dalam grup bersama (misalnya grup `media`):

```bash
# Buat grup media
sudo groupadd -f media

# Tambahkan user jellyfin dan user omnifetch ke grup media
sudo usermod -aG media jellyfin
sudo usermod -aG media $USER

# Atur kepemilikan direktori media
sudo chown -R $USER:media /var/lib/omnifetch/data/library
sudo chmod -R 775 /var/lib/omnifetch/data/library

# Pasang SGID bit agar setiap subfolder baru otomatis mewarisi grup media
sudo chmod g+s /var/lib/omnifetch/data/library/Movies
```

### 3. Contoh Service Systemd untuk OmniFetch Backend
Berkas `/etc/systemd/system/omnifetch.service`:

```ini
[Unit]
Description=OmniFetch Studio Media Engine & Library Backend
After=network.target jellyfin.service

[Service]
Type=simple
User=YOUR_LINUX_USER
Group=media
WorkingDirectory=/path/ke/folder/VideoDownloader
EnvironmentFile=/etc/omnifetch/omnifetch.env
ExecStart=/path/ke/folder/VideoDownloader/.venv/bin/python backend/main.py --vps --port 6666 --download-dir /var/lib/omnifetch/data/temporary
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Aktifkan service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable omnifetch
sudo systemctl start omnifetch
```

---

## 5. SETUP LIBRARY MOVIES DI JELLYFIN & MEMBUAT API KEY

### Langkah 1: Selesaikan Wizard Awal Jellyfin
1. Buka browser: `http://IP_SERVER:8096`.
2. Pilih bahasa, buat akun Administrator awal (misalnya username: `admin_jellyfin`).

### Langkah 2: Buat Library "Movies"
1. Masuk ke **Dashboard** -> **Libraries** -> **Add Media Library**.
2. **Content type**: Pilih `Movies`.
3. **Display name**: Ketik `Film Perpustakaan` atau `Movies`.
4. **Folders**: Klik tombol `+` dan masukkan path: `/var/lib/omnifetch/data/library/Movies`.
5. **Metadata downloaders**: Centang *TheMovieDb* atau *The Open Movie Database* jika diinginkan.
6. **Subtitle downloaders**: Opsional (karena subtitle sudah disediakan otomatis oleh OmniFetch).
7. Klik **OK** untuk menyimpan.

### Langkah 3: Dapatkan Library ID
1. Pada menu **Dashboard** -> **Libraries**, klik titik tiga (⋮) pada kartu library Movies Anda.
2. Catat ID pada URL browser (atau biarkan kosong agar OmniFetch memicu scan seluruh library jika ID belum ditentukan).

### Langkah 4: Buat API Key untuk Backend OmniFetch
1. Masuk ke **Dashboard** -> **Advanced** -> **API Keys**.
2. Klik tombol `+` (Add).
3. Masukkan App Name: `OmniFetch-Backend`.
4. Salin API Key yang dihasilkan.
5. Tempel ke berkas environment `/etc/omnifetch/omnifetch.env` pada baris:
   ```bash
   JELLYFIN_API_KEY=paste_api_key_disini
   ```
6. Muat ulang backend OmniFetch:
   ```bash
   sudo systemctl restart omnifetch
   ```

### Langkah 5: Buat Akun Penonton (Non-Admin Viewer)
1. Masuk ke **Dashboard** -> **Users** -> **Add User**.
2. Beri nama akun penonton, misalnya `keluarga` atau `tv_kamar`.
3. **PENTING**: Hapus centang pada *"Allow this user to manage the server"* (Jangan berikan hak admin).
4. Pastikan opsi *"Enable playback"* dicentang.
5. Gunakan akun ini saat login di Android TV.

---

## 6. PANDUAN PENGGUNAAN EKSTENSI & ANDROID TV

### Menggunakan Ekstensi Browser
1. Buka halaman streaming media di browser (Chrome/Brave/Edge/Firefox).
2. Putar video selama 2–3 detik agar manifes HLS/video terdeteksi.
3. Buka popup **OmniFetch Companion** pada toolbar.
4. Klik tombol ungu **"Simpan ke Perpustakaan"**.
5. Dialog konfirmasi judul & tahun akan muncul:
   - Judul dan tahun terdeteksi otomatis dari halaman.
   - Anda dapat mengoreksi judul atau tahun jika diperlukan.
   - Subtitle bahasa Indonesia akan diprioritaskan otomatis.
6. Klik **"Ya, Simpan ke Perpustakaan"**.
7. Ekstensi akan memantau tahapan proses secara transparan:
   - `Mengunduh media`
   - `Memeriksa container (ffprobe)`
   - `Memindahkan ke perpustakaan`
   - `Memindai katalog Jellyfin`
   - `Siap ditonton`
8. Setelah selesai, tombol **"▶️ Buka di Jellyfin"** akan aktif untuk membuka film di web Jellyfin.

### Menggunakan Aplikasi Resmi Jellyfin di Android TV
1. Buka Google Play Store di Android TV / Google TV.
2. Cari dan pasang aplikasi resmi: **Jellyfin for Android TV**.
3. Buka aplikasi, pilih **Add Server**.
4. Masukkan alamat server Jellyfin:
   - Di jaringan lokal/rumah: `http://192.168.1.50:8096`
   - Melalui reverse proxy publik: `https://jellyfin.domainpribadi.id`
5. Masukkan akun penonton non-admin (`tv_kamar`) beserta sandi.
6. Koleksi film perpustakaan Anda akan langsung tampil dengan poster, judul, tahun, dan subtitle Indonesia.

---

## 7. KOMPATIBILITAS FORMAT, DIRECT PLAY & TRANSCODING

### Direct Play vs Transcoding
- **Direct Play** terjadi saat format video (H.264/AAC), audio, dan subtitle (.srt) didukung langsung oleh perangkat TV. Pada mode ini, beban CPU/GPU server mendekati 0% (hanya membaca disk dan mengirim stream jaringan).
- **Transcoding** terjadi jika TV tidak mendukung codec sumber (misalnya codec khusus atau subtitle bitmap ASS/SSA). Server akan mengonversi video secara real-time, yang dapat membebani CPU VPS.
- **Standar Output OmniFetch**:
  - Video: MP4 / MKV (H.264/AAC standard compatible).
  - Subtitle: Format SubRip (.srt) UTF-8 terpisah berdampingan. Format ini didukung 100% oleh Jellyfin Android TV untuk Direct Play tanpa memicu transcoding video!

### Catatan Penting Mengenai Cloudflare Tunnel
- **Kebijakan Layanan Cloudflare (Section 2.8)**: Cloudflare CDN melarang streaming video volume tinggi secara kontinu melalui Cloudflare Tunnel gratis tanpa paket video khusus (Stream).
- **Aplikasi Android TV**: Cloudflare Zero Trust Access (login berbasis halaman browser/Google SSO) **tidak kompatibel** dengan aplikasi resmi Jellyfin Android TV karena aplikasi TV tidak memiliki browser internal untuk menyelesaikan web prompt.
- **Rekomendasi Akses Luar**:
  1. Gunakan VPN pribadi (WireGuard / Tailscale) antara Android TV dan server untuk akses tercepat tanpa eksposur port publik.
  2. Atau gunakan Reverse Proxy HTTPS mandiri (Nginx / Caddy dengan SSL Let's Encrypt) yang diarahkan langsung ke port `8096`.

---

## 8. KEBIJAKAN PENYIMPANAN, ENOSPC & RETENSI

| Kategori | Lokasi | Kebijakan Retensi |
|---|---|---|
| Unduhan Biasa | `data/temporary/` | Dihapus otomatis setelah **2 jam** (`RETENTION_SECONDS`) |
| Staging Impor | `data/staging/` | Hanya sementara saat download berjalan; dibersihkan setelah diverifikasi |
| Perpustakaan Film | `data/library/Movies/` | **PERMANEN**. Kebal terhadap pembersihan otomatis 2 jam |
| Database Katalog | `data/omnifetch-library.sqlite3` | **PERMANEN**. Menggunakan SQLite WAL Mode |

### Perlindungan ENOSPC (Disk Penuh)
- OmniFetch menerapkan batas aman minimum **2.0 GB** ruang kosong.
- Sebelum mengunduh atau memindahkan file library, kapasitas partisi hard disk diverifikasi terlebih dahulu.
- Jika ruang penyimpanan tidak mencukupi, proses ditolak dengan pesan error yang ramah dan aman.

---

## 9. PROSEDUR BACKUP, RESTORE & ROLLBACK

### Cara Backup Database & Koleksi
```bash
# 1. Backup Database SQLite (gunakan vacuum into agar aman dari transaksi aktif)
sqlite3 /var/lib/omnifetch/data/omnifetch-library.sqlite3 ".backup '/var/backups/omnifetch-library-$(date +%F).sqlite3'"

# 2. Backup Metadata NFO dan Subtitle (ringan dan cepat)
rsync -av --include="*/" --include="*.nfo" --include="*.srt" --include="*.jpg" --exclude="*" \
  /var/lib/omnifetch/data/library/Movies/ /var/backups/library-metadata/
```

### Cara Rollback Aplikasi Tanpa Menghapus Koleksi Film
Jika perlu melakukan rollback kode backend OmniFetch:
1. Matikan service: `sudo systemctl stop omnifetch`
2. Pindah git commit atau ganti file backend.
3. Folder media permanen di `/var/lib/omnifetch/data/library/Movies` terpisah secara fisik dari repository git dan tidak akan tersentuh.
4. Nyalakan kembali service: `sudo systemctl start omnifetch`
5. Data koleksi tetap utuh di SQLite dan disk.

---

## 10. DAFTAR PORT & PROTOKOL AKSES

| Port | Protokol | Layanan | Keterangan |
|---|---|---|---|
| `6666` | HTTP / TCP | OmniFetch Studio Backend | API status, download engine, dan manajemen perpustakaan |
| `8096` | HTTP / TCP | Jellyfin Web & Android TV API | Pemutaran video, katalog, dan streaming |
| `8920` | HTTPS / TCP | Jellyfin HTTPS (Opsional) | Jika sertifikat SSL dipasang langsung di Jellyfin |
| `1900` | UDP | DLNA Discovery (Opsional) | Deteksi perangkat otomatis dalam jaringan lokal |
| `7359` | UDP | Client Discovery (Opsional) | Memudahkan aplikasi TV menemukan server lokal |

---

## 11. PAKET EKSTENSI TERBARU

Arsip ekstensi siap pasang berada di root folder:
- **`OmniFetch-Companion-v2.1.4.zip`** (Paket rilis v2.1.4 dengan tombol *"Simpan ke Perpustakaan"* dan integrasi Jellyfin)
- **`extension.zip`** (Salinan mirror rilis terbaru)

Cara memuat di Chrome/Brave/Edge:
1. Buka `chrome://extensions/`.
2. Aktifkan **Developer mode** di pojok kanan atas.
3. Ekstrak file zip ke folder, lalu klik **Load unpacked** dan pilih folder `chrome-extension`.
4. Ekstensi siap digunakan untuk mengunduh atau menyimpan ke perpustakaan.
