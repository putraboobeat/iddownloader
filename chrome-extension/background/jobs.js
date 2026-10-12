import { BASE_URL, Config } from './config.js';
import { APIClient } from './api.js';
import { convertVttToSrt } from '../utils/subtitle.js';

const WINDOWS_RESERVED = new Set([
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
]);

export class JobManager {
    static isPolling = false;
    static _lock = Promise.resolve();

    /**
     * Serialisasi mutasi storage untuk mencegah race condition antara
     * polling status, stop request, dan event onChanged unduhan.
     */
    static async withLock(fn) {
        const run = async () => {
            return await fn();
        };
        const next = this._lock.then(run, run);
        this._lock = next.catch(() => {});
        return next;
    }

    static sanitizeFolderName(name) {
        if (!name || typeof name !== 'string') return 'Media';
        let clean = name.replace(/[\\/:*?"<>|\r\n\t]/g, '_').trim();
        clean = clean.replace(/^\.+|\.+$/g, '').trim();
        if (!clean || WINDOWS_RESERVED.has(clean.toUpperCase())) {
            clean = 'Media_' + Math.random().toString(36).slice(2, 6);
        }
        return clean.slice(0, 60).trim() || 'Media';
    }

    static sanitizeFileName(name, defaultExt = '.mp4') {
        if (!name || typeof name !== 'string') return 'video' + defaultExt;
        
        let ext = defaultExt;
        let base = name.trim();
        const lastDot = base.lastIndexOf('.');
        if (lastDot > 0) {
            const rawExt = base.slice(lastDot).toLowerCase().replace(/[\\/:*?"<>|\r\n\t\s]/g, '');
            if (rawExt.length >= 2 && rawExt.length <= 10) {
                ext = rawExt;
                base = base.slice(0, lastDot);
            }
        }

        base = base.replace(/[\\/:*?"<>|\r\n\t]/g, '_').trim();
        base = base.replace(/^\.+|\.+$/g, '').trim();

        if (!base || WINDOWS_RESERVED.has(base.toUpperCase())) {
            base = 'file_' + Math.random().toString(36).slice(2, 7);
        }

        // Pertahankan ekstensi file, batasi hanya panjang basename
        base = base.slice(0, 60).trim();
        return `${base}${ext}`;
    }

    static async getJobs() {
        const data = await chrome.storage.local.get(['omni_jobs', 'omni_active_job_id']);
        const rawJobs = data.omni_jobs || {};
        // Deep clone untuk menghindari mutasi referensi objek langsung
        const clonedJobs = JSON.parse(JSON.stringify(rawJobs));
        return {
            jobs: clonedJobs,
            activeJobId: data.omni_active_job_id || null
        };
    }

    static async saveJobs(jobs, activeJobId = undefined) {
        const update = { omni_jobs: jobs };
        if (activeJobId !== undefined) {
            update.omni_active_job_id = activeJobId;
        }
        await chrome.storage.local.set(update);
    }

    /**
     * Mendaftarkan pekerjaan baru dengan mengikat token sesi pengguna secara persisten.
     */
    static async addJob(jobId, payload, title = 'Media', sessionToken = null) {
        return await this.withLock(async () => {
            let token = sessionToken;
            if (!token) {
                const info = await Config.getAppInfo();
                token = info.token || '';
            }

            const { jobs } = await this.getJobs();
            const now = Date.now();
            const cleanFolder = this.sanitizeFolderName(title || 'Media Video');

            const newJob = {
                id: jobId,
                sessionToken: token, // Terikat pada sesi saat pekerjaan dibuat
                title: cleanFolder,
                url: payload.urls || payload.url || '',
                payload: payload,
                status: 'queued',
                stageLabel: 'Diterima server (Menunggu antrean)…',
                percent: 0,
                speed: '',
                eta: '',
                logs: [],
                files: [],
                preferZip: !!payload.prefer_zip,
                slideIndex: payload.slide_index ? parseInt(payload.slide_index, 10) : null,
                downloadTriggered: false,
                downloadStatus: 'pending',
                createdAt: now,
                updatedAt: now,
                lastPollTime: now,
                lastPollError: null,
                error: null
            };

            const jobKeys = Object.keys(jobs);
            if (jobKeys.length > 30) {
                const oldestKey = jobKeys.sort((a, b) => jobs[a].createdAt - jobs[b].createdAt)[0];
                delete jobs[oldestKey];
            }

            jobs[jobId] = newJob;
            await this.saveJobs(jobs, jobId);

            this.startPollingLoop();
            return newJob;
        });
    }

    static async updateJob(jobId, updates) {
        return await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            if (!jobs[jobId]) return null;
            Object.assign(jobs[jobId], updates, { updatedAt: Date.now() });
            await this.saveJobs(jobs);
            return jobs[jobId];
        });
    }

    static startPollingLoop() {
        if (this.isPolling) return;
        this.isPolling = true;

        let backoffDelay = 2000;

        const loop = async () => {
            try {
                const { jobs } = await this.getJobs();
                const activeJobs = Object.values(jobs).filter(j => 
                    ['queued', 'downloading', 'validating', 'saving', 'cancelling'].includes(j.status)
                );

                if (activeJobs.length === 0) {
                    this.isPolling = false;
                    return;
                }

                for (const job of activeJobs) {
                    if (job.status === 'saving') {
                        // Rekonsiliasi unduhan lokal yang sedang berjalan di Chrome
                        await this.reconcileJobDownloads(job.id);
                        continue;
                    }

                    try {
                        const statusData = await APIClient.getJobStatus(job.id, job.sessionToken);
                        await this.processJobStatusUpdate(job.id, statusData);
                        backoffDelay = 2000; // Reset backoff jika sukses
                    } catch (err) {
                        const now = Date.now();
                        if (err.code === 'JOB_NOT_FOUND') {
                            await this.updateJob(job.id, {
                                status: 'failed',
                                stageLabel: 'Pekerjaan tidak ditemukan di server dw.pmlab.id.',
                                lastPollTime: now,
                                lastPollError: 'Pekerjaan tidak ditemukan (404)',
                                error: 'Job not found'
                            });
                        } else if (err.code === 'AUTH_EXPIRED') {
                            await this.updateJob(job.id, {
                                status: 'failed',
                                stageLabel: 'Sesi ditolak oleh server (401 Unauthorized).',
                                lastPollTime: now,
                                lastPollError: 'Sesi ditolak (401)',
                                error: 'AUTH_EXPIRED'
                            });
                        } else {
                            // Catat error jaringan/server dan terapkan backoff
                            await this.updateJob(job.id, {
                                lastPollTime: now,
                                lastPollError: err.message || 'Koneksi terputus'
                            });
                            backoffDelay = Math.min(12000, backoffDelay * 1.5);
                        }
                    }
                }
            } catch (loopErr) {
                // Abaikan kesalahan loop umum
            }

            if (this.isPolling) {
                setTimeout(loop, backoffDelay);
            }
        };

        loop();
    }

    static async processJobStatusUpdate(jobId, data) {
        let shouldStartLocalSave = false;
        let filesToSave = null;

        await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            const job = jobs[jobId];
            if (!job) return;

            // Jika pekerjaan sudah dibatalkan atau selesai secara lokal, jangan timpa
            if (job.status === 'cancelled') return;

            const serverStatus = data.status || '';

            // Jika pekerjaan dalam proses pembatalan ('cancelling'), jangan biarkan respons polling menimpa niat pembatalan
            if (job.status === 'cancelling') {
                if (serverStatus === 'cancelled') {
                    job.status = 'cancelled';
                    job.stageLabel = '⏹️ Dibatalkan oleh server.';
                } else if (serverStatus === 'completed') {
                    // Respons polling completed yang tiba saat stop tidak boleh memulai unduhan lokal secara otomatis
                    job.stageLabel = 'Tugas selesai di server sebelum permintaan stop diproses.';
                } else if (serverStatus === 'failed') {
                    job.status = 'failed';
                    job.stageLabel = 'Pekerjaan dihentikan/gagal di server.';
                }
                job.updatedAt = Date.now();
                await this.saveJobs(jobs);
                return;
            }

            let stageLabel = job.stageLabel;
            let percent = typeof data.percent === 'number' ? data.percent : job.percent;
            let speed = '';
            let eta = '';

            if (serverStatus.includes('|')) {
                const parts = serverStatus.split('|');
                if (parts.length >= 2) speed = parts[1].trim();
                if (parts.length >= 3) eta = parts[2].trim();
            }

            if (serverStatus === 'queued') {
                stageLabel = 'Diterima server (Dalam antrean)…';
            } else if (serverStatus === 'downloading' || serverStatus.includes('Mengunduh') || serverStatus.includes('Menghubungi')) {
                const speedEtaText = [speed, eta].filter(Boolean).join(' | ');
                stageLabel = `Diproses server (${percent}%${speedEtaText ? ' • ' + speedEtaText : ''})`;
            } else if (serverStatus === 'validating' || serverStatus.includes('Menggabungkan')) {
                stageLabel = 'Menggabungkan & memvalidasi media di server…';
                percent = 98;
            }

            if (serverStatus === 'completed' || (!data.running && serverStatus === 'completed')) {
                percent = 100;
                jobs[jobId].percent = 100;
                jobs[jobId].logs = data.logs || job.logs;

                // Respons server completed yang datang berulang tidak boleh mereset semua file ke pending atau memicu unduh ganda
                if (job.status === 'saving' || job.status === 'completed' || job.downloadTriggered) {
                    jobs[jobId].updatedAt = Date.now();
                    await this.saveJobs(jobs);
                    return;
                }

                const incomingFiles = Array.isArray(data.files) ? data.files : [];
                const structuredFiles = this.prepareStructuredFiles(job, incomingFiles);

                jobs[jobId].files = structuredFiles;
                jobs[jobId].status = 'saving';
                jobs[jobId].downloadTriggered = true;
                jobs[jobId].stageLabel = 'Memulai penyimpanan hasil ke komputer…';
                jobs[jobId].updatedAt = Date.now();
                await this.saveJobs(jobs);

                shouldStartLocalSave = true;
                filesToSave = structuredFiles;
                return;
            }

            if (serverStatus === 'failed') {
                let lastLog = (data.logs && data.logs.length > 0) ? data.logs[data.logs.length - 1] : 'Unduhan gagal di server';
                const hasNoVideoErr = Array.isArray(data.logs) && data.logs.some(l => l && l.includes('No video formats found'));
                if (hasNoVideoErr) {
                    lastLog = 'Postingan ini hanya berisi FOTO (tanpa video). Server hanya memproses video. Silakan klik "Pindai Ulang Halaman" untuk mengunduh foto langsung!';
                }
                jobs[jobId].status = 'failed';
                jobs[jobId].stageLabel = `Gagal di server: ${lastLog}`;
                jobs[jobId].logs = data.logs || job.logs;
                jobs[jobId].error = lastLog;
                jobs[jobId].updatedAt = Date.now();
                await this.saveJobs(jobs);
                return;
            }

            if (serverStatus === 'cancelled') {
                jobs[jobId].status = 'cancelled';
                jobs[jobId].stageLabel = 'Dibatalkan oleh server.';
                jobs[jobId].logs = data.logs || job.logs;
                jobs[jobId].updatedAt = Date.now();
                await this.saveJobs(jobs);
                return;
            }

            jobs[jobId].percent = percent;
            jobs[jobId].speed = speed;
            jobs[jobId].eta = eta;
            jobs[jobId].stageLabel = stageLabel;
            jobs[jobId].status = data.running ? 'downloading' : job.status;
            jobs[jobId].logs = data.logs || job.logs;
            jobs[jobId].updatedAt = Date.now();
            await this.saveJobs(jobs);
        });

        // Operasi simpan file ke disk dijalankan DI LUAR lock utama
        if (shouldStartLocalSave && filesToSave) {
            await this.saveFilesToDisk(jobId, filesToSave, { onlyFailed: false });
        }
    }

    /**
     * Mempersiapkan daftar file yang diharapkan dengan nama dan path bersih.
     */
    static prepareStructuredFiles(job, incomingFiles = []) {
        const folderTitle = this.sanitizeFolderName(job.title || 'Media');
        const hasZip = incomingFiles.some(f => f.is_zip || f.name?.endsWith('.zip'));
        const nonZipCount = incomingFiles.filter(f => !f.is_zip && !f.name?.endsWith('.zip')).length;

        // Tentukan apakah mendownload ZIP, file individual, atau hanya 1 slide tertentu
        let selectedFiles = incomingFiles;
        if (job.slideIndex && job.slideIndex > 0) {
            // Pengguna secara spesifik memilih hanya 1 slide / gambar dalam carousel
            const mediaFiles = incomingFiles.filter(f => !f.is_zip && !f.name?.endsWith('.zip'));
            if (mediaFiles.length > 0) {
                const targetIdx = Math.min(Math.max(0, job.slideIndex - 1), mediaFiles.length - 1);
                selectedFiles = [mediaFiles[targetIdx]];
            }
        } else if (hasZip && nonZipCount > 0) {
            if (job.preferZip) {
                selectedFiles = incomingFiles.filter(f => f.is_zip || f.name?.endsWith('.zip'));
            } else {
                selectedFiles = incomingFiles.filter(f => !f.is_zip && !f.name?.endsWith('.zip'));
            }
        }

        // Selaraskan nama dasar subtitle dengan video utama jika memungkinkan
        let mainVideoBase = '';
        const videoFile = selectedFiles.find(f => /\.(mp4|mkv|webm|mov)$/i.test(f.name));
        if (videoFile) {
            const dot = videoFile.name.lastIndexOf('.');
            mainVideoBase = dot > 0 ? videoFile.name.slice(0, dot) : videoFile.name;
        }

        return selectedFiles.map((f, index) => {
            let finalName = f.name;
            // Jika ada video utama dan file ini adalah subtitle, selaraskan nama depannya
            if (mainVideoBase && /\.(srt|vtt)$/i.test(f.name)) {
                const subExt = f.name.slice(f.name.lastIndexOf('.'));
                const langMatch = f.name.match(/\.(id|en|in|indonesia|eng)\./i);
                const langTag = langMatch ? `.${langMatch[1].toLowerCase()}` : '';
                finalName = `${mainVideoBase}${langTag}${subExt}`;
            }

            const cleanFileName = this.sanitizeFileName(finalName);
            const targetPath = `OmniFetch/${folderTitle}/${cleanFileName}`;
            const fileId = `${job.id}::${f.name || 'file'}::${index}`;

            return {
                fileId: fileId,
                name: f.name,
                cleanName: cleanFileName,
                targetPath: targetPath,
                url: f.url,
                actualDownloadUrl: null,
                isZip: !!(f.is_zip || f.name?.endsWith('.zip')),
                size: f.size || 0,
                downloadId: null,
                claimId: null,
                intent: null,
                intentTime: null,
                status: 'pending', // 'pending' | 'starting' | 'downloading' | 'completed' | 'interrupted' | 'failed' | 'needs_verification'
                bytesReceived: 0,
                totalBytes: f.size || 0,
                error: null
            };
        });
    }

    /**
     * Helper streaming reader untuk fetch dan konversi WebVTT ke SubRip (.srt).
     * Menerapkan batas byte riil (2 MB) dan timeout yang mencakup pembacaan body.
     */
    static async fetchAndConvertVtt(fullUrl, folderTitle, originalFileName) {
        const MAX_VTT_BYTES = 2 * 1024 * 1024; // 2 MB
        const vttController = new AbortController();
        const vttTimer = setTimeout(() => vttController.abort(), 6000);
        let vttReader = null;
        let converted = false;
        let warning = null;
        let downloadUrl = fullUrl;
        let targetFileName = originalFileName;

        try {
            const vttRes = await fetch(fullUrl, { signal: vttController.signal });
            if (vttRes.ok) {
                const chunks = [];
                let totalBytesRead = 0;
                let exceeded = false;

                if (vttRes.body && typeof vttRes.body.getReader === 'function') {
                    vttReader = vttRes.body.getReader();
                    while (true) {
                        const { done, value } = await vttReader.read();
                        if (done) break;
                        if (value) {
                            totalBytesRead += value.byteLength;
                            if (totalBytesRead > MAX_VTT_BYTES) {
                                exceeded = true;
                                try { await vttReader.cancel(); } catch (_) {}
                                break;
                            }
                            chunks.push(value);
                        }
                    }
                } else {
                    const rawText = await vttRes.text();
                    const encoder = new TextEncoder();
                    const u8 = encoder.encode(rawText);
                    if (u8.byteLength > MAX_VTT_BYTES) {
                        exceeded = true;
                    } else {
                        totalBytesRead = u8.byteLength;
                        chunks.push(u8);
                    }
                }

                if (exceeded) {
                    warning = 'Ukuran subtitle VTT melebihi batas 2 MB; disimpan sebagai VTT asli';
                } else if (chunks.length > 0) {
                    const fullBuffer = new Uint8Array(totalBytesRead);
                    let offset = 0;
                    for (const chunk of chunks) {
                        fullBuffer.set(chunk, offset);
                        offset += chunk.byteLength;
                    }
                    const vttText = new TextDecoder('utf-8').decode(fullBuffer);
                    if (vttText && (vttText.includes('WEBVTT') || vttText.includes('-->'))) {
                        const convertedSrt = convertVttToSrt(vttText);
                        if (convertedSrt && convertedSrt.length > 10) {
                            downloadUrl = 'data:text/plain;charset=utf-8,' + encodeURIComponent(convertedSrt);
                            targetFileName = targetFileName.replace(/\.vtt$/i, '.srt');
                            converted = true;
                        } else {
                            warning = 'Konversi VTT ke SRT tidak menghasilkan format valid; disimpan sebagai VTT asli';
                        }
                    } else {
                        warning = 'File bukan WebVTT yang valid; disimpan sebagai format asli';
                    }
                }
            } else {
                warning = `HTTP ${vttRes.status} saat mengambil VTT; disimpan via URL server`;
            }
        } catch (e) {
            warning = `Fetch VTT gagal (${e.message}); disimpan via URL server`;
        } finally {
            clearTimeout(vttTimer);
            if (vttReader) {
                try { await vttReader.cancel(); } catch (_) {}
            }
        }

        return {
            downloadUrl,
            targetFileName,
            converted,
            warning
        };
    }

    /**
     * Mengunduh file hasil pekerjaan ke komputer pengguna.
     * Operasi jaringan dan Chrome Downloads dijalankan di luar lock,
     * klaim file diserialisasikan dengan claimId unik untuk mencegah unduh ganda saat concurrent.
     */
    static async saveFilesToDisk(jobId, filesToSave = null, options = { onlyFailed: false }) {
        let job = null;
        let claimedFiles = [];
        let alreadyRunningCount = 0;
        let completedCount = 0;
        let eligibleCount = 0;

        await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            job = jobs[jobId];
            if (!job || ['cancelled', 'cancelling'].includes(job.status)) return;

            let files = Array.isArray(filesToSave) && filesToSave.length > 0 ? filesToSave : job.files;
            if (!files || files.length === 0) {
                job.status = 'failed';
                job.downloadStatus = 'failed';
                job.stageLabel = 'Gagal menyimpan ke komputer: Tidak ada file hasil dari server.';
                job.error = 'NO_FILES';
                await this.saveJobs(jobs);
                return;
            }

            // Rekonsiliasi file yang masuk dengan file yang sudah tercatat pada job
            if (Array.isArray(job.files)) {
                for (let i = 0; i < files.length; i++) {
                    const f = files[i];
                    if (!f.fileId) {
                        f.fileId = `${jobId}::${f.name || 'file'}::${i}`;
                    }
                    const existing = job.files.find(ex =>
                        ex.fileId === f.fileId ||
                        ex.url === f.url ||
                        (ex.cleanName && (ex.cleanName === f.cleanName || ex.cleanName === f.name))
                    );
                    if (existing) {
                        if (existing.downloadId) f.downloadId = existing.downloadId;
                        if (existing.claimId) f.claimId = existing.claimId;
                        if (existing.status && existing.status !== 'pending') f.status = existing.status;
                        f.bytesReceived = existing.bytesReceived || f.bytesReceived || 0;
                        f.totalBytes = existing.totalBytes || f.totalBytes || 0;
                        f.targetPath = f.targetPath || existing.targetPath;
                        f.cleanName = f.cleanName || existing.cleanName;
                        f.actualDownloadUrl = existing.actualDownloadUrl || f.actualDownloadUrl;
                    }
                }
            }

            job.files = files;
            job.downloadTriggered = true;
            if (job.status !== 'completed') {
                job.status = 'saving';
            }
            job.stageLabel = `Menyiapkan penyimpanan ke komputer (Downloads/OmniFetch/${this.sanitizeFolderName(job.title || 'Media')}/)…`;

            // Klaim file yang memenuhi syarat di dalam lock singkat
            for (let i = 0; i < job.files.length; i++) {
                const f = job.files[i];
                if (!f.fileId) f.fileId = `${jobId}::${f.name || 'file'}::${i}`;

                // Jika sudah completed, catat dan lewati
                if (f.status === 'completed') {
                    completedCount++;
                    continue;
                }

                // Jika sedang diklaim atau sedang berjalan di operasi lain, catat dan lewati
                if (f.claimId || f.status === 'starting' || (f.downloadId && f.status === 'downloading')) {
                    alreadyRunningCount++;
                    continue;
                }

                // Filter daftar target file spesifik jika disediakan (misal dari retryJob)
                if (Array.isArray(options.targetFileIds) && !options.targetFileIds.includes(f.fileId)) {
                    continue;
                }

                // Mode retry: hanya file yang gagal / interrupted / pending
                if (options.onlyFailed && !['failed', 'interrupted', 'pending'].includes(f.status)) {
                    continue;
                }

                eligibleCount++;
                // Berikan klaim eksklusif
                const claimId = 'claim_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
                f.claimId = claimId;
                f.status = 'starting';
                f.intent = 'download_initiated';
                f.intentTime = Date.now();
                claimedFiles.push({
                    fileId: f.fileId,
                    claimId: claimId,
                    fileIndex: i,
                    name: f.name,
                    cleanName: f.cleanName,
                    url: f.url,
                    targetPath: f.targetPath,
                    isZip: f.isZip
                });
            }

            await this.saveJobs(jobs);
        });

        // Jika tidak ada file yang diklaim (semua sudah selesai atau sedang diproses oleh pemanggilan lain)
        if (claimedFiles.length === 0) {
            return {
                started: 0,
                alreadyRunning: alreadyRunningCount,
                completed: completedCount,
                eligible: eligibleCount,
                errors: []
            };
        }

        const latestData = await this.getJobs();
        job = latestData.jobs[jobId];
        if (!job || ['cancelled', 'cancelling'].includes(job.status)) {
            return {
                started: 0,
                alreadyRunning: alreadyRunningCount,
                completed: completedCount,
                eligible: eligibleCount,
                errors: ['Job dibatalkan']
            };
        }

        const folderTitle = this.sanitizeFolderName(job.title || 'Media');
        const sessionToken = job.sessionToken || '';
        let startedCount = 0;
        let failureReasons = [];
        const hasSrtAlready = job.files.some(other => other.name && other.name.toLowerCase().endsWith('.srt'));

        for (const cf of claimedFiles) {
            // Periksa pembatalan sebelum memproses file
            const checkData = await this.getJobs();
            const currentJob = checkData.jobs[jobId];
            if (!currentJob || ['cancelled', 'cancelling'].includes(currentJob.status)) {
                // Rilis klaim dan tandai interrupted
                await this.withLock(async () => {
                    const { jobs } = await this.getJobs();
                    const j = jobs[jobId];
                    const f = j?.files?.find(x => x.fileId === cf.fileId);
                    if (f && f.claimId === cf.claimId) {
                        f.claimId = null;
                        f.status = 'interrupted';
                        f.error = 'Dibatalkan pengguna';
                        await this.saveJobs(jobs);
                    }
                });
                break;
            }

            const fileRelUrl = cf.url;
            if (!fileRelUrl || typeof fileRelUrl !== 'string') {
                await this.withLock(async () => {
                    const { jobs } = await this.getJobs();
                    const j = jobs[jobId];
                    const f = j?.files?.find(x => x.fileId === cf.fileId);
                    if (f && f.claimId === cf.claimId) {
                        f.status = 'failed';
                        f.error = 'URL file tidak valid';
                        f.claimId = null;
                        await this.saveJobs(jobs);
                    }
                });
                failureReasons.push('URL file tidak valid');
                continue;
            }

            const baseUrl = await Config.getBaseUrl();
            let fullUrl = fileRelUrl.startsWith('http') ? fileRelUrl : new URL(fileRelUrl, baseUrl).href;
            if (!fullUrl.startsWith(`${baseUrl}/download/`) && !fullUrl.startsWith(`${BASE_URL}/download/`)) {
                await this.withLock(async () => {
                    const { jobs } = await this.getJobs();
                    const j = jobs[jobId];
                    const f = j?.files?.find(x => x.fileId === cf.fileId);
                    if (f && f.claimId === cf.claimId) {
                        f.status = 'failed';
                        f.error = 'URL di luar origin server yang diizinkan';
                        f.claimId = null;
                        await this.saveJobs(jobs);
                    }
                });
                failureReasons.push('URL di luar server');
                continue;
            }

            if (sessionToken && !fullUrl.includes('session=')) {
                const joiner = fullUrl.includes('?') ? '&' : '?';
                fullUrl += `${joiner}session=${encodeURIComponent(sessionToken)}`;
            }

            let downloadUrlToUse = fullUrl;
            let targetFileName = cf.cleanName || this.sanitizeFileName(cf.name);
            let targetPath = cf.targetPath || `OmniFetch/${folderTitle}/${targetFileName}`;
            let conversionWarning = null;

            // Fetch & konversi VTT di LUAR lock
            const isVtt = cf.name && cf.name.toLowerCase().endsWith('.vtt');
            if (isVtt && !hasSrtAlready) {
                const convResult = await this.fetchAndConvertVtt(fullUrl, folderTitle, targetFileName);
                downloadUrlToUse = convResult.downloadUrl;
                targetFileName = convResult.targetFileName;
                targetPath = `OmniFetch/${folderTitle}/${targetFileName}`;
                conversionWarning = convResult.warning;
            }

            // Periksa kembali pembatalan dan status klaim setelah fetch/konversi VTT SEBELUM memulai Chrome download!
            const preCheck = await this.getJobs();
            const preJob = preCheck.jobs[jobId];
            const preFile = preJob?.files?.find(x => x.fileId === cf.fileId);
            if (!preJob || ['cancelled', 'cancelling'].includes(preJob.status) || !preFile || preFile.claimId !== cf.claimId) {
                await this.withLock(async () => {
                    const { jobs } = await this.getJobs();
                    const j = jobs[jobId];
                    const f = j?.files?.find(x => x.fileId === cf.fileId);
                    if (f && f.claimId === cf.claimId) {
                        f.claimId = null;
                        f.status = 'interrupted';
                        f.error = 'Dibatalkan pengguna';
                        await this.saveJobs(jobs);
                    }
                });
                if (preJob && ['cancelled', 'cancelling'].includes(preJob.status)) {
                    break;
                }
                continue;
            }

            let dlId = null;
            let downloadError = null;

            if (chrome.downloads && typeof chrome.downloads.download === 'function') {
                try {
                    dlId = await chrome.downloads.download({
                        url: downloadUrlToUse,
                        filename: targetPath,
                        conflictAction: 'uniquify',
                        saveAs: false
                    });
                } catch (err) {
                    downloadError = err.message || 'Gagal memulai unduhan Chrome';
                }
            } else {
                downloadError = 'Chrome Downloads API tidak tersedia';
            }

            // Simpan status hasil pemanggilan dalam lock singkat
            await this.withLock(async () => {
                const { jobs } = await this.getJobs();
                const curJob = jobs[jobId];
                if (!curJob) return;

                const fileItem = curJob.files?.find(x => x.fileId === cf.fileId);
                if (!fileItem) return;

                // Jika klaim sudah tidak cocok, lewati
                if (fileItem.claimId !== cf.claimId) {
                    if (dlId && chrome.downloads?.cancel) {
                        try { await chrome.downloads.cancel(dlId); } catch (_) {}
                    }
                    return;
                }

                if (['cancelled', 'cancelling'].includes(curJob.status)) {
                    // Jika dibatalkan saat inisiasi unduhan, batalkan unduhan Chrome yang baru dibuat
                    if (dlId && chrome.downloads?.cancel) {
                        try { await chrome.downloads.cancel(dlId); } catch (_) {}
                    }
                    fileItem.status = 'interrupted';
                    fileItem.error = 'Dibatalkan pengguna';
                    fileItem.intent = 'cancelled';
                    fileItem.claimId = null;
                    await this.saveJobs(jobs);
                    return;
                }

                if (dlId) {
                    fileItem.downloadId = dlId;
                    fileItem.status = 'downloading';
                    fileItem.intent = 'download_confirmed';
                    fileItem.actualDownloadUrl = downloadUrlToUse;
                    fileItem.cleanName = targetFileName;
                    fileItem.targetPath = targetPath;
                    fileItem.claimId = null;
                    if (conversionWarning) fileItem.warning = conversionWarning;
                    startedCount++;
                } else {
                    fileItem.status = 'failed';
                    fileItem.error = downloadError;
                    fileItem.intent = 'failed';
                    fileItem.claimId = null;
                    failureReasons.push(downloadError);
                }

                fileItem.updatedAt = Date.now();
                await this.saveJobs(jobs);
            });
        }

        // Evaluasi akhir setelah seluruh file selesai diproses
        await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            const finalJob = jobs[jobId];
            if (!finalJob || ['cancelled', 'cancelling'].includes(finalJob.status)) return;

            const files = finalJob.files || [];
            const allFilesFailed = files.every(f => f.status === 'failed');
            const totalCount = files.length;
            const runningCount = files.filter(f => ['downloading', 'starting'].includes(f.status)).length;
            const completedCount = files.filter(f => f.status === 'completed').length;

            if (allFilesFailed && runningCount === 0 && completedCount === 0) {
                finalJob.status = 'failed';
                finalJob.downloadStatus = 'failed';
                finalJob.stageLabel = `Gagal menyimpan ke komputer: ${failureReasons[0] || 'Tidak dapat memulai transfer file'}`;
                finalJob.error = failureReasons.join('; ');
            } else if (runningCount > 0) {
                finalJob.status = 'saving';
                finalJob.downloadStatus = 'downloading';
                finalJob.stageLabel = `Menyimpan ke komputer (${runningCount + completedCount}/${totalCount} file berjalan)…`;
            }

            finalJob.updatedAt = Date.now();
            await this.saveJobs(jobs);
        });

        return {
            started: startedCount,
            alreadyRunning: alreadyRunningCount,
            completed: completedCount,
            eligible: eligibleCount,
            failed: failureReasons.length,
            errors: failureReasons
        };
    }

    /**
     * Memproses event perubahan status dari Chrome Downloads API.
     * Mengkalkulasi progres byte riil tanpa persentase rekaan.
     */
    static async handleDownloadDelta(delta) {
        if (!delta || !delta.id) return;

        return await this.withLock(async () => {
            const { jobs } = await this.getJobs();

            for (const job of Object.values(jobs)) {
                if (!Array.isArray(job.files)) continue;

                const file = job.files.find(f => f.downloadId === delta.id);
                if (!file) continue;

                // Perbarui ukuran transfer dari delta
                if (delta.bytesReceived && typeof delta.bytesReceived.current === 'number') {
                    file.bytesReceived = delta.bytesReceived.current;
                }
                if (delta.totalBytes && typeof delta.totalBytes.current === 'number') {
                    file.totalBytes = Math.max(0, delta.totalBytes.current);
                }

                if (delta.state && delta.state.current === 'complete') {
                    file.status = 'completed';
                    file.error = null;
                } else if (delta.state && delta.state.current === 'interrupted') {
                    file.status = 'interrupted';
                    file.error = delta.error?.current || 'Unduhan dibatalkan atau jaringan terputus';
                }

                const totalCount = job.files.length;
                const completedCount = job.files.filter(f => f.status === 'completed').length;
                const interruptedCount = job.files.filter(f => f.status === 'interrupted' || f.status === 'failed').length;
                const downloadingCount = job.files.filter(f => f.status === 'downloading' || f.status === 'pending' || f.status === 'starting').length;

                const folderTitle = this.sanitizeFolderName(job.title || 'Media');

                if (completedCount === totalCount && totalCount > 0) {
                    job.status = 'completed';
                    job.downloadStatus = 'complete';
                    job.percent = 100;
                    job.stageLabel = `✅ Selesai! Tersimpan di Downloads/OmniFetch/${folderTitle}/`;
                } else if (downloadingCount === 0 && interruptedCount > 0) {
                    job.status = 'failed';
                    job.downloadStatus = 'interrupted';
                    job.stageLabel = `⚠️ Unduhan komputer terputus (${interruptedCount}/${totalCount} file gagal). Klik Coba Lagi untuk mengulang.`;
                    job.error = file.error || 'Unduhan Chrome terputus';
                } else if (interruptedCount > 0) {
                    job.stageLabel = `Menyimpan ke komputer (${completedCount}/${totalCount} selesai, ada file tersendat)…`;
                } else {
                    const totalReceived = job.files.reduce((acc, f) => acc + (f.bytesReceived || 0), 0);
                    const totalExpected = job.files.reduce((acc, f) => acc + (f.totalBytes || 0), 0);
                    const allHaveTotal = job.files.every(f => (f.totalBytes || 0) > 0);

                    if (totalExpected > 0 && allHaveTotal) {
                        const localPct = Math.round((totalReceived / totalExpected) * 100);
                        const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
                        const mbTot = (totalExpected / (1024 * 1024)).toFixed(1);
                        job.stageLabel = `Menyimpan ke komputer: ${mbRec} MB / ${mbTot} MB (${localPct}%)`;
                    } else if (totalReceived > 0) {
                        const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
                        job.stageLabel = `Menyimpan ke komputer: ${mbRec} MB (ukuran total tidak diketahui)`;
                    } else {
                        job.stageLabel = `Menyimpan ke komputer (Downloads/OmniFetch/${folderTitle}/)…`;
                    }
                }

                job.updatedAt = Date.now();
                await this.saveJobs(jobs);
                break;
            }
        });
    }

    /**
     * Memeriksa apakah URL riwayat Chrome cocok pasti dengan target URL unduhan.
     * Pencocokan data URL harus exact match string.
     * Pencocokan HTTP URL harus exact match termasuk origin, pathname, dan search query.
     */
    static isMatchDownloadUrl(itemUrl, targetUrl) {
        if (!itemUrl || !targetUrl) return false;
        if (targetUrl.startsWith('data:')) {
            return itemUrl === targetUrl;
        }
        if (itemUrl.startsWith('data:')) {
            return false;
        }
        try {
            const u1 = new URL(itemUrl);
            const u2 = targetUrl.startsWith('http') ? new URL(targetUrl) : new URL(targetUrl, BASE_URL);
            return (u1.origin === u2.origin) && 
                   (u1.pathname === u2.pathname) && 
                   (u1.search === u2.search);
        } catch (_) {
            return itemUrl === targetUrl;
        }
    }

    /**
     * Memeriksa apakah filename riwayat Chrome berada dalam folder target yang tepat.
     * Mendukung uniquify Chrome (file (1).ext) HANYA jika folder induknya tepat sama.
     */
    static isMatchTargetPath(itemFilename, expectedTargetPath) {
        if (!itemFilename || !expectedTargetPath) return false;
        const normItem = itemFilename.replace(/\\/g, '/');
        const normTarget = expectedTargetPath.replace(/\\/g, '/');

        // 1. Kecocokan tepat di ujung path
        if (normItem.endsWith(normTarget)) {
            const idx = normItem.lastIndexOf(normTarget);
            if (idx === 0 || normItem[idx - 1] === '/') {
                return true;
            }
        }

        // 2. Uniquify dari Chrome di folder induk yang SAMA PERSIS
        const lastSlash = normTarget.lastIndexOf('/');
        const targetFolder = lastSlash >= 0 ? normTarget.slice(0, lastSlash + 1) : '';
        const targetFileName = lastSlash >= 0 ? normTarget.slice(lastSlash + 1) : normTarget;

        const dotIdx = targetFileName.lastIndexOf('.');
        const baseName = dotIdx >= 0 ? targetFileName.slice(0, dotIdx) : targetFileName;
        const ext = dotIdx >= 0 ? targetFileName.slice(dotIdx) : '';

        const escapedFolder = targetFolder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const escapedBase = baseName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const escapedExt = ext.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

        const uniquifyRegex = new RegExp(`(?:^|/)${escapedFolder}${escapedBase} \\(\\d+\\)${escapedExt}$`);
        return uniquifyRegex.test(normItem);
    }

    /**
     * Rekonsiliasi unduhan aktif terhadap pencarian Chrome setelah browser/worker restart.
     * Mengatasi pemulihan di tiga titik (sebelum panggilan, sesudah terima tetapi sebelum ID, setelah ID tersimpan),
     * menyalin byte aktual langsung dari search, dan mencocokkan target filename & URL secara tepat.
     */
    static async reconcileJobDownloads(jobId) {
        if (!chrome.downloads || typeof chrome.downloads.search !== 'function') return;

        let needsContinuation = false;

        await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            const job = jobs[jobId];
            if (!job || !Array.isArray(job.files)) return;

            let anyChange = false;

            for (const file of job.files) {
                // Kasus 1: File sudah memiliki downloadId tercatat (Point C)
                if (file.downloadId) {
                    try {
                        const items = await chrome.downloads.search({ id: file.downloadId });
                        if (items && items.length > 0) {
                            const item = items[0];

                            // Salin byte aktual dari hasil search ke state per file
                            if (typeof item.bytesReceived === 'number') {
                                if (file.bytesReceived !== item.bytesReceived) {
                                    file.bytesReceived = item.bytesReceived;
                                    anyChange = true;
                                }
                            }
                            if (typeof item.totalBytes === 'number') {
                                const newTot = Math.max(0, item.totalBytes);
                                if (file.totalBytes !== newTot) {
                                    file.totalBytes = newTot;
                                    anyChange = true;
                                }
                            }

                            if (item.state === 'complete' && file.status !== 'completed') {
                                file.status = 'completed';
                                file.error = null;
                                anyChange = true;
                            } else if (item.state === 'interrupted' && file.status !== 'interrupted') {
                                file.status = 'interrupted';
                                file.error = item.error || 'Terputus';
                                anyChange = true;
                            } else if (item.state === 'in_progress' && file.status !== 'downloading') {
                                file.status = 'downloading';
                                anyChange = true;
                            }
                        } else {
                            file.status = 'failed';
                            file.error = 'Download ID hilang';
                            anyChange = true;
                        }
                    } catch (e) {}
                }
                // Kasus 2: Unduhan diinisiasi ke Chrome tetapi ID belum sempat tersimpan (Point B)
                else if (file.intent === 'download_initiated') {
                    try {
                        const queryItems = await chrome.downloads.search({});
                        let targetUrl = file.actualDownloadUrl || file.url;
                        if (targetUrl && !targetUrl.startsWith('data:') && job.sessionToken && !targetUrl.includes('session=')) {
                            const joiner = targetUrl.includes('?') ? '&' : '?';
                            targetUrl = `${targetUrl}${joiner}session=${encodeURIComponent(job.sessionToken)}`;
                        }

                        const confidentMatches = (queryItems || []).filter(item => {
                            if (!item) return false;

                            // 1. Cocokkan URL secara tepat
                            const urlMatch = JobManager.isMatchDownloadUrl(item.url, targetUrl);

                            // 2. Cocokkan target folder dan filename secara tepat jika tersedia di riwayat Chrome
                            let pathMatch = true;
                            if (item.filename) {
                                if (file.targetPath) {
                                    pathMatch = JobManager.isMatchTargetPath(item.filename, file.targetPath);
                                } else if (file.cleanName) {
                                    const normItem = item.filename.replace(/\\/g, '/');
                                    pathMatch = normItem.endsWith('/' + file.cleanName) || normItem === file.cleanName;
                                }
                            }

                            // 3. Cocokkan rentang waktu intent (2 menit)
                            const timeMatch = file.intentTime && item.startTime ? 
                                Math.abs(new Date(item.startTime).getTime() - file.intentTime) < 120000 : true;

                            return urlMatch && pathMatch && timeMatch;
                        });

                        if (confidentMatches.length === 1) {
                            const matched = confidentMatches[0];
                            file.downloadId = matched.id;
                            file.bytesReceived = matched.bytesReceived || 0;
                            file.totalBytes = matched.totalBytes || file.totalBytes || 0;
                            file.status = matched.state === 'complete' ? 'completed' : 
                                         (matched.state === 'interrupted' ? 'interrupted' : 'downloading');
                            file.intent = 'download_confirmed';
                            file.error = null;
                            anyChange = true;
                        } else {
                            // Jika ada ambiguitas (0 atau multi-kandidat), jangan membuat duplikat otomatis
                            file.status = 'needs_verification';
                            file.error = confidentMatches.length > 1 ? 
                                'Ditemukan beberapa kandidat unduhan Chrome yang mirip. Buka unduhan Chrome untuk memeriksa.' :
                                'Status unduhan memerlukan verifikasi manual di Chrome Downloads.';
                            job.downloadStatus = 'needs_verification';
                            anyChange = true;
                        }
                    } catch (err) {
                        file.status = 'needs_verification';
                        file.error = 'Gagal mencocokkan riwayat unduhan Chrome.';
                        job.downloadStatus = 'needs_verification';
                        anyChange = true;
                    }
                }
                // Kasus 3: File masih pending tanpa intent (Point A)
                else if (file.status === 'pending' && !file.intent) {
                    if (['saving', 'downloading'].includes(job.status)) {
                        needsContinuation = true;
                    }
                }
            }

            // Hitung progres lokal dari data yang tersedia
            const total = job.files.length;
            const completed = job.files.filter(f => f.status === 'completed').length;
            const interrupted = job.files.filter(f => ['interrupted', 'failed'].includes(f.status)).length;
            const downloading = job.files.filter(f => ['downloading', 'starting'].includes(f.status)).length;
            const needsVerify = job.files.filter(f => f.status === 'needs_verification').length;
            const folderTitle = this.sanitizeFolderName(job.title || 'Media');

            if (completed === total && total > 0) {
                job.status = 'completed';
                job.downloadStatus = 'complete';
                job.percent = 100;
                job.stageLabel = `✅ Selesai! Tersimpan di Downloads/OmniFetch/${folderTitle}/`;
                anyChange = true;
            } else if (downloading === 0 && needsVerify > 0) {
                job.status = 'saving';
                job.downloadStatus = 'needs_verification';
                job.stageLabel = '⚠️ Beberapa file memerlukan pemeriksaan status unduhan Chrome.';
                anyChange = true;
            } else if (downloading === 0 && interrupted > 0 && !needsContinuation) {
                job.status = 'failed';
                job.downloadStatus = 'interrupted';
                job.stageLabel = `⚠️ Unduhan komputer terputus (${interrupted}/${total} file gagal).`;
                anyChange = true;
            } else if (downloading > 0) {
                const totalReceived = job.files.reduce((acc, f) => acc + (f.bytesReceived || 0), 0);
                const totalExpected = job.files.reduce((acc, f) => acc + (f.totalBytes || 0), 0);
                const allKnown = job.files.every(f => (f.totalBytes || 0) > 0);

                if (totalExpected > 0 && allKnown) {
                    const localPct = Math.round((totalReceived / totalExpected) * 100);
                    const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
                    const mbTot = (totalExpected / (1024 * 1024)).toFixed(1);
                    job.stageLabel = `Menyimpan ke komputer: ${mbRec} MB / ${mbTot} MB (${localPct}%)`;
                } else if (totalReceived > 0) {
                    const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
                    job.stageLabel = `Menyimpan ke komputer: ${mbRec} MB (ukuran total tidak diketahui)`;
                } else {
                    job.stageLabel = `Menyimpan ke komputer (Downloads/OmniFetch/${folderTitle}/)…`;
                }
                anyChange = true;
            }

            if (anyChange) {
                job.updatedAt = Date.now();
                await this.saveJobs(jobs);
            }
        });

        // Lanjutkan file berikutnya yang belum dimulai setelah rekonsiliasi
        if (needsContinuation) {
            await this.saveFilesToDisk(jobId, null, { onlyFailed: false });
        }
    }

    /**
     * Menghentikan pekerjaan tanpa menahan lock selama operasi jaringan atau Downloads API.
     * Memisahkan pembatalan lokal dari pembatalan server terkonfirmasi.
     */
    static async stopJob(jobId) {
        let sessionToken = '';
        let shouldCallServer = false;
        let localFilesToCancel = [];

        const preCheck = await this.withLock(async () => {
            const { jobs } = await this.getJobs();
            const job = jobs[jobId];
            if (!job) return { notFound: true };

            if (Array.isArray(job.files)) {
                localFilesToCancel = job.files.filter(f => f.downloadId && ['downloading', 'starting'].includes(f.status));
                for (const f of job.files) {
                    if (['downloading', 'starting', 'pending'].includes(f.status)) {
                        f.status = 'interrupted';
                        f.error = 'Dibatalkan pengguna';
                        f.claimId = null;
                    }
                }
            }

            if (job.status === 'cancelled') {
                return { alreadyCancelled: true };
            }

            if (['queued', 'downloading', 'validating', 'cancelling'].includes(job.status)) {
                shouldCallServer = true;
                sessionToken = job.sessionToken;
                job.status = 'cancelling';
                job.stageLabel = 'Mengirim permintaan stop ke server dw.pmlab.id…';
            } else {
                job.status = 'cancelled';
                job.stageLabel = '⏹️ Unduhan lokal dibatalkan oleh pengguna.';
            }
            job.updatedAt = Date.now();
            await this.saveJobs(jobs);
            return { ok: true };
        });

        if (preCheck.notFound) {
            return { ok: false, error: 'Job not found' };
        }
        if (preCheck.alreadyCancelled) {
            return { ok: true, alreadyCancelled: true };
        }

        // Pembatalan download Chrome lokal di luar lock
        if (chrome.downloads && typeof chrome.downloads.cancel === 'function') {
            for (const f of localFilesToCancel) {
                try { await chrome.downloads.cancel(f.downloadId); } catch (_) {}
            }
        }

        if (!shouldCallServer) {
            return { ok: true };
        }

        // Permintaan ke backend dijalankan DI LUAR lock
        try {
            const stopRes = await APIClient.stopJob(jobId, sessionToken);
            const serverConfirmed = stopRes && stopRes.status === 'cancelled';

            return await this.withLock(async () => {
                const { jobs } = await this.getJobs();
                const job = jobs[jobId];
                if (!job) return { ok: false, error: 'Job not found' };

                if (serverConfirmed) {
                    job.status = 'cancelled';
                    job.stageLabel = '⏹️ Dibatalkan oleh pengguna (Terkonfirmasi server).';
                    job.updatedAt = Date.now();
                    await this.saveJobs(jobs);
                    return { ok: true };
                } else {
                    job.stageLabel = 'Server merespons stop tetapi status belum berubah.';
                    job.updatedAt = Date.now();
                    await this.saveJobs(jobs);
                    return { ok: false, error: 'Server did not confirm cancellation' };
                }
            });
        } catch (err) {
            return await this.withLock(async () => {
                const { jobs } = await this.getJobs();
                const job = jobs[jobId];
                if (job) {
                    // JANGAN mengklaim cancelled jika request gagal karena offline/koneksi terputus!
                    // Status job tetap 'cancelling' dan tetap dipantau oleh startPollingLoop.
                    job.stageLabel = `⚠️ Permintaan stop belum terkonfirmasi; server mungkin masih bekerja (Gagal menghentikan di server: ${err.message}). Klik Hentikan untuk mencoba lagi.`;
                    job.updatedAt = Date.now();
                    await this.saveJobs(jobs);
                }
                return { ok: false, error: err.message, unconfirmed: true };
            });
        }
    }

    /**
     * Mengulang unduhan:
     * - Jika file hasil server sudah ada: retry lokal murni tanpa memanggil APIClient.sendJob / POST /api/start.
     * - Menghapus asumsi 24 jam; hanya menggunakan expiresAt dari API jika tersedia.
     * - Jika pekerjaan server yang gagal: mengirim pekerjaan baru ke backend.
     */
    static async retryJob(jobId) {
        const { jobs } = await this.getJobs();
        const job = jobs[jobId];
        if (!job) return { ok: false, error: 'Job not found' };

        // Kasus 1: Hasil server sudah tersedia (ada array files dengan URL)
        // Retry lokal mengambil ulang file yang diperlukan tanpa POST /api/start
        const hasServerFiles = Array.isArray(job.files) && job.files.length > 0 && job.files.some(f => f.url);
        const isServerProcessed = !!job.serverProcessed;

        if (hasServerFiles || isServerProcessed) {
            // Periksa kedaluwarsa HANYA jika server menyediakan expiresAt yang valid
            if (job.expiresAt && Date.now() > job.expiresAt) {
                return {
                    ok: false,
                    error: 'Masa berlaku file di server telah kedaluwarsa.',
                    expired: true
                };
            }

            // Identifikasi file yang berhak diulang secara tepat
            let retryFileIds = [];
            let alreadyRunningCount = 0;
            let completedCount = 0;

            for (let i = 0; i < job.files.length; i++) {
                const f = job.files[i];
                if (!f.fileId) f.fileId = `${jobId}::${f.name || 'file'}::${i}`;
                if (f.status === 'completed') {
                    completedCount++;
                } else if (f.claimId || f.status === 'starting' || (f.downloadId && f.status === 'downloading')) {
                    alreadyRunningCount++;
                } else if (['failed', 'interrupted', 'pending'].includes(f.status)) {
                    retryFileIds.push(f.fileId);
                }
            }

            if (retryFileIds.length === 0) {
                if (alreadyRunningCount > 0) {
                    return {
                        ok: true,
                        mode: 'local_files',
                        status: 'already_running',
                        started: 0,
                        alreadyRunning: alreadyRunningCount,
                        message: 'Transfer file sedang berjalan di latar belakang.'
                    };
                }
                if (completedCount === job.files.length) {
                    return {
                        ok: true,
                        mode: 'local_files',
                        status: 'no_files_to_retry',
                        started: 0,
                        message: 'Semua file sudah selesai diunduh.'
                    };
                }
                return {
                    ok: false,
                    error: 'Tidak ada file yang memenuhi syarat untuk diulang.'
                };
            }

            await this.withLock(async () => {
                const currentData = await this.getJobs();
                const targetJob = currentData.jobs[jobId];
                if (targetJob) {
                    targetJob.status = 'saving';
                    targetJob.downloadStatus = 'downloading';
                    targetJob.stageLabel = 'Mengulang penyimpanan file ke komputer…';
                    for (let i = 0; i < targetJob.files.length; i++) {
                        const f = targetJob.files[i];
                        if (!f.fileId) f.fileId = `${jobId}::${f.name || 'file'}::${i}`;
                        // Jangan mereset file completed, downloading, atau starting milik operasi lain
                        if (retryFileIds.includes(f.fileId)) {
                            f.status = 'interrupted';
                            f.error = null;
                            f.downloadId = null;
                            f.claimId = null;
                            f.intent = null;
                            f.intentTime = null;
                        }
                    }
                    targetJob.updatedAt = Date.now();
                    await this.saveJobs(currentData.jobs);
                }
            });

            const saveRes = await this.saveFilesToDisk(jobId, null, { targetFileIds: retryFileIds, onlyFailed: true });

            if (saveRes && saveRes.started > 0) {
                return {
                    ok: true,
                    mode: 'local_files',
                    status: 'started',
                    started: saveRes.started,
                    alreadyRunning: saveRes.alreadyRunning
                };
            }

            if (saveRes && saveRes.alreadyRunning > 0) {
                return {
                    ok: true,
                    mode: 'local_files',
                    status: 'already_running',
                    started: 0,
                    alreadyRunning: saveRes.alreadyRunning,
                    message: 'Transfer file sedang berjalan di latar belakang.'
                };
            }

            return {
                ok: false,
                mode: 'local_files',
                status: 'failed',
                error: (saveRes?.errors && saveRes.errors.length > 0) ? saveRes.errors.join('; ') : 'Gagal memulai unduhan Chrome.'
            };
        }

        // Kasus 2: Pekerjaan gagal di tahap server -> buat tugas baru
        try {
            const res = await APIClient.sendJob(job.payload, job.sessionToken);
            if (res && res.job_id) {
                const newJob = await this.addJob(res.job_id, job.payload, job.title, job.sessionToken);
                return { ok: true, mode: 'new_server_job', newJobId: res.job_id };
            }
            return { ok: false, error: 'Tidak mendapatkan Job ID baru' };
        } catch (e) {
            return { ok: false, error: e.message };
        }
    }
}

