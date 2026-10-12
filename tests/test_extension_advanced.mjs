import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Setup deep-cloning mock Chrome storage and mock Chrome downloads API
const mockDownloads = [];
globalThis.chrome = {
    storage: {
        local: {
            data: {},
            async get(keys) {
                const cloned = JSON.parse(JSON.stringify(this.data));
                const res = {};
                if (Array.isArray(keys)) {
                    for (const k of keys) res[k] = cloned[k];
                } else if (typeof keys === 'string') {
                    res[keys] = cloned[keys];
                } else if (keys && typeof keys === 'object') {
                    for (const k of Object.keys(keys)) res[k] = cloned[k] !== undefined ? cloned[k] : keys[k];
                } else {
                    Object.assign(res, cloned);
                }
                return res;
            },
            async set(obj) {
                const cloned = JSON.parse(JSON.stringify(obj));
                Object.assign(this.data, cloned);
            }
        },
        session: {
            data: {},
            async get(key) {
                const cloned = JSON.parse(JSON.stringify(this.data));
                return { [key]: cloned[key] || [] };
            },
            async set(obj) {
                const cloned = JSON.parse(JSON.stringify(obj));
                Object.assign(this.data, cloned);
            },
            async remove(key) {
                delete this.data[key];
            }
        }
    },
    action: {
        async setBadgeBackgroundColor() {},
        async setBadgeText() {}
    },
    runtime: {
        id: 'mock-ext-id',
        getManifest() {
            return JSON.parse(fs.readFileSync(path.join(extDir, 'manifest.json'), 'utf8'));
        },
        onInstalled: { addListener() {} },
        onStartup: { addListener() {} },
        onMessage: { addListener() {} }
    },
    webRequest: {
        onResponseStarted: { addListener() {} },
        onBeforeRequest: { addListener() {} }
    },
    tabs: {
        onRemoved: { addListener() {} }
    },
    downloads: {
        downloadFailMode: false,
        nextId: 501,
        async download(options) {
            if (this.downloadFailMode) {
                throw new Error('Download API Failed: ACCESS_DENIED');
            }
            const id = this.nextId++;
            const item = { id, ...options, state: 'in_progress', bytesReceived: 0, totalBytes: 1048576 };
            mockDownloads.push(item);
            return id;
        },
        async search(query) {
            if (this.searchFailMode) {
                throw new Error('Search API internal failure');
            }
            if (query && query.id) {
                return mockDownloads.filter(d => d.id === query.id);
            }
            if (query && query.url) {
                return mockDownloads.filter(d => d.url === query.url || d.url.includes(query.url));
            }
            return mockDownloads;
        },
        async cancel(id) {
            const item = mockDownloads.find(d => d.id === id);
            if (item) item.state = 'interrupted';
        },
        onChanged: { addListener() {} }
    },
    alarms: {
        create() {},
        onAlarm: { addListener() {} }
    },
    contextMenus: {
        create() {},
        onClicked: { addListener() {} }
    }
};

// Pasang mock fetch sebelum modul diimpor agar tidak menyentuh server produksi
globalThis.fetch = async (url) => {
    throw new Error(`UNEXPECTED NETWORK CALL to ${url} in tests!`);
};

async function runAdvancedTests() {
    const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));
    const { Config, BASE_URL } = await import(path.join(extDir, 'background/config.js'));
    const { APIClient } = await import(path.join(extDir, 'background/api.js'));
    const { Detector } = await import(path.join(extDir, 'background/detector.js'));
    const { convertVttToSrt } = await import(path.join(extDir, 'utils/subtitle.js'));

    console.log('=== TEST 1: Semua Download Gagal Dimulai — Tidak Boleh Completed ===');
    chrome.storage.local.data = { app: BASE_URL, token: 'session_A' };
    mockDownloads.length = 0;
    chrome.downloads.downloadFailMode = true;

    const failJob = await JobManager.addJob('job-fail-all', { urls: 'https://example.com/fail.mp4' }, 'Gagal Total', 'session_A');
    await JobManager.saveFilesToDisk('job-fail-all', [
        { name: 'video.mp4', url: '/download/job-fail-all/video.mp4' }
    ]);

    const { jobs: jobsFail1 } = await JobManager.getJobs();
    assert.equal(jobsFail1['job-fail-all'].status, 'failed', 'Status must be failed if download cannot start');
    assert.equal(jobsFail1['job-fail-all'].downloadStatus, 'failed');
    assert.ok(jobsFail1['job-fail-all'].stageLabel.includes('Gagal menyimpan'), 'Stage label must explain storage failure');
    chrome.downloads.downloadFailMode = false;
    console.log('✔ Verified: Failed download start never claims completed.');

    console.log('\n=== TEST 2: Satu File Gagal di Antara Beberapa Hasil & Status Per-File ===');
    mockDownloads.length = 0;
    const partialFiles = [
        { name: 'good.mp4', url: '/download/job-partial/good.mp4', size: 1000 },
        { name: 'bad.mp4', url: '', size: 500 } // URL tidak valid
    ];

    await JobManager.addJob('job-partial', { urls: 'https://example.com/part.mp4' }, 'Sebagian Berhasil', 'session_A');
    await JobManager.saveFilesToDisk('job-partial', partialFiles);

    const { jobs: jobsPartial } = await JobManager.getJobs();
    const pj = jobsPartial['job-partial'];
    assert.equal(pj.files.length, 2);
    assert.equal(pj.files[0].status, 'downloading');
    assert.equal(pj.files[1].status, 'failed');
    assert.ok(pj.files[1].error);
    assert.ok(pj.stageLabel.includes('1/2 file berjalan'));
    console.log('✔ Verified: Partial failure tracked per-file with honest progress reporting.');

    console.log('\n=== TEST 3: Interrupted Lalu Retry Lokal Tanpa POST /api/start Baru ===');
    let apiStartCalled = false;
    globalThis.fetch = async (url, opts) => {
        if (url.includes('/api/start')) {
            apiStartCalled = true;
            return { ok: true, json: async () => ({ job_id: 'new-job' }) };
        }
        return { ok: false, status: 404 };
    };

    // Tandai file baik sebagai interrupted
    await JobManager.handleDownloadDelta({
        id: mockDownloads[0].id,
        state: { current: 'interrupted' },
        error: { current: 'NETWORK_TIMEOUT' }
    });

    const { jobs: jobsBeforeRetry } = await JobManager.getJobs();
    assert.equal(jobsBeforeRetry['job-partial'].downloadStatus, 'interrupted');
    assert.equal(jobsBeforeRetry['job-partial'].status, 'failed');

    // Perbaiki URL yang buruk sebelum retry
    jobsBeforeRetry['job-partial'].files[1].url = '/download/job-partial/bad.mp4';
    await JobManager.saveJobs(jobsBeforeRetry);

    // Jalankan retry lokal
    const retryRes = await JobManager.retryJob('job-partial');
    assert.equal(retryRes.ok, true);
    assert.equal(retryRes.mode, 'local_files', 'Retry must execute local files retry mode without re-triggering server');
    assert.equal(apiStartCalled, false, 'No /api/start should be called for local download retry');
    console.log('✔ Verified: Local file retry resumes without re-processing on server.');

    console.log('\n=== TEST 4: ID Download Hilang & Search Gagal Tidak Boleh Dianggap Selesai ===');
    await JobManager.addJob('job-lost-id', { urls: 'https://example.com/v.mp4' }, 'Lost ID Test', 'session_A');
    await JobManager.saveFilesToDisk('job-lost-id', [
        { name: 'test.mp4', url: '/download/job-lost-id/test.mp4' }
    ]);

    // Hapus download dari mock Downloads Chrome (seolah ID terhapus atau browser crash)
    mockDownloads.length = 0;
    await JobManager.reconcileJobDownloads('job-lost-id');

    const { jobs: jobsLost } = await JobManager.getJobs();
    assert.equal(jobsLost['job-lost-id'].status, 'failed', 'Lost ID must result in failed status');
    assert.equal(jobsLost['job-lost-id'].files[0].status, 'failed');
    assert.equal(jobsLost['job-lost-id'].files[0].error, 'Download ID hilang');

    // Kasus search error
    chrome.downloads.searchFailMode = true;
    jobsLost['job-lost-id'].files[0].status = 'downloading';
    jobsLost['job-lost-id'].files[0].downloadId = 888;
    await JobManager.saveJobs(jobsLost);

    await JobManager.reconcileJobDownloads('job-lost-id');
    const { jobs: jobsSearchErr } = await JobManager.getJobs();
    assert.notEqual(jobsSearchErr['job-lost-id'].status, 'completed', 'Search API error must NEVER be assumed completed');
    chrome.downloads.searchFailMode = false;
    console.log('✔ Verified: Missing ID and search failure handled honestly without false complete.');

    console.log('\n=== TEST 5: Worker Berhenti Sebelum dan Sesudah Chrome Mengembalikan ID ===');
    // Simulasi worker di-kill sebelum chrome.downloads.download selesai
    const killedJob = await JobManager.addJob('job-killed', { urls: 'https://example.com/k.mp4' }, 'Kill Test', 'session_A');
    assert.equal(killedJob.downloadTriggered, false);

    // Saat proses saveFilesToDisk berjalan, status disimpan segera
    mockDownloads.length = 0;
    await JobManager.saveFilesToDisk('job-killed', [
        { name: 'k.mp4', url: '/download/job-killed/k.mp4' }
    ]);

    const { jobs: jobsKilled } = await JobManager.getJobs();
    assert.ok(jobsKilled['job-killed'].files[0].downloadId, 'downloadId must be immediately recorded per-file');
    assert.equal(jobsKilled['job-killed'].files[0].status, 'downloading');

    // Simulasi restart dan rekonsiliasi
    mockDownloads[0].state = 'complete';
    await JobManager.reconcileJobDownloads('job-killed');

    const { jobs: jobsRecovered } = await JobManager.getJobs();
    assert.equal(jobsRecovered['job-killed'].status, 'completed');
    assert.equal(jobsRecovered['job-killed'].downloadStatus, 'complete');
    console.log('✔ Verified: Service worker state immediately saved per-file and reconciled on startup.');

    console.log('\n=== TEST 6: Penyelesaian Download Ketika Popup Tertutup ===');
    // Event onChanged diproses di background worker tanpa membutuhkan interaksi UI
    const bgJob = await JobManager.addJob('job-bg', { urls: 'https://example.com/bg.mp4' }, 'Background Test', 'session_A');
    await JobManager.saveFilesToDisk('job-bg', [
        { name: 'bg.mp4', url: '/download/job-bg/bg.mp4', size: 500000 }
    ]);

    const assignedId = mockDownloads[mockDownloads.length - 1].id;
    await JobManager.handleDownloadDelta({
        id: assignedId,
        bytesReceived: { current: 500000 },
        totalBytes: { current: 500000 },
        state: { current: 'complete' }
    });

    const { jobs: jobsBgDone } = await JobManager.getJobs();
    assert.equal(jobsBgDone['job-bg'].status, 'completed');
    assert.equal(jobsBgDone['job-bg'].percent, 100);
    assert.ok(jobsBgDone['job-bg'].stageLabel.includes('Selesai'));
    console.log('✔ Verified: Downloads complete properly in background even when popup is closed.');

    console.log('\n=== TEST 7: Stop Server Gagal & Stop Bertabrakan Dengan Polling ===');
    let stopAttempts = 0;
    globalThis.fetch = async (url) => {
        if (url.includes('/api/stop')) {
            stopAttempts++;
            throw new Error('500 Internal Server Error: Worker crashed');
        }
        return { ok: true, json: async () => ({}) };
    };

    const stopJob = await JobManager.addJob('job-stop-test', { urls: 'https://example.com/stop.mp4' }, 'Stop Fail', 'session_A');
    await JobManager.updateJob('job-stop-test', { status: 'downloading' });

    const stopResult = await JobManager.stopJob('job-stop-test');
    assert.equal(stopResult.ok, false);
    assert.ok(stopResult.error.includes('500'));

    const { jobs: jobsAfterStopFail } = await JobManager.getJobs();
    assert.notEqual(jobsAfterStopFail['job-stop-test'].status, 'cancelled', 'Job must NOT be marked cancelled if server stop call fails');
    assert.ok(jobsAfterStopFail['job-stop-test'].stageLabel.includes('Gagal menghentikan di server'));
    console.log('✔ Verified: Stop failures do not falsely claim job cancelled.');

    console.log('\n=== TEST 8: Perubahan Sesi Saat Pekerjaan Masih Ada ===');
    // Buat pekerjaan dengan token lama
    await JobManager.addJob('job-session-test', { urls: 'https://example.com/s.mp4' }, 'Session Test', 'token_OLD_123');
    
    // Pengguna mengganti token di pengaturan
    await Config.setAppInfo('token_NEW_999');

    // Ambil pekerjaan kembali
    const { jobs: jobsSession } = await JobManager.getJobs();
    assert.equal(jobsSession['job-session-test'].sessionToken, 'token_OLD_123', 'Job must preserve bound session token created at launch');
    console.log('✔ Verified: User session changes do not break or invalidate active/historical jobs.');

    console.log('\n=== TEST 9: Penanganan API — HTTP 500, HTML Proxy, JSON Tidak Sesuai & Body Timeout ===');
    // 1. HTTP 500
    globalThis.fetch = async () => ({
        ok: false,
        status: 500,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ error: "Database error" }),
        text: async () => '{"error": "Database error"}'
    });
    await assert.rejects(
        async () => await APIClient.getJobStatus('job-500', 'token_A'),
        (err) => err.code === 'HTTP_500'
    );

    // 2. HTML dari Proxy / Captive Portal
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'text/html' }),
        text: async () => '<!DOCTYPE html><html><body>Login Required</body></html>'
    });
    await assert.rejects(
        async () => await APIClient.getJobStatus('job-html', 'token_A'),
        (err) => err.code === 'HTML_RESPONSE'
    );

    // 3. JSON Tidak Sesuai / Format Kosong
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        text: async () => JSON.stringify({ jobs: [] }) // Tidak ada job_id
    });
    await assert.rejects(
        async () => await APIClient.getJobStatus('job-malformed', 'token_A'),
        (err) => err.code === 'INVALID_RESPONSE'
    );
    console.log('✔ Verified: HTTP 500, Proxy HTML, and malformed responses caught and classified cleanly.');

    console.log('\n=== TEST 10: Subtitle dari Dua Video Berbeda & Pilihan Tanpa Subtitle ===');
    const items = [
        { kind: 'subtitle', url: 'https://cdn1.site.com/movieA/sub_indonesia.vtt', statusLabel: 'Indonesia' },
        { kind: 'subtitle', url: 'https://cdn2.site.com/movieB/sub_indonesia.vtt', statusLabel: 'Indonesia' },
        { kind: 'subtitle', url: 'https://cdn2.site.com/movieB/sub_english.vtt', statusLabel: 'English' }
    ];

    // Video saat ini adalah movieB
    const currentVideo = 'https://cdn2.site.com/movieB/master.m3u8';
    const subMatch = Detector.findDefaultSubtitle(items, currentVideo);
    assert.ok(subMatch, 'Must find matching subtitle');
    assert.equal(subMatch.url, 'https://cdn2.site.com/movieB/sub_indonesia.vtt', 'Must correlate to movieB, not movieA');

    // Video tanpa korelasi apapun
    const unrelatedVideo = 'https://cdn3.other.com/movieC/video.mp4';
    const subNoMatch = Detector.findDefaultSubtitle(items, unrelatedVideo);
    assert.equal(subNoMatch, null, 'Must NOT erroneously pair unrelated subtitles to new video');
    console.log('✔ Verified: Subtitles strictly correlated to video origin and path.');

    console.log('\n=== TEST 11: Sanitasi Nama File Panjang & Preservasi Ekstensi ===');
    const superLong = 'Ini adalah nama video yang luar biasa panjang sekali melebihi batas enam puluh karakter pada sistem file komputer pengguna yang normal.mp4';
    const cleanLong = JobManager.sanitizeFileName(superLong);
    assert.ok(cleanLong.endsWith('.mp4'), 'Extension .mp4 must be preserved');
    assert.ok(cleanLong.length <= 65, 'Base name must be clamped while keeping extension');

    // Reserved Windows filename
    const reservedCon = JobManager.sanitizeFileName('CON.mkv');
    assert.ok(!reservedCon.toUpperCase().startsWith('CON.'), 'Reserved CON name must be sanitized');
    assert.ok(reservedCon.endsWith('.mkv'), 'Extension .mkv must be preserved for reserved names');

    // Dots only
    const dotFile = JobManager.sanitizeFileName('.....mp3');
    assert.ok(dotFile.endsWith('.mp3'), 'Extension .mp3 must be preserved for leading dots');
    console.log('✔ Verified: Long filenames clamped, extensions preserved, reserved names protected.');

    console.log('\n=== TEST 12: ZIP vs File Individual Tanpa Gandakan Bandwidth ===');
    const mixedFiles = [
        { name: 'Video.mp4', url: '/download/j1/Video.mp4', size: 1000 },
        { name: 'Video.srt', url: '/download/j1/Video.srt', size: 50 },
        { name: 'Bundle.zip', url: '/download/j1/Bundle.zip', size: 1050, is_zip: true }
    ];

    // Default: preferZip = false -> hanya download file individual (video + srt)
    const structuredDefault = JobManager.prepareStructuredFiles({ title: 'Test', preferZip: false }, mixedFiles);
    assert.equal(structuredDefault.length, 2, 'Default must exclude duplicate ZIP archive');
    assert.equal(structuredDefault.some(f => f.isZip), false);

    // Prefer ZIP: hanya download ZIP
    const structuredZip = JobManager.prepareStructuredFiles({ title: 'Test', preferZip: true }, mixedFiles);
    assert.equal(structuredZip.length, 1, 'preferZip must select only the ZIP archive');
    assert.equal(structuredZip[0].isZip, true);

    // Carousel: Single slide selection (hanya unduh slide 2)
    const carouselFiles = [
        { name: 'Instagram_Post [1].jpg', url: '/download/j2/1.jpg', size: 500 },
        { name: 'Instagram_Post [2].jpg', url: '/download/j2/2.jpg', size: 600 },
        { name: 'Instagram_Post [3].mp4', url: '/download/j2/3.mp4', size: 2000 },
        { name: 'Instagram_Post.zip', url: '/download/j2/bundle.zip', size: 3100, is_zip: true }
    ];
    const structuredSlide2 = JobManager.prepareStructuredFiles({ title: 'Test IG', slideIndex: 2 }, carouselFiles);
    assert.equal(structuredSlide2.length, 1, 'Single slide selection must filter down to exactly 1 file');
    assert.equal(structuredSlide2[0].name, 'Instagram_Post [2].jpg');
    assert.equal(structuredSlide2[0].isZip, false);

    console.log('✔ Verified: Individual vs ZIP selection and single slide carousel filtering work perfectly.');

    console.log('\n=== TEST 13: Subtitle WebVTT ke SubRip (.srt) Converter & Alur Pengguna ===');
    const sampleVtt = `WEBVTT
NOTE Komentar pembuat

1
00:00:01.000 --> 00:00:04.500
<v Narrator>Halo <b>dunia</b> &amp; selamat datang!

2
00:00:05.100 --> 00:00:08.200
Baris kedua subtitle`;

    const convertedSrt = convertVttToSrt(sampleVtt);
    assert.ok(convertedSrt.includes('00:00:01,000 --> 00:00:04,500'), 'Timecodes must use SubRip comma format');
    assert.ok(convertedSrt.includes('Halo dunia & selamat datang!'), 'Tags must be stripped and entities decoded');
    assert.ok(!convertedSrt.includes('WEBVTT'));
    console.log('✔ Verified: VTT to SRT conversion adheres strictly to SubRip UTF-8 standard.');

    console.log('\n=== TEST 14: Regresi 1 - Progres Transfer Lokal Disalin dari Search & Format Jujur ===');
    const jobBytes = await JobManager.addJob('job-bytes-test', { urls: 'https://example.com/b.mp4' }, 'Bytes Test', 'session_A');
    jobBytes.status = 'saving';
    jobBytes.files = [
        { name: 'b.mp4', cleanName: 'b.mp4', url: '/download/job-bytes-test/b.mp4', status: 'downloading', downloadId: 701, bytesReceived: 0, totalBytes: 0 }
    ];
    await JobManager.saveJobs({ 'job-bytes-test': jobBytes });

    // Mock downloads.search mengembalikan 500 bytes dari 1000 bytes
    mockDownloads.length = 0;
    mockDownloads.push({
        id: 701,
        state: 'in_progress',
        bytesReceived: 500,
        totalBytes: 1000
    });

    await JobManager.reconcileJobDownloads('job-bytes-test');
    const { jobs: jobsBytes1 } = await JobManager.getJobs();
    const jb1 = jobsBytes1['job-bytes-test'];
    assert.equal(jb1.files[0].bytesReceived, 500, 'bytesReceived harus disalin dari hasil search');
    assert.equal(jb1.files[0].totalBytes, 1000, 'totalBytes harus disalin dari hasil search');
    assert.equal(jb1.files[0].status, 'downloading');
    assert.ok(jb1.stageLabel.includes('(50%)'), 'Progres lokal harus terhitung dari search');

    // Uji jika totalBytes tidak diketahui (totalBytes <= 0): tidak boleh ada % rekaan
    mockDownloads[0].bytesReceived = 1048576; // 1 MB
    mockDownloads[0].totalBytes = 0; // tidak diketahui
    await JobManager.reconcileJobDownloads('job-bytes-test');
    const { jobs: jobsBytes2 } = await JobManager.getJobs();
    const jb2 = jobsBytes2['job-bytes-test'];
    assert.equal(jb2.files[0].bytesReceived, 1048576);
    assert.ok(jb2.stageLabel.includes('1.0 MB'), 'Harus menampilkan megabyte terunduh');
    assert.ok(!jb2.stageLabel.includes('%'), 'Tidak boleh ada persentase rekaan jika totalBytes tidak diketahui');
    console.log('✔ Verified: Local transfer progress copied from search without fake percentages.');

    console.log('\n=== TEST 15: Regresi 2 - Cancel Lokal Lalu Retry Tanpa POST /api/start Baru ===');
    const jobCancelRetry = await JobManager.addJob('job-cancel-retry', { urls: 'https://example.com/cr.mp4' }, 'Cancel Retry Test', 'session_A');
    jobCancelRetry.status = 'saving';
    jobCancelRetry.files = [
        { name: 'cr.mp4', cleanName: 'cr.mp4', url: '/download/job-cancel-retry/cr.mp4', status: 'downloading', downloadId: 702, bytesReceived: 100, totalBytes: 200 }
    ];
    await JobManager.saveJobs({ 'job-cancel-retry': jobCancelRetry });

    // Pengguna menghentikan unduhan lokal
    await JobManager.stopJob('job-cancel-retry');
    const { jobs: jobsAfterStop } = await JobManager.getJobs();
    assert.equal(jobsAfterStop['job-cancel-retry'].status, 'cancelled');

    // Pengguna memilih retry: pastikan APIClient.sendJob TIDAK DIPANGGIL!
    let illegalSendJobCalled = false;
    const origSendJob = APIClient.sendJob;
    APIClient.sendJob = async () => {
        illegalSendJobCalled = true;
        throw new Error('ILLEGAL: sendJob should never be called when server files exist!');
    };

    const retryLocalResult = await JobManager.retryJob('job-cancel-retry');
    APIClient.sendJob = origSendJob;

    assert.equal(illegalSendJobCalled, false, 'Dilarang memanggil APIClient.sendJob baru saat retry hasil lokal!');
    assert.equal(retryLocalResult.ok, true);
    assert.equal(retryLocalResult.mode, 'local_files');
    const { jobs: jobsAfterRetry } = await JobManager.getJobs();
    assert.equal(jobsAfterRetry['job-cancel-retry'].status, 'saving');
    console.log('✔ Verified: Local retry resumes files without triggering server POST /api/start.');

    console.log('\n=== TEST 16: Regresi 3 - Pemulihan Worker di Tiga Titik Sebelum/Sesudah ID Tersimpan ===');
    // Titik A: Berhenti sebelum pemanggilan download (pending, intent null)
    // Titik B: Berhenti sesudah Chrome menerima download tetapi sebelum ID tersimpan (intent initiated, downloadId null)
    // Titik C: Berhenti setelah ID tersimpan (downloadId 703)
    const jobRecovery = await JobManager.addJob('job-rec-3pt', { urls: 'https://example.com/rec.mp4' }, 'Recovery 3PT', 'session_A');
    jobRecovery.status = 'saving';
    jobRecovery.files = [
        // File 1: Titik B
        { name: 'b_point.mp4', cleanName: 'b_point.mp4', url: '/download/job-rec-3pt/b_point.mp4', status: 'pending', intent: 'download_initiated', intentTime: Date.now(), downloadId: null },
        // File 2: Titik C
        { name: 'c_point.mp4', cleanName: 'c_point.mp4', url: '/download/job-rec-3pt/c_point.mp4', status: 'downloading', intent: 'download_confirmed', downloadId: 703, bytesReceived: 10, totalBytes: 20 },
        // File 3: Titik A
        { name: 'a_point.mp4', cleanName: 'a_point.mp4', url: '/download/job-rec-3pt/a_point.mp4', status: 'pending', intent: null, downloadId: null }
    ];
    await JobManager.saveJobs({ 'job-rec-3pt': jobRecovery });

    // Mock Chrome state:
    // Download yang cocok dengan File 1 (Titik B)
    mockDownloads.length = 0;
    mockDownloads.push({
        id: 704,
        url: 'https://dw.pmlab.id/download/job-rec-3pt/b_point.mp4?session=session_A',
        state: 'in_progress',
        bytesReceived: 50,
        totalBytes: 100,
        startTime: new Date().toISOString()
    });
    // Download yang cocok dengan File 2 (Titik C)
    mockDownloads.push({
        id: 703,
        url: 'https://dw.pmlab.id/download/job-rec-3pt/c_point.mp4?session=session_A',
        state: 'complete',
        bytesReceived: 20,
        totalBytes: 20,
        startTime: new Date().toISOString()
    });

    await JobManager.reconcileJobDownloads('job-rec-3pt');
    const { jobs: jobsRecAfter } = await JobManager.getJobs();
    const jr = jobsRecAfter['job-rec-3pt'];

    // File 1 (Titik B) berhasil dipulihkan ID-nya dari Chrome history
    assert.equal(jr.files[0].downloadId, 704, 'File titik B harus pulih dan terikat ke downloadId Chrome');
    assert.equal(jr.files[0].status, 'downloading');
    assert.equal(jr.files[0].intent, 'download_confirmed');

    // File 2 (Titik C) terupdate statusnya
    assert.equal(jr.files[1].status, 'completed');

    // File 3 (Titik A) harus dilanjutkan setelah pemulihan
    assert.ok(jr.files[2].downloadId !== null || jr.files[2].status === 'downloading', 'File titik A harus dilanjutkan');
    console.log('✔ Verified: Three-point crash recovery handles pre-call, unpersisted-ID, and post-ID states safely.');

    console.log('\n=== TEST 17: Regresi 4 - Subtitle Film Berbeda pada Hostname & Folder Umum yang Sama ===');
    const videoFilmA = 'https://cdn.test/videos/filmA/master.m3u8';
    const subFilmB = { url: 'https://cdn.test/videos/filmB/id.srt', kind: 'subtitle', statusLabel: 'Subtitle ID Film B' };
    const subFilmA = { url: 'https://cdn.test/videos/filmA/id.srt', kind: 'subtitle', statusLabel: 'Subtitle ID Film A' };

    // Jangan pernah memilih subFilmB untuk videoFilmA meskipun folder "videos" sama!
    const subChoiceWrong = Detector.findDefaultSubtitle([subFilmB], videoFilmA);
    assert.equal(subChoiceWrong, null, 'Subtitle filmB tidak boleh dipilih untuk video filmA!');

    // Pilih yang benar jika ada subFilmA
    const subChoiceRight = Detector.findDefaultSubtitle([subFilmB, subFilmA], videoFilmA);
    assert.equal(subChoiceRight?.url, subFilmA.url, 'Harus memilih subtitle yang identitasnya cocok dengan filmA');

    // Uji juga struktur /v/{server}/{videoId}/
    const videoV = 'https://cdn.test/v/srv1/filmX/master.m3u8';
    const subVMatch = { url: 'https://cdn.test/v/srv1/filmX/id.vtt', kind: 'subtitle', statusLabel: 'Subtitle Indonesia' };
    const subVMismatch = { url: 'https://cdn.test/v/srv1/filmY/id.vtt', kind: 'subtitle', statusLabel: 'Subtitle Indonesia' };
    assert.equal(Detector.findDefaultSubtitle([subVMismatch], videoV), null);
    assert.equal(Detector.findDefaultSubtitle([subVMismatch, subVMatch], videoV)?.url, subVMatch.url);
    console.log('✔ Verified: Subtitle matching prevents cross-movie contamination on generic path folders.');

    console.log('\n=== TEST 18: Regresi 5 - Batas Ukuran VTT 2 MB dan Timeout Body Stream ===');
    // 18a: Body VTT melebihi 2 MB
    const chunk1MB = new Uint8Array(1024 * 1024).fill(65); // 'A'
    let streamReaderCancelled = false;
    globalThis.fetch = async (url) => {
        return {
            ok: true,
            status: 200,
            body: {
                getReader() {
                    let count = 0;
                    return {
                        async read() {
                            count++;
                            if (count <= 3) { // 3 x 1MB = 3MB > 2MB
                                return { done: false, value: chunk1MB };
                            }
                            return { done: true };
                        },
                        async cancel() {
                            streamReaderCancelled = true;
                        }
                    };
                }
            }
        };
    };

    const bigVttRes = await JobManager.fetchAndConvertVtt('https://dw.pmlab.id/download/test/big.vtt', 'Media', 'big.vtt');
    assert.equal(bigVttRes.converted, false, 'VTT > 2 MB tidak boleh dikonversi');
    assert.equal(bigVttRes.targetFileName, 'big.vtt', 'Nama file harus tetap .vtt');
    assert.ok(bigVttRes.warning.includes('2 MB'), 'Harus mencatat peringatan batas 2 MB');
    assert.equal(streamReaderCancelled, true, 'Stream reader harus di-cancel saat limit terlampaui');

    // 18b: Stream macet / timeout pembacaan body
    let timeoutReaderCancelled = false;
    globalThis.fetch = async (url, opts) => {
        return {
            ok: true,
            status: 200,
            body: {
                getReader() {
                    return {
                        async read() {
                            return new Promise((resolve, reject) => {
                                if (opts && opts.signal) {
                                    opts.signal.addEventListener('abort', () => {
                                        const err = new Error('The operation was aborted');
                                        err.name = 'AbortError';
                                        reject(err);
                                    });
                                }
                            });
                        },
                        async cancel() {
                            timeoutReaderCancelled = true;
                        }
                    };
                }
            }
        };
    };

    // Panggil fetchAndConvertVtt: abort signal akan membatalkan stream
    const hangVttRes = await JobManager.fetchAndConvertVtt('https://dw.pmlab.id/download/test/hang.vtt', 'Media', 'hang.vtt');
    assert.equal(hangVttRes.converted, false);
    assert.equal(hangVttRes.targetFileName, 'hang.vtt');
    assert.ok(hangVttRes.warning);
    console.log('✔ Verified: VTT 2 MB limit streaming cancellation and body-covering timeout confirmed.');

    console.log('\n=== TEST 19: Regresi 6 - Stop Saat Operasi Jaringan/Konversi Subtitle Sedang Berjalan ===');
    const jobSlowSub = await JobManager.addJob('job-slow-sub', { urls: 'https://example.com/slow.mp4' }, 'Slow Sub', 'session_A');
    jobSlowSub.status = 'saving';
    jobSlowSub.files = [
        { name: 'slow.vtt', url: '/download/job-slow-sub/slow.vtt', status: 'pending' }
    ];
    await JobManager.saveJobs({ 'job-slow-sub': jobSlowSub });

    // Mock fetch lambat (80ms)
    globalThis.fetch = async (url) => {
        await new Promise(r => setTimeout(r, 80));
        return {
            ok: true,
            body: {
                getReader() {
                    return {
                        async read() {
                            return { done: true };
                        },
                        async cancel() {}
                    };
                }
            }
        };
    };

    // Mulai proses penyimpanan (asinkron di latar belakang)
    const savePromise = JobManager.saveFilesToDisk('job-slow-sub');

    // Pengguna langsung menekan stop saat penyimpanan masih menunggu fetch
    await new Promise(r => setTimeout(r, 20));
    const stopAsyncResult = await JobManager.stopJob('job-slow-sub');
    assert.equal(stopAsyncResult.ok, true, 'StopJob tidak boleh terblokir oleh operasi jaringan yang berjalan!');

    // Tunggu proses simpan selesai
    await savePromise;
    const { jobs: jobsSlowAfter } = await JobManager.getJobs();
    assert.equal(jobsSlowAfter['job-slow-sub'].status, 'cancelled', 'Respons fetch yang terlambat tidak boleh menimpa status pembatalan');
    console.log('✔ Verified: Lock separation ensures instant stop and prevents late response race condition.');

    console.log('\n=== TEST 20: Regresi 7 - Dua Retry Bersamaan (SubmissionCoordinator) ===');
    const { SubmissionCoordinator } = await import(path.join(extDir, 'background/main.js'));
    const retryKey = 'retry::concurrent-test-123';
    const lock1 = await SubmissionCoordinator.acquire(retryKey);
    const lock2 = await SubmissionCoordinator.acquire(retryKey);
    assert.equal(lock1, true, 'Retry pertama harus berhasil mendapatkan lock');
    assert.equal(lock2, false, 'Retry kedua yang bersamaan harus ditolak sebagai duplikat');
    await SubmissionCoordinator.release(retryKey);
    const lock3 = await SubmissionCoordinator.acquire(retryKey);
    assert.equal(lock3, true, 'Setelah rilis, lock dapat diambil kembali');
    await SubmissionCoordinator.release(retryKey);
    console.log('✔ Verified: SubmissionCoordinator prevents concurrent duplicate submissions and retries.');

    console.log('\n=== TEST 21: Regresi 8 - Dua Kandidat Sniffer Jaringan Ditambahkan Bersamaan ===');
    await Detector.clear(999);
    // Jalankan dua addCandidate secara paralel pada tab yang sama
    await Promise.all([
        Detector.addCandidate(999, 'https://cdn.example.com/stream1.m3u8', 'application/vnd.apple.mpegurl'),
        Detector.addCandidate(999, 'https://cdn.example.com/video2.mp4', 'video/mp4')
    ]);
    const candidatesTab = await Detector.getCandidates(999);
    assert.equal(candidatesTab.length, 2, 'Kedua kandidat harus tersimpan tanpa saling menimpa!');
    console.log('✔ Verified: Per-tab mutex queue serializes concurrent media additions without dropping candidates.');

    console.log('\n=== TEST 22: Regresi 9 - Navigasi Terjadi Saat Verifikasi Jaringan Berlangsung ===');
    const tabNavId = 888;
    await Detector.clear(tabNavId);
    let verifyResolve = null;
    globalThis.fetch = async (url) => {
        return new Promise(resolve => {
            verifyResolve = () => {
                resolve({
                    ok: true,
                    status: 200,
                    text: async () => '#EXTM3U\n#EXT-X-VERSION:3\nchunk.m3u8'
                });
            };
        });
    };

    // Tambahkan kandidat JSON unverified
    const addPromise = Detector.addCandidate(tabNavId, 'https://example.com/feed.json', 'application/json');
    // Beri waktu sejenak agar request verifikasi berjalan
    await new Promise(r => setTimeout(r, 20));

    // Navigasi halaman terjadi (clear dipanggil)
    await Detector.clear(tabNavId);

    // Sekarang verifikasi jaringan lama baru selesai
    if (verifyResolve) verifyResolve();
    await addPromise;

    const itemsAfterNav = await Detector.getCandidates(tabNavId);
    assert.equal(itemsAfterNav.length, 0, 'Kandidat lama tidak boleh bangkit kembali setelah navigasi!');
    console.log('✔ Verified: Navigation generation guard discards stale verification results.');

    console.log('\n=== TEST 23: Regresi 10 - Penyamaran Menyeluruh Token Sintetis dan URL Bertanda Tangan ===');
    const { maskSensitive } = await import(path.join(extDir, 'background/main.js'));
    const syntheticSensitiveReport = `
Job ID: j_test_123
Sesi Terikat: omni9876543210abcdef999
Error: Gagal memuat https://cdn.example.com/media.mp4?sig=SECRET_SIGNATURE_ABC&session=omni_sess_test_111&token=SECRET_TOKEN_XYZ
Header: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret
Kredensial: https://admin:SuperSecretPass123@dw.pmlab.id/download/file.mp4
`;

    const sanitizedResult = maskSensitive(syntheticSensitiveReport);
    assert.ok(!sanitizedResult.includes('omni9876543210abcdef999'), 'Token sesi sintetis harus disamarkan');
    assert.ok(!sanitizedResult.includes('SECRET_SIGNATURE_ABC'), 'Signature query URL harus disamarkan');
    assert.ok(!sanitizedResult.includes('omni_sess_test_111'), 'Token session query harus disamarkan');
    assert.ok(!sanitizedResult.includes('SECRET_TOKEN_XYZ'), 'Token query harus disamarkan');
    assert.ok(!sanitizedResult.includes('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret'), 'Auth bearer token harus disamarkan');
    assert.ok(!sanitizedResult.includes('SuperSecretPass123'), 'Password dalam URL harus disamarkan');
    console.log('✔ Verified: Full redaction masks session tokens, auth headers, signed queries, and credentials.');

    console.log('\n=== TEST 24: Validitas Generator ZIP Murni Tanpa Server / Library Eksternal ===');
    const { createZipBlob, crc32 } = await import(path.join(extDir, 'utils/zip.js'));
    const enc = new TextEncoder();
    const testZipBlob = createZipBlob([
        { name: 'slide_1.jpg', data: enc.encode('Foto Slide 1 Buffer Data') },
        { name: 'slide_2.jpg', data: enc.encode('Foto Slide 2 Buffer Data') },
        { name: 'slide_3.mp4', data: enc.encode('Video Slide 3 Buffer Data') }
    ]);
    assert.ok(testZipBlob instanceof Blob, 'Output harus berupa Blob');
    assert.equal(testZipBlob.type, 'application/zip');
    const zipArrayBuffer = await testZipBlob.arrayBuffer();
    assert.ok(zipArrayBuffer.byteLength > 200, 'Ukuran ZIP harus valid dan memuat header');
    
    // Verifikasi struktur byte ZIP standar
    const zipU8 = new Uint8Array(zipArrayBuffer);
    const view = new DataView(zipU8.buffer);
    assert.equal(view.getUint32(0, true), 0x04034b50, 'Local header magic PK\\x03\\x04 harus valid');
    console.log('✔ Verified: Pure JS ZIP archive builder produces valid standard ZIP container.');

    console.log('\n============================================================');
    console.log('🏆 ALL 24 RIGOROUS COMPREHENSIVE EDGE-CASE & REGRESSION TESTS PASSED! 🏆');
    console.log('============================================================');
    process.exit(0);
}

runAdvancedTests().catch(err => {
    console.error('ADVANCED TEST FAILED:', err);
    process.exit(1);
});
