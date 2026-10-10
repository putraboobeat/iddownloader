import { BASE_URL, Config } from './config.js';

export class APIClient {
    static async sendJob(payload) {
        const info = await Config.getAppInfo();
        
        // 1. Validasi Autentikasi lokal
        if (!info.token) {
            const err = new Error("Sesi belum terhubung atau kedaluwarsa. Silakan masukkan Session Token di Pengaturan.");
            err.code = "AUTH_MISSING";
            throw err;
        }

        // 2. Validasi Payload Sumber Media
        const targetUrl = payload.urls || payload.url;
        if (!targetUrl || typeof targetUrl !== 'string' || !targetUrl.trim()) {
            const err = new Error("Sumber media tidak valid: URL kosong atau tidak dikenali.");
            err.code = "INVALID_MEDIA";
            throw err;
        }

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000);

        let res;
        try {
            res = await fetch(`${BASE_URL}/api/start`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-App-Session': info.token
                },
                body: JSON.stringify(payload),
                signal: controller.signal
            });
        } catch (e) {
            clearTimeout(timeoutId);
            if (e.name === 'AbortError') {
                const err = new Error("Waktu koneksi habis (timeout 15 detik). Server tidak merespons.");
                err.code = "TIMEOUT";
                throw err;
            }
            const err = new Error("Tidak dapat terhubung ke server dw.pmlab.id.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }

        // 3. Klasifikasi Status HTTP
        if (res.status === 401) {
            const err = new Error("Sesi belum terhubung atau kedaluwarsa (401 Unauthorized). Silakan periksa Session Token di Pengaturan.");
            err.code = "AUTH_EXPIRED";
            throw err;
        }

        if (res.status === 404) {
            const err = new Error("Endpoint atau versi server tidak kompatibel (404).");
            err.code = "INCOMPATIBLE_VERSION";
            throw err;
        }

        // 4. Deteksi respons HTML (misal Cloudflare block, reverse proxy error, atau redirect login)
        const contentType = res.headers.get('content-type') || '';
        if (contentType.includes('text/html')) {
            const err = new Error("Server mengembalikan halaman web (HTML), bukan API JSON. Periksa koneksi atau Cloudflare.");
            err.code = "HTML_RESPONSE";
            throw err;
        }

        let data;
        try {
            data = await res.json();
        } catch (e) {
            const err = new Error("Format respons server tidak valid.");
            err.code = "INVALID_RESPONSE";
            throw err;
        }

        // 5. Penanganan Error dari Server
        if (!res.ok) {
            const serverMsg = data.error || res.statusText || 'Gagal memproses';
            if (res.status === 400) {
                const err = new Error(`Sumber media tidak valid: ${serverMsg}`);
                err.code = "INVALID_MEDIA";
                throw err;
            }
            const err = new Error(`Pekerjaan ditolak server (${res.status}): ${serverMsg}`);
            err.code = "REJECTED";
            throw err;
        }

        // 6. Validasi Kontrak Pekerjaan Diterima
        if (!data || !data.job_id) {
            const err = new Error("Pekerjaan ditolak server: respons tidak menyertakan Job ID.");
            err.code = "CONTRACT_MISMATCH";
            throw err;
        }

        return data; // { job_id: "...", status: "queued" }
    }

    static async checkSession(token) {
        if (!token) return { ok: false, message: 'Token sesi kosong' };
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 8000);
            const res = await fetch(`${BASE_URL}/api/status`, {
                headers: { 'X-App-Session': token },
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            
            if (res.status === 401) {
                return { ok: false, code: '401', message: 'Sesi belum terhubung atau ditolak server (401).' };
            }
            if (!res.ok) {
                return { ok: false, code: String(res.status), message: `Server merespons status ${res.status}.` };
            }
            return { ok: true, message: 'Koneksi dan sesi valid.' };
        } catch (e) {
            return { ok: false, code: 'NETWORK_ERROR', message: 'Tidak dapat terhubung ke server dw.pmlab.id.' };
        }
    }

    static async syncCookies(domains) {
        const info = await Config.getAppInfo();
        if (!info.token || !info.cookieSync) return;

        let allCookies = [];
        for (const d of domains) {
            try {
                const cookies = await chrome.cookies.getAll({ domain: d });
                allCookies = allCookies.concat(cookies);
            } catch (e) {}
        }
        
        if (allCookies.length === 0) return;
        
        let netscape = "# Netscape HTTP Cookie File\n# Generated by OmniFetch Companion\n";
        const seen = new Set();
        for (const c of allCookies) {
            const key = `${c.domain}|${c.name}|${c.path}`;
            if (seen.has(key)) continue;
            seen.add(key);
            netscape += `${c.domain}\t${c.domain.startsWith('.') ? 'TRUE' : 'FALSE'}\t${c.path}\t${c.secure ? 'TRUE' : 'FALSE'}\t${c.expirationDate ? Math.floor(c.expirationDate) : 0}\t${c.name}\t${c.value}\n`;
        }
        
        try {
            const res = await fetch(`${BASE_URL}/api/upload-cookies`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/plain',
                    'X-App-Session': info.token
                },
                body: netscape
            });
            if (!res.ok) throw new Error("Gagal mengunggah cookie");
        } catch (e) {
            // Jangan mencatat detail cookie atau token di log
            console.error("Gagal sinkronisasi cookie.");
            throw e;
        }
    }
}
