cd ~/idxdownloader && git pull origin main && pm2 restart iddownloader


# Panduan Lengkap Workflow Git: Lokal (Mac) ➔ GitHub ➔ VPS

Dokumen ini berisi panduan standar setiap kali Anda melakukan perubahan kode di komputer lokal (Mac) dan ingin menerapkannya ke server VPS.

---

## 🔄 Alur Singkat (Ringkasan)

```text
[ Komputer Lokal (Mac) ]
        │  1. Edit kode & uji coba
        │  2. git add . && git commit -m "..."
        │  3. git push origin main
        ▼
   [ GitHub ]
        │
        ▼  4. Masuk ke terminal VPS
        │  5. cd ~/idxdownloader && git pull origin main
        │  6. pm2 restart iddownloader
[ Server VPS ]
```

---

## 💻 Bagian 1: Di Komputer Lokal (Mac)

Jalankan perintah ini di terminal Mac (folder proyek `VideoDownloader`):

### 1. Cek file yang diubah
```bash
git status
```

### 2. Simpan (Stage) & Buat Commit
```bash
git add .
git commit -m "fitur: deskripsi perubahan yang Anda buat"
```

### 3. Kirim (Push) ke GitHub
```bash
git push origin main
```
*Pastikan proses push berhasil tanpa error.*

---

## 🖥️ Bagian 2: Di Server VPS

Buka terminal SSH VPS Anda:
```bash
ssh -o ProxyCommand="cloudflared access ssh --hostname ssh.atrbpnaceh.com" atrbpnaceh@ssh.atrbpnaceh.com
```

Setelah masuk ke VPS, jalankan langkah berikut:

### 1. Masuk ke Folder Proyek
```bash
cd ~/idxdownloader
```

### 2. Tarik Perubahan Terbaru dari GitHub
```bash
git pull origin main
```

### 3. Terapkan Perubahan ke Aplikasi (Restart PM2)
```bash
pm2 restart iddownloader
```

### 4. Verifikasi Status & Log Aplikasi
```bash
# Cek apakah status 'online' (warna hijau)
pm2 status

# Cek log apakah ada error atau berhasil jalan
pm2 logs iddownloader --lines 20
```

---

## 🧩 Bagian 3: Jika Mengubah Ekstensi Chrome

Jika ada perubahan pada file di dalam folder `chrome-extension/`:

1. Buka browser Chrome di komputer Anda.
2. Ketik di bilah alamat: `chrome://extensions` lalu tekan **Enter**.
3. Cari ekstensi **OmniFetch Companion**.
4. Klik tombol **Reload / Muat Ulang (ikon panah melingkar ⟳)**.
5. Ekstensi sudah otomatis menggunakan kode terbaru.

---

## 🛠️ Tips & Mengatasi Masalah di VPS

### Masalah: `error: Your local changes to the following files would be overwritten by merge`
Jika di VPS ada file yang tidak sengaja teredit atau konflik saat `git pull`:
```bash
cd ~/idxdownloader
git reset --hard origin/main
git pull origin main
pm2 restart iddownloader
```

### Memeriksa Commit yang Sedang Aktif di VPS:
```bash
git log -1 --oneline
```

### Memperbarui yt-dlp di VPS (jika ada peringatan versi usang):
```bash
pip install -U yt-dlp || python3 -m pip install -U yt-dlp || yt-dlp -U
```

### Memeriksa Status Cloudflare Tunnel di VPS:
```bash
sudo systemctl status cloudflared-secondary
```
