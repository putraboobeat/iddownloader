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

## Ekstensi Chrome (OmniFetch Companion v2.1.3)

1. Buka `chrome://extensions` dan aktifkan **Developer mode**.
2. Klik **Load unpacked**, pilih folder `chrome-extension` (atau ekstrak `OmniFetch-Companion-v2.1.3.zip`).
3. Ekstensi terhubung otomatis ke server produksi `https://dw.pmlab.id`. Token sesi dibuat otomatis untuk isolasi antrean.
4. Buka halaman video (YouTube, Instagram, TikTok, X) atau putar film streaming (HLS/DASH).
5. Buka popup ekstensi, pilih format/resolusi atau subtitle, lalu klik **Unduh Sekarang**.
6. Pemantauan progres (persentase, kecepatan, ETA) tampil langsung di popup, dan file hasil otomatis tersimpan ke komputer (`Downloads/OmniFetch/<Judul Media>/`) tanpa mewajibkan pengguna membuka dashboard web.

Paket `OmniFetch-Companion-v2.1.3.zip` dan `omnifetch-companion-v2.zip` tersedia di root direktori. Detail lengkap: `chrome-extension/PANDUAN.txt`.

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
