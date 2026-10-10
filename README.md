# OmniFetch — Universal Media Studio

Studio pengunduh media serbaguna dengan antarmuka modern, bersih, dan cepat. Mendukung unduhan streaming film HLS, Instagram Reels & Carousel (otomatis pack .ZIP), YouTube 4K, audio MP3/FLAC, serta ribuan situs media sosial dengan integrasi yt-dlp & zero-cache engine. Dilengkapi ekstensi Chrome pendamping (OmniFetch Companion).

## Kebutuhan

- macOS / Linux (VPS) / Windows, Python 3.10+.
- FFmpeg & yt-dlp tersedia pada PATH.
- Paket Python pada `requirements.txt`: Playwright dan yt-dlp.

## Menjalankan

1. Simpan seluruh proyek dalam satu folder.
2. Jalankan `Pasang.command` untuk membuat lingkungan Python dan memasang dependensi.
3. Jalankan `Mulai.command`.
4. Browser membuka alamat lokal `http://127.0.0.1:PORT`. Biarkan Terminal berjalan.

Alternatif terminal:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

## Ekstensi Chrome

1. Buka `chrome://extensions` dan aktifkan Developer mode.
2. Klik Load unpacked, pilih folder `chrome-extension`.
3. Buka Pengaturan ekstensi, isi alamat aplikasi lokal yang aktif.
4. Muat ulang halaman film, selesaikan verifikasi dan lewati iklan seperti biasa.
5. Panel otomatis menyediakan Copy link JSON atau Kirim ke Downloader.
6. Playlist config diprioritaskan dan subtitle Indonesia dari sumber yang sama dipilih bila terdeteksi. Periksa sumber jika hasil yang ditemukan adalah iklan.

Paket `Video-Downloader-Extension.zip` juga tersedia dari tombol unduh ekstensi pada aplikasi. Pemasangan lewat Chrome Web Store belum tersedia. Detail: `chrome-extension/PANDUAN.txt` dan panduan dalam aplikasi.

## Penyimpanan

Folder utama yang dipilih akan berisi subfolder nama film. Video dan subtitle utama memakai nama yang sama, misalnya `Film/Film.mp4` dan `Film/Film.srt`. VTT dikonversi ke SRT UTF-8 dengan FFmpeg. File yang sudah ada tidak ditimpa; subtitle tambahan dan unduhan video berulang mendapat penamaan terpisah.

## Diagnostik

Status menampilkan progres, kecepatan, estimasi waktu, dan retry. Salin status & log menyertakan URL sumber lengkap, termasuk token akses: bagikan hanya kepada pihak yang Anda tuju. Server dapat menolak akses (403) atau mengalami timeout. Aplikasi tidak melewati CAPTCHA; akses browser tidak menjamin URL dapat diunduh oleh aplikasi.

## Pengujian

```sh
.venv/bin/python -m unittest discover -s tests -v
RUN_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

Pengujian browser memerlukan Chrome, FFmpeg, dan izin menjalankan server lokal. Pengujian menggunakan video sintetis.

File media hasil unduhan, lingkungan Python, dan cookie sesi tidak disertakan dalam repositori.
