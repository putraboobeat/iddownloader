import { BASE_URL, Config } from './config.js';

export class APIClient {
    static async sendJob(payload, sessionToken = null) {
        let token = sessionToken;
        if (!token) {
            const info = await Config.getAppInfo();
            token = info.token;
        }
        
        // 1. Validasi Autentikasi lokal
        if (!token) {
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

        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 15000);

        try {
            const res = await fetch(`${baseUrl}/api/start`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-App-Session': token
                },
                body: JSON.stringify(payload),
                signal: controller.signal
            });

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
                const serverMsg = data?.error || res.statusText || 'Gagal memproses';
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
        } catch (e) {
            if (e.name === 'AbortError') {
                const err = new Error("Waktu koneksi habis (timeout 15 detik). Server tidak merespons.");
                err.code = "TIMEOUT";
                throw err;
            }
            if (e.code) throw e;
            const err = new Error("Tidak dapat terhubung ke server dw.pmlab.id.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    static async inspectUrl(targetUrl, sessionToken = null) {
        let token = sessionToken;
        if (!token) {
            const info = await Config.getAppInfo();
            token = info.token;
        }

        if (!token) {
            const err = new Error("Sesi belum terhubung atau kedaluwarsa.");
            err.code = "AUTH_MISSING";
            throw err;
        }

        if (!targetUrl || typeof targetUrl !== 'string' || !targetUrl.trim()) {
            const err = new Error("URL target tidak valid.");
            err.code = "INVALID_MEDIA";
            throw err;
        }

        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 20000);

        try {
            const res = await fetch(`${baseUrl}/api/inspect?url=${encodeURIComponent(targetUrl.trim())}`, {
                headers: {
                    'X-App-Session': token
                },
                signal: controller.signal
            });

            if (res.status === 401) {
                const err = new Error("Sesi belum terhubung atau kedaluwarsa (401 Unauthorized).");
                err.code = "AUTH_EXPIRED";
                throw err;
            }

            const contentType = res.headers.get('content-type') || '';
            if (contentType.includes('text/html')) {
                const err = new Error("Server mengembalikan HTML, bukan JSON.");
                err.code = "HTML_RESPONSE";
                throw err;
            }

            let data;
            try {
                data = await res.json();
            } catch (e) {
                const err = new Error("Format respons analisis server tidak valid.");
                err.code = "INVALID_RESPONSE";
                throw err;
            }

            if (!res.ok) {
                const err = new Error(data?.error || `Gagal menganalisis metadata (${res.status})`);
                err.code = "INSPECT_FAILED";
                throw err;
            }
            return data;
        } catch (e) {
            if (e.name === 'AbortError') {
                const err = new Error("Waktu analisis habis (timeout).");
                err.code = "TIMEOUT";
                throw err;
            }
            if (e.code) throw e;
            const err = new Error("Tidak dapat terhubung ke server dw.pmlab.id.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    static async getJobStatus(jobId, sessionToken = null) {
        let token = sessionToken;
        if (!token) {
            const info = await Config.getAppInfo();
            token = info.token;
        }

        if (!token) {
            const err = new Error("Sesi belum terhubung.");
            err.code = "AUTH_MISSING";
            throw err;
        }

        if (!jobId) {
            const err = new Error("Job ID tidak boleh kosong.");
            err.code = "INVALID_PARAM";
            throw err;
        }

        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000);

        try {
            const res = await fetch(`${baseUrl}/api/status?job_id=${encodeURIComponent(jobId)}`, {
                headers: {
                    'X-App-Session': token
                },
                signal: controller.signal
            });

            if (res.status === 401) {
                const err = new Error("Sesi belum terhubung atau kedaluwarsa (401 Unauthorized).");
                err.code = "AUTH_EXPIRED";
                throw err;
            }

            if (res.status === 404) {
                const err = new Error("Pekerjaan tidak ditemukan di server (404).");
                err.code = "JOB_NOT_FOUND";
                throw err;
            }

            const contentType = res.headers.get('content-type') || '';
            if (contentType.includes('text/html')) {
                const err = new Error("Server mengembalikan HTML, bukan JSON.");
                err.code = "HTML_RESPONSE";
                throw err;
            }

            let data;
            try {
                data = await res.json();
            } catch (e) {
                const err = new Error("Format respons status server tidak valid.");
                err.code = "INVALID_RESPONSE";
                throw err;
            }

            if (!res.ok) {
                const err = new Error(data?.error || `Server status error (${res.status})`);
                err.code = res.status >= 500 ? "HTTP_500" : "SERVER_ERROR";
                throw err;
            }

            // Validasi respons: jika server mengembalikan { jobs: [] } dan bukan info job yang dicari
            if (data.jobs && Array.isArray(data.jobs) && data.jobs.length === 0 && !data.job_id) {
                const err = new Error("Pekerjaan tidak ditemukan di server.");
                err.code = "JOB_NOT_FOUND";
                throw err;
            }

            return data;
        } catch (e) {
            if (e.name === 'AbortError') {
                const err = new Error("Waktu pengambilan status habis (timeout 10s).");
                err.code = "TIMEOUT";
                throw err;
            }
            if (e.code) throw e;
            const err = new Error("Tidak dapat terhubung ke server dw.pmlab.id.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    static async stopJob(jobId, sessionToken = null) {
        let token = sessionToken;
        if (!token) {
            const info = await Config.getAppInfo();
            token = info.token;
        }

        if (!token) {
            const err = new Error("Sesi belum terhubung.");
            err.code = "AUTH_MISSING";
            throw err;
        }

        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000);

        try {
            const res = await fetch(`${baseUrl}/api/stop`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-App-Session': token
                },
                body: JSON.stringify({ job_id: jobId }),
                signal: controller.signal
            });

            if (res.status === 401) {
                const err = new Error("Sesi kedaluwarsa (401 Unauthorized).");
                err.code = "AUTH_EXPIRED";
                throw err;
            }

            if (res.status === 404) {
                const err = new Error("Pekerjaan tidak ditemukan atau tidak diizinkan di server.");
                err.code = "JOB_NOT_FOUND";
                throw err;
            }

            const contentType = res.headers.get('content-type') || '';
            if (contentType.includes('text/html')) {
                const err = new Error("Server mengembalikan HTML, bukan JSON.");
                err.code = "HTML_RESPONSE";
                throw err;
            }

            let data;
            try {
                data = await res.json();
            } catch (e) {
                const err = new Error("Format respons stop server tidak valid.");
                err.code = "INVALID_RESPONSE";
                throw err;
            }

            if (!res.ok) {
                const err = new Error(data?.error || `Gagal menghentikan tugas (${res.status})`);
                err.code = "STOP_FAILED";
                throw err;
            }

            return data;
        } catch (e) {
            if (e.name === 'AbortError') {
                const err = new Error("Waktu penghentian pekerjaan habis (timeout).");
                err.code = "TIMEOUT";
                throw err;
            }
            if (e.code) throw e;
            const err = new Error(e.message || "Gagal menghubungi server untuk menghentikan pekerjaan.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    static async getDiagnostic(jobId, sessionToken = null) {
        let token = sessionToken;
        if (!token) {
            const info = await Config.getAppInfo();
            token = info.token;
        }

        if (!token) {
            const err = new Error("Sesi belum terhubung.");
            err.code = "AUTH_MISSING";
            throw err;
        }

        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 12000);

        try {
            const res = await fetch(`${baseUrl}/api/diagnostic?job_id=${encodeURIComponent(jobId)}`, {
                headers: {
                    'X-App-Session': token
                },
                signal: controller.signal
            });

            if (res.status === 401) {
                const err = new Error("Sesi kedaluwarsa (401).");
                err.code = "AUTH_EXPIRED";
                throw err;
            }

            const contentType = res.headers.get('content-type') || '';
            if (contentType.includes('text/html')) {
                const err = new Error("Server mengembalikan HTML, bukan JSON.");
                err.code = "HTML_RESPONSE";
                throw err;
            }

            let data;
            try {
                data = await res.json();
            } catch (e) {
                const err = new Error("Format respons diagnostik tidak valid.");
                err.code = "INVALID_RESPONSE";
                throw err;
            }

            if (!res.ok) {
                const err = new Error(data?.error || `Gagal mengambil diagnostik (${res.status})`);
                err.code = "DIAGNOSTIC_FAILED";
                throw err;
            }

            return data;
        } catch (e) {
            if (e.name === 'AbortError') {
                const err = new Error("Waktu pengambilan diagnostik habis (timeout).");
                err.code = "TIMEOUT";
                throw err;
            }
            if (e.code) throw e;
            const err = new Error("Tidak dapat mengambil diagnostik dari server.");
            err.code = "NETWORK_ERROR";
            throw err;
        } finally {
            clearTimeout(timeoutId);
        }
    }

    static async checkSession(token) {
        if (!token) return { ok: false, message: 'Token sesi kosong' };
        const baseUrl = await Config.getBaseUrl();
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 8000);
            let res;
            try {
                res = await fetch(`${baseUrl}/api/status`, {
                    headers: { 'X-App-Session': token },
                    signal: controller.signal
                });
            } finally {
                clearTimeout(timeoutId);
            }
            
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

    static async syncCookies(domains = []) {
        const info = await Config.getAppInfo();
        if (!info.token || !info.cookieSync) return;

        const targetDomains = (Array.isArray(domains) && domains.length > 0)
            ? domains
            : (info.cookieDomains && info.cookieDomains.length > 0 ? info.cookieDomains : ['.instagram.com', '.youtube.com', '.tiktok.com']);

        let allCookies = [];
        for (const d of targetDomains) {
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
        
        const baseUrl = await Config.getBaseUrl();
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000);
        try {
            const res = await fetch(`${baseUrl}/api/upload-cookies`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/plain',
                    'X-App-Session': info.token
                },
                body: netscape,
                signal: controller.signal
            });
            if (!res.ok) throw new Error("Gagal mengunggah cookie");
        } catch (e) {
            // Jangan mencatat detail cookie atau token di log
            console.error("Gagal sinkronisasi cookie.");
            throw e;
        } finally {
            clearTimeout(timeoutId);
        }
    }
}
