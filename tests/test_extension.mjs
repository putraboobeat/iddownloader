import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Mock Chrome APIs before importing extension modules
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
        }
    },
    downloads: {
        async download(options) {
            const id = mockDownloads.length + 101;
            mockDownloads.push({ id, ...options, state: 'in_progress' });
            return id;
        },
        async search(query) {
            if (query.id) {
                return mockDownloads.filter(d => d.id === query.id);
            }
            return mockDownloads;
        },
        async cancel(id) {
            const item = mockDownloads.find(d => d.id === id);
            if (item) item.state = 'interrupted';
        }
    },
    alarms: {
        create() {},
        onAlarm: { addListener() {} }
    }
};

async function runTests() {
    console.log('=== TEST 1: Manifest Verification ===');
    const manifest = JSON.parse(fs.readFileSync(path.join(extDir, 'manifest.json'), 'utf8'));
    assert.equal(manifest.version, '2.1.4', 'Manifest version should be updated to 2.1.4');
    assert.ok(manifest.permissions.includes('downloads'), 'permissions must include downloads API');
    assert.ok(manifest.host_permissions.includes('https://dw.pmlab.id/*'), 'host_permissions must contain https://dw.pmlab.id/*');
    assert.ok(manifest.host_permissions.includes('*://*/*'), 'host_permissions must contain *://*/* for streaming media sniffer');
    assert.ok(!manifest.host_permissions.includes('http://127.0.0.1/*'), 'localhost should be removed from mandatory host_permissions');
    console.log('✔ Manifest version 2.1.4, downloads permission, and streaming host permissions verified.');

    console.log('\n=== TEST 2: Config, BASE_URL & Auto-Session Token Generation ===');
    const { BASE_URL, Config } = await import(path.join(extDir, 'background/config.js'));
    assert.equal(BASE_URL, 'https://dw.pmlab.id', 'BASE_URL must be https://dw.pmlab.id');
    
    // Simulate legacy storage with localhost address
    chrome.storage.local.data = { app: 'http://localhost:6666', token: 'legacytoken123' };
    const appInfo = await Config.getAppInfo();
    assert.equal(appInfo.url, 'https://dw.pmlab.id', 'Config must return BASE_URL even if storage has legacy server');
    assert.equal(chrome.storage.local.data.app, 'https://dw.pmlab.id', 'Legacy storage should be migrated to BASE_URL');
    assert.equal(appInfo.token, 'legacytoken123', 'Valid token must be preserved');

    // Test session token auto-generation
    chrome.storage.local.data = { app: 'https://dw.pmlab.id', token: '' };
    const generatedToken = await Config.ensureSessionToken();
    assert.ok(typeof generatedToken === 'string' && generatedToken.startsWith('omni'), 'Auto-generated token should start with omni');
    assert.ok(/^[a-zA-Z0-9]+$/.test(generatedToken), 'Token must be alphanumeric');
    assert.equal(chrome.storage.local.data.token, generatedToken, 'Generated token must be saved to storage');
    console.log('✔ Config migration and auto-session token generation verified.');

    console.log('\n=== TEST 3: Detector - Media vs Non-Media Classification & Smart Ranking ===');
    const { Detector } = await import(path.join(extDir, 'background/detector.js'));

    // Standard media
    const hls = Detector.initialClassify('https://cdn.example.com/playlist.m3u8?token=sig123', '');
    assert.equal(hls.kind, 'hls');
    assert.equal(hls.isMedia, true);

    const dash = Detector.initialClassify('https://cdn.example.com/manifest.mpd', '');
    assert.equal(dash.kind, 'dash');
    assert.equal(dash.isMedia, true);

    const video = Detector.initialClassify('https://cdn.example.com/video.mp4', '');
    assert.equal(video.kind, 'video');
    assert.equal(video.isMedia, true);

    const audio = Detector.initialClassify('https://cdn.example.com/audio.mp3', '');
    assert.equal(audio.kind, 'audio');
    assert.equal(audio.isMedia, true);

    // Subtitles
    const idlixSub = Detector.initialClassify('https://e2e.majorplay.net/v/z2/u2p5MnQ-2kac/i18n/id/4dc7d6e7f652f636.vtt?t=sig', 'text/vtt');
    assert.equal(idlixSub.kind, 'subtitle');
    assert.equal(idlixSub.isMedia, true);
    assert.equal(idlixSub.statusLabel, 'Subtitle Indonesia');

    // config-*.json di Idlix / Majorplay adalah playlist player config yang valid
    const idlixConfig = Detector.initialClassify('https://e2e.majorplay.net/v/z2/u2p5MnQ-2kac/config-456381.json?t=sig', 'application/json');
    assert.equal(idlixConfig.kind, 'hls');
    assert.equal(idlixConfig.isMedia, true);
    assert.equal(idlixConfig.statusLabel, 'Playlist Config (Idlix)');

    // Generic JSON tetap unverified
    const genericJson = Detector.initialClassify('https://example.com/api/data.json', 'application/json');
    assert.equal(genericJson.kind, 'unverified');
    assert.equal(genericJson.isMedia, false);

    // Segment filtering
    const segment = Detector.initialClassify('https://cdn.example.com/seg1.ts', '');
    assert.equal(segment.kind, 'segment');
    assert.equal(segment.isMedia, false);

    // Ads and telemetry filtering
    const adCandidate = Detector.initialClassify('https://doubleclick.net/pagead/ads?client=ca-pub-12345', '');
    assert.equal(adCandidate.kind, 'non_media');
    assert.equal(adCandidate.isMedia, false);

    // Smart source selection (findBestMedia)
    const candidatesList = [
        { url: 'https://ad.example.com/ad.mp4', kind: 'video', isMedia: true, time: 200 },
        { url: 'https://cdn.example.com/master.m3u8', kind: 'hls', statusLabel: 'Playlist HLS (Master)', isMedia: true, time: 100 },
        { url: 'https://cdn.example.com/audio.mp3', kind: 'audio', isMedia: true, time: 50 }
    ];
    const bestMedia = Detector.findBestMedia(candidatesList);
    assert.equal(bestMedia.url, 'https://cdn.example.com/master.m3u8', 'findBestMedia should prioritize HLS Master over single formats');

    // Subtitle auto-selection (findDefaultSubtitle)
    const subsList = [
        { url: 'https://cdn.example.com/sub_en.vtt', kind: 'subtitle', statusLabel: 'Subtitle Inggris' },
        { url: 'https://cdn.example.com/sub_id.vtt', kind: 'subtitle', statusLabel: 'Subtitle Indonesia' }
    ];
    const defaultSub = Detector.findDefaultSubtitle(subsList);
    assert.equal(defaultSub.statusLabel, 'Subtitle Indonesia', 'findDefaultSubtitle must auto-select Indonesian subtitle');
    console.log('✔ Media classification, unverified config-*.json, ads filtering, and ranking verified.');

    console.log('\n=== TEST 4: Detector - Content Verification Heuristics ===');
    const candHlsInJson = {
        url: 'https://example.com/stream.json',
        kind: 'unverified',
        statusLabel: 'Belum diverifikasi',
        isMedia: false,
        verified: false
    };

    // Subtest: #EXTM3U detection
    const hlsSnippet = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\nchunklist.m3u8';
    const verifiedHls = Detector.verifyCandidateContent({ ...candHlsInJson }, hlsSnippet);
    assert.equal(verifiedHls.kind, 'hls');
    assert.equal(verifiedHls.statusLabel, 'Playlist HLS (Master)');
    assert.equal(verifiedHls.isMedia, true);

    // Subtest: DASH manifest inside XML
    const dashSnippet = '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"></MPD>';
    const verifiedDash = Detector.verifyCandidateContent({ ...candHlsInJson }, dashSnippet);
    assert.equal(verifiedDash.kind, 'dash');
    assert.equal(verifiedDash.statusLabel, 'Manifest DASH');
    assert.equal(verifiedDash.isMedia, true);

    // Subtest: Player config JSON containing media sources
    const configSnippet = JSON.stringify({
        title: 'Sample Movie',
        sources: [{ file: 'https://stream.example.com/master.m3u8?token=signed999', type: 'hls' }]
    });
    const verifiedConfig = Detector.verifyCandidateContent({ ...candHlsInJson }, configSnippet);
    assert.equal(verifiedConfig.kind, 'hls');
    assert.equal(verifiedConfig.isMedia, true);
    assert.equal(verifiedConfig.extractedMediaUrl, 'https://stream.example.com/master.m3u8?token=signed999');

    // Subtest: JSON without media should NOT be treated as playlist
    const nonStreamConfig = JSON.stringify({ user: 'guest', profile: 'https://example.com/avatar.jpg' });
    const nonStreamVerified = Detector.verifyCandidateContent({ ...candHlsInJson }, nonStreamConfig);
    assert.equal(nonStreamVerified.isMedia, false, 'Non-stream JSON should not be considered media');

    // Subtest: Signed URL preservation
    chrome.storage.session.data = {};
    const tabId = 123;
    const signedUrl = 'https://cdn.example.com/video.m3u8?token=secret123&expires=9999999&sig=abcd';
    await Detector.addCandidate(tabId, signedUrl, 'application/x-mpegurl');
    const storedItems = await Detector.getCandidates(tabId);
    assert.equal(storedItems.length, 1);
    assert.equal(storedItems[0].url, signedUrl, 'Query parameters and signature must be fully preserved');
    console.log('✔ Content verification heuristics and signed URL preservation verified.');

    console.log('\n=== TEST 5: Subtitle Conversion (VTT to SRT) ===');
    const { convertVttToSrt } = await import(path.join(extDir, 'utils/subtitle.js'));
    const sampleVtt = `WEBVTT - Sample File

1
00:00:01.250 --> 00:00:04.500
Halo dunia! <c.yellow>Subtitle Indonesia</c>

2
00:00:05.100 --> 00:00:08.300
Baris kedua dengan <b>tebal</b>.`;

    const convertedSrt = convertVttToSrt(sampleVtt);
    assert.ok(convertedSrt.includes('00:00:01,250 --> 00:00:04,500'), 'SRT must use comma format for milliseconds');
    assert.ok(convertedSrt.includes('Halo dunia! Subtitle Indonesia'), 'VTT tags should be stripped cleanly');
    assert.ok(convertedSrt.includes('Baris kedua dengan tebal.'), 'HTML tags should be stripped');
    console.log('✔ Pure JavaScript VTT to SRT converter verified.');

    console.log('\n=== TEST 6: APIClient Error Differentiation & Endpoints ===');
    const { APIClient } = await import(path.join(extDir, 'background/api.js'));

    // Test missing auth token
    chrome.storage.local.data = { app: 'https://dw.pmlab.id', token: '' };
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'AUTH_MISSING' && err.message.includes('Sesi belum terhubung')
    );

    // Test invalid media
    chrome.storage.local.data = { app: 'https://dw.pmlab.id', token: 'validtoken123' };
    await assert.rejects(
        () => APIClient.sendJob({ urls: '' }),
        err => err.code === 'INVALID_MEDIA'
    );

    // Mock original fetch
    const originalFetch = globalThis.fetch;

    // Test HTTP 401
    globalThis.fetch = async () => ({
        ok: false,
        status: 401,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ error: 'Unauthorized' })
    });
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'AUTH_EXPIRED'
    );

    // Test HTTP 404
    globalThis.fetch = async () => ({
        ok: false,
        status: 404,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ error: 'Not found' })
    });
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'INCOMPATIBLE_VERSION'
    );

    // Test HTML response
    globalThis.fetch = async () => ({
        ok: false,
        status: 502,
        headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
        text: async () => '<html><body>Bad Gateway</body></html>'
    });
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'HTML_RESPONSE'
    );

    // Test getJobStatus
    globalThis.fetch = async (url) => {
        if (url.includes('/api/status?job_id=test-job-999')) {
            return {
                ok: true,
                status: 200,
                headers: new Headers({ 'content-type': 'application/json' }),
                json: async () => ({
                    job_id: 'test-job-999',
                    status: 'completed',
                    percent: 100,
                    running: false,
                    logs: ['Selesai'],
                    files: [{ name: 'Movie.mp4', size: 1024, url: '/download/test-job-999/Movie.mp4', is_zip: false }]
                })
            };
        }
        return { ok: false, status: 404 };
    };

    const statusRes = await APIClient.getJobStatus('test-job-999');
    assert.equal(statusRes.job_id, 'test-job-999');
    assert.equal(statusRes.status, 'completed');
    assert.equal(statusRes.files[0].name, 'Movie.mp4');
    console.log('✔ APIClient status check and error handling verified.');

    console.log('\n=== TEST 7: JobManager - Single-flow Lifecycle & Chrome Downloads API ===');
    const { JobManager } = await import(path.join(extDir, 'background/jobs.js'));

    globalThis.fetch = async (url) => {
        if (url.includes('job-abc-123')) {
            return {
                ok: true,
                status: 200,
                headers: new Headers({ 'content-type': 'application/json' }),
                json: async () => ({
                    job_id: 'job-abc-123',
                    status: 'downloading',
                    percent: 45.0,
                    running: true,
                    logs: ['Mengunduh']
                })
            };
        }
        return { ok: false, status: 404 };
    };

    chrome.storage.local.data = {
        app: 'https://dw.pmlab.id',
        token: 'validtoken123',
        omni_jobs: {}
    };

    // 1. Add job to manager
    const jobRecord = await JobManager.addJob('job-abc-123', { urls: 'https://example.com/video.mp4' }, 'Film Seru');
    assert.equal(jobRecord.id, 'job-abc-123');
    assert.equal(jobRecord.status, 'queued');

    // 2. Process status update: downloading -> 45%
    await JobManager.processJobStatusUpdate('job-abc-123', {
        status: '⬇️ Mengunduh media... — 45.0% | 2.5MiB/s | ETA 00:15',
        percent: 45.0,
        running: true,
        logs: ['Mengunduh']
    });

    const { jobs: jobsMid } = await JobManager.getJobs();
    assert.equal(jobsMid['job-abc-123'].percent, 45.0);
    assert.equal(jobsMid['job-abc-123'].status, 'downloading');
    assert.ok(jobsMid['job-abc-123'].stageLabel.includes('45%'));

    // 3. Process status update: completed -> triggers saveFilesToDisk
    mockDownloads.length = 0;
    await JobManager.processJobStatusUpdate('job-abc-123', {
        status: 'completed',
        percent: 100.0,
        running: false,
        files: [
            { name: 'Film Seru.mp4', size: 1048576, url: '/download/job-abc-123/Film%20Seru.mp4', is_zip: false }
        ]
    });

    const { jobs: jobsDone } = await JobManager.getJobs();
    assert.equal(jobsDone['job-abc-123'].status, 'saving');
    assert.equal(mockDownloads.length, 1, 'Chrome downloads should be triggered for finished job');
    assert.equal(mockDownloads[0].filename, 'OmniFetch/Film Seru/Film Seru.mp4', 'Files must be organized under Downloads/OmniFetch/Title/');
    assert.ok(mockDownloads[0].url.startsWith('https://dw.pmlab.id/download/job-abc-123/'), 'Download URL must be validated and point to dw.pmlab.id/download/');

    // 4. Duplicate download prevention: Calling again must not add downloads
    await JobManager.saveFilesToDisk('job-abc-123', [{ name: 'Film Seru.mp4', url: '/download/job-abc-123/Film.mp4' }]);
    assert.equal(mockDownloads.length, 1, 'Duplicate download must be prevented');

    // 5. Chrome download complete delta
    await JobManager.handleDownloadDelta({
        id: mockDownloads[0].id,
        state: { current: 'complete' }
    });

    const { jobs: jobsFinished } = await JobManager.getJobs();
    assert.equal(jobsFinished['job-abc-123'].status, 'completed');
    assert.ok(jobsFinished['job-abc-123'].stageLabel.includes('Selesai'), 'Job must be marked completed once saved to computer');
    console.log('\n=== TEST 8: Streaming / Idlix Movie Title Sanitization ===');
    const { cleanMovieTitle, getCleanDisplayTitle } = await import(path.join(extDir, 'utils/title.js'));

    // 1. Idlix tab titles
    assert.equal(
        cleanMovieTitle('Nonton Film Venom: The Last Dance (2024) Sub Indo | IDLIX'),
        'Venom: The Last Dance (2024)',
        'Harus membersihkan Nonton Film, Sub Indo, dan | IDLIX'
    );
    assert.equal(
        cleanMovieTitle('Nonton Arcane: Season 2 Episode 1 Sub Indo - IDLIX Official'),
        'Arcane: Season 2 Episode 1',
        'Harus membersihkan Nonton, Sub Indo, dan - IDLIX Official'
    );
    assert.equal(
        cleanMovieTitle('Nonton Film A Quiet Place: Day One (2024) Subtitle Indonesia | LK21'),
        'A Quiet Place: Day One (2024)',
        'Harus membersihkan Nonton Film, Subtitle Indonesia, dan | LK21'
    );

    // 2. getCleanDisplayTitle: Jangan gunakan nama file teknis config-*.json jika tabTitle valid!
    const idlixCand = { displayTitle: 'config-796335' };
    const resolvedTitle = getCleanDisplayTitle(idlixCand, 'Nonton Film Venom: The Last Dance (2024) Sub Indo | IDLIX');
    assert.equal(
        resolvedTitle,
        'Venom: The Last Dance (2024)',
        'Nama file teknis config-*.json tidak boleh mengalahkan judul film dari tab'
    );

    const hashCand = { displayTitle: 'ff450ba01b0ca2695d.mp4' };
    assert.equal(
        getCleanDisplayTitle(hashCand, 'Nonton Film Gladiator II (2024) Sub Indo - IDLIX'),
        'Gladiator II (2024)',
        'Hash hex mp4 tidak boleh mengalahkan judul film dari tab'
    );

    console.log('✔ Idlix / streaming movie title extraction & sanitization verified.');

    // Restore fetch
    globalThis.fetch = originalFetch;

    console.log('\n=== ALL JS UNIT & EXTENSION INTEGRATION TESTS PASSED SUCCESSFULLY! ===');
}

runTests().catch(err => {
    console.error('TEST FAILED:', err);
    process.exit(1);
});
