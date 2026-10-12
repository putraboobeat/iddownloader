import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Setup Chrome mock dengan structured clone
let mockDownloads = [];
let nextDownloadId = 100;
let currentTime = 1000000000;

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
        async create(opts) { return { id: 999, ...opts }; }
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
                totalBytes: 1000,
                startTime: new Date(currentTime).toISOString()
            });
            return id;
        },
        async search(query) {
            if (query && query.id) return mockDownloads.filter(d => d.id === query.id);
            return mockDownloads;
        },
        async cancel(id) {},
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
            async json() { return { job_id: 'new_job_123', status: 'queued' }; }
        };
    }
    throw new Error(`Offline mock: ${url}`);
};

const { SubmissionCoordinator } = await import(path.join(extDir, 'background/main.js'));
const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));

async function testReproBugA() {
    console.log('\n--- TEST REPRO A: Retry Mengaku Berhasil Tetapi Tidak Mengunduh ---');
    chrome.storage.local.data = {};
    mockDownloads = [];
    startApiCallCount = 0;

    const jobId = 'job_retry_test_1';
    const jobData = {
        id: jobId,
        title: 'Video Test',
        status: 'saving',
        downloadStatus: 'failed',
        serverProcessed: true,
        files: [
            {
                fileId: `${jobId}::f1::0`,
                name: 'video.mp4',
                cleanName: 'video.mp4',
                targetPath: 'OmniFetch/Video Test/video.mp4',
                url: '/download/job_retry_test_1/video.mp4',
                status: 'completed',
                downloadId: 50
            },
            {
                fileId: `${jobId}::f2::1`,
                name: 'audio.m4a',
                cleanName: 'audio.m4a',
                targetPath: 'OmniFetch/Video Test/audio.m4a',
                url: '/download/job_retry_test_1/audio.m4a',
                status: 'interrupted',
                downloadId: null,
                error: 'Download interrupted'
            }
        ]
    };

    await JobManager.saveJobs({ [jobId]: jobData });

    const downloadCountBefore = mockDownloads.length;
    console.log(`Downloads sebelum retry: ${downloadCountBefore}`);

    const retryResult = await JobManager.retryJob(jobId);
    console.log('retryJob result:', retryResult);

    const downloadCountAfter = mockDownloads.length;
    const newDownloads = downloadCountAfter - downloadCountBefore;
    console.log(`Downloads baru setelah retry: ${newDownloads}, Panggilan /api/start: ${startApiCallCount}`);

    return {
        retryResult,
        newDownloads,
        startApiCallCount
    };
}

async function testReproBugB() {
    console.log('\n--- TEST REPRO B: Pemulihan Salah Mengadopsi File ---');
    chrome.storage.local.data = {};
    mockDownloads = [];

    const jobId = 'job_reconcile_test';
    // Skenario 1: Data URL berbeda tetapi nama sama
    const jobData = {
        id: jobId,
        title: 'My Video',
        status: 'saving',
        downloadStatus: 'downloading',
        files: [
            {
                fileId: `${jobId}::sub::0`,
                name: 'a.srt',
                cleanName: 'a.srt',
                targetPath: 'OmniFetch/My Video/a.srt',
                actualDownloadUrl: 'data:text/plain,EXPECTED_CONTENT',
                status: 'starting',
                intent: 'download_initiated',
                intentTime: currentTime,
                downloadId: null
            }
        ]
    };

    await JobManager.saveJobs({ [jobId]: jobData });

    // Mock chrome downloads memiliki URL data: dengan konten BERBEDA, dan folder /Downloads/OTHER/
    mockDownloads = [
        {
            id: 201,
            url: 'data:text/plain,WRONG_CONTENT',
            filename: '/Users/test/Downloads/OTHER/a.srt',
            state: 'complete',
            bytesReceived: 500,
            totalBytes: 500,
            startTime: new Date(currentTime).toISOString()
        }
    ];

    await JobManager.reconcileJobDownloads(jobId);

    const { jobs } = await JobManager.getJobs();
    const checkedFile = jobs[jobId].files[0];
    console.log('Hasil pencocokan file data URL berbeda:', {
        status: checkedFile.status,
        downloadId: checkedFile.downloadId,
        error: checkedFile.error
    });

    return checkedFile;
}

async function testReproBugC() {
    console.log('\n--- TEST REPRO C: Submitting Terputus > 120s Dihapus & Memungkinkan Submit Ganda ---');
    chrome.storage.local.data = {};

    const key = 'https://example.com/watch?v=123::video';
    const opId1 = 'op_first';

    // 1. Worker pertama mengakuisisi
    const acq1 = await SubmissionCoordinator.acquire(key, opId1);
    console.log('Acquire 1 (awal):', acq1);

    // 2. Simulasi worker terputus (crash / sleep) dan waktu berlalu 125 detik
    currentTime += 125000;

    // 3. Worker baru (atau request otomatis baru) mencoba acquire tanpa force
    const opId2 = 'op_second';
    const acq2 = await SubmissionCoordinator.acquire(key, opId2, { force: false });
    console.log('Acquire 2 (setelah 125 detik tanpa force):', acq2);

    const sub = await SubmissionCoordinator.getSubmission(key);
    console.log('Submission state di storage:', sub);

    return { acq1, acq2, sub };
}

async function run() {
    console.log('=== MEMERIKSA PERILAKU KODE SAAT INI (v2.1.2) ===');
    
    const resA = await testReproBugA();
    if (resA.retryResult.ok === true && resA.newDownloads === 0) {
        console.log('🔴 BUG A TERKONFIRMASI: retry mengaku ok:true tetapi download terpanggil 0 kali!');
    } else {
        console.log('🟢 BUG A TIDAK TERJADI (download terpanggil:', resA.newDownloads, ')');
    }

    const resB = await testReproBugB();
    if (resB.downloadId === 201) {
        console.log('🔴 BUG B TERKONFIRMASI: Ekstensi mengadopsi ID 201 (data URL salah & folder salah)!');
    } else {
        console.log('🟢 BUG B TIDAK TERJADI (status:', resB.status, ', downloadId:', resB.downloadId, ')');
    }

    const resC = await testReproBugC();
    if (resC.acq2 === true) {
        console.log('🔴 BUG C TERKONFIRMASI: Pengiriman ulang otomatis berhasil setelah 120s terlampaui!');
    } else {
        console.log('🟢 BUG C TIDAK TERJADI (acquire 2 ditolak:', resC.acq2, ')');
    }
}

run().catch(console.error);
