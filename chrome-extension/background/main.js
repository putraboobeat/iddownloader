import { Detector } from './detector.js';
import { APIClient } from './api.js';
import { Config } from './config.js';

let queue = Promise.resolve();
function enqueue(fn) { queue = queue.then(fn).catch(console.error); }

// === Network Listener ===
chrome.webRequest.onResponseStarted.addListener(d => {
    if (d.tabId < 0 || ![200, 206].includes(d.statusCode)) return;
    const mime = (d.responseHeaders || []).find(h => h.name.toLowerCase() === 'content-type')?.value || '';
    
    // Ignore images, html, css, js quickly
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
    // Validate sender: must be from our extension context or a valid content script
    if (sender.id !== chrome.runtime.id) return;

    if (message.type === 'SEND_JOB') {
        enqueue(async () => {
            try {
                const res = await APIClient.sendJob(message.payload);
                sendResponse({ ok: true, data: res });
            } catch (e) {
                sendResponse({ error: e.message });
            }
        });
        return true; // Keep channel open
    }
    
    if (message.type === 'GET_CANDIDATES') {
        if (!sender.tab) {
            sendResponse({ error: 'No tab context' });
            return;
        }
        enqueue(async () => {
            const items = await Detector.getCandidates(sender.tab.id);
            sendResponse({ items });
        });
        return true;
    }
});

// === Context Menu ===
chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({
        id: 'omni-download-link',
        title: '\uD83D\uDE80 OmniFetch: Unduh URL',
        contexts: ['link', 'video', 'page']
    });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const settings = await Config.getAppInfo();
    if (!settings.url) { 
        chrome.runtime.openOptionsPage(); 
        return; 
    }
    const url = info.linkUrl || info.srcUrl || info.pageUrl;
    if (!url) return;
    
    try {
        await APIClient.sendJob({ urls: url, mode: 'ytdlp', media_type: 'video', resolution: 'best' });
        // Optional: show a notification if successful
    } catch(e) {
        console.error("Context menu download failed", e);
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
