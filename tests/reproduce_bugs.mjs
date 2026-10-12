import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Setup Chrome mock with realistic asynchronous microtask delay
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
            await new Promise(r => setTimeout(r, 10));
            const id = this.nextId++;
            mockDownloads.push({ id, ...opts, state: 'in_progress', bytesReceived: 0, totalBytes: 1000 });
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

globalThis.fetch = async (url) => {
    throw new Error(`Offline mock: ${url}`);
};

async function testReproductions() {
    const { SubmissionCoordinator, maskSensitive } = await import(path.join(extDir, 'background/main.js'));
    const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));

    console.log('--- REPRO 1: Concurrent acquire ---');
    chrome.storage.local.data = {};
    const [res1, res2] = await Promise.all([
        SubmissionCoordinator.acquire('key1'),
        SubmissionCoordinator.acquire('key1')
    ]);
    console.log('Result acquire concurrent:', [res1, res2]);
    if (res1 === true && res2 === true) {
        console.log('❌ BUG 1 REPRODUCED: Both concurrent acquires succeeded!');
    }

    console.log('\n--- REPRO 2: Uncertain permits resubmission ---');
    chrome.storage.local.data = {};
    const acq1 = await SubmissionCoordinator.acquire('key2');
    await SubmissionCoordinator.markUncertain('key2', 'TIMEOUT');
    const acq2 = await SubmissionCoordinator.acquire('key2');
    console.log('Result acquire after uncertain:', acq2);
    if (acq2 === true) {
        console.log('❌ BUG 2 REPRODUCED: acquire succeeded after markUncertain!');
    }

    console.log('\n--- REPRO 3: Concurrent saveFilesToDisk duplicate download ---');
    chrome.storage.local.data = {};
    mockDownloads.length = 0;
    const testJob = await JobManager.addJob('job-dup', { urls: 'https://example.com/v.mp4' }, 'Title', 'token');
    testJob.status = 'saving';
    testJob.files = [
        { name: 'v.mp4', url: '/download/job-dup/v.mp4', status: 'pending' }
    ];
    await JobManager.saveJobs({ 'job-dup': testJob });

    await Promise.all([
        JobManager.saveFilesToDisk('job-dup'),
        JobManager.saveFilesToDisk('job-dup')
    ]);
    console.log('Mock downloads count:', mockDownloads.length);
    if (mockDownloads.length > 1) {
        console.log('❌ BUG 3 REPRODUCED: Multiple downloads triggered concurrently!');
    }

    console.log('\n--- REPRO 5: maskSensitive leaks signed URL parameter ---');
    const masked = maskSensitive('https://cdn.test/config.json?t=FAKE_SECRET&pm=browser');
    console.log('Masked URL result:', masked);
    if (masked.includes('FAKE_SECRET')) {
        console.log('❌ BUG 5 REPRODUCED: maskSensitive leaked FAKE_SECRET!');
    }

    console.log('\n--- REPRO 6: 24h expiration assumption in retryJob ---');
    const oldJob = await JobManager.addJob('job-old', { urls: 'https://example.com/old.mp4' }, 'Old', 'token');
    oldJob.createdAt = Date.now() - (25 * 60 * 60 * 1000); // 25 hours ago
    oldJob.files = [
        { name: 'old.mp4', url: '/download/job-old/old.mp4', status: 'interrupted' }
    ];
    await JobManager.saveJobs({ 'job-old': oldJob });
    const retryOldRes = await JobManager.retryJob('job-old');
    console.log('Retry old result:', retryOldRes);
    if (retryOldRes.expired === true) {
        console.log('❌ BUG 6 REPRODUCED: retryJob rejected solely due to 24-hour age assumption!');
    }
}

testReproductions().catch(console.error);
