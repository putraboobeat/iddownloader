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

            // 7. JSON atau teks yang belum diverifikasi
            // JANGAN anggap semua JSON sebagai config/playlist! Berikan status "Belum diverifikasi"
            if (mime.includes('json') || path.endsWith('.json') || mime.includes('text/plain') || mime.includes('octet-stream')) {
                return { kind: 'unverified', statusLabel: 'Belum diverifikasi', isMedia: false, verified: false };
            }
        } catch (e) {}

        return null;
    }

    /**
     * Memeriksa konten kandidat secara terbatas tanpa menyimpan atau menampilkan isi sensitif.
     */
    static async verifyCandidateContent(candidate) {
        if (candidate.verified || candidate.kind === 'segment') return candidate;

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 4000);

            // Fetch terbatas (hanya ambil potongan awal untuk memeriksa header / magic bytes)
            const headers = { 'Range': 'bytes=0-4096' };
            if (candidate.referer) headers['Referer'] = candidate.referer;

            const res = await fetch(candidate.url, {
                method: 'GET',
                headers,
                signal: controller.signal
            });
            clearTimeout(timeoutId);

            if (!res.ok && res.status !== 206) {
                return candidate; // Biarkan tetap unverified
            }

            const rawText = await res.text();
            const snippet = rawText.slice(0, 4096).trim();

            // 1. Periksa apakah HLS (#EXTM3U) meskipun berekstensi .json atau tanpa ekstensi
            if (snippet.startsWith('#EXTM3U') || snippet.includes('#EXT-X-STREAM-INF') || snippet.includes('#EXT-X-TARGETDURATION')) {
                candidate.kind = 'hls';
                candidate.statusLabel = 'Playlist HLS';
                candidate.isMedia = true;
                candidate.verified = true;
                return candidate;
            }

            // 2. Periksa apakah DASH XML (<MPD>)
            if (snippet.includes('<MPD') || snippet.includes('<mpd')) {
                candidate.kind = 'dash';
                candidate.statusLabel = 'Manifest DASH';
                candidate.isMedia = true;
                candidate.verified = true;
                return candidate;
            }

            // 3. Periksa JSON
            try {
                const parsed = JSON.parse(snippet);
                const extractedUrl = this.extractMediaUrlFromJson(parsed);

                if (extractedUrl) {
                    // Ditemukan field media valid di dalam JSON
                    candidate.kind = 'hls';
                    candidate.statusLabel = 'Playlist HLS';
                    candidate.extractedMediaUrl = extractedUrl;
                    candidate.isMedia = true;
                    candidate.verified = true;
                    return candidate;
                }

                // 4. Jika JSON akun, analytics, token, challenge, atau non-media
                // Keluarkan dari daftar video!
                const isNonMediaJson = this.isNonMediaJsonPayload(parsed, candidate.url);
                if (isNonMediaJson) {
                    candidate.kind = 'non_media';
                    candidate.statusLabel = 'Bukan media';
                    candidate.isMedia = false;
                    candidate.verified = true;
                    return candidate;
                }
            } catch (jsonErr) {
                // Bukan JSON valid
            }
        } catch (e) {
            // Fetch gagal atau diblokir CORS browser
        }

        return candidate;
    }

    static extractMediaUrlFromJson(data) {
        if (!data || typeof data !== 'object') return null;

        // Cari field populer di player config
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

    static isNonMediaJsonPayload(data, urlStr) {
        if (!data || typeof data !== 'object') return false;

        const path = new URL(urlStr).pathname.toLowerCase();
        const nonMediaKeys = ['token', 'claim', 'challenge', 'user', 'account', 'auth', 'analytics', 'event', 'metrics', 'captcha', 'recaptcha', 'csrf', 'session_id'];
        
        // Cek nama path URL
        const pathMatches = nonMediaKeys.some(k => path.includes(k));
        
        // Cek kunci JSON
        const keys = Object.keys(data).map(k => k.toLowerCase());
        const keyMatches = keys.some(k => nonMediaKeys.includes(k));

        return pathMatches || keyMatches;
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
        
        // Ambil nama file atau path
        let displayTitle = '';
        try {
            const parsed = new URL(cleanUrl);
            const pathSegments = parsed.pathname.split('/').filter(Boolean);
            displayTitle = pathSegments.pop() || parsed.hostname;
        } catch(e) {
            displayTitle = cleanUrl.slice(0, 30);
        }

        let cand = {
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

        // Jika unverified, lakukan verifikasi asinkron terbatas
        if (!cand.verified && cand.kind === 'unverified') {
            cand = await this.verifyCandidateContent(cand);
        }

        // Jika terbukti bukan media (respons akun/analytics/claim/challenge), keluarkan dari daftar video!
        if (cand.kind === 'non_media') {
            return items.length;
        }

        // Cek duplikasi
        const existingIndex = items.findIndex(x => x.url === cleanUrl);
        if (existingIndex >= 0) {
            items[existingIndex].time = now;
            items[existingIndex].kind = cand.kind;
            items[existingIndex].statusLabel = cand.statusLabel;
            items[existingIndex].isMedia = cand.isMedia;
            items[existingIndex].verified = cand.verified;
            if (cand.extractedMediaUrl) items[existingIndex].extractedMediaUrl = cand.extractedMediaUrl;
        } else {
            items.push(cand);
        }

        // Bersihkan item kedaluwarsa
        items = items.filter(x => (now - x.time) < this.TTL);

        // Maksimal 50 kandidat per tab
        items = items.slice(-50);
        await chrome.storage.session.set({ [key]: items });

        // Update badge hanya untuk kandidat media yang valid
        const validMediaCount = items.filter(x => x.isMedia && x.kind !== 'subtitle').length;
        if (validMediaCount > 0) {
            await chrome.action.setBadgeBackgroundColor({ color: '#15803d', tabId });
            await chrome.action.setBadgeText({ text: String(validMediaCount), tabId });
        } else {
            await chrome.action.setBadgeText({ text: '', tabId });
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
