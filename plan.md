# 🚀 OMNIFETCH - GOD-TIER DOWNLOADER ROADMAP

Dokumen ini berisi rencana pengembangan fitur-fitur level dewa dan penyempurnaan sistem untuk mengubah OmniFetch menjadi aplikasi downloader yang viral, memiliki retensi pengguna tinggi, dan mengalahkan kompetitor.

---

## 🔥 FASE 1: TURBO ENGINE & CORE UPGRADES (In Progress)
Fokus pada peningkatan kecepatan unduhan dan penanganan URL cerdas di sisi server (Backend).

- [x] **Integrasi Aria2c (Turbo Engine):** Memecah koneksi unduhan (YouTube/Instagram) menjadi 16-jalur (16 threads). Kecepatan unduhan meningkat hingga 500%.
- [ ] **Profile & Playlist Sweeper (Unduhan Massal):** 
  - Mendeteksi link *Profile* (Instagram/TikTok) atau *Playlist* (YouTube).
  - Mengunduh seluruh video dari profil/playlist tersebut secara otomatis.
  - Membungkus semua file hasil unduhan ke dalam satu file `.zip` agar mudah diunduh pengguna.
- [ ] **Auto-Bypass Mode (Anti-Ban):** 
  - Menyiapkan integrasi proxy rotator jika perlu.
  - Memungkinkan pengunduhan video Private dari Instagram/Facebook Group (lewat injeksi *cookies* aman).

---

## 🧙‍♂️ FASE 2: CHROME EXTENSION GOD-MODE
Mengubah ekstensi yang awalnya hanya untuk HLS/Streaming menjadi asisten pintar yang menyatu dengan website media sosial.

- [ ] **Tombol Unduh Injeksi Langsung (DOM Injection):**
  - Ekstensi mendeteksi pengguna sedang membuka Instagram, TikTok, atau YouTube.
  - Ekstensi menambahkan tombol "⬇️ Unduh (OmniFetch)" tepat di sebelah tombol Like/Share/Save bawaan website.
  - Pengguna cukup klik tombol tersebut, dan video langsung dikirim ke server OmniFetch (VPS) di latar belakang.
- [ ] **Mode "Klik Kanan" (Context Menu):** Tambahan menu "Unduh dengan OmniFetch" saat pengguna mengklik kanan pada sembarang video di web.

---

## 💎 FASE 3: FITUR VIRAL & KENYAMANAN PENGGUNA (UX)
Fitur-fitur untuk membuat pengguna nyaman dan aplikasi menyebar dari mulut ke mulut.

- [ ] **Fitur PWA (Install to Homescreen):** 
  - Pengguna Android/iOS bisa menekan "Install App" langsung dari browser.
  - OmniFetch muncul sebagai aplikasi di layar HP (Homescreen) tanpa harus di-download melalui PlayStore/AppStore.
- [ ] **Shareable Link (Link Berbagi 2 Jam):**
  - Setelah berhasil diunduh, aplikasi menghasilkan link khusus (misal: `omnifetch.app/d/xyz123`).
  - Link bisa dikirim ke WhatsApp/Telegram. Teman yang mengklik link bisa langsung mengunduh file MP4/MP3 tersebut ke HP mereka sebelum file dihapus dari server (2 jam).
- [ ] **Riwayat Unduhan Lokal (Local History):**
  - Aplikasi menyimpan 10 - 20 history unduhan terakhir (Titel Video & Link) di *localStorage* browser pengguna.
  - Jika pengguna terhapus filenya, mereka bisa men-download ulang tanpa mencari lagi di medsos.
- [ ] **Estimasi Ukuran File (File Size):** Menampilkan estimasi "MB" di sebelah resolusi kualitas video sebelum pengguna mulai men-download.
- [ ] **Audio Tagger (Cover Art):** Jika mengunduh format MP3 (Spotify/YouTube), server otomatis menanamkan *thumbnail* video sebagai sampul file (Cover Art).

---

## ✂️ FASE 4: MINI STUDIO EDITING
Memberikan nilai lebih (Value Add) bagi *Content Creator*.

- [ ] **Audio Cutter (Ringtone Maker):** UI untuk memotong menit ke-berapa sebuah lagu (YouTube) yang mau diunduh dan diubah jadi MP3.
- [ ] **GIF Maker:** Mengubah cuplikan video singkat (misal 5 detik) langsung menjadi animasi `.gif` (untuk stiker WhatsApp).

---
*Dokumen ini akan terus diperbarui seiring berjalannya proses pengembangan.*
