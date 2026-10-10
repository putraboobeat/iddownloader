import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const extDir = path.resolve(__dirname, '../chrome-extension');

// Mock Chrome APIs before importing extension modules
globalThis.chrome = {
    storage: {
        local: {
            data: {},
            async get(keys) {
                const res = {};
                for (const k of keys) res[k] = this.data[k];
                return res;
            },
            async set(obj) {
                Object.assign(this.data, obj);
            }
        },
        session: {
            data: {},
            async get(key) {
                return { [key]: this.data[key] || [] };
            },
            async set(obj) {
                Object.assign(this.data, obj);
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
    }
};

async function runTests() {
    console.log('=== TEST 1: Manifest Verification ===');
    const manifest = JSON.parse(fs.readFileSync(path.join(extDir, 'manifest.json'), 'utf8'));
    assert.equal(manifest.version, '2.0.3', 'Manifest version should be 2.0.3');
    assert.ok(manifest.host_permissions.includes('https://dw.pmlab.id/*'), 'host_permissions must contain https://dw.pmlab.id/*');
    assert.ok(!manifest.host_permissions.includes('http://127.0.0.1/*'), 'localhost should be removed from mandatory host_permissions');
    console.log('✔ Manifest host_permissions and version verified.');

    console.log('\n=== TEST 2: Config & Permanent BASE_URL Migration ===');
    const { BASE_URL, Config } = await import(path.join(extDir, 'background/config.js'));
    assert.equal(BASE_URL, 'https://dw.pmlab.id', 'BASE_URL must be https://dw.pmlab.id');
    
    // Simulate legacy storage with localhost address
    chrome.storage.local.data = { app: 'http://localhost:6666', token: 'legacytoken123' };
    const appInfo = await Config.getAppInfo();
    assert.equal(appInfo.url, 'https://dw.pmlab.id', 'Config must return BASE_URL even if storage has legacy server');
    assert.equal(chrome.storage.local.data.app, 'https://dw.pmlab.id', 'Legacy storage should be migrated to BASE_URL');
    assert.equal(appInfo.token, 'legacytoken123', 'Valid token must be preserved');
    console.log('✔ Config migration and BASE_URL verified.');

    console.log('\n=== TEST 3: Detector - Media vs Non-Media Classification ===');
    const { Detector } = await import(path.join(extDir, 'background/detector.js'));

    // Test standard media
    const hls = Detector.initialClassify('https://cdn.example.com/playlist.m3u8?token=sig123', '');
    assert.equal(hls.kind, 'hls');
    assert.equal(hls.statusLabel, 'Playlist HLS');
    assert.equal(hls.isMedia, true);

    const dash = Detector.initialClassify('https://cdn.example.com/manifest.mpd', '');
    assert.equal(dash.kind, 'dash');
    assert.equal(dash.statusLabel, 'Manifest DASH');
    assert.equal(dash.isMedia, true);

    const video = Detector.initialClassify('https://cdn.example.com/video.mp4', '');
    assert.equal(video.kind, 'video');
    assert.equal(video.statusLabel, 'Video langsung');
    assert.equal(video.isMedia, true);

    const segment = Detector.initialClassify('https://cdn.example.com/seg1.ts', '');
    assert.equal(segment.kind, 'segment');
    assert.equal(segment.isMedia, false);

    // Test ambiguous / JSON / claim
    const unverifiedClaim = Detector.initialClassify('https://player.example.com/api/claim', 'application/json');
    assert.equal(unverifiedClaim.kind, 'unverified');
    assert.equal(unverifiedClaim.statusLabel, 'Belum diverifikasi');
    assert.equal(unverifiedClaim.isMedia, false, 'Claim/JSON must NOT be marked as media initially');
    console.log('✔ Initial classification correctly segregates media from non-media.');

    console.log('\n=== TEST 4: Detector - Content Verification Heuristics ===');
    // Mock candidate with HLS content inside .json URL
    const candHlsInJson = {
        url: 'https://example.com/stream.json',
        kind: 'unverified',
        statusLabel: 'Belum diverifikasi',
        isMedia: false,
        verified: false
    };

    // Override fetch temporarily to simulate responses
    const originalFetch = globalThis.fetch;
    
    // Subtest: Content starts with #EXTM3U
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        text: async () => '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=1280000\nchunklist.m3u8'
    });
    const verifiedHls = await Detector.verifyCandidateContent({ ...candHlsInJson });
    assert.equal(verifiedHls.kind, 'hls');
    assert.equal(verifiedHls.statusLabel, 'Playlist HLS');
    assert.equal(verifiedHls.isMedia, true);
    console.log('✔ #EXTM3U detected in .json URL correctly classified as Playlist HLS.');

    // Subtest: DASH manifest inside XML/text
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        text: async () => '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"></MPD>'
    });
    const verifiedDash = await Detector.verifyCandidateContent({ ...candHlsInJson });
    assert.equal(verifiedDash.kind, 'dash');
    assert.equal(verifiedDash.statusLabel, 'Manifest DASH');
    assert.equal(verifiedDash.isMedia, true);
    console.log('✔ <MPD> XML correctly classified as Manifest DASH.');

    // Subtest: Player config JSON containing media sources
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
            title: 'Sample Movie',
            sources: [{ file: 'https://stream.example.com/master.m3u8?token=signed999', type: 'hls' }]
        })
    });
    const verifiedConfig = await Detector.verifyCandidateContent({ ...candHlsInJson });
    assert.equal(verifiedConfig.kind, 'hls');
    assert.equal(verifiedConfig.isMedia, true);
    assert.equal(verifiedConfig.extractedMediaUrl, 'https://stream.example.com/master.m3u8?token=signed999');
    console.log('✔ Player JSON config with media source successfully extracted.');

    // Subtest: Non-media JSON (e.g. claim / analytics / challenge)
    globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
            claim: 'approved',
            token: 'xyz-secret-token',
            expires_in: 3600
        })
    });
    const nonMediaClaim = await Detector.verifyCandidateContent({
        url: 'https://example.com/api/claim',
        kind: 'unverified',
        statusLabel: 'Belum diverifikasi',
        isMedia: false,
        verified: false
    });
    assert.equal(nonMediaClaim.kind, 'non_media');
    assert.equal(nonMediaClaim.statusLabel, 'Bukan media');
    assert.equal(nonMediaClaim.isMedia, false);
    console.log('✔ Non-media claim response detected and marked as "Bukan media" (excluded from download).');

    // Subtest: Adding candidate with claim excludes it from candidate list
    chrome.storage.session.data = {};
    const tabId = 123;
    await Detector.addCandidate(tabId, 'https://example.com/api/claim', 'application/json');
    const itemsAfterClaim = await Detector.getCandidates(tabId);
    assert.equal(itemsAfterClaim.length, 0, 'Non-media claim response must NOT be stored in candidates list');
    console.log('✔ Claim URL was filtered out and NOT added to candidate registry.');

    // Subtest: Signed URL preservation
    const signedUrl = 'https://cdn.example.com/video.m3u8?token=secret123&expires=9999999&sig=abcd';
    await Detector.addCandidate(tabId, signedUrl, 'application/x-mpegurl');
    const storedItems = await Detector.getCandidates(tabId);
    assert.equal(storedItems.length, 1);
    assert.equal(storedItems[0].url, signedUrl, 'Query parameters and signature must be fully preserved');
    console.log('✔ Signed parameters in URL preserved without corruption.');

    // Restore original fetch
    globalThis.fetch = originalFetch;

    console.log('\n=== TEST 5: APIClient Error Differentiation ===');
    const { APIClient } = await import(path.join(extDir, 'background/api.js'));

    // Test missing auth token
    chrome.storage.local.data = { app: 'https://dw.pmlab.id', token: '' };
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'AUTH_MISSING' && err.message.includes('Sesi belum terhubung')
    );
    console.log('✔ Missing auth token throws AUTH_MISSING.');

    // Test invalid media
    chrome.storage.local.data = { app: 'https://dw.pmlab.id', token: 'validtoken123' };
    await assert.rejects(
        () => APIClient.sendJob({ urls: '' }),
        err => err.code === 'INVALID_MEDIA'
    );
    console.log('✔ Empty media URL throws INVALID_MEDIA.');

    // Test HTTP 401
    globalThis.fetch = async () => ({
        ok: false,
        status: 401,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ error: 'Unauthorized' })
    });
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'AUTH_EXPIRED' && err.message.includes('401 Unauthorized')
    );
    console.log('✔ 401 response throws AUTH_EXPIRED.');

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
    console.log('✔ 404 response throws INCOMPATIBLE_VERSION.');

    // Test HTML response (Cloudflare / reverse proxy error)
    globalThis.fetch = async () => ({
        ok: false,
        status: 502,
        headers: new Headers({ 'content-type': 'text/html; charset=utf-8' }),
        text: async () => '<html><body>502 Bad Gateway</body></html>'
    });
    await assert.rejects(
        () => APIClient.sendJob({ urls: 'https://example.com/video.m3u8' }),
        err => err.code === 'HTML_RESPONSE'
    );
    console.log('✔ HTML response throws HTML_RESPONSE.');

    // Restore fetch
    globalThis.fetch = originalFetch;

    console.log('\n=== ALL JS UNIT & LOGIC TESTS PASSED SUCCESSFULLY! ===');
}

runTests().catch(err => {
    console.error('TEST FAILED:', err);
    process.exit(1);
});
