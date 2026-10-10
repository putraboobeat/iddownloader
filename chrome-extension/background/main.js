import { Detector } from './detector.js';
import { APIClient } from './api.js';
import { Config, BASE_URL } from './config.js';

let queue = Promise.resolve();
function enqueue(fn) { queue = queue.then(fn).catch(console.error); }

// === Migration & Setup on Install ===
chrome.runtime.onInstalled.addListener(async () => {
    // Pastikan server lama termigrasi ke BASE_URL permanen
    await chrome.storage.local.set({ app: BASE_URL });

    chrome.contextMenus.create({
        id: 'omni-download-link',
        title: '\uD83D\uDE80 OmniFetch: Unduh URL',
        contexts: ['link', 'video', 'page']
    });
});

// === Network Listener ===
chrome.webRequest.onResponseStarted.addListener(d => {
    if (d.tabId < 0 || ![200, 206].includes(d.statusCode)) return;
    const mime = (d.responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type')?.value || '';
    
    // Abaikan image, html, css, js umum
    if (mime.includes('image/') || mime.includes('text/html') || mime.includes('javascript') || mime.includes('css')) return;
    
    enqueue(async () => {
        await Detector.addCandidate(d.tabId, d.url, mime.toLowerCase(), d.initiator);
    });
}, { urls: ['*://*/*'] }, ['responseHeaders']);

// === Navigation Cleanup ===
chrome.webRequest.onBeforeRequest.addListener(d => {
    if (d.tabId < 0 || d.type !== 'main_frame') return;
    enqueue(() => Detector.clear(d.tabId));
}, { urls: ['http://*/*', 'https://*/*'], types: ['main_frame'] });

chrome.tabs.onRemoved.addListener(id => {
    enqueue(() => Detector.clear(id));
});

// === Messaging ===
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    // Validasi pengirim: harus dari ekstensi kita
    if (sender.id !== chrome.runtime.id) return;

    if (message.type === 'SEND_JOB') {
        enqueue(async () => {
            try {
                const res = await APIClient.sendJob(message.payload);
                sendResponse({ ok: true, data: res });
            } catch (e) {
                sendResponse({ error: e.message, code: e.code || 'UNKNOWN_ERROR' });
            }
        });
        return true; // Keep channel open
    }
    
    if (message.type === 'CHECK_SESSION') {
        enqueue(async () => {
            const res = await APIClient.checkSession(message.token);
            sendResponse(res);
        });
        return true;
    }

    if (message.type === 'GET_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (!tabId) {
            sendResponse({ error: 'No tab context' });
            return;
        }
        enqueue(async () => {
            const items = await Detector.getCandidates(tabId);
            sendResponse({ items });
        });
        return true;
    }
    
    if (message.type === 'ADD_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (!tabId || !message.items) return;
        enqueue(async () => {
            for (const item of message.items) {
                await Detector.addCandidate(tabId, item.url, '', '', item.sourceType);
            }
            sendResponse({ ok: true });
        });
        return true;
    }
    
    if (message.type === 'CLEAR_CANDIDATES') {
        const tabId = sender.tab ? sender.tab.id : message.tabId;
        if (tabId) {
            enqueue(async () => {
                await Detector.clear(tabId);
                sendResponse({ ok: true });
            });
            return true;
        }
    }
    
    if (message.type === 'SYNC_COOKIES') {
        enqueue(async () => {
            const domains = ['.instagram.com', '.youtube.com', '.tiktok.com', '.twitter.com', '.x.com'];
            try {
                await APIClient.syncCookies(domains);
                sendResponse({ ok: true });
            } catch (e) {
                sendResponse({ error: e.message });
            }
        });
        return true;
    }
});

// === Context Menu ===
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const settings = await Config.getAppInfo();
    if (!settings.token) { 
        chrome.runtime.openOptionsPage(); 
        return; 
    }
    const url = info.linkUrl || info.srcUrl || info.pageUrl;
    if (!url) return;
    
    try {
        await APIClient.sendJob({ urls: url, mode: 'ytdlp', media_type: 'video', resolution: 'best' });
    } catch(e) {
        console.error("Context menu download failed", e.message);
    }
});

// === Cookie Sync Alarm ===
chrome.alarms.create('omni-cookie-sync', { periodInMinutes: 60 });
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'omni-cookie-sync') {
        const domains = ['.instagram.com', '.youtube.com', '.tiktok.com', '.twitter.com', '.x.com'];
        APIClient.syncCookies(domains);
    }
});
