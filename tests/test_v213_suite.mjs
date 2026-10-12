import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

let mockDownloads = [];
let nextDownloadId = 1000;
let currentTime = 1700000000000;

globalThis.Date.now = () => currentTime;

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
        onRemoved: { addListener() {} },
        async create(opts) { return { id: 888, ...opts }; }
    },
    downloads: {
        async download(opts) {
            await new Promise(r => setTimeout(r, 5));
            const id = nextDownloadId++;
            mockDownloads.push({
                id,
                ...opts,
                state: 'in_progress',
                bytesReceived: 0,
                totalBytes: 5000,
                startTime: new Date(currentTime).toISOString()
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

let startApiCallCount = 0;
globalThis.fetch = async (url, opts) => {
    if (url.includes('/api/start')) {
        startApiCallCount++;
        return {
            ok: true,
            status: 200,
            headers: new Map([['content-type', 'application/json']]),
            async json() { return { job_id: 'job_api_' + startApiCallCount, status: 'queued' }; }
        };
    }
    if (url.includes('/subtitle.vtt')) {
        return {
            ok: true,
            status: 200,
            async text() {
                return `WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nHello World\n`;
            }
        };
    }
    throw new Error(`Offline mock network: ${url}`);
};

const { SubmissionCoordinator, maskSensitive } = await import(path.join(extDir, 'background/main.js'));
const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));
const { BASE_URL } = await import(path.join(extDir, 'background/config.js'));

async function resetEnvironment() {
    chrome.storage.local.data = {};
    mockDownloads = [];
    startApiCallCount = 0;
    currentTime = 1700000000000;
}

async function runAllTests() {
    console.log('====================================================');
    console.log('   OMNIFETCH COMPANION v2.1.3 REGRESSION TEST SUITE');
    console.log('====================================================\n');

    // =========================================================================
    // GRUP A: RETRY FLOW INTEGRITY
    // =========================================================================
    console.log('--- TEST A1: Satu file interrupted -> retry -> tepat 1 download baru, 0 /api/start, completed tidak diulang ---');
    await resetEnvironment();

    const jobIdA1 = 'job_a1';
    await JobManager.saveJobs({
        [jobIdA1]: {
            id: jobIdA1,
            title: 'Sample Video',
            status: 'saving',
            downloadStatus: 'failed',
            serverProcessed: true,
            files: [
                {
                    fileId: `${jobIdA1}::f1::0`,
                    name: 'video.mp4',
                    cleanName: 'video.mp4',
                    targetPath: 'OmniFetch/Sample Video/video.mp4',
                    url: '/download/job_a1/video.mp4',
                    status: 'completed',
                    downloadId: 501
                },
                {
                    fileId: `${jobIdA1}::f2::1`,
                    name: 'audio.m4a',
                    cleanName: 'audio.m4a',
                    targetPath: 'OmniFetch/Sample Video/audio.m4a',
                    url: '/download/job_a1/audio.m4a',
                    status: 'interrupted',
                    downloadId: null,
                    error: 'Network interrupted'
                }
            ]
        }
    });

    const dlBefore = mockDownloads.length;
    const retryResA1 = await JobManager.retryJob(jobIdA1);

    assert.equal(retryResA1.ok, true, 'Retry harus mengembalikan ok: true');
    assert.equal(retryResA1.mode, 'local_files', 'Mode harus local_files');
    assert.equal(retryResA1.status, 'started', 'Status harus started');
    assert.equal(retryResA1.started, 1, 'Tepat 1 file harus dimulai unduhannya');
    assert.equal(mockDownloads.length - dlBefore, 1, 'Tepat 1 unduhan Chrome baru harus tercatat');
    assert.equal(startApiCallCount, 0, 'Nol panggilan ke /api/start');

    const { jobs: jobsA1 } = await JobManager.getJobs();
    const f1 = jobsA1[jobIdA1].files[0];
    const f2 = jobsA1[jobIdA1].files[1];
    assert.equal(f1.status, 'completed', 'File completed tidak boleh diunduh ulang atau statusnya berubah');
    assert.equal(f1.downloadId, 501, 'Download ID file completed harus tetap 501');
    assert.equal(f2.status, 'downloading', 'File interrupted harus menjadi downloading');
    assert.ok(f2.downloadId > 0, 'File interrupted harus mendapatkan downloadId baru');
    console.log('✔ A1 Passed: Tepat 1 unduhan baru, file completed terlindungi, 0 POST /api/start');

    console.log('\n--- TEST A2: Retry ketika transfer sudah berjalan -> status: already_running ---');
    const retryResA2 = await JobManager.retryJob(jobIdA1);
    assert.equal(retryResA2.ok, true);
    assert.equal(retryResA2.status, 'already_running', 'Harus membedakan transfer yang sudah berjalan');
    assert.equal(retryResA2.started, 0, 'Tidak boleh memulai transfer baru');
    console.log('✔ A2 Passed: Terdeteksi already_running secara akurat tanpa membuat download ganda');

    console.log('\n--- TEST A3: Retry ketika semua file sudah selesai -> status: no_files_to_retry ---');
    await JobManager.withLock(async () => {
        const { jobs } = await JobManager.getJobs();
        jobs[jobIdA1].files[1].status = 'completed';
        await JobManager.saveJobs(jobs);
    });
    const retryResA3 = await JobManager.retryJob(jobIdA1);
    assert.equal(retryResA3.ok, true);
    assert.equal(retryResA3.status, 'no_files_to_retry');
    assert.equal(retryResA3.started, 0);
    console.log('✔ A3 Passed: Menyatakan no_files_to_retry dengan jujur');

    // =========================================================================
    // GRUP B: EXACT RECONCILIATION MATCHING
    // =========================================================================
    console.log('\n--- TEST B1: Data URL berbeda tetapi nama sama -> Ditolak (needs_verification) ---');
    await resetEnvironment();
    const jobIdB = 'job_b';
    await JobManager.saveJobs({
        [jobIdB]: {
            id: jobIdB,
            title: 'Series S01E01',
            status: 'saving',
            downloadStatus: 'downloading',
            files: [
                {
                    fileId: `${jobIdB}::sub::0`,
                    name: 'subtitle.srt',
                    cleanName: 'subtitle.srt',
                    targetPath: 'OmniFetch/Series S01E01/subtitle.srt',
                    actualDownloadUrl: 'data:text/plain;charset=utf-8,CORRECT_SUBTITLE_DATA',
                    status: 'starting',
                    intent: 'download_initiated',
                    intentTime: currentTime,
                    downloadId: null
                }
            ]
        }
    });

    mockDownloads = [
        {
            id: 2001,
            url: 'data:text/plain;charset=utf-8,WRONG_DIFFERENT_SUBTITLE',
            filename: '/Users/test/Downloads/OmniFetch/Series S01E01/subtitle.srt',
            state: 'complete',
            bytesReceived: 200,
            totalBytes: 200,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB1 } = await JobManager.getJobs();
    assert.equal(jobsB1[jobIdB].files[0].status, 'needs_verification', 'File data URL beda tidak boleh diadopsi');
    assert.equal(jobsB1[jobIdB].files[0].downloadId, null, 'Download ID tidak boleh diisi file yang salah');
    console.log('✔ B1 Passed: Data URL berbeda ditolak');

    console.log('\n--- TEST B2: HTTP URL sama path tetapi query berbeda -> Ditolak (needs_verification) ---');
    await JobManager.withLock(async () => {
        const { jobs } = await JobManager.getJobs();
        jobs[jobIdB].files[0].actualDownloadUrl = 'https://dw.pmlab.id/download/f.mp4?token=valid_123';
        jobs[jobIdB].files[0].cleanName = 'f.mp4';
        jobs[jobIdB].files[0].targetPath = 'OmniFetch/Series S01E01/f.mp4';
        jobs[jobIdB].files[0].status = 'starting';
        jobs[jobIdB].files[0].intent = 'download_initiated';
        jobs[jobIdB].files[0].downloadId = null;
        await JobManager.saveJobs(jobs);
    });

    mockDownloads = [
        {
            id: 2002,
            url: 'https://dw.pmlab.id/download/f.mp4?token=other_token_999',
            filename: '/Users/test/Downloads/OmniFetch/Series S01E01/f.mp4',
            state: 'complete',
            bytesReceived: 5000,
            totalBytes: 5000,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB2 } = await JobManager.getJobs();
    assert.equal(jobsB2[jobIdB].files[0].status, 'needs_verification');
    assert.equal(jobsB2[jobIdB].files[0].downloadId, null);
    console.log('✔ B2 Passed: HTTP URL dengan query beda ditolak');

    console.log('\n--- TEST B3: Filename sama di folder berbeda -> Ditolak (needs_verification) ---');
    mockDownloads = [
        {
            id: 2003,
            url: 'https://dw.pmlab.id/download/f.mp4?token=valid_123',
            filename: '/Users/test/Downloads/SOME_OTHER_FOLDER/f.mp4',
            state: 'complete',
            bytesReceived: 5000,
            totalBytes: 5000,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB3 } = await JobManager.getJobs();
    assert.equal(jobsB3[jobIdB].files[0].status, 'needs_verification');
    assert.equal(jobsB3[jobIdB].files[0].downloadId, null);
    console.log('✔ B3 Passed: Folder berbeda ditolak');

    console.log('\n--- TEST B4: Satu kandidat benar -> Diadopsi dengan sukses ---');
    mockDownloads = [
        {
            id: 2004,
            url: 'https://dw.pmlab.id/download/f.mp4?token=valid_123',
            filename: '/Users/test/Downloads/OmniFetch/Series S01E01/f.mp4',
            state: 'complete',
            bytesReceived: 5000,
            totalBytes: 5000,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB4 } = await JobManager.getJobs();
    assert.equal(jobsB4[jobIdB].files[0].status, 'completed');
    assert.equal(jobsB4[jobIdB].files[0].downloadId, 2004);
    assert.equal(jobsB4[jobIdB].files[0].bytesReceived, 5000);
    console.log('✔ B4 Passed: Kandidat benar diadopsi secara akurat');

    console.log('\n--- TEST B5: Uniquify dari Chrome di folder yang sama -> Diadopsi dengan sukses ---');
    await JobManager.withLock(async () => {
        const { jobs } = await JobManager.getJobs();
        jobs[jobIdB].files[0].status = 'starting';
        jobs[jobIdB].files[0].intent = 'download_initiated';
        jobs[jobIdB].files[0].downloadId = null;
        await JobManager.saveJobs(jobs);
    });

    mockDownloads = [
        {
            id: 2005,
            url: 'https://dw.pmlab.id/download/f.mp4?token=valid_123',
            filename: '/Users/test/Downloads/OmniFetch/Series S01E01/f (1).mp4',
            state: 'complete',
            bytesReceived: 5000,
            totalBytes: 5000,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB5 } = await JobManager.getJobs();
    assert.equal(jobsB5[jobIdB].files[0].status, 'completed');
    assert.equal(jobsB5[jobIdB].files[0].downloadId, 2005);
    console.log('✔ B5 Passed: Uniquify Chrome dalam folder yang sama diadopsi');

    console.log('\n--- TEST B6: Subtitle VTT yang dikonversi sebelum ID tersimpan -> Teradopsi akurat ---');
    const srtDataUrl = 'data:text/plain;charset=utf-8,1%0A00%3A00%3A01%2C000%20--%3E%2000%3A00%3A04%2C000%0AHello%20World%0A';
    await JobManager.withLock(async () => {
        const { jobs } = await JobManager.getJobs();
        jobs[jobIdB].files[0].actualDownloadUrl = srtDataUrl;
        jobs[jobIdB].files[0].cleanName = 'subtitle.srt';
        jobs[jobIdB].files[0].targetPath = 'OmniFetch/Series S01E01/subtitle.srt';
        jobs[jobIdB].files[0].status = 'starting';
        jobs[jobIdB].files[0].intent = 'download_initiated';
        jobs[jobIdB].files[0].downloadId = null;
        await JobManager.saveJobs(jobs);
    });

    mockDownloads = [
        {
            id: 2006,
            url: srtDataUrl,
            filename: '/Users/test/Downloads/OmniFetch/Series S01E01/subtitle.srt',
            state: 'complete',
            bytesReceived: 100,
            totalBytes: 100,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobIdB);
    const { jobs: jobsB6 } = await JobManager.getJobs();
    assert.equal(jobsB6[jobIdB].files[0].status, 'completed');
    assert.equal(jobsB6[jobIdB].files[0].downloadId, 2006);
    console.log('✔ B6 Passed: Subtitle terkonversi diadopsi dengan tepat');

    // =========================================================================
    // GRUP C: SUBMISSION PERSISTENCE & UNCERTAINTY
    // =========================================================================
    console.log('\n--- TEST C1: Submitting terputus > 120s ditransisikan menjadi uncertain & blokir resubmit otomatis ---');
    await resetEnvironment();

    const keyC = 'https://example.com/video_c::video';
    const op1 = 'op_first_run';
    const acq1 = await SubmissionCoordinator.acquire(keyC, op1);
    assert.equal(acq1, true, 'Klaim pertama harus berhasil');

    // Majukan waktu 130 detik
    currentTime += 130000;

    // Instance / worker restart mencoba pengiriman ulang otomatis tanpa force
    const op2 = 'op_auto_restart';
    const acq2 = await SubmissionCoordinator.acquire(keyC, op2, { force: false });
    assert.equal(acq2, false, 'Pengiriman ulang otomatis setelah 130 detik HARUS DIBLOKIR');

    const subC = await SubmissionCoordinator.getSubmission(keyC);
    assert.equal(subC.status, 'uncertain', 'Status di storage harus berubah menjadi uncertain');
    console.log('✔ C1 Passed: Status submitting lama berubah menjadi uncertain, submit otomatis diblokir');

    console.log('\n--- TEST C2: Pengiriman paksa eksplisit (force: true) diizinkan ---');
    const acqForce = await SubmissionCoordinator.acquire(keyC, 'op_force_user', { force: true });
    assert.equal(acqForce, true, 'Pengguna dengan force: true diizinkan melanjutkan');
    console.log('✔ C2 Passed: Force resubmit berhasil dengan konfirmasi pengguna');

    console.log('\n--- TEST C3: Release hanya memengaruhi operationId miliknya ---');
    // Jika op lama memanggil release, tidak boleh menghapus klaim milik op_force_user
    await SubmissionCoordinator.release(keyC, op1);
    const subAfterOldRelease = await SubmissionCoordinator.getSubmission(keyC);
    assert.ok(subAfterOldRelease !== null, 'Klaim tidak boleh terhapus oleh operationId lama');
    assert.equal(subAfterOldRelease.operationId, 'op_force_user', 'Klaim harus tetap milik operationId aktif');

    await SubmissionCoordinator.release(keyC, 'op_force_user');
    const subAfterCorrectRelease = await SubmissionCoordinator.getSubmission(keyC);
    assert.equal(subAfterCorrectRelease, null, 'Klaim terhapus setelah operationId yang sesuai merilis');
    console.log('✔ C3 Passed: Isolasi antar operationId terbukti aman');

    // =========================================================================
    // GRUP D: CONCURRENCY & INTEGRITY AUDIT
    // =========================================================================
    console.log('\n--- TEST D1: Dua klik Download concurrent -> Tepat 1 acquire berhasil ---');
    await resetEnvironment();
    const [c1, c2] = await Promise.all([
        SubmissionCoordinator.acquire('concurrent_key', 'op_c1'),
        SubmissionCoordinator.acquire('concurrent_key', 'op_c2')
    ]);
    assert.equal((c1 && !c2) || (!c1 && c2), true, 'Tepat satu dari dua panggilan concurrent boleh berhasil');
    console.log('✔ D1 Passed: Mutex mencegah race condition download concurrent');

    console.log('\n--- TEST D2: Diagnostik menyamarkan URL query dan token sensitif ---');
    const rawDiag = {
        token: 'secret_session_token_12345',
        url: 'https://example.com/video?auth=secret_token&user=john',
        sessionToken: 'xyz_secret'
    };
    const masked = maskSensitive(rawDiag);
    assert.ok(!JSON.stringify(masked).includes('secret_session_token_12345'), 'Token rahasia harus disamarkan');
    assert.ok(!JSON.stringify(masked).includes('secret_token'), 'Query parameter URL harus disamarkan');
    console.log('✔ D2 Passed: Diagnostik aman dari kebocoran token dan parameter URL');

    console.log('\n====================================================');
    console.log('   SEMUA TES v2.1.3 LULUS DENGAN SUKSES (100%)');
    console.log('====================================================\n');
}

runAllTests().catch(err => {
    console.error('❌ REGRESSION TEST FAILED:', err);
    process.exit(1);
});
