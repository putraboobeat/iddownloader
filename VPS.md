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
- Satu jam dihitung setelah pekerjaan selesai, gagal, atau dihentikan. File parsial juga dibersihkan.
- Pengecekan setiap 15 detik: penghapusan normal terjadi antara 1 jam dan 1 jam 15 detik.
- Unduhan yang masih aktif tidak dihapus, sehingga bisa menggunakan disk lebih dari satu jam.
- Jadwal tersimpan di `.expiry.json` dalam folder pekerjaan. Saat aplikasi restart, pekerjaan yang terputus menggunakan tenggat awal pembuatan + 1 jam; yang sudah lewat tenggat dibersihkan.
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

## Batasan VPS saat ini

- Gunakan link playlist/subtitle dari ekstensi Chrome di komputer sendiri. Deteksi halaman dengan Chrome interaktif membutuhkan layar/browser, sehingga tidak cocok untuk VPS headless dan CAPTCHA.
- Ekstensi masih menerima alamat lokal HTTP saja. Gunakan Copy link JSON lalu tempel pada web VPS; dukungan kirim langsung ke HTTPS belum ditambahkan.
- Hasil tersimpan di VPS, bukan otomatis pada komputer pengguna. Ambil melalui SFTP/SCP sebelum tenggat 1 jam. Tombol unduh hasil dari VPS ke browser belum tersedia.
- Aplikasi memiliki satu antrean/status bersama dan hanya satu pekerjaan aktif; bukan layanan multi-user.
- Cloudflare Tunnel hanya membawa akses UI. Ia tidak memperbaiki timeout atau penolakan 403 dari sumber video.
