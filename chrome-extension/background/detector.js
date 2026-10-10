// Universal Media Detector & Registry
export class Detector {
    static TTL = 3600000; // 1 hour

    static classify(url, mime, requestType) {
        const path = new URL(url).pathname.toLowerCase();
        
        // Subtitles
        if (/\.(vtt|srt)$/.test(path) || mime.includes('text/vtt')) return 'subtitle';
        
        // HLS
        if (/\.m3u8$/.test(path) || mime.includes('mpegurl')) return 'hls';
        
        // DASH
        if (/\.mpd$/.test(path) || mime.includes('dash+xml')) return 'dash';
        
        // Ignore segments
        if (/\.(ts|m4s|mp4a|mp4v)$/.test(path)) return 'segment';
        
        // Audio
        if (/\.(mp3|wav|ogg|m4a)$/.test(path) || mime.includes('audio/')) return 'audio';

        // Video
        if (/\.(mp4|webm|mov|mkv)$/.test(path) || mime.includes('video/')) return 'video';
        
        // JSON Configs
        if (mime.includes('json') || path.endsWith('.json')) return 'config';

        return null;
    }

    static async addCandidate(tabId, url, mime, referer, type = 'network', extraMeta = {}) {
        if (tabId < 0) return;
        
        // Clean URL to avoid tracking parameters making infinite candidates
        const cleanUrl = url.split('#')[0]; 
        
        const kind = this.classify(cleanUrl, mime);
        if (!kind || kind === 'segment' || kind === 'config') return;

        const key = `tab:${tabId}`;
        const data = await chrome.storage.session.get(key);
        let items = data[key] || [];
        
        const now = Date.now();
        
        // Check if exact URL already exists
        const existing = items.find(x => x.url === cleanUrl);
        if (existing) {
            existing.time = now;
            Object.assign(existing.meta, extraMeta);
        } else {
            items.push({
                id: 'cand_' + Math.random().toString(36).substr(2, 9),
                url: cleanUrl,
                kind,
                sourceType: type, // network, dom, iframe
                time: now,
                referer: referer || '',
                meta: extraMeta
            });
        }

        // Deduplicate heuristics: If it's a video and we have HLS, don't show the blob URL if it exists
        // Clean up expired items
        items = items.filter(x => (now - x.time) < this.TTL);

        // Keep last 60 items max
        items = items.slice(-60);
        await chrome.storage.session.set({ [key]: items });

        // Update badge
        const mediaItems = items.filter(x => ['video', 'hls', 'dash', 'audio'].includes(x.kind));
        if (mediaItems.length > 0) {
            await chrome.action.setBadgeBackgroundColor({ color: '#15803d', tabId });
            await chrome.action.setBadgeText({ text: String(mediaItems.length), tabId });
        }
        
        return items.length;
    }

    static async clear(tabId) {
        await chrome.storage.session.remove(`tab:${tabId}`);
        await chrome.action.setBadgeText({ text: '', tabId });
    }
    
    static async getCandidates(tabId) {
        const key = `tab:${tabId}`;
        const data = await chrome.storage.session.get(key);
        return data[key] || [];
    }
}
