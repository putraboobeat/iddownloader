// Universal Media Detector & Registry
export class Detector {
    static TTL = 3600000; // 1 jam

    static initialClassify(url, mime = '') {
        try {
            const u = new URL(url);
            const path = u.pathname.toLowerCase();
            
            // 1. Subtitles
            if (/\.(vtt|srt)$/.test(path) || mime.includes('text/vtt')) {
                return { kind: 'subtitle', statusLabel: 'Subtitle', isMedia: true, verified: true };
            }
            
            // 2. Format Segment HLS / DASH (Abaikan agar tidak memenuhi daftar)
            if (/\.(ts|m4s|mp4a|mp4v)$/.test(path) || mime.includes('video/mp2t')) {
                return { kind: 'segment', statusLabel: 'Segment', isMedia: false, verified: false };
            }

            // 3. HLS Playlist (Pasti)
            if (/\.m3u8$/.test(path) || mime.includes('mpegurl') || mime.includes('x-mpegurl')) {
                return { kind: 'hls', statusLabel: 'Playlist HLS', isMedia: true, verified: true };
            }
            
            // 4. DASH Manifest (Pasti)
            if (/\.mpd$/.test(path) || mime.includes('dash+xml')) {
                return { kind: 'dash', statusLabel: 'Manifest DASH', isMedia: true, verified: true };
            }
            
            // 5. Video Langsung (Pasti)
            if (/\.(mp4|webm|mov|mkv)$/.test(path) || (mime.startsWith('video/') && !mime.includes('mp2t'))) {
                return { kind: 'video', statusLabel: 'Video langsung', isMedia: true, verified: true };
            }

            // 6. Audio Langsung (Pasti)
            if (/\.(mp3|wav|ogg|m4a|aac)$/.test(path) || mime.startsWith('audio/')) {
                return { kind: 'audio', statusLabel: 'Audio langsung', isMedia: true, verified: true };
            }

            // 7. Endpoint token claim / analytics / telemetry murni -> Tandai sebagai Bukan media
            if (path.includes('/claim') || path.includes('/analytics') || path.includes('/telemetry') || path.includes('/beacon') || path.includes('/metrics')) {
                return { kind: 'non_media', statusLabel: 'Bukan media', isMedia: false, verified: true };
            }

            // 8. JSON, AJAX, config atau teks streaming lainnya -> Belum diverifikasi (tetap dimasukkan ke daftar agar tidak hilang)
            if (mime.includes('json') || path.endsWith('.json') || mime.includes('text/plain') || mime.includes('octet-stream') || path.includes('stream') || path.includes('play') || path.includes('video') || path.includes('config')) {
                return { kind: 'unverified', statusLabel: 'Belum diverifikasi', isMedia: false, verified: false };
            }
        } catch (e) {}

        return null;
    }

    static verifyCandidateContent(candidate, snippet = '') {
        if (!snippet) return candidate;
        if (snippet.startsWith('#EXTM3U') || snippet.includes('#EXT-X-STREAM-INF') || snippet.includes('#EXT-X-TARGETDURATION')) {
            candidate.kind = 'hls';
            candidate.statusLabel = 'Playlist HLS';
            candidate.isMedia = true;
            candidate.verified = true;
            return candidate;
        }
        if (snippet.includes('<MPD') || snippet.includes('<mpd')) {
            candidate.kind = 'dash';
            candidate.statusLabel = 'Manifest DASH';
            candidate.isMedia = true;
            candidate.verified = true;
            return candidate;
        }
        try {
            const parsed = JSON.parse(snippet);
            const mediaUrl = this.extractMediaUrlFromJson(parsed);
            if (mediaUrl) {
                candidate.kind = 'hls';
                candidate.statusLabel = 'Playlist HLS';
                candidate.extractedMediaUrl = mediaUrl;
                candidate.isMedia = true;
                candidate.verified = true;
                return candidate;
            }
            if (parsed && typeof parsed === 'object') {
                const keys = Object.keys(parsed).map(k => k.toLowerCase());
                if (keys.includes('claim')) {
                    candidate.kind = 'non_media';
                    candidate.statusLabel = 'Bukan media';
                    candidate.isMedia = false;
                    candidate.verified = true;
                    return candidate;
                }
            }
        } catch(e) {}
        return candidate;
    }

    /**
     * Memeriksa konten kandidat secara non-blocking di latar belakang.
     */
    static async verifyCandidateContentAsync(tabId, cleanUrl) {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 3500);

            // Fetch terbatas tanpa header terlarang (Referer tidak boleh diset manual di fetch browser)
            const res = await fetch(cleanUrl, {
                method: 'GET',
                headers: { 'Range': 'bytes=0-4096' },
                signal: controller.signal
            });
            clearTimeout(timeoutId);

            if (!res.ok && res.status !== 206) return;

            const rawText = await res.text();
            const snippet = rawText.slice(0, 4096).trim();

            const key = `tab:${tabId}`;
            const data = await chrome.storage.session.get(key);
            let items = data[key] || [];
            const idx = items.findIndex(x => x.url === cleanUrl);
            if (idx === -1) return;

            // 1. Cek HLS (#EXTM3U) meskipun URL .json atau tanpa ekstensi
            if (snippet.startsWith('#EXTM3U') || snippet.includes('#EXT-X-STREAM-INF') || snippet.includes('#EXT-X-TARGETDURATION')) {
                items[idx].kind = 'hls';
                items[idx].statusLabel = 'Playlist HLS';
                items[idx].isMedia = true;
                items[idx].verified = true;
                await chrome.storage.session.set({ [key]: items });
                return;
            }

            // 2. Cek DASH (<MPD>)
            if (snippet.includes('<MPD') || snippet.includes('<mpd')) {
                items[idx].kind = 'dash';
                items[idx].statusLabel = 'Manifest DASH';
                items[idx].isMedia = true;
                items[idx].verified = true;
                await chrome.storage.session.set({ [key]: items });
                return;
            }

            // 3. Cek JSON untuk media source
            try {
                const parsed = JSON.parse(snippet);
                const mediaUrl = this.extractMediaUrlFromJson(parsed);
                if (mediaUrl) {
                    items[idx].kind = 'hls';
                    items[idx].statusLabel = 'Playlist HLS';
                    items[idx].extractedMediaUrl = mediaUrl;
                    items[idx].isMedia = true;
                    items[idx].verified = true;
                    await chrome.storage.session.set({ [key]: items });
                    return;
                }

                // Cek jika murni respons klaim/analitik non-media
                if (parsed && typeof parsed === 'object') {
                    const keys = Object.keys(parsed).map(k => k.toLowerCase());
                    if (keys.includes('claim') && !mediaUrl) {
                        items[idx].kind = 'non_media';
                        items[idx].statusLabel = 'Bukan media';
                        items[idx].isMedia = false;
                        items[idx].verified = true;
                        await chrome.storage.session.set({ [key]: items });
                        return;
                    }
                }
            } catch(e) {}
        } catch (e) {
            // Abaikan kegagalan verifikasi latar belakang
        }
    }

    static extractMediaUrlFromJson(data) {
        if (!data || typeof data !== 'object') return null;

        const candidatesToCheck = [
            data.file, data.url, data.src, data.stream, data.video, data.playbackUrl, data.manifest
        ];

        for (const c of candidatesToCheck) {
            if (typeof c === 'string' && (c.includes('.m3u8') || c.includes('.mpd') || c.includes('.mp4') || c.startsWith('http'))) {
                return c;
            }
        }

        if (Array.isArray(data.sources)) {
            for (const s of data.sources) {
                const sUrl = s?.file || s?.src || s?.url;
                if (typeof sUrl === 'string' && (sUrl.includes('.m3u8') || sUrl.includes('.mpd') || sUrl.includes('.mp4') || sUrl.startsWith('http'))) {
                    return sUrl;
                }
            }
        }

        return null;
    }

    static async addCandidate(tabId, url, mime = '', referer = '', type = 'network', extraMeta = {}) {
        if (tabId < 0) return;
        
        // Pertahankan query parameters & signed token, hanya bersihkan hash fragmen
        const cleanUrl = url.split('#')[0]; 
        
        const classification = this.initialClassify(cleanUrl, mime);
        if (!classification || classification.kind === 'segment') return;

        const key = `tab:${tabId}`;
        const data = await chrome.storage.session.get(key);
        let items = data[key] || [];
        
        const now = Date.now();
        
        let displayTitle = '';
        try {
            const parsed = new URL(cleanUrl);
            const pathSegments = parsed.pathname.split('/').filter(Boolean);
            displayTitle = pathSegments.pop() || parsed.hostname;
        } catch(e) {
            displayTitle = cleanUrl.slice(0, 30);
        }

        const cand = {
            id: 'cand_' + Math.random().toString(36).substr(2, 9),
            url: cleanUrl,
            displayTitle: displayTitle,
            kind: classification.kind,
            statusLabel: classification.statusLabel,
            isMedia: classification.isMedia,
            verified: classification.verified,
            sourceType: type,
            time: now,
            referer: referer || '',
            meta: extraMeta
        };

        // Simpan kandidat langsung agar tidak hilang dari daftar UI
        const existingIndex = items.findIndex(x => x.url === cleanUrl);
        if (existingIndex >= 0) {
            items[existingIndex].time = now;
            items[existingIndex].kind = cand.kind;
            items[existingIndex].statusLabel = cand.statusLabel;
            items[existingIndex].isMedia = cand.isMedia;
            items[existingIndex].verified = cand.verified;
        } else {
            items.push(cand);
        }

        // Bersihkan item kedaluwarsa
        items = items.filter(x => (now - x.time) < this.TTL);

        // Maksimal 50 kandidat per tab
        items = items.slice(-50);
        await chrome.storage.session.set({ [key]: items });

        // Update badge untuk item yang terdeteksi
        const mediaCount = items.filter(x => x.kind !== 'subtitle').length;
        if (mediaCount > 0) {
            await chrome.action.setBadgeBackgroundColor({ color: '#15803d', tabId });
            await chrome.action.setBadgeText({ text: String(mediaCount), tabId });
        } else {
            await chrome.action.setBadgeText({ text: '', tabId });
        }

        // Jalankan verifikasi konten di latar belakang tanpa memblokir antrean
        if (!cand.verified && cand.kind === 'unverified') {
            this.verifyCandidateContentAsync(tabId, cleanUrl).catch(() => {});
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
