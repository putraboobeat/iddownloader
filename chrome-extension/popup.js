import { BASE_URL } from './background/config.js';

const $ = id => document.getElementById(id);
let currentTab = null;
let items = [];
let isSending = false;

const SOCIAL_DOMAINS = ['youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com'];

function setStatus(containerId, type, messageHtml) {
    const el = $(containerId);
    if (!el) return;
    if (!type || !messageHtml) {
        el.style.display = 'none';
        el.className = 'action-status';
        el.innerHTML = '';
        return;
    }
    el.className = `action-status ${type}`;
    el.innerHTML = messageHtml;
    el.style.display = 'block';
}

function label(item) {
    const status = item.statusLabel || (item.kind === 'hls' ? 'Playlist HLS' : item.kind === 'dash' ? 'Manifest DASH' : item.kind === 'video' ? 'Video langsung' : item.kind === 'non_media' ? 'Bukan media' : 'Belum diverifikasi');
    return `[${status}] ${item.displayTitle || 'Media'}`;
}

async function refresh() {
    if (!currentTab || !currentTab.id) return;
    setStatus('sniffer-status', null);
    
    chrome.runtime.sendMessage({ type: 'GET_CANDIDATES', tabId: currentTab.id }, (res) => {
        if (chrome.runtime.lastError) {
            setStatus('sniffer-status', 'error', 'Error runtime: ' + chrome.runtime.lastError.message);
            return;
        }
        if (!res || res.error) {
            setStatus('sniffer-status', 'error', 'Gagal memuat kandidat: ' + (res?.error || 'Tidak ada respons'));
            return;
        }
        
        items = res.items || [];
        
        // Prioritaskan media terverifikasi: HLS & DASH (4) -> Video (3) -> Unverified (2) -> Subtitle (1) -> Non-media (0)
        items.sort((a, b) => {
            const score = x => {
                if (['hls', 'dash'].includes(x.kind)) return 4;
                if (x.kind === 'video') return 3;
                if (x.kind === 'unverified') return 2;
                if (x.kind === 'subtitle') return 1;
                return 0;
            };
            return score(b) - score(a) || (b.time - a.time);
        });
        
        $('media').replaceChildren(new Option('Pilih sumber video…', ''));
        $('subtitle').replaceChildren(new Option('Tanpa subtitle tambahan', ''));
        
        for (const item of items) {
            const opt = new Option(label(item), item.url);
            $(item.kind === 'subtitle' ? 'subtitle' : 'media').append(opt);
        }
        
        // Cari media yang benar-benar valid terlebih dahulu
        const best = items.find(x => x.kind !== 'subtitle' && x.isMedia);
        if (best) {
            $('media').value = best.url;
            onMediaSelected();
            setStatus('sniffer-status', 'success', `🎬 Terdeteksi ${best.statusLabel}: siap dikirim ke server.`);
        } else {
            const fallback = items.find(x => x.kind !== 'subtitle');
            if (fallback) {
                $('media').value = fallback.url;
                onMediaSelected();
            } else {
                $('send').disabled = true;
                setStatus('sniffer-status', null);
            }
        }
    });
}

function onMediaSelected() {
    const selectedUrl = $('media').value;
    const selected = items.find(x => x.url === selectedUrl);
    
    if (!selected) {
        $('send').disabled = true;
        $('send').textContent = '⬇ Kirim Playlist ke Server';
        return;
    }

    if (selected.kind === 'non_media') {
        $('send').disabled = true;
        $('send').textContent = '⚠️ Bukan Sumber Media';
        setStatus('sniffer-status', 'error', '❌ Terdeteksi respons klaim/analitik non-media. Tombol unduh dinonaktifkan.');
    } else if (!selected.isMedia) {
        // Belum diverifikasi (misal JSON player config)
        $('send').disabled = false;
        $('send').textContent = '⬇ Kirim Playlist ke Server';
        setStatus('sniffer-status', 'loading', '⚠️ Sumber config ini belum diverifikasi otomatis. Anda dapat mencoba mengirimkannya atau gunakan "Analisis Mendalam DOM".');
    } else {
        $('send').disabled = false;
        $('send').textContent = '⬇ Kirim Playlist ke Server';
        setStatus('sniffer-status', 'success', `🎬 ${selected.statusLabel} siap dikirim ke server.`);
    }
}

$('media').onchange = onMediaSelected;

function sendToBackground(payload, btnElement, statusContainerId, successText = '✅ Terkirim', originalText = 'Unduh') {
    if (isSending) return;
    isSending = true;

    if (btnElement) {
        btnElement.innerHTML = '⏳ Mengirim ke dw.pmlab.id…';
        btnElement.disabled = true;
    }
    
    setStatus(statusContainerId, 'loading', 'Mengirim pekerjaan ke server dw.pmlab.id… Mohon tunggu.');

    chrome.runtime.sendMessage({ type: 'SEND_JOB', payload }, (res) => {
        isSending = false;
        
        if (chrome.runtime.lastError || (res && res.error)) {
            const errMsg = res?.error || chrome.runtime.lastError?.message || 'Gagal mengirim tugas.';
            let actionHtml = '';

            // Jika sesi hilang atau kedaluwarsa, sediakan tombol buka pengaturan langsung
            if (res?.code === 'AUTH_MISSING' || res?.code === 'AUTH_EXPIRED' || errMsg.includes('Sesi belum terhubung')) {
                actionHtml = `<div style="margin-top:6px;"><button id="btn-fix-auth" style="background:#ff3d58;color:#fff;border:none;border-radius:4px;padding:3px 8px;font-size:10px;cursor:pointer;">⚙️ Atur Session Token Sekarang</button></div>`;
            }

            setStatus(statusContainerId, 'error', `❌ <strong>Gagal:</strong> ${errMsg}${actionHtml}`);
            
            if ($('btn-fix-auth')) {
                $('btn-fix-auth').onclick = () => chrome.runtime.openOptionsPage();
            }

            if (btnElement) {
                btnElement.innerHTML = '❌ Gagal Terkirim';
                btnElement.disabled = false;
                setTimeout(() => {
                    btnElement.innerHTML = originalText;
                    onMediaSelected();
                }, 3000);
            }
        } else if (res && res.data && res.data.job_id) {
            // Kontrak pekerjaan diterima sesuai spesifikasi backend
            const jobId = res.data.job_id;
            const successHtml = `
                ✅ <strong>Pekerjaan Diterima Server!</strong><br>
                Job ID: <code style="font-size:10px;color:#cbd5e1;">${jobId}</code><br>
                <a href="#" id="view-job-btn" style="color:#4ade80;text-decoration:underline;font-weight:bold;margin-top:4px;display:inline-block;">👉 Buka Dashboard untuk Memantau</a>
            `;
            
            setStatus(statusContainerId, 'success', successHtml);

            if ($('view-job-btn')) {
                $('view-job-btn').onclick = (e) => {
                    e.preventDefault();
                    chrome.tabs.create({ url: BASE_URL });
                };
            }

            if (btnElement) {
                btnElement.innerHTML = successText;
                btnElement.style.background = '#15803d';
                setTimeout(() => {
                    btnElement.innerHTML = originalText;
                    btnElement.style.background = '';
                    btnElement.disabled = false;
                    onMediaSelected();
                }, 4000);
            }
        } else {
            setStatus(statusContainerId, 'error', '❌ Format respons server tidak sesuai kontrak pekerjaan.');
            if (btnElement) {
                btnElement.innerHTML = '❌ Gagal';
                btnElement.disabled = false;
                setTimeout(() => { btnElement.innerHTML = originalText; onMediaSelected(); }, 3000);
            }
        }
    });
}

// Initializer
(async () => {
    [currentTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!currentTab || !currentTab.url) return;

    let host = '';
    try {
        host = new URL(currentTab.url).hostname;
    } catch(e) {}

    const isSocial = SOCIAL_DOMAINS.some(d => host.endsWith(d));
    
    // Cek permissions situs asal
    try {
        const hasAllPerm = await chrome.permissions.contains({ origins: ['<all_urls>'] });
        const origin = new URL(currentTab.url).origin + '/*';
        const hasOriginPerm = await chrome.permissions.contains({ origins: [origin] });
        if ((hasAllPerm || hasOriginPerm) && $('request-permission')) {
            $('request-permission').style.display = 'none';
        }
    } catch(e) {}

    if (isSocial) {
        $('direct-dl-section').style.display = 'block';
        $('sniffer-section').style.display = 'none';
        $('current-url').textContent = currentTab.url.length > 55 ? currentTab.url.substring(0, 52) + '...' : currentTab.url;
    } else {
        $('direct-dl-section').style.display = 'none';
        $('sniffer-section').style.display = 'block';
        refresh();
    }
    
    $('open-app').onclick = (e) => {
        e.preventDefault();
        chrome.tabs.create({ url: BASE_URL });
    };
    
    $('current-version').textContent = chrome.runtime.getManifest().version;
    
    $('sync-cookies').onclick = () => {
        const btn = $('sync-cookies');
        btn.textContent = '⏳ Mengunggah cookie…';
        btn.disabled = true;
        setStatus('sync-status', 'loading', 'Menyinkronkan cookie ke dw.pmlab.id…');
        
        chrome.runtime.sendMessage({ type: 'SYNC_COOKIES' }, (res) => {
            btn.disabled = false;
            btn.textContent = 'Paksa Sinkronisasi Cookie';
            if (res && res.ok) {
                setStatus('sync-status', 'success', '✅ Cookie berhasil disinkronkan ke dw.pmlab.id.');
                setTimeout(() => setStatus('sync-status', null), 4000);
            } else {
                setStatus('sync-status', 'error', '❌ Gagal: ' + (res?.error || 'Periksa token sesi.'));
            }
        });
    };
    
    $('send-sub').onclick = () => {
        const subtitleUrl = $('subtitle').value;
        if (!subtitleUrl) {
            setStatus('sniffer-status', 'loading', '⚠️ Silakan pilih subtitle terlebih dahulu.');
            return;
        }
        sendToBackground(
            { urls: subtitleUrl, mode: 'subtitle', media_type: 'video', resolution: 'best' },
            $('send-sub'),
            'sniffer-status',
            '✅ Terkirim',
            'Subtitle Saja'
        );
    };
    
    $('clear').onclick = () => {
        chrome.runtime.sendMessage({ type: 'CLEAR_CANDIDATES', tabId: currentTab.id }, () => {
            refresh();
        });
    };
})();

$('btn-dl-direct').onclick = () => {
    if (!currentTab || !currentTab.url) return;
    sendToBackground(
        { urls: currentTab.url, mode: 'ytdlp', media_type: 'video', resolution: 'best' },
        $('btn-dl-direct'),
        'direct-status',
        '✅ Sukses Terkirim',
        '🚀 Sedot ke dw.pmlab.id'
    );
};

$('refresh').onclick = refresh;

$('send').onclick = () => {
    const selectedUrl = $('media').value;
    const selected = items.find(x => x.url === selectedUrl);
    
    if (!selected) {
        setStatus('sniffer-status', 'loading', '⚠️ Silakan pilih playlist film terlebih dahulu.');
        return;
    }

    if (selected.kind === 'non_media') {
        setStatus('sniffer-status', 'error', '❌ Terdeteksi respons klaim/analitik non-media. Nonaktif.');
        return;
    }
    
    const subtitleUrl = $('subtitle').value;
    const actualDownloadUrl = selected.extractedMediaUrl || selected.url;
    const customName = $('name')?.value?.trim() || '';

    const payload = { 
        urls: actualDownloadUrl, 
        mode: selected.kind === 'hls' ? 'direct' : 'auto', 
        media_type: 'video', 
        resolution: 'best',
        referer: selected.referer || ''
    };

    if (customName) {
        payload.name = customName;
    }
    
    if (subtitleUrl) {
        payload.subtitle_url = subtitleUrl;
        payload.embed_subs = true;
    }
    
    sendToBackground(
        payload,
        $('send'),
        'sniffer-status',
        '✅ Terkirim',
        '⬇ Kirim Playlist ke Server'
    );
};

$('settings').onclick = () => chrome.runtime.openOptionsPage();

$('deep-analysis').onclick = async () => {
    const btn = $('deep-analysis');
    btn.textContent = 'Menganalisis...';
    btn.disabled = true;
    setStatus('sniffer-status', 'loading', 'Memindai elemen video/audio di dalam DOM dan iframe…');
    
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
                
                document.querySelectorAll('iframe').forEach(ifr => {
                    try {
                        ifr.contentDocument.querySelectorAll('video, audio, source, track').forEach(el => {
                            const src = el.currentSrc || el.src;
                            if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
                                cands.push({ url: src, kind: 'video', time: Date.now(), sourceType: 'iframe' });
                            }
                        });
                    } catch(e) {}
                });
                return cands;
            }
        });
        
        if (results && results[0] && results[0].result) {
            const domCands = results[0].result;
            if (domCands.length > 0) {
                chrome.runtime.sendMessage({ type: 'ADD_CANDIDATES', tabId: currentTab.id, items: domCands }, () => {
                    refresh();
                    setStatus('sniffer-status', 'success', `✅ ${domCands.length} media ditemukan via analisis DOM!`);
                });
            } else {
                setStatus('sniffer-status', 'loading', '⚠️ Tidak ada elemen media langsung di DOM. Putar pemutar film untuk menangkap lalu lintas jaringan.');
            }
        }
    } catch(e) {
        setStatus('sniffer-status', 'error', '❌ Analisis DOM ditolak browser: ' + e.message);
    } finally {
        btn.textContent = '🔍 Analisis Mendalam DOM';
        btn.disabled = false;
    }
};

$('request-permission').onclick = async () => {
    try {
        const origin = new URL(currentTab.url).origin + '/*';
        let granted = false;
        try {
            granted = await chrome.permissions.request({ origins: ['<all_urls>'] });
        } catch(e) {
            granted = await chrome.permissions.request({ origins: [origin] });
        }
        if (granted) {
            setStatus('sniffer-status', 'success', '✅ Izin deteksi diberikan! Silakan putar ulang video film.');
            $('request-permission').style.display = 'none';
        } else {
            setStatus('sniffer-status', 'loading', '⚠️ Permintaan izin akses situs ditolak.');
        }
    } catch(e) {
        setStatus('sniffer-status', 'error', '❌ Gagal meminta izin: ' + e.message);
    }
};
