import { BASE_URL, Config } from './background/config.js';
import { Detector } from './background/detector.js';
import { createZipBlob } from './utils/zip.js';
import { cleanMovieTitle, getCleanDisplayTitle } from './utils/title.js';

const $ = id => document.getElementById(id);
let currentTab = null;
let detectedItems = [];
let selectedMediaType = 'video'; // 'video' | 'audio' | 'subtitle'
let currentActiveJobId = null;
let lastKnownJob = null;
let isSubmitting = false;
let userExplicitNoSub = false;

const SOCIAL_DOMAINS = [
    'youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com',
    'twitter.com', 'x.com', 'facebook.com', 'fb.watch', 'reddit.com'
];

function sanitize(str) {
    return (str || '').replace(/[\\/:*?"<>|\r\n\t]/g, '_').trim();
}

// Ekstraktor media browser untuk Instagram (Slide Carousel Foto & Video)
// Strictly scoped ke targetShortcode postingan yang dituju — mendukung 3 strategi:
// 1. Modern Web API & GraphQL
// 2. In-page script tags & Relay cache JSON
// 3. Physical DOM Carousel Navigation Sweep (klik Next hingga slide terakhir, panen seluruh slide, lalu rewind)
let currentInspectInfo = null;
let currentExtractedCarousel = [];

function populateSlideOptions(count) {
    const slideSelect = $('slide-number-select');
    if (!slideSelect || !count || count <= 0) return;
    slideSelect.replaceChildren();
    for (let i = 1; i <= count; i++) {
        const label = i === 1 ? `Slide 1 (Foto Pertama)` : (i === count ? `Slide ${i} (Foto Terakhir)` : `Slide ${i}`);
        slideSelect.append(new Option(label, String(i)));
    }
}

async function extractInstagramMedia(tabId, targetUrl = '') {
    if (!tabId) return [];

    const urlToUse = targetUrl || '';
    const m = urlToUse.match(/\/(?:p|reel|tv)\/([A-Za-z0-9_-]+)/);
    const targetShortcode = m ? m[1] : null;

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId },
            args: [targetShortcode],
            func: async (shortcode) => {
                const items = [];
                const seen = new Set();
                const add = (url, type) => {
                    if (!url || typeof url !== 'string' || !url.startsWith('http')) return;
                    const clean = url.replace(/\\u0026/g, '&').replace(/\\/g, '');
                    if (seen.has(clean)) return;
                    seen.add(clean);
                    items.push({ url: clean, type });
                };

                const processCarouselList = (carouselList) => {
                    if (!Array.isArray(carouselList) || carouselList.length === 0) return false;
                    let countAdded = 0;
                    for (const slide of carouselList) {
                        if (!slide) continue;
                        if (slide.is_video || slide.video_versions) {
                            const vUrl = slide.video_versions?.[0]?.url || slide.video_url;
                            if (vUrl) { add(vUrl, 'video'); countAdded++; }
                        } else {
                            const candidates = slide.image_versions2?.candidates || [];
                            const bestCand = candidates.length > 0 ? candidates[0].url : slide.display_url;
                            if (bestCand) { add(bestCand, 'image'); countAdded++; }
                        }
                    }
                    return countAdded > 0;
                };

                // STRATEGI 1: Panggil Modern Web API Instagram via sesi tab aktif
                if (shortcode) {
                    const endpoints = [
                        `/api/v1/media/info/?code=${shortcode}`,
                        `/p/${shortcode}/?__a=1&__d=dis`
                    ];
                    for (const ep of endpoints) {
                        try {
                            const apiRes = await fetch(ep, {
                                headers: {
                                    'X-IG-App-ID': '936619743392459',
                                    'X-Requested-With': 'XMLHttpRequest'
                                }
                            });
                            if (apiRes.ok) {
                                const data = await apiRes.json();
                                const postItem = data?.items?.[0] || data?.graphql?.shortcode_media || data?.data?.xdt_shortcode_media;
                                if (postItem) {
                                    const carousel = postItem.carousel_media || (postItem.edge_sidecar_to_children?.edges?.map(e => e.node));
                                    if (processCarouselList(carousel)) return items;

                                    if (postItem.is_video || postItem.video_versions) {
                                        const vUrl = postItem.video_versions?.[0]?.url || postItem.video_url;
                                        if (vUrl) { add(vUrl, 'video'); return items; }
                                    } else {
                                        const imgUrl = postItem.image_versions2?.candidates?.[0]?.url || postItem.display_url;
                                        if (imgUrl) { add(imgUrl, 'image'); return items; }
                                    }
                                }
                            }
                        } catch (e) {
                            // Abaikan dan coba endpoint selanjutnya
                        }
                    }
                }

                // STRATEGI 2: Ekstraksi dari Script Tags & Relay Cache pada HTML Dokumen
                try {
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const txt = s.textContent || '';
                        if (!txt) continue;
                        if (txt.includes('carousel_media') || txt.includes('edge_sidecar_to_children')) {
                            // A. Jika JSON murni
                            if (s.type === 'application/json' || txt.startsWith('{') || txt.startsWith('[')) {
                                try {
                                    const parsed = JSON.parse(txt);
                                    const findCar = (obj) => {
                                        if (!obj || typeof obj !== 'object') return null;
                                        if (Array.isArray(obj.carousel_media) && obj.carousel_media.length > 0) return obj.carousel_media;
                                        if (Array.isArray(obj.edge_sidecar_to_children?.edges) && obj.edge_sidecar_to_children.edges.length > 0) {
                                            return obj.edge_sidecar_to_children.edges.map(e => e.node);
                                        }
                                        for (const k of Object.keys(obj)) {
                                            const f = findCar(obj[k]);
                                            if (f) return f;
                                        }
                                        return null;
                                    };
                                    const foundCar = findCar(parsed);
                                    if (foundCar && processCarouselList(foundCar)) return items;
                                } catch {}
                            }

                            // B. Ekstraksi balanced JSON array untuk payload inline Relay / Web JS
                            const carIdx = txt.indexOf('"carousel_media"');
                            if (carIdx !== -1) {
                                try {
                                    const bracketStart = txt.indexOf('[', carIdx);
                                    if (bracketStart !== -1) {
                                        let depth = 0, inStr = false, esc = false;
                                        for (let i = bracketStart; i < txt.length; i++) {
                                            const c = txt[i];
                                            if (esc) { esc = false; continue; }
                                            if (c === '\\') { esc = true; continue; }
                                            if (c === '"') { inStr = !inStr; continue; }
                                            if (!inStr) {
                                                if (c === '[') depth++;
                                                else if (c === ']') {
                                                    depth--;
                                                    if (depth === 0) {
                                                        const jsonArr = txt.slice(bracketStart, i + 1);
                                                        const carArr = JSON.parse(jsonArr);
                                                        if (processCarouselList(carArr)) return items;
                                                        break;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                } catch {}
                            }
                        }
                    }
                } catch (e) {}

                // STRATEGI 3: DOM Scoping & Interactive Navigation Sweep
                // Mengatasi batasan virtualized DOM Instagram (yang hanya memuat slide aktif & adjacent)
                // dengan menavigasikan tombol Next ke ujung lalu rewind kembali ke awal.
                let targetArticle = null;
                const dialog = document.querySelector('div[role="dialog"]');
                if (dialog) {
                    if (!shortcode || dialog.querySelector(`a[href*="${shortcode}"]`)) {
                        targetArticle = dialog;
                    }
                }
                if (!targetArticle && shortcode) {
                    const articles = document.querySelectorAll('article');
                    for (const art of articles) {
                        if (art.querySelector(`a[href*="${shortcode}"]`)) {
                            targetArticle = art;
                            break;
                        }
                    }
                }
                if (!targetArticle && window.location.pathname.includes('/p/')) {
                    targetArticle = document.querySelector('main article') || document.querySelector('article');
                }
                if (!targetArticle) {
                    targetArticle = document.querySelector('article') || document.body;
                }

                if (targetArticle) {
                    const harvestCurrentDom = () => {
                        const imgs = targetArticle.querySelectorAll('img');
                        for (const img of imgs) {
                            let best = img.currentSrc || img.src;
                            if (img.srcset) {
                                const parts = img.srcset.split(',').map(x => x.trim());
                                if (parts.length > 0) {
                                    const last = parts[parts.length - 1].split(' ')[0];
                                    if (last && last.startsWith('http')) best = last;
                                }
                            }
                            if (best && best.startsWith('http')) {
                                const isIg = best.includes('cdninstagram.com') || best.includes('fbcdn.net');
                                const w = img.naturalWidth || img.width || 0;
                                const h = img.naturalHeight || img.height || 0;
                                // Abaikan avatar profil & ikon kecil
                                if (isIg && !best.includes('s150x150') && !best.includes('s320x320') && (w >= 150 || h >= 150 || img.closest('ul') || img.closest('li') || img.classList.contains('_aagv'))) {
                                    add(best, 'image');
                                }
                            }
                        }

                        const videos = targetArticle.querySelectorAll('video');
                        for (const v of videos) {
                            const src = v.currentSrc || v.src;
                            if (src && src.startsWith('http')) {
                                add(src, 'video');
                            }
                        }
                    };

                    const findNextBtn = () => {
                        const direct = targetArticle.querySelector([
                            'button[aria-label="Next"]',
                            'button[aria-label="Selanjutnya"]',
                            'button[aria-label="Berikutnya"]',
                            'button[aria-label*="Next" i]',
                            'button[aria-label*="Selanjut" i]',
                            'button[aria-label*="Berikut" i]',
                            'div[role="button"][aria-label*="Next" i]',
                            'div[role="button"][aria-label*="Selanjut" i]'
                        ].join(','));
                        if (direct && !direct.disabled && direct.offsetParent !== null) return direct;

                        const btns = targetArticle.querySelectorAll('button, div[role="button"]');
                        for (const b of btns) {
                            if (b.disabled || b.offsetParent === null) continue;
                            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                            if (aria.includes('next') || aria.includes('selanjut') || aria.includes('berikut')) return b;
                            const svg = b.querySelector('svg');
                            if (svg) {
                                const svgAria = (svg.getAttribute('aria-label') || '').toLowerCase();
                                if (svgAria.includes('next') || svgAria.includes('selanjut') || svgAria.includes('berikut')) return b;
                            }
                        }
                        return null;
                    };

                    const findPrevBtn = () => {
                        const direct = targetArticle.querySelector([
                            'button[aria-label="Previous"]',
                            'button[aria-label="Sebelumnya"]',
                            'button[aria-label="Kembali"]',
                            'button[aria-label="Go back"]',
                            'button[aria-label*="Prev" i]',
                            'button[aria-label*="Sebelum" i]',
                            'button[aria-label*="Back" i]',
                            'div[role="button"][aria-label*="Prev" i]',
                            'div[role="button"][aria-label*="Sebelum" i]'
                        ].join(','));
                        if (direct && !direct.disabled && direct.offsetParent !== null) return direct;

                        const btns = targetArticle.querySelectorAll('button, div[role="button"]');
                        for (const b of btns) {
                            if (b.disabled || b.offsetParent === null) continue;
                            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                            if (aria.includes('prev') || aria.includes('sebelum') || aria.includes('kembali')) return b;
                            const svg = b.querySelector('svg');
                            if (svg) {
                                const svgAria = (svg.getAttribute('aria-label') || '').toLowerCase();
                                if (svgAria.includes('prev') || svgAria.includes('sebelum') || svgAria.includes('kembali')) return b;
                            }
                        }
                        return null;
                    };

                    // A. Jika pengguna berada di slide tengah, putar balik ke awal
                    let rewindInit = 0;
                    while (rewindInit < 20) {
                        const prev = findPrevBtn();
                        if (!prev) break;
                        prev.click();
                        rewindInit++;
                        await new Promise(r => setTimeout(r, 40));
                    }

                    // B. Panen slide pertama
                    harvestCurrentDom();

                    // C. Sapu maju ke seluruh slide (klik Next hingga habis)
                    let forwardCount = 0;
                    while (forwardCount < 25) {
                        const next = findNextBtn();
                        if (!next) break;
                        next.click();
                        forwardCount++;
                        await new Promise(r => setTimeout(r, 65));
                        harvestCurrentDom();
                    }

                    // D. Rewind kembali ke slide awal agar tampilan pengguna tetap rapi
                    for (let i = 0; i < forwardCount; i++) {
                        const prev = findPrevBtn();
                        if (!prev) break;
                        prev.click();
                        await new Promise(r => setTimeout(r, 25));
                    }

                    if (items.length > 0) return items;
                }

                return items;
            }
        });

        if (results && results[0] && Array.isArray(results[0].result)) {
            return results[0].result;
        }
    } catch (e) {
        console.warn('extractInstagramMedia error:', e);
    }
    return [];
}

function showStatus(type, message) {
    const el = $('action-status');
    if (!el) return;
    if (!type || !message) {
        el.style.display = 'none';
        el.className = 'action-status';
        el.textContent = '';
        return;
    }
    el.className = `action-status ${type}`;
    el.textContent = message;
    el.style.display = 'block';
}

function showCookieStatus(type, message) {
    const el = $('cookie-status');
    if (!el) return;
    if (!type || !message) {
        el.style.display = 'none';
        el.className = 'action-status';
        el.textContent = '';
        return;
    }
    el.className = `action-status ${type}`;
    el.textContent = message;
    el.style.display = 'block';
}

// === TABS / PILLS SWITCHING ===
function setupTypePills() {
    const pills = [$('pill-video'), $('pill-audio'), $('pill-sub'), $('pill-gallery')];
    pills.forEach(pill => {
        if (!pill) return;
        pill.onclick = () => {
            pills.forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            selectedMediaType = pill.dataset.type;

            const videoGroup = $('video-options-group');
            const audioGroup = $('audio-options-group');
            const subGroup = $('subtitle-options-group');
            const galleryGroup = $('gallery-options-group');

            if (selectedMediaType === 'video') {
                if (videoGroup) videoGroup.style.display = 'block';
                if (audioGroup) audioGroup.style.display = 'none';
                if (subGroup) subGroup.style.display = 'block';
                if (galleryGroup) galleryGroup.style.display = 'none';
            } else if (selectedMediaType === 'audio') {
                if (videoGroup) videoGroup.style.display = 'none';
                if (audioGroup) audioGroup.style.display = 'block';
                if (subGroup) subGroup.style.display = 'none';
                if (galleryGroup) galleryGroup.style.display = 'none';
            } else if (selectedMediaType === 'subtitle') {
                if (videoGroup) videoGroup.style.display = 'none';
                if (audioGroup) audioGroup.style.display = 'none';
                if (subGroup) subGroup.style.display = 'block';
                if (galleryGroup) galleryGroup.style.display = 'none';
            } else if (selectedMediaType === 'gallery') {
                if (videoGroup) videoGroup.style.display = 'none';
                if (audioGroup) audioGroup.style.display = 'none';
                if (subGroup) subGroup.style.display = 'none';
                if (galleryGroup) galleryGroup.style.display = 'block';
            }
        };
    });

    const galRadios = document.querySelectorAll('input[name="gallery-mode"]');
    galRadios.forEach(radio => {
        radio.addEventListener('change', () => {
            const slideBox = $('slide-selector-box');
            if (slideBox) {
                slideBox.style.display = ($('gal-mode-single')?.checked) ? 'block' : 'none';
            }
        });
    });
}

// === REFRESH DETECTED MEDIA & ACTIVE DOM SCAN ===
async function refreshCandidates(activeScan = false) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) currentTab = tab;
    if (!currentTab || !currentTab.id) return;

    const btnRefresh = $('btn-refresh');
    if (activeScan && btnRefresh) {
        btnRefresh.textContent = '⏳ Memindai…';
        btnRefresh.disabled = true;

        try {
            // Pertahankan kandidat streaming yang sudah tertangkap sniffer jaringan

            // Ekstrak shortcode Instagram jika ada untuk membatasi pemindaian hanya pada postingan ini
            const mShortcode = currentTab?.url?.match(/\/(?:p|reel|tv)\/([A-Za-z0-9_-]+)/);
            const igShortcode = mShortcode ? mShortcode[1] : null;

            // Jalankan DOM scanner pada halaman yang sedang aktif
            const results = await chrome.scripting.executeScript({
                target: { tabId: currentTab.id },
                args: [igShortcode],
                func: (targetShortcode) => {
                    const found = [];
                    const seen = new Set();
                    const add = (url, kind, label) => {
                        if (!url || typeof url !== 'string' || !url.startsWith('http')) return;
                        if (seen.has(url)) return;
                        seen.add(url);
                        found.push({ url, kind, statusLabel: label, title: document.title });
                    };

                    // 1. Tag video & audio langsung
                    document.querySelectorAll('video, audio').forEach(el => {
                        if (el.src) add(el.src, el.tagName.toLowerCase(), `${el.tagName.toUpperCase()} DOM`);
                        if (el.currentSrc && el.currentSrc !== el.src) add(el.currentSrc, el.tagName.toLowerCase(), `${el.tagName.toUpperCase()} Stream`);
                    });

                    // 2. Tag source di dalam player
                    document.querySelectorAll('source').forEach(el => {
                        if (el.src) {
                            const isAudio = (el.type || '').includes('audio');
                            add(el.src, isAudio ? 'audio' : 'video', isAudio ? 'Audio Source' : 'Video Source');
                        }
                    });

                    // 2b. Tag track (Subtitle) di dalam video player
                    document.querySelectorAll('track').forEach(el => {
                        const s = el.src || '';
                        if (s.startsWith('http')) {
                            const sLabel = el.label || (s.includes('/id/') ? 'Subtitle Indonesia' : 'Subtitle');
                            add(s, 'subtitle', sLabel);
                        }
                    });

                    // 3. Iframe video player embeds (Idlix, LK21, video players)
                    document.querySelectorAll('iframe').forEach(el => {
                        const s = el.src || '';
                        if (s.startsWith('http') && !s.includes('google') && !s.includes('facebook') && !s.includes('doubleclick') && !s.includes('analytics')) {
                            add(s, 'iframe', 'Player Embed');
                        }
                    });

                    // 4. Deteksi regex m3u8, mp4, player config JSON, dan subtitle langsung di dalam skrip / DOM halaman
                    try {
                        const html = document.documentElement.innerHTML || '';
                        
                        // A. Player config JSON (khusus Idlix / Majorplay: config-*.json)
                        const configMatches = html.match(/https?:\/\/[^"'\s<>]+\/config-[^"'\s<>]+\.json(\?[^"'\s<>]*)?/gi) || [];
                        configMatches.slice(0, 5).forEach(u => add(u, 'hls', 'Playlist Config (Idlix)'));

                        // B. Subtitles VTT & SRT
                        const subMatches = html.match(/https?:\/\/[^"'\s<>]+\.(vtt|srt)(\?[^"'\s<>]*)?/gi) || [];
                        subMatches.slice(0, 10).forEach(u => {
                            let subLabel = 'Subtitle';
                            if (u.includes('/id/') || u.includes('/indonesian') || u.includes('/indonesia')) subLabel = 'Subtitle Indonesia';
                            else if (u.includes('/en/') || u.includes('/english')) subLabel = 'Subtitle Inggris';
                            add(u, 'subtitle', subLabel);
                        });

                        // C. HLS m3u8
                        const m3u8Matches = html.match(/https?:\/\/[^"'\s<>]+\.m3u8(\?[^"'\s<>]*)?/gi) || [];
                        m3u8Matches.slice(0, 5).forEach(u => add(u, 'hls', 'Playlist HLS (Halaman)'));

                        // D. Video MP4
                        const mp4Matches = html.match(/https?:\/\/[^"'\s<>]+\.mp4(\?[^"'\s<>]*)?/gi) || [];
                        mp4Matches.slice(0, 5).forEach(u => add(u, 'video', 'Video MP4 (Halaman)'));
                    } catch (_) {}

                    // 5. Gambar / foto resolusi tinggi — strictly scoped ke postingan yang dituju jika di Instagram
                    try {
                        let targetScope = document;
                        if (targetShortcode) {
                            const dialog = document.querySelector('div[role="dialog"]');
                            if (dialog && (!targetShortcode || dialog.querySelector(`a[href*="${targetShortcode}"]`))) {
                                targetScope = dialog;
                            } else {
                                const articles = document.querySelectorAll('article');
                                for (const art of articles) {
                                    if (art.querySelector(`a[href*="${targetShortcode}"]`)) {
                                        targetScope = art;
                                        break;
                                    }
                                }
                            }
                            if (targetScope === document && window.location.pathname.includes('/p/')) {
                                targetScope = document.querySelector('main article') || document.querySelector('article') || document;
                            }
                        }

                        targetScope.querySelectorAll('img').forEach((el, idx) => {
                            let src = el.currentSrc || el.src;
                            if (el.srcset) {
                                const parts = el.srcset.split(',').map(s => s.trim());
                                if (parts.length > 0) {
                                    const best = parts[parts.length - 1].split(' ')[0];
                                    if (best && best.startsWith('http')) src = best;
                                }
                            }
                            if (!src || typeof src !== 'string' || !src.startsWith('http')) return;
                            const w = el.naturalWidth || el.width || 0;
                            const h = el.naturalHeight || el.height || 0;
                            const isIgCdn = src.includes('cdninstagram.com') || src.includes('fbcdn.net');
                            if ((w >= 200 && h >= 200) || (isIgCdn && !src.includes('s150x150') && !src.includes('s320x320'))) {
                                add(src, 'image', `Foto / Slide #${idx + 1}`);
                            }
                        });
                    } catch (_) {}

                    return found;
                }
            });

            if (results && results[0] && Array.isArray(results[0].result) && results[0].result.length > 0) {
                const domItems = results[0].result;
                await new Promise(r => chrome.runtime.sendMessage({
                    type: 'ADD_CANDIDATES',
                    tabId: currentTab.id,
                    items: domItems.map(x => ({ url: x.url, sourceType: 'dom' }))
                }, r));
            }

            // Jika situs sosial media, lakukan juga inspeksi metadata backend
            if (currentTab.url && isSocialMediaUrl(currentTab.url)) {
                inspectPageMetadata(currentTab.url);
            }
        } catch (scanErr) {
            console.warn('Gagal memindai DOM:', scanErr);
        }
    }

    const previousSelectedMedia = $('media-select')?.value;
    const previousSelectedSub = $('subtitle-select')?.value;

    return new Promise((resolve) => {
        chrome.runtime.sendMessage({ type: 'GET_CANDIDATES', tabId: currentTab.id }, (res) => {
            if (activeScan && btnRefresh) {
                btnRefresh.disabled = false;
            }

            if (chrome.runtime.lastError || !res || res.error) {
                if (activeScan && btnRefresh) {
                    btnRefresh.textContent = '🔍 Pindai Ulang Halaman';
                }
                resolve();
                return;
            }

            detectedItems = res.items || [];
            const mediaSelect = $('media-select');
            const subSelect = $('subtitle-select');
            mediaSelect.replaceChildren();
            subSelect.replaceChildren();

            const defaultMediaOpt = new Option('Pilih sumber video/stream…', '');
            mediaSelect.append(defaultMediaOpt);

            const defaultSubOpt = new Option('Tanpa subtitle tambahan', '__NONE__');
            subSelect.append(defaultSubOpt);

            const videoCandidates = detectedItems.filter(x => 
                x.kind !== 'subtitle' && 
                x.kind !== 'non_media' && 
                x.kind !== 'segment' && 
                !Detector.isAdOrAnalytics(x.url)
            );
            const subCandidates = detectedItems.filter(x => x.kind === 'subtitle');

            for (const item of videoCandidates) {
                const cleanTitle = getCleanDisplayTitle(item, currentTab?.title);
                const label = `[${item.statusLabel || item.kind}] ${cleanTitle}`;
                const opt = new Option(label, item.url);
                mediaSelect.append(opt);
            }

            for (const sub of subCandidates) {
                const label = `[${sub.statusLabel || 'Subtitle'}] ${sub.displayTitle || 'Sub'}`;
                const opt = new Option(label, sub.url);
                subSelect.append(opt);
            }

            // Pertahankan pilihan pengguna sebelumnya jika masih ada di daftar
            if (previousSelectedMedia && videoCandidates.some(x => x.url === previousSelectedMedia)) {
                mediaSelect.value = previousSelectedMedia;
            } else {
                // Pilih otomatis sumber terbaik
                const best = Detector.findBestMedia(videoCandidates);
                if (best) {
                    mediaSelect.value = best.url;
                    const matchedOpt = Array.from(mediaSelect.options).find(o => o.value === best.url);
                    if (matchedOpt) matchedOpt.selected = true;

                    const bestTitle = getCleanDisplayTitle(best, currentTab?.title);
                    $('media-title').textContent = bestTitle;
                    if ($('custom-name') && !$('custom-name').value) {
                        $('custom-name').placeholder = bestTitle;
                    }
                    $('media-platform-tag').textContent = best.statusLabel || 'Stream';
                } else if (mediaSelect.options.length > 1) {
                    mediaSelect.selectedIndex = 1;
                    const firstItem = videoCandidates[0];
                    if (firstItem) {
                        const firstTitle = getCleanDisplayTitle(firstItem, currentTab?.title);
                        $('media-title').textContent = firstTitle;
                        if ($('custom-name') && !$('custom-name').value) {
                            $('custom-name').placeholder = firstTitle;
                        }
                        $('media-platform-tag').textContent = firstItem.statusLabel || 'Stream';
                    }
                } else if (currentTab?.title) {
                    const fallbackTitle = cleanMovieTitle(currentTab.title);
                    $('media-title').textContent = fallbackTitle;
                    if ($('custom-name') && !$('custom-name').value) {
                        $('custom-name').placeholder = fallbackTitle;
                    }
                }
            }

            if (userExplicitNoSub) {
                subSelect.value = '__NONE__';
            } else if (previousSelectedSub && subCandidates.some(x => x.url === previousSelectedSub)) {
                subSelect.value = previousSelectedSub;
            } else {
                // Cari subtitle yang berkorelasi dengan sumber video terpilih
                const chosenMedia = $('media-select')?.value || currentTab?.url || '';
                const bestSub = Detector.findDefaultSubtitle(subCandidates, chosenMedia);
                if (bestSub) {
                    subSelect.value = bestSub.url;
                    const matchedSub = Array.from(subSelect.options).find(o => o.value === bestSub.url);
                    if (matchedSub) matchedSub.selected = true;
                } else if (subCandidates.length > 0) {
                    const idSub = subCandidates.find(s => s.statusLabel?.includes('Indonesia') || s.url?.includes('/id/')) || subCandidates[0];
                    if (idSub) {
                        subSelect.value = idSub.url;
                        const matchedSub = Array.from(subSelect.options).find(o => o.value === idSub.url);
                        if (matchedSub) matchedSub.selected = true;
                    }
                } else {
                    subSelect.value = '__NONE__';
                }
            }

            if (activeScan && btnRefresh) {
                const mediaCount = videoCandidates.length;
                btnRefresh.textContent = mediaCount > 0 ? `✅ Ditemukan ${mediaCount} Media` : 'ℹ️ Pemindaian Selesai';
                if (mediaCount > 0 && $('source-dropdown-group')) {
                    $('source-dropdown-group').style.display = 'block';
                }
                setTimeout(() => {
                    btnRefresh.textContent = '🔍 Pindai Ulang Halaman';
                }, 2500);
            }
            resolve();
        });
    });
}

// === METADATA INSPECTOR VIA API (Untuk YouTube, IG, TikTok, X, dll) ===
async function inspectPageMetadata(url) {
    if (!url) return;
    $('media-title').textContent = 'Menganalisis info media…';

    chrome.runtime.sendMessage({ type: 'INSPECT_URL', url: url }, (res) => {
        if (chrome.runtime.lastError || !res || !res.ok || !res.info) {
            // Fallback ke info tab
            $('media-title').textContent = currentTab?.title || 'Media Video';
            $('media-platform-tag').textContent = currentTab?.url?.includes('instagram.com') ? 'Instagram' : 'Halaman Web';
            if (currentTab?.id && currentTab?.url?.includes('instagram.com')) {
                extractInstagramMedia(currentTab.id, url).then(igItems => {
                    if (igItems && igItems.length > 0) {
                        currentExtractedCarousel = igItems;
                        $('media-platform-tag').textContent = `Instagram Carousel (${igItems.length} Slide)`;
                        if ($('pill-gallery')) $('pill-gallery').click();
                        populateSlideOptions(igItems.length);
                    }
                }).catch(() => {});
            }
            return;
        }

        const info = res.info;
        currentInspectInfo = info;
        if (info.title) $('media-title').textContent = info.title;
        if (info.thumbnail) $('media-thumb').src = info.thumbnail;

        if (info.duration) {
            $('media-duration-tag').style.display = 'inline-block';
            $('media-duration-tag').textContent = `⏱ ${info.duration}`;
        }

        if (info.is_instagram) {
            const initialCount = info.is_carousel ? (info.carousel_count || 0) : 0;
            $('media-platform-tag').textContent = initialCount > 0 ? `Instagram Carousel (${initialCount} Slide)` : 'Instagram';
            if (initialCount > 0 && $('pill-gallery')) {
                $('pill-gallery').click();
                populateSlideOptions(initialCount);
            }
            if (currentTab?.id) {
                extractInstagramMedia(currentTab.id, url).then(igItems => {
                    if (igItems && igItems.length > 0) {
                        currentExtractedCarousel = igItems;
                        const finalCount = Math.max(igItems.length, initialCount);
                        $('media-platform-tag').textContent = `Instagram Carousel (${finalCount} Slide)`;
                        if ($('pill-gallery')) $('pill-gallery').click();
                        populateSlideOptions(finalCount);
                    }
                }).catch(() => {});
            }
        } else {
            $('media-platform-tag').textContent = info.uploader || 'Online Media';
        }

        // Sesuaikan pilihan resolusi yang tersedia di server
        if (Array.isArray(info.formats) && info.formats.length > 0) {
            const qSelect = $('video-quality');
            qSelect.replaceChildren();
            qSelect.append(new Option('Kualitas Terbaik (Otomatis)', 'best'));
            for (const f of info.formats) {
                if (typeof f === 'number') {
                    qSelect.append(new Option(`${f}p HD`, String(f)));
                }
            }
        }
    });
}

// === JOB MONITOR UI UPDATE ===
function renderJobMonitor(job) {
    if (!job) {
        $('job-monitor-card').style.display = 'none';
        return;
    }

    lastKnownJob = job;
    currentActiveJobId = job.id;
    $('job-monitor-card').style.display = 'block';

    $('job-title-display').textContent = job.title || 'Unduhan Media';
    $('job-stage-display').textContent = job.stageLabel || 'Memproses…';

    const isLocalSaving = job.status === 'saving';
    const isCompleted = job.status === 'completed';
    const isFailed = job.status === 'failed' || job.downloadStatus === 'interrupted' || job.downloadStatus === 'failed';
    const isCancelled = job.status === 'cancelled';

    // Perhitungan persentase jujur
    if (isCompleted) {
        $('job-progress-fill').style.width = '100%';
        $('job-percent-text').textContent = '100%';
        $('job-speed-text').textContent = '';
        $('job-eta-text').textContent = '';
    } else if (isLocalSaving) {
        // Jangan klaim 100% jika masih menyimpan ke komputer
        const files = Array.isArray(job.files) ? job.files : [];
        const totalExpected = files.reduce((acc, f) => acc + (f.totalBytes || 0), 0);
        const totalReceived = files.reduce((acc, f) => acc + (f.bytesReceived || 0), 0);
        const allKnown = files.every(f => (f.totalBytes || 0) > 0);

        if (totalExpected > 0 && allKnown) {
            const localPct = Math.max(0, Math.min(99, Math.round((totalReceived / totalExpected) * 100)));
            $('job-progress-fill').style.width = `${localPct}%`;
            $('job-percent-text').textContent = `${localPct}% (Lokal)`;
            const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
            const mbTot = (totalExpected / (1024 * 1024)).toFixed(1);
            $('job-speed-text').textContent = `${mbRec}/${mbTot} MB`;
            $('job-eta-text').textContent = 'Menulis ke disk';
        } else if (totalReceived > 0) {
            $('job-progress-fill').style.width = '70%';
            $('job-percent-text').textContent = 'Menyimpan…';
            const mbRec = (totalReceived / (1024 * 1024)).toFixed(1);
            $('job-speed-text').textContent = `${mbRec} MB`;
            $('job-eta-text').textContent = 'Menulis ke disk';
        } else {
            // Belum ada data byte dari Chrome: tampilkan indikator transfer tanpa persentase rekaan
            $('job-progress-fill').style.width = '30%';
            $('job-percent-text').textContent = 'Menyimpan…';
            $('job-speed-text').textContent = files.length > 0 ? `${files.length} file` : '';
            $('job-eta-text').textContent = '';
        }
    } else if (isFailed) {
        const pct = Math.max(0, Math.min(100, Math.round(job.percent || 0)));
        $('job-progress-fill').style.width = `${pct}%`;
        $('job-percent-text').textContent = `${pct}% (Gagal)`;
        $('job-speed-text').textContent = '';
        $('job-eta-text').textContent = '';
    } else {
        // Progres server
        const pct = Math.max(0, Math.min(99, Math.round(job.percent || 0)));
        $('job-progress-fill').style.width = `${pct}%`;
        $('job-percent-text').textContent = `${pct}%`;
        $('job-speed-text').textContent = job.speed ? `Kecepatan: ${job.speed}` : '';
        $('job-eta-text').textContent = job.eta ? `ETA: ${job.eta}` : '';
    }

    const badge = $('job-badge');
    badge.className = 'meta-tag';

    if (isCompleted) {
        badge.textContent = 'Selesai';
        badge.style.background = '#052e16';
        badge.style.color = '#86efac';
        $('btn-job-stop').style.display = 'none';
        $('btn-job-retry').style.display = 'none';
    } else if (job.downloadStatus === 'needs_verification' || job.status === 'needs_verification') {
        badge.textContent = 'Periksa';
        badge.style.background = '#431407';
        badge.style.color = '#fdba74';
        $('btn-job-stop').style.display = 'none';
        $('btn-job-retry').style.display = 'block';
    } else if (isFailed) {
        badge.textContent = job.downloadStatus === 'interrupted' ? 'Terputus' : 'Gagal';
        badge.style.background = '#450a0a';
        badge.style.color = '#fca5a5';
        $('btn-job-stop').style.display = 'none';
        $('btn-job-retry').style.display = 'block';
    } else if (isCancelled) {
        badge.textContent = 'Dibatalkan';
        badge.style.background = '#27272a';
        badge.style.color = '#d4d4d8';
        $('btn-job-stop').style.display = 'none';
        $('btn-job-retry').style.display = 'block';
    } else if (isLocalSaving) {
        badge.textContent = 'Menyimpan';
        badge.style.background = '#1e1b4b';
        badge.style.color = '#a5b4fc';
        $('btn-job-stop').style.display = 'block';
        $('btn-job-retry').style.display = 'none';
    } else {
        badge.textContent = 'Berjalan';
        badge.style.background = '#3d0f19';
        badge.style.color = '#ff8fa3';
        $('btn-job-stop').style.display = 'block';
        $('btn-job-retry').style.display = 'none';
    }

    const isNeedsVerification = (job.downloadStatus === 'needs_verification' || job.status === 'needs_verification');
    const btnOpenDl = $('btn-job-open-downloads');
    if (btnOpenDl) {
        btnOpenDl.style.display = isNeedsVerification ? 'block' : 'none';
        btnOpenDl.onclick = () => {
            if (chrome.tabs && chrome.tabs.create) {
                chrome.tabs.create({ url: 'chrome://downloads' });
            }
        };
    }

    const btnRecheck = $('btn-job-recheck');
    if (btnRecheck) {
        btnRecheck.style.display = isNeedsVerification ? 'block' : 'none';
        btnRecheck.onclick = async () => {
            btnRecheck.disabled = true;
            btnRecheck.textContent = '⏳ Memeriksa…';
            try {
                chrome.runtime.sendMessage({ type: 'RECONCILE_JOB', jobId: job.id }, (res) => {
                    btnRecheck.disabled = false;
                    btnRecheck.textContent = '🔍 Periksa Ulang';
                    if (res && res.job) {
                        renderActiveJob(res.job);
                    }
                });
            } catch (_) {
                btnRecheck.disabled = false;
                btnRecheck.textContent = '🔍 Periksa Ulang';
            }
        };
    }

    // Tampilkan daftar status per-file jika ada
    const filesDisplay = $('job-files-display');
    if (filesDisplay) {
        const files = Array.isArray(job.files) ? job.files : [];
        if (files.length > 0) {
            filesDisplay.style.display = 'block';
            filesDisplay.replaceChildren();
            for (const f of files) {
                const itemDiv = document.createElement('div');
                itemDiv.style.display = 'flex';
                itemDiv.style.justifyContent = 'space-between';
                itemDiv.style.padding = '2px 0';
                itemDiv.style.borderTop = '1px solid #220810';

                const nameSpan = document.createElement('span');
                nameSpan.style.overflow = 'hidden';
                nameSpan.style.textOverflow = 'ellipsis';
                nameSpan.style.whiteSpace = 'nowrap';
                nameSpan.style.maxWidth = '210px';
                nameSpan.textContent = f.cleanName || f.name;

                const statusSpan = document.createElement('span');
                if (f.status === 'completed') {
                    statusSpan.style.color = '#86efac';
                    statusSpan.textContent = '✔ Tersimpan';
                } else if (f.status === 'needs_verification') {
                    statusSpan.style.color = '#fb923c';
                    statusSpan.textContent = '⚠️ Perlu Periksa';
                } else if (f.status === 'interrupted' || f.status === 'failed') {
                    statusSpan.style.color = '#fca5a5';
                    statusSpan.textContent = `✖ ${f.error || 'Gagal'}`;
                } else {
                    statusSpan.style.color = '#fde047';
                    statusSpan.textContent = '⏳ Mengunduh…';
                }

                itemDiv.append(nameSpan, statusSpan);
                filesDisplay.append(itemDiv);
            }
        } else {
            filesDisplay.style.display = 'none';
        }
    }
}

// === LOAD ACTIVE & RECENT JOBS ===
let dismissedJobId = null;

async function loadJobsState() {
    chrome.storage.local.get(['omni_dismissed_jobs'], (stored) => {
        const dismissedMap = stored?.omni_dismissed_jobs || {};
        chrome.runtime.sendMessage({ type: 'GET_ALL_JOBS' }, (data) => {
            if (!data || !data.jobs) return;
            const jobs = Object.values(data.jobs).sort((a, b) => b.createdAt - a.createdAt);
            
            // Prioritas 1: Job yang sedang aktif berjalan di latar belakang
            const runningJob = jobs.find(j => ['queued', 'downloading', 'validating', 'saving'].includes(j.status));
            // Prioritas 2: Job terbaru HANYA jika BELUM ditutup/dismissed oleh user
            const recentJob = jobs.find(j => !j.dismissed && !dismissedMap[j.id] && j.id !== dismissedJobId);
            const activeJob = runningJob || recentJob;

            if (activeJob) {
                renderJobMonitor(activeJob);
            } else {
                currentActiveJobId = null;
                $('job-monitor-card').style.display = 'none';
            }

        // Render riwayat ke accordion
        const historyContainer = $('history-list');
        if (jobs.length === 0) {
            historyContainer.replaceChildren();
            const emptyNotice = document.createElement('div');
            emptyNotice.style.color = '#666';
            emptyNotice.style.fontSize = '10px';
            emptyNotice.textContent = 'Belum ada riwayat unduhan.';
            historyContainer.append(emptyNotice);
            return;
        }

        historyContainer.replaceChildren();
        for (const j of jobs.slice(0, 10)) {
            const row = document.createElement('div');
            row.style.padding = '6px 0';
            row.style.borderBottom = '1px solid #220810';
            row.style.display = 'flex';
            row.style.justifyContent = 'space-between';
            row.style.alignItems = 'center';

            const left = document.createElement('div');
            left.style.minWidth = '0';
            left.style.flex = '1';

            const jTitle = document.createElement('div');
            jTitle.style.fontWeight = '600';
            jTitle.style.fontSize = '11px';
            jTitle.style.color = '#fff';
            jTitle.style.overflow = 'hidden';
            jTitle.style.textOverflow = 'ellipsis';
            jTitle.style.whiteSpace = 'nowrap';
            jTitle.textContent = j.title || 'Media';

            const jSub = document.createElement('div');
            jSub.style.fontSize = '10px';
            jSub.style.color = '#888';
            jSub.textContent = `${new Date(j.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} • ${j.status}`;

            left.append(jTitle, jSub);

            const btnInspect = document.createElement('button');
            btnInspect.className = 'btn-sm';
            btnInspect.style.flex = 'none';
            btnInspect.style.padding = '3px 8px';
            btnInspect.style.fontSize = '10px';
            btnInspect.textContent = 'Lihat';
            btnInspect.onclick = () => renderJobMonitor(j);

            row.append(left, btnInspect);
            historyContainer.append(row);
        }
    });
    });
}

// Listen to storage changes for live progress updates
chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.omni_jobs) {
        const jobs = changes.omni_jobs.newValue || {};
        chrome.storage.local.get(['omni_dismissed_jobs'], (res) => {
            const dismissedMap = res?.omni_dismissed_jobs || {};
            if (currentActiveJobId && jobs[currentActiveJobId]) {
                const cur = jobs[currentActiveJobId];
                if (!cur.dismissed && !dismissedMap[cur.id] && cur.id !== dismissedJobId) {
                    renderJobMonitor(cur);
                } else {
                    currentActiveJobId = null;
                    $('job-monitor-card').style.display = 'none';
                }
            } else {
                const active = Object.values(jobs).find(j => ['queued', 'downloading', 'validating', 'saving'].includes(j.status));
                if (active && !active.dismissed && !dismissedMap[active.id] && active.id !== dismissedJobId) {
                    renderJobMonitor(active);
                }
            }
        });
    }
});

// === MAIN DOWNLOAD TRIGGER ===
async function triggerDownload(isForced = false) {
    if (isSubmitting) return;

    const btn = $('btn-download-main');
    btn.disabled = true;
    isSubmitting = true;
    showStatus(null);
    const uncertainBox = $('uncertain-box');
    if (uncertainBox && !isForced) uncertainBox.style.display = 'none';

    try {
        // Tentukan URL target
        let targetUrl = '';
        let referer = currentTab?.url || '';
        let mediaTitle = $('custom-name')?.value?.trim() || $('media-title')?.textContent || 'Unduhan Media';

        const selectedDropdownUrl = $('media-select')?.value;
        const selectedCandidate = detectedItems.find(x => x.url === selectedDropdownUrl);

        if (selectedDropdownUrl && selectedCandidate) {
            targetUrl = selectedCandidate.extractedMediaUrl || selectedCandidate.url;
            if (selectedCandidate.referer) referer = selectedCandidate.referer;
        } else if (currentTab && currentTab.url) {
            targetUrl = currentTab.url;
        }

        if (!targetUrl) {
            showStatus('error', 'Tidak ada sumber media atau URL yang terdeteksi.');
            btn.disabled = false;
            isSubmitting = false;
            return;
        }

        // Jika sumber yang dipilih adalah gambar/foto langsung dari DOM atau URL gambar
        if (selectedCandidate && selectedCandidate.kind === 'image') {
            btn.querySelector('span').textContent = 'Menyimpan Foto…';
            const cleanFolder = sanitize(mediaTitle || 'Foto_Media');
            let ext = 'jpg';
            if (targetUrl.includes('.png')) ext = 'png';
            else if (targetUrl.includes('.webp')) ext = 'webp';
            const filename = `OmniFetch/${cleanFolder}/${cleanFolder}_${Date.now()}.${ext}`;

            chrome.downloads.download({
                url: targetUrl,
                filename: filename,
                conflictAction: 'uniquify',
                saveAs: false
            }, (downloadId) => {
                btn.disabled = false;
                isSubmitting = false;
                btn.querySelector('span').textContent = 'Unduh Sekarang';
                if (chrome.runtime.lastError || !downloadId) {
                    showStatus('error', `Gagal menyimpan foto: ${chrome.runtime.lastError?.message || 'Download ditolak browser'}`);
                } else {
                    showStatus('success', `Foto berhasil diunduh ke folder ${filename}!`);
                }
            });
            return;
        }

        // Susun payload sesuai kemampuan backend
        const payload = {
            urls: targetUrl,
            referer: referer,
            media_type: selectedMediaType === 'audio' ? 'audio' : 'video'
        };

        if (mediaTitle) payload.name = sanitize(mediaTitle);

        if (selectedMediaType === 'video') {
            payload.resolution = $('video-quality')?.value || 'best';
            payload.video_format = 'mp4';

            const subUrl = $('subtitle-select')?.value;
            if (subUrl && subUrl !== '__NONE__') {
                payload.subtitle_url = subUrl;
                payload.embed_subs = $('embed-subs')?.checked ?? true;
            }
        } else if (selectedMediaType === 'audio') {
            payload.audio_format = $('audio-format')?.value || 'mp3';
            payload.audio_quality = '0';
        } else if (selectedMediaType === 'subtitle') {
            payload.mode = 'subtitle';
            const subUrl = $('subtitle-select')?.value;
            if (subUrl && subUrl !== '__NONE__') payload.urls = subUrl;
        } else if (selectedMediaType === 'gallery') {
            payload.media_type = 'video';
            payload.resolution = 'best';
            payload.video_format = 'mp4';
            const isSingle = $('gal-mode-single')?.checked;
            const isFiles = $('gal-mode-files')?.checked;
            if (isSingle) {
                payload.prefer_zip = false;
                payload.slide_index = parseInt($('slide-number-select')?.value || '1', 10);
            } else if (isFiles) {
                payload.prefer_zip = false;
            } else {
                payload.prefer_zip = true;
            }

            // Jika berada di Instagram, ekstrak media langsung dari halaman browser agar tidak gagal di yt-dlp
            if (currentTab?.id && (currentTab?.url?.includes('instagram.com/p/') || currentTab?.url?.includes('instagram.com/reel/'))) {
                btn.querySelector('span').textContent = 'Mengekstrak Slide Instagram…';
                showStatus('loading', 'Membaca seluruh slide dari postingan Instagram…');
                let igItems = (currentExtractedCarousel && currentExtractedCarousel.length > 0) ? currentExtractedCarousel : await extractInstagramMedia(currentTab.id, targetUrl);
                if (!igItems || igItems.length === 0) {
                    igItems = await extractInstagramMedia(currentTab.id, targetUrl);
                }
                const serverCount = currentInspectInfo?.carousel_count || 0;
                // Jika server terhubung dan mendeteksi lebih banyak slide daripada browser untuk mode ZIP, serahkan ke server seperti di aplikasi utama
                const delegateToServer = !isSingle && isServerConnected && serverCount > (igItems ? igItems.length : 0);

                if (igItems && igItems.length > 0 && !delegateToServer) {
                    currentExtractedCarousel = igItems;
                    const cleanFolder = sanitize(mediaTitle || 'Instagram_Carousel');

                    if (isSingle) {
                        const slideNum = parseInt($('slide-number-select')?.value || '1', 10);
                        const targetIdx = Math.max(0, Math.min(slideNum - 1, igItems.length - 1));
                        const targetItem = igItems[targetIdx];
                        const ext = targetItem.type === 'video' ? 'mp4' : 'jpg';
                        const filename = `OmniFetch/${cleanFolder}/${cleanFolder}_Slide_${targetIdx + 1}.${ext}`;

                        btn.querySelector('span').textContent = `Menyimpan Slide ${targetIdx + 1}…`;
                        chrome.downloads.download({
                            url: targetItem.url,
                            filename: filename,
                            conflictAction: 'uniquify',
                            saveAs: false
                        }, (dlId) => {
                            btn.disabled = false;
                            isSubmitting = false;
                            btn.querySelector('span').textContent = 'Unduh Sekarang';
                            if (chrome.runtime.lastError || !dlId) {
                                showStatus('error', `Gagal menyimpan: ${chrome.runtime.lastError?.message || 'Download ditolak'}`);
                            } else {
                                showStatus('success', `✅ Slide ${targetIdx + 1} berhasil disimpan ke folder ${filename}!`);
                            }
                        });
                        return;
                    } else if (isFiles) {
                        btn.querySelector('span').textContent = `Menyimpan ${igItems.length} Slide…`;
                        let startedCount = 0;
                        for (let i = 0; i < igItems.length; i++) {
                            const it = igItems[i];
                            const ext = it.type === 'video' ? 'mp4' : 'jpg';
                            const filename = `OmniFetch/${cleanFolder}/${cleanFolder}_Slide_${i + 1}.${ext}`;
                            chrome.downloads.download({
                                url: it.url,
                                filename: filename,
                                conflictAction: 'uniquify',
                                saveAs: false
                            }, () => {
                                startedCount++;
                                if (startedCount === igItems.length) {
                                    btn.disabled = false;
                                    isSubmitting = false;
                                    btn.querySelector('span').textContent = 'Unduh Sekarang';
                                    showStatus('success', `✅ Semua ${igItems.length} slide berhasil disimpan ke folder OmniFetch/${cleanFolder}/!`);
                                }
                            });
                        }
                        return;
                    } else {
                        // Paket .ZIP
                        btn.querySelector('span').textContent = 'Menyiapkan paket ZIP…';
                        showStatus('loading', `Mengunduh ${igItems.length} slide untuk arsip .ZIP…`);
                        try {
                            const zipFiles = [];
                            for (let i = 0; i < igItems.length; i++) {
                                const it = igItems[i];
                                btn.querySelector('span').textContent = `Mengambil Slide ${i + 1}/${igItems.length}…`;
                                const resp = await fetch(it.url);
                                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                                const buf = await resp.arrayBuffer();
                                const ext = it.type === 'video' ? 'mp4' : 'jpg';
                                zipFiles.push({
                                    name: `${cleanFolder}_Slide_${i + 1}.${ext}`,
                                    data: new Uint8Array(buf)
                                });
                            }
                            btn.querySelector('span').textContent = 'Membuat file .ZIP…';
                            const zipBlob = createZipBlob(zipFiles);
                            const zipUrl = URL.createObjectURL(zipBlob);
                            const zipFilename = `OmniFetch/${cleanFolder}/${cleanFolder}.zip`;

                            chrome.downloads.download({
                                url: zipUrl,
                                filename: zipFilename,
                                conflictAction: 'uniquify',
                                saveAs: false
                            }, (dlId) => {
                                btn.disabled = false;
                                isSubmitting = false;
                                btn.querySelector('span').textContent = 'Unduh Sekarang';
                                if (chrome.runtime.lastError || !dlId) {
                                    showStatus('error', `Gagal membuat ZIP: ${chrome.runtime.lastError?.message || 'Download ditolak'}`);
                                } else {
                                    showStatus('success', `✅ Paket ZIP (${igItems.length} slide) berhasil diunduh ke ${zipFilename}!`);
                                }
                            });
                            return;
                        } catch (err) {
                            console.warn('Gagal membuat ZIP, mengunduh file terpisah:', err);
                            for (let i = 0; i < igItems.length; i++) {
                                const it = igItems[i];
                                const ext = it.type === 'video' ? 'mp4' : 'jpg';
                                chrome.downloads.download({
                                    url: it.url,
                                    filename: `OmniFetch/${cleanFolder}/${cleanFolder}_Slide_${i + 1}.${ext}`,
                                    conflictAction: 'uniquify'
                                });
                            }
                            btn.disabled = false;
                            isSubmitting = false;
                            btn.querySelector('span').textContent = 'Unduh Sekarang';
                            showStatus('success', `✅ Disimpan sebagai ${igItems.length} file terpisah di OmniFetch/${cleanFolder}/.`);
                            return;
                        }
                    }
                }
            }
        }

        // Pilihan arsip ZIP atau file individual untuk tipe non-gallery
        if (selectedMediaType !== 'gallery') {
            payload.prefer_zip = $('prefer-zip')?.checked ?? false;
        }

        // Waktu potong jika ada
        const startTime = $('time-start')?.value?.trim();
        const endTime = $('time-end')?.value?.trim();
        if (startTime) payload.start_time = startTime;
        if (endTime) payload.end_time = endTime;

        // Preset cerdas
        const preset = $('preset-select')?.value;
        if (preset) payload.preset = preset;

        // Tentukan mode direct untuk streaming m3u8/mpd
        if (selectedCandidate && ['hls', 'dash'].includes(selectedCandidate.kind)) {
            payload.mode = 'direct';
        }

        btn.querySelector('span').textContent = 'Mengirim ke Server…';

        chrome.runtime.sendMessage({
            type: 'SEND_JOB',
            payload: payload,
            title: mediaTitle,
            force: !!isForced
        }, (res) => {
            btn.disabled = false;
            isSubmitting = false;
            btn.querySelector('span').textContent = 'Unduh Sekarang';

            if (chrome.runtime.lastError || !res || !res.ok) {
                const errMsg = res?.error || chrome.runtime.lastError?.message || 'Gagal mengirim tugas.';
                showStatus('error', `Gagal: ${errMsg}`);
                if (res?.uncertain && uncertainBox) {
                    uncertainBox.style.display = 'block';
                    const btnForce = $('btn-force-resubmit');
                    if (btnForce) {
                        btnForce.onclick = () => triggerDownload(true);
                    }
                }
            } else {
                if (uncertainBox) uncertainBox.style.display = 'none';
                showStatus('success', '✅ Pekerjaan berhasil dikirim ke server!');
                currentActiveJobId = res.data.job_id;
                loadJobsState();
            }
        });

    } catch (err) {
        btn.disabled = false;
        isSubmitting = false;
        btn.querySelector('span').textContent = 'Unduh Sekarang';
        showStatus('error', err.message);
    }
}

// === BUTTON EVENT HANDLERS ===
if ($('btn-dismiss-job')) {
    $('btn-dismiss-job').onclick = () => {
        const jobToDismiss = currentActiveJobId || lastKnownJob?.id;
        dismissedJobId = jobToDismiss;
        currentActiveJobId = null;
        lastKnownJob = null;
        $('job-monitor-card').style.display = 'none';

        if (jobToDismiss) {
            chrome.runtime.sendMessage({ type: 'DISMISS_JOB', jobId: jobToDismiss });
            chrome.storage.local.get(['omni_dismissed_jobs'], (res) => {
                const d = res?.omni_dismissed_jobs || {};
                d[jobToDismiss] = Date.now();
                chrome.storage.local.set({ omni_dismissed_jobs: d, omni_active_job_id: null });
            });
        }
    };
}

$('btn-download-main').onclick = () => triggerDownload(false);
$('btn-refresh').onclick = async () => {
    await refreshCandidates(true);
    loadJobsState();
};

if ($('media-select')) {
    $('media-select').onchange = (e) => {
        const val = e.target.value;
        const item = detectedItems.find(x => x.url === val);
        if (item) {
            const cleanTitle = getCleanDisplayTitle(item, currentTab?.title);
            $('media-title').textContent = cleanTitle;
            if ($('custom-name') && !$('custom-name').value) {
                $('custom-name').placeholder = cleanTitle;
            }
            $('media-platform-tag').textContent = item.statusLabel || 'Stream';
        }
        if (!userExplicitNoSub) {
            const subCandidates = detectedItems.filter(x => x.kind === 'subtitle');
            const chosenMedia = val || currentTab?.url || '';
            const bestSub = Detector.findDefaultSubtitle(subCandidates, chosenMedia);
            const subSelect = $('subtitle-select');
            if (subSelect) {
                subSelect.value = bestSub ? bestSub.url : '__NONE__';
            }
        }
    };
}

if ($('subtitle-select')) {
    $('subtitle-select').onchange = (e) => {
        userExplicitNoSub = (e.target.value === '__NONE__' || e.target.value === '');
    };
}

$('btn-job-stop').onclick = () => {
    if (!currentActiveJobId) return;
    const btn = $('btn-job-stop');
    if (btn.disabled) return;
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '⏳ Menghentikan…';
    showStatus('loading', 'Mengirim perintah penghentian ke server…');

    chrome.runtime.sendMessage({ type: 'STOP_JOB', jobId: currentActiveJobId }, (res) => {
        btn.disabled = false;
        btn.textContent = origText;
        if (chrome.runtime.lastError || !res || !res.ok) {
            const err = res?.error || chrome.runtime.lastError?.message || 'Gagal menghentikan tugas.';
            showStatus('error', `Gagal menghentikan: ${err}`);
        } else {
            showStatus('success', '⏹️ Permintaan stop terkonfirmasi.');
        }
        loadJobsState();
    });
};

$('btn-job-retry').onclick = () => {
    if (!currentActiveJobId) return;
    const btn = $('btn-job-retry');
    if (btn.disabled) return;
    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '⏳ Mencoba lagi…';
    showStatus('loading', 'Mengulang proses unduhan…');

    chrome.runtime.sendMessage({ type: 'RETRY_JOB', jobId: currentActiveJobId }, (res) => {
        btn.disabled = false;
        btn.textContent = origText;
        if (chrome.runtime.lastError || !res || !res.ok) {
            const err = res?.error || chrome.runtime.lastError?.message || 'Gagal mengulang unduhan.';
            showStatus('error', `Gagal coba lagi: ${err}`);
        } else {
            if (res.mode === 'local_files') {
                showStatus('success', '🔄 Mengunduh ulang file lokal yang terputus…');
            } else if (res.mode === 'new_server_job') {
                showStatus('success', '🔄 Tugas baru telah dikirim ke server!');
                if (res.newJobId) currentActiveJobId = res.newJobId;
            }
            loadJobsState();
        }
    });
};

$('btn-job-diag').onclick = () => {
    if (!currentActiveJobId) return;
    chrome.runtime.sendMessage({ type: 'GET_DIAGNOSTIC', jobId: currentActiveJobId }, async (res) => {
        if (res && res.report) {
            try {
                await navigator.clipboard.writeText(res.report);
                const btn = $('btn-job-diag');
                const orig = btn.textContent;
                btn.textContent = '✅ Disalin!';
                setTimeout(() => { btn.textContent = orig; }, 2000);
            } catch(e) {
                alert(res.report);
            }
        }
    });
};

if ($('btn-copy-media')) {
    $('btn-copy-media').onclick = async () => {
        const val = $('media-select')?.value;
        if (!val) {
            showStatus('error', '⚠️ Belum ada sumber stream/playlist yang dipilih.');
            return;
        }
        try {
            await navigator.clipboard.writeText(val);
            const btn = $('btn-copy-media');
            const orig = btn.textContent;
            btn.textContent = '✅ Link Playlist Disalin!';
            showStatus('success', '📋 Link stream/playlist berhasil disalin ke clipboard.');
            setTimeout(() => { btn.textContent = orig; }, 2000);
        } catch(e) {
            showStatus('error', '❌ Gagal menyalin: ' + e.message);
        }
    };
}

if ($('btn-copy-sub')) {
    $('btn-copy-sub').onclick = async () => {
        const val = $('subtitle-select')?.value;
        if (!val || val === '__NONE__') {
            showStatus('error', '⚠️ Belum ada subtitle yang dipilih.');
            return;
        }
        try {
            await navigator.clipboard.writeText(val);
            const btn = $('btn-copy-sub');
            const orig = btn.textContent;
            btn.textContent = '✅ Link Subtitle Disalin!';
            showStatus('success', '📋 Link subtitle berhasil disalin ke clipboard.');
            setTimeout(() => { btn.textContent = orig; }, 2000);
        } catch(e) {
            showStatus('error', '❌ Gagal menyalin: ' + e.message);
        }
    };
}

$('btn-preview-media').onclick = () => {
    const val = $('media-select')?.value;
    const item = detectedItems.find(x => x.url === val);
    const modal = $('preview-modal');
    const content = $('preview-modal-content');
    const title = $('preview-modal-title');

    modal.style.display = 'block';
    title.textContent = item?.displayTitle || 'Cuplikan Playlist';
    content.textContent = item?.previewSnippet || val || 'Belum ada cuplikan respons teks yang tersimpan.';
};

$('btn-close-preview').onclick = () => {
    $('preview-modal').style.display = 'none';
};

$('btn-copy-preview').onclick = async () => {
    const text = $('preview-modal-content')?.textContent;
    if (text) {
        await navigator.clipboard.writeText(text);
        $('btn-copy-preview').textContent = '✅ Cuplikan Berhasil Disalin!';
        setTimeout(() => { $('btn-copy-preview').textContent = '📋 Salin Seluruh Cuplikan'; }, 2000);
    }
};

$('btn-deep-dom').onclick = async () => {
    const btn = $('btn-deep-dom');
    btn.textContent = 'Memindai DOM…';
    btn.disabled = true;

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: currentTab.id },
            func: () => {
                const cands = [];
                document.querySelectorAll('video, audio, source, track').forEach(el => {
                    const src = el.currentSrc || el.src;
                    if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
                        let kind = el.tagName.toLowerCase();
                        if (kind === 'source') kind = el.closest('video') ? 'video' : 'audio';
                        if (kind === 'track') kind = 'subtitle';
                        cands.push({ url: src, kind: kind, time: Date.now(), sourceType: 'dom' });
                    }
                });
                return cands;
            }
        });

        if (results && results[0]?.result?.length > 0) {
            chrome.runtime.sendMessage({
                type: 'ADD_CANDIDATES',
                tabId: currentTab.id,
                items: results[0].result
            }, () => {
                refreshCandidates();
                showStatus('success', `✅ ${results[0].result.length} media baru ditemukan via DOM!`);
            });
        } else {
            showStatus('loading', 'Tidak ada elemen video/audio langsung di DOM. Putar pemutar web untuk menangkap streaming.');
        }
    } catch (e) {
        showStatus('error', 'Gagal analisis DOM: ' + e.message);
    } finally {
        btn.textContent = '🔍 Analisis Mendalam DOM Video';
        btn.disabled = false;
    }
};

$('btn-sync-cookies').onclick = () => {
    if (!currentTab || !currentTab.url) return;
    try {
        const domain = new URL(currentTab.url).hostname;
        showCookieStatus('loading', `Mengirim cookie untuk ${domain}…`);
        chrome.runtime.sendMessage({ type: 'SYNC_COOKIES', domains: [domain] }, (res) => {
            if (res && res.ok) {
                showCookieStatus('success', `✅ Cookie untuk ${domain} berhasil disinkronkan.`);
            } else {
                showCookieStatus('error', res?.error || 'Gagal sinkronisasi cookie.');
            }
        });
    } catch(e) {}
};

$('btn-open-options').onclick = () => chrome.runtime.openOptionsPage();
$('link-dashboard').onclick = async (e) => {
    e.preventDefault();
    const info = await Config.getAppInfo();
    chrome.tabs.create({ url: info.url });
};

// === INITIALIZATION ===
(async () => {
    setupTypePills();

    // Pastikan session token siap
    await Config.ensureSessionToken();

    // Tampilkan status & mode server aktif
    const info = await Config.getAppInfo();
    const serverBadge = $('server-badge');
    if (serverBadge) {
        if (info.serverMode === 'local') {
            serverBadge.textContent = '💻 Lokal';
            serverBadge.style.background = '#082813';
            serverBadge.style.color = '#86efac';
            serverBadge.style.border = '1px solid #166534';
            serverBadge.title = `Server Lokal Aktif: ${info.url} (Klik untuk pengaturan)`;
        } else if (info.serverMode === 'custom') {
            serverBadge.textContent = '⚙️ Kustom';
            serverBadge.style.background = '#201a08';
            serverBadge.style.color = '#fde047';
            serverBadge.style.border = '1px solid #78350f';
            serverBadge.title = `Server Kustom: ${info.url}`;
        } else {
            serverBadge.textContent = '🌐 Cloud';
            serverBadge.style.background = '#2a0b12';
            serverBadge.style.color = '#ff8fa3';
            serverBadge.style.border = '1px solid #4a1320';
            serverBadge.title = `Server Produksi: ${info.url}`;
        }
        serverBadge.onclick = () => chrome.runtime.openOptionsPage();
    }

    // Query tab aktif
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    currentTab = tab;

    if (currentTab && currentTab.url) {
        let host = '';
        try { host = new URL(currentTab.url).hostname.toLowerCase(); } catch(e) {}
        const isSocial = SOCIAL_DOMAINS.some(d => host.endsWith(d));

        if (isSocial) {
            // Sembunyikan dropdown streaming mentah untuk sosial media agar bersih
            $('source-dropdown-group').style.display = 'none';
            inspectPageMetadata(currentTab.url);
        } else {
            $('source-dropdown-group').style.display = 'block';
            await refreshCandidates(false);
            if (!detectedItems || detectedItems.length === 0) {
                await refreshCandidates(true);
            }
        }
    }

    // Periksa status server
    if (info.token) {
        chrome.runtime.sendMessage({ type: 'CHECK_SESSION', token: info.token }, (res) => {
            if (res && res.ok) {
                $('conn-dot').className = 'status-dot';
                $('conn-dot').title = `Terhubung & Sesi Valid (${info.url})`;
            } else {
                $('conn-dot').className = 'status-dot offline';
                $('conn-dot').title = `Server belum terhubung (${info.url}). Klik badge Server untuk beralih ke Lokal/Pengaturan.`;
            }
        });
    }

    loadJobsState();
})();
