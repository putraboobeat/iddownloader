// Universal Media Detector & Registry
export class Detector {
    static TTL = 3600000; // 1 jam
    static _tabQueues = new Map();
    static _navGenerations = new Map();

    static getGeneration(tabId) {
        return this._navGenerations.get(tabId) || 0;
    }

    static bumpGeneration(tabId) {
        const next = (this._navGenerations.get(tabId) || 0) + 1;
        this._navGenerations.set(tabId, next);
        return next;
    }

    /**
     * Serialisasi mutasi per tab untuk mencegah race condition pembacaan/penulisan storage.
     */
    static async withTabLock(tabId, fn) {
        const prev = this._tabQueues.get(tabId) || Promise.resolve();
        let release;
        const current = new Promise(r => { release = r; });
        this._tabQueues.set(tabId, current);

        try {
            await prev;
            return await fn();
        } finally {
            release();
            if (this._tabQueues.get(tabId) === current) {
                this._tabQueues.delete(tabId);
            }
        }
    }

    static isAdOrAnalytics(url) {
        const lower = url.toLowerCase();
        const adPatterns = [
            'doubleclick.net', 'googlesyndication.com', 'googleads', '/pagead/',
            'adnxs.com', 'adsystem', 'popads', 'adsterra', 'scorecardresearch.com',
            '/telemetry', '/analytics', '/beacon', '/metrics', '/event-collector',
            '/tr/pixel', 'facebook.com/tr', 'tiktok.com/api/v1/pixel',
            '/claim', '/track', '/speculation', '/web-observations',
            'challenges.cloudflare.com', 'cloudflareinsights.com', '/cdn-cgi/challenge',
            '/cdn-cgi/speculation'
        ];
        return adPatterns.some(p => lower.includes(p));
    }

    static initialClassify(url, mime = '') {
        try {
            const u = new URL(url);
            const path = u.pathname.toLowerCase();
            const fullLower = url.toLowerCase();
            
            // 0. Filter iklan dan pelacak analitik
            if (this.isAdOrAnalytics(fullLower)) {
                return { kind: 'non_media', statusLabel: 'Bukan media (Iklan/Analitik)', isMedia: false, verified: true };
            }

            // 1. Subtitles
            if (/\.(vtt|srt)$/.test(path) || mime.includes('text/vtt') || ((path.includes('/i18n/') || path.includes('/subtitles/')) && (path.includes('.vtt') || path.includes('.srt')))) {
                let subLabel = 'Subtitle';
                if (path.includes('/id/') || path.includes('/indonesian') || path.includes('/indonesia') || path.includes('_id.')) {
                    subLabel = 'Subtitle Indonesia';
                } else if (path.includes('/en/') || path.includes('/english') || path.includes('_en.')) {
                    subLabel = 'Subtitle Inggris';
                }
                return { kind: 'subtitle', statusLabel: subLabel, isMedia: true, verified: true };
            }

            // 1b. Segment HLS / DASH / Fragmen (Abaikan agar tidak memenuhi daftar)
            if (/\.(ts|m4s|mp4a|mp4v)$/.test(path) || mime.includes('video/mp2t') || /\/seg-\d+/.test(path)) {
                return { kind: 'segment', statusLabel: 'Segment', isMedia: false, verified: false };
            }

            // 2. Player Config JSON (misal config-*.json di Idlix / Majorplay / Jeniusplay)
            if (/\/config-[^/]+\.json$/.test(path) || (path.includes('config-') && path.endsWith('.json')) || (path.includes('config-') && fullLower.includes('.json'))) {
                return { kind: 'hls', statusLabel: 'Playlist Config (Idlix)', isMedia: true, verified: true };
            }

            // 3. HLS Playlist (Pasti jika berekstensi .m3u8 atau mime)
            if (/\.m3u8$/.test(path) || mime.includes('mpegurl') || mime.includes('x-mpegurl')) {
                const label = fullLower.includes('master') ? 'Playlist HLS (Master)' : 'Playlist HLS';
                return { kind: 'hls', statusLabel: label, isMedia: true, verified: true };
            }
            
            // 4. DASH Manifest (Pasti jika .mpd)
            if (/\.mpd$/.test(path) || mime.includes('dash+xml')) {
                return { kind: 'dash', statusLabel: 'Manifest DASH', isMedia: true, verified: true };
            }
            
            // 5. Video Langsung (Pasti)
            if (/\.(mp4|webm|mov|mkv)$/.test(path) || (mime.startsWith('video/') && !mime.includes('mp2t'))) {
                return { kind: 'video', statusLabel: 'Video langsung', isMedia: true, verified: true };
            }

            // 6. Audio Langsung (Pasti)
            if (/\.(mp3|wav|ogg|m4a|aac|flac)$/.test(path) || mime.startsWith('audio/')) {
                return { kind: 'audio', statusLabel: 'Audio langsung', isMedia: true, verified: true };
            }

            // 7. Endpoint token claim murni -> Tandai sebagai Bukan media
            if (path.includes('/claim')) {
                return { kind: 'non_media', statusLabel: 'Bukan media', isMedia: false, verified: true };
            }

            // 8. JSON, AJAX, config atau teks streaming lainnya -> Belum diverifikasi
            if (mime.includes('json') || path.endsWith('.json') || mime.includes('text/plain') || mime.includes('octet-stream') || path.includes('stream') || path.includes('play') || path.includes('video') || path.includes('config')) {
                return { kind: 'unverified', statusLabel: 'Belum diverifikasi', isMedia: false, verified: false };
            }
        } catch (e) {}

        return null;
    }

    static verifyCandidateContent(candidate, snippet = '') {
        if (!snippet) return candidate;
        
        // Cek HLS
        if (snippet.startsWith('#EXTM3U') || snippet.includes('#EXT-X-STREAM-INF') || snippet.includes('#EXT-X-TARGETDURATION')) {
            candidate.kind = 'hls';
            candidate.statusLabel = snippet.includes('#EXT-X-STREAM-INF') ? 'Playlist HLS (Master)' : 'Playlist HLS';
            candidate.isMedia = true;
            candidate.verified = true;
            return candidate;
        }

        // Cek DASH
        if (snippet.includes('<MPD') || snippet.includes('<mpd')) {
            candidate.kind = 'dash';
            candidate.statusLabel = 'Manifest DASH';
            candidate.isMedia = true;
            candidate.verified = true;
            return candidate;
        }

        // Cek JSON
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
     * Menggunakan ReadableStream reader untuk membatasi ukuran bacaan maksimal 8KB.
     * Hasil verifikasi lama dibuang jika tab telah mengalami navigasi/clear.
     */
    static async verifyCandidateContentAsync(tabId, cleanUrl, expectedGen = 0) {
        let streamReader = null;
        let timeoutId = null;

        try {
            const controller = new AbortController();
            timeoutId = setTimeout(() => controller.abort(), 4000);

            const res = await fetch(cleanUrl, {
                method: 'GET',
                headers: { 'Range': 'bytes=0-8192' },
                signal: controller.signal
            });

            if (!res.ok && res.status !== 206) return;

            let snippet = '';
            if (res.body && typeof res.body.getReader === 'function') {
                streamReader = res.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let bytesRead = 0;
                const MAX_BYTES = 8192;
                while (bytesRead < MAX_BYTES) {
                    const { done, value } = await streamReader.read();
                    if (done) break;
                    snippet += decoder.decode(value, { stream: true });
                    bytesRead += value.length;
                    if (bytesRead >= MAX_BYTES) {
                        await streamReader.cancel();
                        break;
                    }
                }
            } else {
                const raw = await res.text();
                snippet = raw.slice(0, 8192);
            }

            snippet = snippet.trim();
            if (!snippet) return;

            // Jika tab telah dinavigasi selama pembacaan jaringan, buang hasil verifikasi
            if (this.getGeneration(tabId) !== expectedGen) return;

            // Perbarui penyimpanan dalam lock tab singkat
            await this.withTabLock(tabId, async () => {
                if (this.getGeneration(tabId) !== expectedGen) return;

                const key = `tab:${tabId}`;
                const data = await chrome.storage.session.get(key);
                let items = data[key] || [];
                const idx = items.findIndex(x => x.url === cleanUrl);
                if (idx === -1) return;

                items[idx].previewSnippet = snippet.slice(0, 1024);
                const updatedCandidate = this.verifyCandidateContent(items[idx], snippet);
                items[idx] = updatedCandidate;

                try {
                    const parsed = JSON.parse(snippet);
                    const subs = this.extractSubtitlesFromJson(parsed);
                    for (const sub of subs) {
                        if (!items.some(x => x.url === sub.url)) {
                            items.push({
                                id: 'cand_' + Math.random().toString(36).substr(2, 9),
                                url: sub.url,
                                displayTitle: sub.label,
                                kind: 'subtitle',
                                statusLabel: sub.label,
                                isMedia: true,
                                verified: true,
                                sourceType: 'json_tracks',
                                time: Date.now(),
                                referer: cleanUrl
                            });
                        }
                    }
                } catch (_) {}

                await chrome.storage.session.set({ [key]: items });

                const mediaCount = items.filter(x => x.kind !== 'subtitle' && x.isMedia).length;
                if (mediaCount > 0) {
                    await chrome.action.setBadgeBackgroundColor({ color: '#15803d', tabId });
                    await chrome.action.setBadgeText({ text: String(mediaCount), tabId });
                }
            });
        } catch (e) {
            // Abaikan kegagalan verifikasi latar belakang
        } finally {
            if (timeoutId) clearTimeout(timeoutId);
            if (streamReader) {
                try { await streamReader.cancel(); } catch (err) {}
            }
        }
    }

    /**
     * Ekstraksi URL media dari objek JSON.
     * Tidak menerima semua HTTP URL, hanya URL yang memiliki format media atau stream valid.
     */
    static extractMediaUrlFromJson(data) {
        if (!data || typeof data !== 'object') return null;

        const isMediaLikeUrl = (str) => {
            if (typeof str !== 'string' || !str.startsWith('http')) return false;
            const lower = str.toLowerCase();
            return (
                lower.includes('.m3u8') ||
                lower.includes('.mpd') ||
                lower.includes('.mp4') ||
                lower.includes('.webm') ||
                lower.includes('.mkv') ||
                lower.includes('.m4a') ||
                lower.includes('/hls/') ||
                lower.includes('/dash/') ||
                lower.includes('manifest') ||
                lower.includes('master.m3u8')
            );
        };

        const candidatesToCheck = [
            data.file, data.url, data.src, data.stream, data.video, data.playbackUrl, data.manifest
        ];

        for (const c of candidatesToCheck) {
            if (isMediaLikeUrl(c)) return c;
        }

        if (Array.isArray(data.sources)) {
            for (const s of data.sources) {
                const sUrl = s?.file || s?.src || s?.url;
                if (isMediaLikeUrl(sUrl)) return sUrl;
            }
        }

        return null;
    }

    /**
     * Ekstraksi URL subtitle dari objek JSON player (misal JWPlayer / Plyr tracks).
     */
    static extractSubtitlesFromJson(data) {
        if (!data || typeof data !== 'object') return [];
        const subs = [];
        const tracks = Array.isArray(data.tracks) ? data.tracks : (Array.isArray(data.subtitles) ? data.subtitles : []);
        for (const t of tracks) {
            const file = t?.file || t?.src || t?.url;
            if (file && typeof file === 'string' && (file.includes('.vtt') || file.includes('.srt') || file.includes('/i18n/'))) {
                let label = t.label || t.name || 'Subtitle';
                const lower = (label + ' ' + file).toLowerCase();
                if (lower.includes('indonesia') || lower.includes('/id/') || lower.includes('_id.')) {
                    label = 'Subtitle Indonesia';
                } else if (lower.includes('english') || lower.includes('/en/') || lower.includes('_en.')) {
                    label = 'Subtitle Inggris';
                }
                subs.push({
                    url: file,
                    label: label,
                    kind: 'subtitle',
                    default: !!t.default
                });
            }
        }
        return subs;
    }

    static async addCandidate(tabId, url, mime = '', referer = '', type = 'network', extraMeta = {}) {
        if (tabId < 0) return;
        
        // Pertahankan query parameters & signed token, hanya bersihkan hash fragmen
        const cleanUrl = url.split('#')[0]; 
        
        const classification = this.initialClassify(cleanUrl, mime);
        if (!classification || classification.kind === 'segment') return;

        const currentGen = this.getGeneration(tabId);

        // Mutasi storage diserialisasikan per tab untuk menghindari race condition overwrite
        const mutationResult = await this.withTabLock(tabId, async () => {
            if (this.getGeneration(tabId) !== currentGen) return null;

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

            items = items.filter(x => (now - x.time) < this.TTL);
            items = items.slice(-50);
            await chrome.storage.session.set({ [key]: items });

            const mediaCount = items.filter(x => x.kind !== 'subtitle' && x.isMedia).length;
            if (mediaCount > 0) {
                await chrome.action.setBadgeBackgroundColor({ color: '#15803d', tabId });
                await chrome.action.setBadgeText({ text: String(mediaCount), tabId });
            } else {
                await chrome.action.setBadgeText({ text: '', tabId });
            }

            return { count: items.length, shouldVerify: !cand.verified && cand.kind === 'unverified' };
        });

        // Verifikasi konten berjalan di latar belakang di luar tab lock
        if (mutationResult && mutationResult.shouldVerify) {
            this.verifyCandidateContentAsync(tabId, cleanUrl, currentGen).catch(() => {});
        }

        return mutationResult?.count || 0;
    }

    static async clear(tabId) {
        this.bumpGeneration(tabId);
        return await this.withTabLock(tabId, async () => {
            await chrome.storage.session.remove(`tab:${tabId}`);
            await chrome.action.setBadgeText({ text: '', tabId });
        });
    }

    static async getCandidates(tabId) {
        return await this.withTabLock(tabId, async () => {
            const key = `tab:${tabId}`;
            const data = await chrome.storage.session.get(key);
            return data[key] || [];
        });
    }

    /**
     * Memilih kandidat media terbaik secara cerdas.
     * Menghindari iklan dan memprioritaskan HLS Master > HLS/DASH > Direct MP4 > Audio.
     */
    static findBestMedia(items = []) {
        if (!items || items.length === 0) return null;

        const mediaItems = items.filter(x => x.kind !== 'subtitle' && x.isMedia);
        if (mediaItems.length === 0) {
            return items.find(x => x.kind !== 'subtitle' && x.kind !== 'non_media') || null;
        }

        const score = x => {
            let s = 0;
            const fullLower = (x.url || '').toLowerCase();
            if (this.isAdOrAnalytics(fullLower)) return -999;
            if (x.statusLabel?.includes('Master')) s += 60;
            else if (x.kind === 'hls') s += 50;
            else if (x.kind === 'dash') s += 45;
            else if (x.kind === 'video') s += 40;
            else if (x.kind === 'audio') s += 25;
            else if (x.kind === 'unverified') s += 10;
            if (x.sourceType === 'dom') s += 2;
            return s;
        };

        const sorted = [...mediaItems].sort((a, b) => score(b) - score(a) || (b.time - a.time));
        return sorted[0] || null;
    }

    /**
     * Otomatis mendeteksi subtitle Indonesia yang cocok dengan media saat ini.
     * Mencocokkan direktori/identitas media secara tepat, bukan potongan path umum.
     */
    static findDefaultSubtitle(items = [], currentVideoUrl = '') {
        if (!items || items.length === 0) return null;
        const subs = items.filter(x => x.kind === 'subtitle');
        if (subs.length === 0) return null;

        if (!currentVideoUrl) {
            // Jika tidak ada konteks video (misal daftar subtitle berdiri sendiri)
            const idSub = subs.find(s => {
                const p = (s.url + ' ' + (s.statusLabel || '')).toLowerCase();
                return p.includes('indonesia') || p.includes('/id/') || p.includes('_id.') || p.includes('bahasa');
            });
            return idSub || subs[0] || null;
        }

        const GENERIC_PATH_SEGMENTS = new Set([
            'video', 'videos', 'movie', 'movies', 'film', 'films', 'media', 'stream', 'streams',
            'hls', 'dash', 'content', 'upload', 'uploads', 'assets', 'static', 'data', 'files',
            'playlist', 'master', 'segment', 'segments', 'vod', 'live', 'cdn', 'api', 'play',
            'player', 'v1', 'v2', 'v3', 'sub', 'subs', 'subtitle', 'subtitles', 'index',
            'master.m3u8', 'index.m3u8', 'playlist.m3u8', 'manifest.mpd'
        ]);

        let vidHost = '';
        let targetIdentity = null;

        try {
            const vidUrl = new URL(currentVideoUrl);
            vidHost = vidUrl.hostname.toLowerCase();
            const parts = vidUrl.pathname.split('/').map(p => p.trim()).filter(Boolean);

            // 1. Dukungan struktur eksplisit /v/{server}/{video-id}/
            const vIdx = parts.indexOf('v');
            if (vIdx >= 0 && parts.length > vIdx + 2) {
                targetIdentity = parts[vIdx + 2].toLowerCase();
            }

            // 2. Jika bukan struktur /v/, ambil segmen folder non-generik paling spesifik
            if (!targetIdentity && parts.length > 0) {
                const meaningful = parts.filter(p => {
                    const lower = p.toLowerCase();
                    const dot = lower.lastIndexOf('.');
                    const base = dot > 0 ? lower.slice(0, dot) : lower;
                    return !GENERIC_PATH_SEGMENTS.has(lower) && !GENERIC_PATH_SEGMENTS.has(base);
                });

                if (meaningful.length > 0) {
                    const lastSeg = meaningful[meaningful.length - 1];
                    const dot = lastSeg.lastIndexOf('.');
                    targetIdentity = (dot > 0 ? lastSeg.slice(0, dot) : lastSeg).toLowerCase();
                }
            }
        } catch (e) {
            return null;
        }

        // Jika tidak dapat menentukan identitas spesifik video, jangan memilih otomatis
        if (!targetIdentity) return null;

        // Cocokkan subtitle yang berada pada hostname yang sama dan memuat identitas spesifik yang sama
        const correlatedSubs = subs.filter(s => {
            try {
                const sUrl = new URL(s.url);
                if (sUrl.hostname.toLowerCase() !== vidHost) return false;

                const sParts = sUrl.pathname.toLowerCase().split('/').filter(Boolean);
                
                // Cek apakah ada segmen folder subtitle yang persis sama dengan identitas media
                const matchesFolder = sParts.includes(targetIdentity);

                // Atau cek apakah nama file subtitle diawali atau dipisahkan dengan identitas tersebut
                const sFileName = sParts[sParts.length - 1] || '';
                const matchesFile = sFileName.startsWith(targetIdentity + '.') || 
                                    sFileName.startsWith(targetIdentity + '_') || 
                                    sFileName.startsWith(targetIdentity + '-');

                return matchesFolder || matchesFile;
            } catch (e) {
                return false;
            }
        });

        if (correlatedSubs.length === 0) return null;

        // Di antara subtitle yang berkorelasi tepat, prioritaskan bahasa Indonesia
        const idSub = correlatedSubs.find(s => {
            const p = (s.url + ' ' + (s.statusLabel || '')).toLowerCase();
            return p.includes('indonesia') || p.includes('/id/') || p.includes('_id.') || p.includes('bahasa');
        });

        return idSub || correlatedSubs[0] || null;
    }
}
