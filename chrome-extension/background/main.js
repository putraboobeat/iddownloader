import { Detector } from './detector.js';
import { APIClient } from './api.js';
import { Config, BASE_URL } from './config.js';
import { JobManager } from './jobs.js';

// === Persistent Submission Coordinator ===
// Melindungi pengiriman tugas dan retry lintas-popup serta bertahan saat worker restart
export class SubmissionCoordinator {
    static _lock = Promise.resolve();

    static async _withLock(fn) {
        const run = async () => {
            return await fn();
        };
        const next = this._lock.then(run, run);
        this._lock = next.catch(() => {});
        return next;
    }

    static generateOperationId() {
        return 'op_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);
    }

    /**
     * Mengambil klaim pengiriman untuk kunci tertentu.
     * Diserialisasikan via mutex worker untuk mencegah race condition concurrency.
     */
    static async acquire(key, operationIdOrOptions = null, maybeOptions = null) {
        if (!key) return false;

        let operationId = null;
        let options = {};
        if (typeof operationIdOrOptions === 'string') {
            operationId = operationIdOrOptions;
            if (typeof maybeOptions === 'object' && maybeOptions !== null) {
                options = maybeOptions;
            }
        } else if (typeof operationIdOrOptions === 'object' && operationIdOrOptions !== null) {
            options = operationIdOrOptions;
            operationId = options.operationId || null;
        }
        if (!operationId) {
            operationId = this.generateOperationId();
        }

        const force = !!options.force;

        return await this._withLock(async () => {
            const now = Date.now();
            const data = await chrome.storage.local.get('omni_in_flight_submissions');
            const submissions = data.omni_in_flight_submissions || {};

            // Transisikan status 'submitting' yang kehilangan kelanjutan setelah batas waktu (> 120s) menjadi 'uncertain'
            // JANGAN DIHAPUS! Lewatnya waktu tidak membuktikan backend belum menerima pekerjaan.
            // Status uncertain dipertahankan secara persisten untuk memblokir pengiriman ulang otomatis.
            let statusChanged = false;
            for (const [k, v] of Object.entries(submissions)) {
                if (v.status === 'submitting' && (now - (v.startedAt || 0) > 120000)) {
                    v.status = 'uncertain';
                    v.reason = 'SUBMISSION_ABANDONED_OR_TIMEOUT';
                    v.uncertainAt = now;
                    statusChanged = true;
                }
            }
            if (statusChanged) {
                await chrome.storage.local.set({ omni_in_flight_submissions: submissions });
            }

            const existing = submissions[key];
            if (existing) {
                if (existing.status === 'uncertain') {
                    // Blokir pengiriman ulang otomatis kecuali pengguna memilih 'force' secara eksplisit
                    if (!force) {
                        return false;
                    }
                } else if (existing.status === 'submitting') {
                    // Sedang dalam proses pengiriman oleh worker/popup lain
                    if (!force) {
                        return false;
                    }
                }
            }

            submissions[key] = {
                operationId,
                startedAt: now,
                status: 'submitting',
                forced: force
            };
            await chrome.storage.local.set({ omni_in_flight_submissions: submissions });
            return true;
        });
    }

    /**
     * Merilis klaim pengiriman.
     * - Hanya menghapus jika operationId cocok (klaim milik operasi ini).
     * - Jangan menghapus jika status adalah 'uncertain' (dipertahankan melintasi worker restart).
     */
    static async release(key, operationId = null) {
        if (!key) return;
        return await this._withLock(async () => {
            const data = await chrome.storage.local.get('omni_in_flight_submissions');
            const submissions = data.omni_in_flight_submissions || {};
            const existing = submissions[key];
            if (!existing) return;

            // Jika status adalah 'uncertain', rilis biasa di blok finally TIDAK BOLEH menghapusnya
            if (existing.status === 'uncertain') {
                return;
            }

            // Jika klaim sudah diambil alih oleh operasi baru dengan ID berbeda, jangan hapus klaim baru
            if (operationId && existing.operationId && existing.operationId !== operationId) {
                return;
            }

            delete submissions[key];
            await chrome.storage.local.set({ omni_in_flight_submissions: submissions });
        });
    }

    /**
     * Menandai pekerjaan sebagai tidak pasti (misal: timeout jaringan saat request ke server).
     * Status ini bertahan melintasi restart worker untuk mencegah submit ganda di backend non-idempotent.
     */
    static async markUncertain(key, operationId = null, reason = 'TIMEOUT', details = {}) {
        if (!key) return;
        return await this._withLock(async () => {
            const data = await chrome.storage.local.get('omni_in_flight_submissions');
            const submissions = data.omni_in_flight_submissions || {};
            const existing = submissions[key] || { operationId: operationId || this.generateOperationId() };

            if (!operationId || !existing.operationId || existing.operationId === operationId) {
                existing.status = 'uncertain';
                existing.reason = reason;
                existing.uncertainAt = Date.now();
                existing.details = details;
                submissions[key] = existing;
                await chrome.storage.local.set({ omni_in_flight_submissions: submissions });
            }
        });
    }

    /**
     * Menghapus catatan submission secara tuntas setelah pekerjaan terkonfirmasi diterima server.
     */
    static async clearSubmission(key, operationId = null) {
        if (!key) return;
        return await this._withLock(async () => {
            const data = await chrome.storage.local.get('omni_in_flight_submissions');
            const submissions = data.omni_in_flight_submissions || {};
            const existing = submissions[key];
            if (!existing) return;

            if (!operationId || !existing.operationId || existing.operationId === operationId) {
                delete submissions[key];
                await chrome.storage.local.set({ omni_in_flight_submissions: submissions });
            }
        });
    }

    static async getSubmission(key) {
        const data = await chrome.storage.local.get('omni_in_flight_submissions');
        const submissions = data.omni_in_flight_submissions || {};
        return submissions[key] || null;
    }

    static async getAllSubmissions() {
        const data = await chrome.storage.local.get('omni_in_flight_submissions');
        return data.omni_in_flight_submissions || {};
    }
}

/**
 * Penyamaran menyeluruh untuk laporan diagnostik default:
 * - Seluruh nilai query URL disamarkan (bukan hanya daftar nama parameter tertentu)
 * - Fragment sensitif (#token=xxx dsb.)
 * - Kredensial URL (https://user:password@...)
 * - Header autentikasi & Cookie
 * - Token sesi manual / aktif yang diketahui
 */
export function maskSensitive(input, knownTokens = []) {
    if (!input) return input || '';
    let isObject = false;
    let text = input;
    if (typeof input === 'object') {
        isObject = true;
        text = JSON.stringify(input, null, 2);
    } else if (typeof input !== 'string') {
        return input;
    }
    let result = text;

    // 1. Kredensial URL (https://user:password@domain)
    result = result.replace(/(https?:\/\/)([^:\s\/@]+):([^@\s\/]+)@/gi, '$1[REDACTED_USER]:[REDACTED_PASS]@');

    // 2. Samarkan SELURUH nilai query URL dalam HTTP/HTTPS URL
    // Pola ini menangani parameter t, token, signature, expires, arbitrary param, dsb.
    result = result.replace(/(https?:\/\/[^\s"'`<>]+)(\?[^\s"'`<>#]+)/gi, (match, base, queryString) => {
        const maskedQuery = queryString.replace(/([?&][^=&#\s]+)=([^&#\s]*)/g, '$1=[REDACTED]');
        return base + maskedQuery;
    });

    // 3. Fragment sensitif (#key=val atau fragment token)
    result = result.replace(/(#[^\s"'`<>]*)/gi, (match) => {
        if (match.includes('=')) {
            return match.replace(/([#&][^=&#\s]+)=([^&#\s]*)/g, '$1=[REDACTED]');
        }
        return match;
    });

    // 4. Header autentikasi dan Cookie
    result = result.replace(/((?:Authorization|Cookie|Set-Cookie):\s*)[^\r\n]+/gi, '$1[REDACTED_HEADER]');
    result = result.replace(/((?:bearer|basic)\s+)[a-zA-Z0-9_\-\.]+/gi, '$1[REDACTED_AUTH]');

    // 5. Properti JSON atau format token/session
    result = result.replace(/(["']?(?:token|sessionToken|session_token|auth_token|secret|password)["']?\s*[:=]\s*["']?)[^"',\s}]+/gi, '$1[REDACTED_TOKEN]');

    // 6. Token sesi format omni (omni[a-f0-9]+)
    result = result.replace(/omni[a-f0-9]{8,}/gi, (match) => match.slice(0, 4) + '***[REDACTED_SESSION]');

    // 7. Token sesi manual / aktif yang diketahui (termasuk token non-omni)
    const tokensToMask = Array.isArray(knownTokens) ? knownTokens : (knownTokens ? [knownTokens] : []);
    for (const tok of tokensToMask) {
        if (tok && typeof tok === 'string' && tok.length >= 4) {
            const escaped = tok.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const re = new RegExp(escaped, 'g');
            result = result.replace(re, tok.slice(0, Math.min(4, Math.floor(tok.length / 2))) + '***[REDACTED_TOKEN]');
        }
    }

    if (isObject) {
        try {
            return JSON.parse(result);
        } catch (_) {
            return result;
        }
    }

    return result;
}

// === Migration, Setup & Worker Startup ===
chrome.runtime.onInstalled.addListener(async () => {
    await chrome.storage.local.set({ app: BASE_URL });
    await Config.ensureSessionToken();

    try {
        chrome.contextMenus.create({
            id: 'omni-download-link',
            title: '\uD83D\uDE80 OmniFetch: Unduh URL',
            contexts: ['link', 'video', 'page']
        });
    } catch (e) {}

    // Buat alarm periodik untuk pemulihan dan monitoring latar belakang
    chrome.alarms.create('omni-job-poller', { periodInMinutes: 1 });
});

chrome.runtime.onStartup.addListener(async () => {
    // Pastikan alarm selalu terpasang setelah browser restart
    chrome.alarms.create('omni-job-poller', { periodInMinutes: 1 });
    
    // Bangkitkan pemantauan pekerjaan aktif dan rekonsiliasi unduhan lokal
    JobManager.startPollingLoop();
});

// Alarm Listener (Service Worker Lifecycle Resilience)
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'omni-job-poller') {
        JobManager.startPollingLoop();
    } else if (alarm.name === 'omni-cookie-sync') {
        Config.getAppInfo().then(info => {
            if (info.cookieSync && info.cookieDomains?.length > 0) {
                APIClient.syncCookies(info.cookieDomains).catch(() => {});
            }
        });
    }
});

// Downloads API Listener (Pantau keberhasilan simpan ke komputer)
if (chrome.downloads && chrome.downloads.onChanged) {
    chrome.downloads.onChanged.addListener(delta => {
        JobManager.handleDownloadDelta(delta);
    });
}

// === Network Listener untuk Sniffer Media ===
chrome.webRequest.onResponseStarted.addListener(d => {
    if (d.tabId < 0 || ![200, 206].includes(d.statusCode)) return;
    const mime = (d.responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type')?.value || '';
    
    if (mime.includes('image/') || mime.includes('text/html') || mime.includes('javascript') || mime.includes('css')) return;
    
    Detector.addCandidate(d.tabId, d.url, mime.toLowerCase(), d.initiator).catch(() => {});
}, { urls: ['*://*/*'] }, ['responseHeaders']);

// === Navigation Cleanup ===
chrome.webRequest.onBeforeRequest.addListener(d => {
    if (d.tabId < 0 || d.type !== 'main_frame') return;
    Detector.clear(d.tabId).catch(() => {});
}, { urls: ['http://*/*', 'https://*/*'], types: ['main_frame'] });

if (chrome.tabs?.onUpdated?.addListener) {
    chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
        if (changeInfo.url) {
            Detector.clear(tabId).catch(() => {});
        }
    });
}

chrome.tabs.onRemoved.addListener(id => {
    Detector.clear(id).catch(() => {});
});

// === Messaging: Pemisahan Operasi Jaringan dari Queue Lokal ===
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return;

    if (message.type === 'SEND_JOB') {
        (async () => {
            const targetUrl = message.payload?.urls || message.payload?.url || '';
            const submissionKey = `${targetUrl}::${message.payload?.media_type || ''}`;
            const opId = SubmissionCoordinator.generateOperationId();

            const acquired = await SubmissionCoordinator.acquire(submissionKey, opId, { force: !!message.force });
            if (!acquired) {
                const sub = await SubmissionCoordinator.getSubmission(submissionKey);
                if (sub?.status === 'uncertain') {
                    sendResponse({ 
                        ok: false, 
                        uncertain: true, 
                        submissionKey,
                        error: 'Status pekerjaan sebelumnya tidak pasti (kemungkinan telah diterima server). Silakan gunakan tombol Kirim Ulang jika ingin memproses ulang dengan risiko duplikasi.' 
                    });
                    return;
                }
                sendResponse({ ok: false, error: 'Pekerjaan untuk URL ini sedang dalam proses pengiriman.' });
                return;
            }

            try {
                const sessionToken = await Config.ensureSessionToken();
                const res = await APIClient.sendJob(message.payload, sessionToken);
                if (res && res.job_id) {
                    await JobManager.addJob(res.job_id, message.payload, message.title || 'Media', sessionToken);
                }
                await SubmissionCoordinator.clearSubmission(submissionKey, opId);
                sendResponse({ ok: true, data: res });
            } catch (e) {
                if (e.code === 'TIMEOUT' || e.code === 'NETWORK_ERROR') {
                    await SubmissionCoordinator.markUncertain(submissionKey, opId, e.code, { error: e.message, url: targetUrl });
                    sendResponse({ 
                        ok: false, 
                        uncertain: true,
                        submissionKey,
                        error: e.code === 'TIMEOUT' ? 
                            'Waktu koneksi habis (Timeout). Server mungkin sudah menerima pekerjaan. Periksa status sebelum mengirim ulang.' :
                            'Koneksi jaringan terputus saat menghubungi server. Server mungkin sudah menerima pekerjaan. Periksa status sebelum mengirim ulang.',
                        code: e.code
                    });
                } else {
                    sendResponse({ ok: false, error: e.message, code: e.code || 'UNKNOWN_ERROR' });
                }
            } finally {
                await SubmissionCoordinator.release(submissionKey, opId);
            }
        })();
        return true;
    }

    if (message.type === 'RETRY_JOB') {
        (async () => {
            const retryKey = `retry::${message.jobId}`;
            const opId = SubmissionCoordinator.generateOperationId();
            const acquired = await SubmissionCoordinator.acquire(retryKey, opId, { force: !!message.force });
            if (!acquired) {
                const sub = await SubmissionCoordinator.getSubmission(retryKey);
                if (sub?.status === 'uncertain') {
                    sendResponse({
                        ok: false,
                        uncertain: true,
                        error: 'Status retry sebelumnya tidak pasti (Timeout/Jaringan). Server mungkin sudah memproses permintaan.'
                    });
                    return;
                }
                sendResponse({ ok: false, error: 'Proses pengulangan untuk tugas ini sedang berjalan.' });
                return;
            }
            try {
                const res = await JobManager.retryJob(message.jobId);
                if (res && res.ok) {
                    await SubmissionCoordinator.clearSubmission(retryKey, opId);
                }
                sendResponse(res);
            } catch (e) {
                if (e.code === 'TIMEOUT' || e.code === 'NETWORK_ERROR') {
                    await SubmissionCoordinator.markUncertain(retryKey, opId, e.code || 'TIMEOUT');
                }
                sendResponse({ ok: false, error: e.message });
            } finally {
                await SubmissionCoordinator.release(retryKey, opId);
            }
        })();
        return true;
    }

    if (message.type === 'RECONCILE_JOB') {
        (async () => {
            try {
                await JobManager.reconcileJobDownloads(message.jobId);
                const { jobs } = await JobManager.getJobs();
                sendResponse({ ok: true, job: jobs[message.jobId] });
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }

    if (message.type === 'INSPECT_URL') {
        (async () => {
            try {
                const sessionToken = await Config.ensureSessionToken();
                const res = await APIClient.inspectUrl(message.url, sessionToken);
                sendResponse(res);
            } catch (e) {
                sendResponse({ ok: false, error: e.message, code: e.code || 'INSPECT_FAILED' });
            }
        })();
        return true;
    }

    if (message.type === 'GET_JOB_STATUS') {
        (async () => {
            try {
                const { jobs } = await JobManager.getJobs();
                const localJob = jobs[message.jobId];
                if (localJob) {
                    sendResponse({ ok: true, job: localJob });
                } else {
                    const sessionToken = await Config.ensureSessionToken();
                    const remoteData = await APIClient.getJobStatus(message.jobId, sessionToken);
                    sendResponse({ ok: true, job: remoteData });
                }
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }

    if (message.type === 'GET_ALL_JOBS') {
        JobManager.getJobs().then(data => sendResponse(data));
        return true;
    }

    if (message.type === 'STOP_JOB') {
        (async () => {
            try {
                const result = await JobManager.stopJob(message.jobId);
                sendResponse(result);
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }

    if (message.type === 'DISMISS_JOB') {
        JobManager.dismissJob(message.jobId).then(res => sendResponse(res));
        return true;
    }

    if (message.type === 'GET_DIAGNOSTIC') {
        (async () => {
            try {
                const { jobs } = await JobManager.getJobs();
                const localJob = jobs[message.jobId];
                const sessionToken = localJob?.sessionToken || (await Config.ensureSessionToken());

                let remoteReport = '';
                try {
                    const diag = await APIClient.getDiagnostic(message.jobId, sessionToken);
                    remoteReport = diag?.report || '';
                } catch (rErr) {
                    remoteReport = `[Gagal mengambil log server: ${rErr.message}]`;
                }

                // Susun laporan diagnostik gabungan lengkap dengan status lokal
                const maskedSession = sessionToken ? sessionToken.slice(0, 6) + '***' : 'None';
                const filesSummary = localJob?.files?.map(f => 
                    `  - ${f.cleanName || f.name} [Status: ${f.status}, ID: ${f.downloadId || 'N/A'}, Size: ${f.size || 'N/A'}${f.error ? ', Error: ' + f.error : ''}]`
                ).join('\n') || '  (Belum ada file)';

                const combinedReport = `=== OmniFetch Comprehensive Diagnostic ===
Waktu Laporan: ${new Date().toISOString()}
Job ID: ${message.jobId}
Sesi Terikat: ${maskedSession}
Status Lokal: ${localJob?.status || 'N/A'}
Tahap: ${localJob?.stageLabel || 'N/A'}
Persentase: ${localJob?.percent || 0}%
Error Terakhir: ${localJob?.error || localJob?.lastPollError || 'None'}
Terakhir Diperbarui: ${localJob?.updatedAt ? new Date(localJob.updatedAt).toLocaleTimeString() : 'N/A'}

-- File Komputer Lokal --
${filesSummary}

-- Laporan Server (Disamarkan) --
${remoteReport}`;

                const sanitizedReport = maskSensitive(combinedReport, [sessionToken, localJob?.sessionToken]);
                sendResponse({ ok: true, report: sanitizedReport });
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }
    
    if (message.type === 'CHECK_SESSION') {
        APIClient.checkSession(message.token).then(res => sendResponse(res));
        return true;
    }

    if (message.type === 'GET_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (!tabId) {
            sendResponse({ error: 'No tab context' });
            return;
        }
        Detector.getCandidates(tabId).then(items => sendResponse({ items }));
        return true;
    }
    
    if (message.type === 'ADD_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (!tabId || !message.items) return;
        (async () => {
            for (const item of message.items) {
                await Detector.addCandidate(tabId, item.url, '', '', item.sourceType);
            }
            sendResponse({ ok: true });
        })();
        return true;
    }
    
    if (message.type === 'CLEAR_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (tabId) {
            Detector.clear(tabId).then(() => sendResponse({ ok: true }));
            return true;
        }
    }
    
    if (message.type === 'SYNC_COOKIES') {
        (async () => {
            const domains = message.domains || ['.instagram.com', '.youtube.com', '.tiktok.com', '.twitter.com', '.x.com'];
            try {
                await APIClient.syncCookies(domains);
                sendResponse({ ok: true });
            } catch (e) {
                sendResponse({ ok: false, error: e.message });
            }
        })();
        return true;
    }
});

// === Context Menu ===
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const sessionToken = await Config.ensureSessionToken();
    if (!sessionToken) { 
        chrome.runtime.openOptionsPage(); 
        return; 
    }
    const url = info.linkUrl || info.srcUrl || info.pageUrl;
    if (!url) return;
    
    const submissionKey = `${url}::video`;
    const opId = SubmissionCoordinator.generateOperationId();
    const acquired = await SubmissionCoordinator.acquire(submissionKey, opId);
    if (!acquired) return;

    try {
        const payload = { urls: url, mode: 'ytdlp', media_type: 'video', resolution: 'best' };
        const res = await APIClient.sendJob(payload, sessionToken);
        if (res && res.job_id) {
            await JobManager.addJob(res.job_id, payload, tab?.title || 'Unduhan Cepat', sessionToken);
        }
        await SubmissionCoordinator.clearSubmission(submissionKey, opId);
    } catch(e) {
        if (e.code === 'TIMEOUT' || e.code === 'NETWORK_ERROR') {
            await SubmissionCoordinator.markUncertain(submissionKey, opId, e.code || 'TIMEOUT');
        }
    } finally {
        await SubmissionCoordinator.release(submissionKey, opId);
    }
});
