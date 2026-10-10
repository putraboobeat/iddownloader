import assert from 'node:assert/strict';

const BASE_URL = 'https://dw.pmlab.id';
const TEST_SESSION = 'livesession' + Math.random().toString(36).substring(2, 8);

async function runLiveTests() {
    console.log(`=== LIVE TESTING AGAINST PRODUCTION: ${BASE_URL} ===\n`);

    // 1. Root dashboard accessibility
    console.log('1. Checking root dashboard GET /');
    const rootRes = await fetch(`${BASE_URL}/`);
    assert.equal(rootRes.status, 200, 'Root dashboard should return 200');
    const rootText = await rootRes.text();
    assert.ok(rootText.includes('OmniFetch'), 'Root page should contain OmniFetch');
    console.log('✔ Root dashboard is online and serving OmniFetch.');

    // 2. Authentication check: Unauthorized without X-App-Session
    console.log('\n2. Checking GET /api/status without authentication');
    const unauthStatusRes = await fetch(`${BASE_URL}/api/status`);
    assert.equal(unauthStatusRes.status, 401, 'Unauthenticated request should return 401');
    const unauthJson = await unauthStatusRes.json();
    assert.equal(unauthJson.error, 'Unauthorized');
    console.log('✔ Server strictly enforces 401 Unauthorized for missing session.');

    // 3. Authentication check: Valid alphanumeric session
    console.log(`\n3. Checking GET /api/status with valid session: ${TEST_SESSION}`);
    const authStatusRes = await fetch(`${BASE_URL}/api/status`, {
        headers: { 'X-App-Session': TEST_SESSION }
    });
    assert.equal(authStatusRes.status, 200, 'Valid session should return 200');
    const authStatusJson = await authStatusRes.json();
    assert.ok(Array.isArray(authStatusJson.jobs), 'Status should return jobs list');
    console.log('✔ Server accepts valid alphanumeric session header and returns job list.');

    // 4. Job submission without session -> 401
    console.log('\n4. Checking POST /api/start without authentication');
    const unauthPostRes = await fetch(`${BASE_URL}/api/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' })
    });
    assert.equal(unauthPostRes.status, 401, 'Unauthenticated POST /api/start must return 401');
    console.log('✔ POST /api/start rejected without X-App-Session.');

    // 5. Job submission with invalid/empty URL -> 400
    console.log('\n5. Checking POST /api/start with empty URL');
    const badPostRes = await fetch(`${BASE_URL}/api/start`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-App-Session': TEST_SESSION
        },
        body: JSON.stringify({ urls: '' })
    });
    assert.equal(badPostRes.status, 400, 'Empty URL must return 400');
    console.log('✔ Server rejects empty URL with 400 Bad Request.');

    // 6. Valid job submission -> 200 and contract verification
    console.log('\n6. Checking POST /api/start with YouTube URL and valid session');
    const testPayload = {
        urls: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        mode: 'ytdlp',
        media_type: 'video',
        resolution: 'best'
    };
    const jobRes = await fetch(`${BASE_URL}/api/start`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-App-Session': TEST_SESSION
        },
        body: JSON.stringify(testPayload)
    });
    assert.equal(jobRes.status, 200, 'Valid job must return 200');
    const jobJson = await jobRes.json();
    assert.ok(jobJson.job_id, 'Server must return job_id');
    assert.equal(jobJson.status, 'queued', 'Server must return queued status');
    console.log(`✔ Job successfully queued on production! Job ID: ${jobJson.job_id}`);

    // 7. Clean up test job immediately
    console.log(`\n7. Cancelling test job: ${jobJson.job_id}`);
    const stopRes = await fetch(`${BASE_URL}/api/stop`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-App-Session': TEST_SESSION
        },
        body: JSON.stringify({ job_id: jobJson.job_id })
    });
    assert.equal(stopRes.status, 200);
    const stopJson = await stopRes.json();
    assert.equal(stopJson.status, 'cancelled');
    console.log('✔ Test job cleanly cancelled on production.');

    console.log('\n=== ALL PRODUCTION ENDPOINT VERIFICATIONS PASSED! ===');
}

runLiveTests().catch(err => {
    console.error('LIVE TEST FAILED:', err);
    process.exit(1);
});
