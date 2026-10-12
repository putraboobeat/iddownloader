import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Setup robust Chrome Mock with structured clone semantics
const mockDownloads = [];
globalThis.chrome = {
    storage: {
        local: {
            data: {},
            async get(keys) {
                await new Promise(r => setImmediate(r));
                const cloned = JSON.parse(JSON.stringify(this.data));
                if (typeof keys === 'string') return { [keys]: cloned[keys] };
                if (Array.isArray(keys)) {
                    const res = {};
                    for (const k of keys) res[k] = cloned[k];
                    return res;
                }
                return cloned;
            },
            async set(obj) {
                await new Promise(r => setImmediate(r));
                const cloned = JSON.parse(JSON.stringify(obj));
                Object.assign(this.data, cloned);
            }
        },
        session: {
            data: {},
            async get(k) { return { [k]: this.data[k] }; },
            async set(obj) { Object.assign(this.data, obj); }
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
        nextId: 100,
        async download(opts) {
            await new Promise(r => setTimeout(r, 15));
            const id = this.nextId++;
            mockDownloads.push({
                id,
                ...opts,
                state: 'in_progress',
                bytesReceived: 0,
                totalBytes: 1000,
                startTime: new Date().toISOString()
            });
            return id;
        },
        async search(query) {
            if (query && query.id) return mockDownloads.filter(d => d.id === query.id);
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

globalThis.fetch = async (url) => {
    throw new Error(`Offline mock: ${url}`);
};

async function runV212Tests() {
    console.log('====================================================');
    console.log('🧪 RUNNING OMNIFETCH COMPANION v2.1.2 RIGOROUS TESTS');
    console.log('====================================================');

    const { SubmissionCoordinator, maskSensitive } = await import(path.join(extDir, 'background/main.js'));
    const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));
    const { APIClient } = await import(path.join(extDir, 'background/api.js'));

    // -------------------------------------------------------------------------
    // PRIORITAS 1: Pengiriman Ganda Saat Concurrent
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 1: Pengiriman Ganda Saat Concurrent ---');
    chrome.storage.local.data = {};
    
    // Test 1.1: Dua acquire bersamaan via Promise.all (tepat satu berhasil)
    const [p1, p2] = await Promise.all([
        SubmissionCoordinator.acquire('url_concurrent'),
        SubmissionCoordinator.acquire('url_concurrent')
    ]);
    assert.deepStrictEqual(
        p1 ? [p1, p2] : [p2, p1],
        [true, false],
        'Dua acquire bersamaan dengan Promise.all harus menghasilkan tepat satu true dan satu false'
    );
    console.log('✔ 1.1: Dua acquire bersamaan via Promise.all: tepat satu berhasil.');

    // Test 1.2: Dua kunci berbeda tidak saling menghilangkan entri
    const [diffA, diffB] = await Promise.all([
        SubmissionCoordinator.acquire('key_A'),
        SubmissionCoordinator.acquire('key_B')
    ]);
    assert.equal(diffA, true);
    assert.equal(diffB, true);
    const storedSubmissions = await SubmissionCoordinator.getAllSubmissions();
    assert.ok(storedSubmissions['key_A'], 'key_A harus ada');
    assert.ok(storedSubmissions['key_B'], 'key_B harus ada');
    console.log('✔ 1.2: Dua kunci berbeda tidak saling menghilangkan entri.');

    // Test 1.3: release operasi lama tidak menghapus klaim baru
    const opOld = 'op_old_111';
    const opNew = 'op_new_222';
    await SubmissionCoordinator.acquire('key_race', opOld);
    // Operasi baru mengambil alih
    await SubmissionCoordinator.acquire('key_race', opNew, { force: true });
    // Operasi lama yang terlambat mencoba merilis
    await SubmissionCoordinator.release('key_race', opOld);
    const subAfterLateRelease = await SubmissionCoordinator.getSubmission('key_race');
    assert.ok(subAfterLateRelease, 'Klaim operasi baru tidak boleh dihapus oleh rilis operasi lama');
    assert.equal(subAfterLateRelease.operationId, opNew);
    console.log('✔ 1.3: release operasi lama tidak menghapus klaim baru.');

    // -------------------------------------------------------------------------
    // PRIORITAS 2: Status Uncertain Memblokir Resubmit Otomatis
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 2: Status Uncertain Memblokir Resubmit Otomatis ---');
    chrome.storage.local.data = {};
    
    // Test 2.1: Acquire -> Mark Uncertain -> Acquire kembali ditolak
    const opU = SubmissionCoordinator.generateOperationId();
    await SubmissionCoordinator.acquire('key_uncertain_test', opU);
    await SubmissionCoordinator.markUncertain('key_uncertain_test', opU, 'TIMEOUT');
    // Calling release in finally block does NOT delete uncertain entry
    await SubmissionCoordinator.release('key_uncertain_test', opU);
    
    const reAcquire = await SubmissionCoordinator.acquire('key_uncertain_test');
    assert.equal(reAcquire, false, 'Acquire biasa setelah markUncertain harus ditolak');
    console.log('✔ 2.1: Acquire setelah uncertain ditolak.');

    // Test 2.2: Setelah restart & lebih dari 60 detik, uncertain TETAP memblokir
    const subData = await SubmissionCoordinator.getSubmission('key_uncertain_test');
    subData.startedAt = Date.now() - (120 * 1000); // 120 detik yang lalu
    chrome.storage.local.data.omni_in_flight_submissions['key_uncertain_test'] = subData;
    
    const reAcquireAfter60s = await SubmissionCoordinator.acquire('key_uncertain_test');
    assert.equal(reAcquireAfter60s, false, 'Status uncertain tetap memblokir submit otomatis > 60 detik');
    console.log('✔ 2.2: Setelah lebih dari 60 detik, status uncertain tetap bertahan dan memblokir.');

    // Test 2.3: Kirim ulang eksplisit (force: true) menghasilkan tepat satu operasi baru
    const opForce = SubmissionCoordinator.generateOperationId();
    const forceAcquire = await SubmissionCoordinator.acquire('key_uncertain_test', opForce, { force: true });
    assert.equal(forceAcquire, true, 'Kirim ulang eksplisit dengan force: true harus diizinkan');
    const subAfterForce = await SubmissionCoordinator.getSubmission('key_uncertain_test');
    assert.equal(subAfterForce.operationId, opForce);
    console.log('✔ 2.3: Tindakan kirim ulang eksplisit (force: true) menghasilkan tepat satu operasi baru.');

    // -------------------------------------------------------------------------
    // PRIORITAS 3: Download File Ganda
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 3: Download File Ganda ---');
    chrome.storage.local.data = {};
    mockDownloads.length = 0;

    const jDup = await JobManager.addJob('job-p3-dup', { urls: 'https://example.com/p3.mp4' }, 'P3 Dup', 'token');
    jDup.status = 'saving';
    jDup.files = [
        { fileId: 'job-p3-dup::v.mp4::0', name: 'v.mp4', cleanName: 'v.mp4', url: '/download/job-p3-dup/v.mp4', status: 'pending' }
    ];
    await JobManager.saveJobs({ 'job-p3-dup': jDup });

    // Test 3.1: Dua saveFilesToDisk bersamaan via Promise.all
    await Promise.all([
        JobManager.saveFilesToDisk('job-p3-dup'),
        JobManager.saveFilesToDisk('job-p3-dup')
    ]);
    assert.equal(mockDownloads.length, 1, 'Dua saveFilesToDisk bersamaan harus memicu tepat satu download Chrome');
    console.log('✔ 3.1: Dua saveFilesToDisk bersamaan menghasilkan tepat satu download.');

    // Test 3.2: Respons completed berulang tidak mengunduh ulang hasil
    const beforeCount = mockDownloads.length;
    await JobManager.processJobStatusUpdate('job-p3-dup', {
        status: 'completed',
        running: false,
        files: [{ name: 'v.mp4', url: '/download/job-p3-dup/v.mp4' }]
    });
    assert.equal(mockDownloads.length, beforeCount, 'Respons completed berulang tidak boleh memicu unduhan ganda');
    console.log('✔ 3.2: Respons completed berulang tidak mengunduh ulang hasil.');

    // Test 3.3: Stop selama konversi VTT tidak memulai download
    const jVtt = await JobManager.addJob('job-vtt-stop', { urls: 'https://example.com/vtt.mp4' }, 'VTT Stop', 'token');
    jVtt.status = 'saving';
    jVtt.files = [
        { fileId: 'job-vtt-stop::sub.vtt::0', name: 'sub.vtt', cleanName: 'sub.vtt', url: '/download/job-vtt-stop/sub.vtt', status: 'pending' }
    ];
    await JobManager.saveJobs({ 'job-vtt-stop': jVtt });

    globalThis.fetch = async () => {
        // Simulasi pembatalan terjadi saat fetch VTT berjalan
        await JobManager.stopJob('job-vtt-stop');
        return {
            ok: true,
            text: async () => 'WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello'
        };
    };

    const dlCountBeforeVtt = mockDownloads.length;
    await JobManager.saveFilesToDisk('job-vtt-stop');
    assert.equal(mockDownloads.length, dlCountBeforeVtt, 'Stop selama konversi VTT tidak boleh memulai Chrome download');
    console.log('✔ 3.3: Stop selama konversi VTT tidak memulai download setelah konversi selesai.');

    // -------------------------------------------------------------------------
    // PRIORITAS 4: Stop Gagal Meninggalkan Job Cancelling
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 4: Stop Gagal Meninggalkan Job Cancelling ---');
    chrome.storage.local.data = {};
    
    // Test 4.1: Pemanggilan stop untuk job yang tidak ada mengembalikan error yang benar
    const stopMissing = await JobManager.stopJob('job-non-existent');
    assert.equal(stopMissing.ok, false);
    assert.equal(stopMissing.error, 'Job not found');
    console.log('✔ 4.1: Stop untuk job yang tidak ada mengembalikan error { ok: false, error: "Job not found" }.');

    // Test 4.2: Stop timeout/offline tidak menghentikan pemantauan (tetap cancelling)
    const jStopFail = await JobManager.addJob('job-stop-offline', { urls: 'https://example.com/vid.mp4' }, 'Stop Offline', 'token');
    await JobManager.updateJob('job-stop-offline', { status: 'downloading' });
    
    globalThis.fetch = async () => { throw new Error('Failed to fetch (Server offline)'); };
    const stopResFail = await JobManager.stopJob('job-stop-offline');
    assert.equal(stopResFail.ok, false);
    assert.equal(stopResFail.unconfirmed, true);

    const { jobs: checkJobsAfterStopFail } = await JobManager.getJobs();
    const jsf = checkJobsAfterStopFail['job-stop-offline'];
    assert.equal(jsf.status, 'cancelling', 'Status harus tetap cancelling dan tidak boleh diklaim cancelled');
    assert.ok(jsf.stageLabel.includes('Permintaan stop belum terkonfirmasi'), 'Harus memuat pesan stop belum terkonfirmasi');
    console.log('✔ 4.2: Stop timeout/offline tidak mengklaim cancelled dan status tetap cancelling.');

    // Test 4.3: Respons polling completed saat cancelling tidak memulai download lokal otomatis
    const dlBeforePoll = mockDownloads.length;
    await JobManager.processJobStatusUpdate('job-stop-offline', {
        status: 'completed',
        running: false,
        files: [{ name: 'late.mp4', url: '/download/job-stop-offline/late.mp4' }]
    });
    assert.equal(mockDownloads.length, dlBeforePoll, 'Polling completed saat cancelling tidak boleh memulai unduhan lokal');
    console.log('✔ 4.3: Respons polling completed saat cancelling tidak memulai unduhan lokal.');

    // -------------------------------------------------------------------------
    // PRIORITAS 5: Diagnostik Masih Membocorkan Signed URL
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 5: Diagnostik Masih Membocorkan Signed URL ---');
    
    // Test 5.1: Masking seluruh query param (t, token, session, arbitrary)
    const rawUrl1 = 'https://cdn.test/config.json?t=FAKE_SECRET&pm=browser&auth=abc12345&CustomKey=SecretVal';
    const masked1 = maskSensitive(rawUrl1);
    assert.ok(!masked1.includes('FAKE_SECRET'), 'FAKE_SECRET tidak boleh bocor');
    assert.ok(!masked1.includes('abc12345'), 'auth tidak boleh bocor');
    assert.ok(!masked1.includes('SecretVal'), 'CustomKey tidak boleh bocor');
    assert.ok(masked1.includes('t=[REDACTED]'), 't harus diganti [REDACTED]');
    assert.ok(masked1.includes('CustomKey=[REDACTED]'), 'CustomKey harus diganti [REDACTED]');
    console.log('✔ 5.1: Seluruh parameter query (t, auth, arbitrary, huruf besar/kecil) tersamarkan.');

    // Test 5.2: Token manual dalam pesan error & multiline text
    const manualToken = 'session_custom_SECRET_987654';
    const multilineMsg = `Log output:
Error fetching https://storage.test/chunk.mp4?signature=SIG123
User active session: ${manualToken}
Authorization: Bearer secret_bearer_token`;

    const maskedMulti = maskSensitive(multilineMsg, [manualToken]);
    assert.ok(!maskedMulti.includes('SIG123'), 'signature tidak boleh bocor');
    assert.ok(!maskedMulti.includes('SECRET_987654'), 'manualToken tidak boleh bocor');
    assert.ok(!maskedMulti.includes('secret_bearer_token'), 'Bearer token tidak boleh bocor');
    console.log('✔ 5.2: Token manual dan URL dalam teks multiline tersamarkan.');

    // -------------------------------------------------------------------------
    // PRIORITAS 6: Asumsi Kedaluwarsa 24 Jam Tidak Valid
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 6: Asumsi Kedaluwarsa 24 Jam Dihapus ---');
    chrome.storage.local.data = {};

    const jOld = await JobManager.addJob('job-old-25h', { urls: 'https://example.com/old.mp4' }, 'Old Job', 'token');
    jOld.createdAt = Date.now() - (30 * 60 * 60 * 1000); // 30 jam yang lalu (> 24 jam)
    jOld.files = [
        { fileId: 'job-old-25h::f.mp4::0', name: 'f.mp4', cleanName: 'f.mp4', url: '/download/job-old-25h/f.mp4', status: 'interrupted' }
    ];
    await JobManager.saveJobs({ 'job-old-25h': jOld });

    const retryRes = await JobManager.retryJob('job-old-25h');
    assert.equal(retryRes.ok, true, 'Retry untuk job > 24 jam tidak boleh ditolak jika server files ada');
    assert.equal(retryRes.mode, 'local_files');
    console.log('✔ 6: Asumsi kedaluwarsa 24 jam berhasil dihapus dari ekstensi.');

    // -------------------------------------------------------------------------
    // PRIORITAS 7: Pemulihan Download Mencocokkan URL Secara Tepat
    // -------------------------------------------------------------------------
    console.log('\n--- PRIORITAS 7: Pemulihan Download Mencocokkan URL Secara Tepat ---');
    chrome.storage.local.data = {};
    mockDownloads.length = 0;

    const jRecStrict = await JobManager.addJob('job-rec-strict', { urls: 'https://example.com/rec.mp4' }, 'Rec Strict', 'token');
    jRecStrict.status = 'saving';
    jRecStrict.files = [
        // File A: /download/job-rec-strict/video.mp4
        {
            fileId: 'fA',
            name: 'video.mp4',
            cleanName: 'video.mp4',
            url: '/download/job-rec-strict/video.mp4',
            actualDownloadUrl: 'https://dw.pmlab.id/download/job-rec-strict/video.mp4?session=token',
            targetPath: 'OmniFetch/Rec Strict/video.mp4',
            status: 'pending',
            intent: 'download_initiated',
            intentTime: Date.now(),
            downloadId: null
        }
    ];
    await JobManager.saveJobs({ 'job-rec-strict': jRecStrict });

    // Mock Chrome state memuat download lain dengan URL yang mengandung substring video.mp4 (video.mp4.part)
    mockDownloads.push({
        id: 999,
        url: 'https://dw.pmlab.id/download/job-rec-strict/video.mp4.part?session=token',
        filename: 'OmniFetch/Rec Strict/video.mp4.part',
        state: 'in_progress',
        bytesReceived: 100,
        totalBytes: 500,
        startTime: new Date().toISOString()
    });

    await JobManager.reconcileJobDownloads('job-rec-strict');
    const { jobs: checkRecStrict } = await JobManager.getJobs();
    const fileAAfter = checkRecStrict['job-rec-strict'].files[0];
    
    // File A TIDAK BOLEH mengadopsi ID 999 karena URL / filename berbeda!
    assert.notEqual(fileAAfter.downloadId, 999, 'Substring URL video.mp4 tidak boleh mencocokkan video.mp4.part');
    assert.equal(fileAAfter.status, 'needs_verification', 'Harus ditandai needs_verification jika tidak ada yang cocok tepat');
    console.log('✔ 7.1: Dua download dengan substring URL tidak tertukar.');

    // Test 7.2: Subtitle hasil konversi VTT ke data URL setelah restart
    const jSubRec = await JobManager.addJob('job-sub-rec', { urls: 'https://example.com/s.mp4' }, 'Sub Rec', 'token');
    jSubRec.status = 'saving';
    jSubRec.files = [
        {
            fileId: 'fSub',
            name: 'caption.vtt',
            cleanName: 'caption.srt',
            url: '/download/job-sub-rec/caption.vtt',
            actualDownloadUrl: 'data:text/plain;charset=utf-8,1%0A00%3A00%3A01',
            targetPath: 'OmniFetch/Sub Rec/caption.srt',
            status: 'pending',
            intent: 'download_initiated',
            intentTime: Date.now(),
            downloadId: null
        }
    ];
    await JobManager.saveJobs({ 'job-sub-rec': jSubRec });

    mockDownloads.push({
        id: 888,
        url: 'data:text/plain;charset=utf-8,1%0A00%3A00%3A01',
        filename: 'OmniFetch/Sub Rec/caption.srt',
        state: 'complete',
        bytesReceived: 50,
        totalBytes: 50,
        startTime: new Date().toISOString()
    });

    await JobManager.reconcileJobDownloads('job-sub-rec');
    const { jobs: checkSubRec } = await JobManager.getJobs();
    const fileSubAfter = checkSubRec['job-sub-rec'].files[0];
    assert.equal(fileSubAfter.downloadId, 888, 'Subtitle data URL harus cocok dan terikat ke ID 888');
    assert.equal(fileSubAfter.status, 'completed');
    console.log('✔ 7.2: Subtitle hasil konversi data URL cocok dengan tepat setelah restart.');

    console.log('\n====================================================');
    console.log('🎉 ALL 7 PRIORITY CRITICAL EDGE CASES VERIFIED! 🎉');
    console.log('====================================================');
}

runV212Tests().catch(err => {
    console.error('❌ V2.1.2 TEST FAILED:', err);
    process.exit(1);
});
