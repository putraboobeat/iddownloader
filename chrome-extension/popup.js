const $ = id => document.getElementById(id);
let tab, items = [];

const SOCIAL_DOMAINS = ['youtube.com', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com'];

// Format Label
function label(item) {
    const u = new URL(item.url);
    return `${item.kind === 'config' ? '★ Config' : item.kind} · ${u.pathname.split('/').pop() || u.hostname}`;
}

async function refresh() {
    chrome.runtime.sendMessage({ type: 'GET_CANDIDATES' }, (res) => {
        if (!res || res.error) {
            $('status').textContent = 'Error: ' + (res?.error || 'Gagal mengambil data');
            return;
        }
        
        items = res.items || [];
        items.sort((a,b) => (b.kind==='config'?3:b.kind==='playlist'?2:1) - (a.kind==='config'?3:a.kind==='playlist'?2:1) || b.time - a.time);
        
        $('media').replaceChildren(new Option('Pilih playlist film…', ''));
        $('subtitle').replaceChildren(new Option('Tanpa subtitle tambahan', ''));
        
        for (const item of items) {
            const opt = new Option(label(item), item.url);
            $(item.kind === 'subtitle' ? 'subtitle' : 'media').append(opt);
        }
        
        const best = items.find(x => x.kind !== 'subtitle');
        if (best) $('media').value = best.url;
        
        $('status').textContent = items.length ? '🎬 Sumber berhasil ditangkap.' : 'Belum ada sumber tertangkap. Putar video dulu.';
        $('send').disabled = !best;
    });
}

function sendToBackground(payload, btnElement, successText = '✅ Terkirim', originalText = 'Unduh') {
    if (btnElement) {
        btnElement.innerHTML = '⏳ Mengirim...';
        btnElement.disabled = true;
    }
    
    chrome.runtime.sendMessage({ type: 'SEND_JOB', payload }, (res) => {
        if (chrome.runtime.lastError || (res && res.error)) {
            $('status').textContent = '❌ ' + (chrome.runtime.lastError?.message || res?.error);
            if (btnElement) {
                btnElement.innerHTML = '❌ Gagal';
                btnElement.disabled = false;
                setTimeout(() => btnElement.innerHTML = originalText, 2000);
            }
        } else {
            $('status').textContent = 'Video sedang diproses di server!';
            if (btnElement) {
                btnElement.innerHTML = successText;
                btnElement.style.background = '#15803d';
                setTimeout(() => {
                    btnElement.innerHTML = originalText;
                    btnElement.style.background = '';
                    btnElement.disabled = false;
                }, 3000);
            }
        }
    });
}

// Initializer
(async () => {
    [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    
    const host = new URL(tab.url).hostname;
    const isSocial = SOCIAL_DOMAINS.some(d => host.endsWith(d));
    
    // Check permission
    try {
        const origin = new URL(tab.url).origin + '/*';
        const hasPerm = await chrome.permissions.contains({ origins: [origin] });
        if (hasPerm) {
            $('request-permission').style.display = 'none';
        }
    } catch(e) {}

    if (isSocial) {
        $('direct-dl-section').style.display = 'block';
        $('sniffer-section').style.display = 'none';
        $('current-url').textContent = tab.url.length > 50 ? tab.url.substring(0, 47) + '...' : tab.url;
    } else {
        $('direct-dl-section').style.display = 'none';
        $('sniffer-section').style.display = 'block';
        refresh();
    }
    
    $('open-app').onclick = () => chrome.runtime.openOptionsPage();
})();

$('btn-dl-direct').onclick = () => {
    sendToBackground(
        { urls: tab.url, mode: 'ytdlp', media_type: 'video', resolution: 'best' },
        $('btn-dl-direct'),
        '✅ Sukses Terkirim',
        '🚀 Sedot Langsung'
    );
};

$('refresh').onclick = refresh;
$('send').onclick = () => {
    const selected = items.find(x => x.url === $('media').value);
    if (!selected) {
        $('status').textContent = 'Pilih playlist dulu.';
        return;
    }
    sendToBackground(
        { urls: selected.url, mode: 'ytdlp', media_type: 'video', resolution: 'best' },
        $('send'),
        '✅ Terkirim',
        '⬇ Kirim Playlist ke Server'
    );
};

$('settings').onclick = () => chrome.runtime.openOptionsPage();

$('deep-analysis').onclick = async () => {
    const btn = $('deep-analysis');
    btn.textContent = 'Menganalisis...';
    btn.disabled = true;
    
    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => {
                const cands = [];
                // Check all standard media
                document.querySelectorAll('video, audio, source, track').forEach(el => {
                    const src = el.currentSrc || el.src;
                    if (src && !src.startsWith('blob:')) {
                        let kind = el.tagName.toLowerCase();
                        if (kind === 'source') kind = el.closest('video') ? 'video' : 'audio';
                        if (kind === 'track') kind = 'subtitle';
                        cands.push({ url: src, kind: kind, time: Date.now(), sourceType: 'dom' });
                    }
                });
                
                // Deep scan iframes (same-origin only due to CORS)
                document.querySelectorAll('iframe').forEach(ifr => {
                    try {
                        ifr.contentDocument.querySelectorAll('video, audio, source, track').forEach(el => {
                            const src = el.currentSrc || el.src;
                            if (src && !src.startsWith('blob:')) {
                                cands.push({ url: src, kind: 'video', time: Date.now(), sourceType: 'iframe' });
                            }
                        });
                    } catch(e) {} // Cross-origin block
                });
                return cands;
            }
        });
        
        if (results && results[0] && results[0].result) {
            const domCands = results[0].result;
            if (domCands.length > 0) {
                // Send to background to register them
                for (const c of domCands) {
                    // Send to background using a slightly modified approach, or just append to UI
                    const opt = new Option(`🔍 DOM: ${c.kind} · ${new URL(c.url).pathname.split('/').pop()}`, c.url);
                    $(c.kind === 'subtitle' ? 'subtitle' : 'media').append(opt);
                    
                    // Pre-select if nothing else is selected
                    if (!items.find(x => x.kind !== 'subtitle') && c.kind !== 'subtitle') {
                        $('media').value = c.url;
                        $('send').disabled = false;
                    }
                }
                $('status').textContent = `✅ ${domCands.length} media tersembunyi ditemukan via DOM!`;
            } else {
                $('status').textContent = '⚠️ Tidak ada media statis di DOM. (Coba putar video untuk mendeteksi jaringan)';
            }
        }
    } catch(e) {
        $('status').textContent = '❌ Analisis ditolak: ' + e.message;
    } finally {
        btn.textContent = '🔍 Analisis Mendalam DOM';
        btn.disabled = false;
    }
};

$('request-permission').onclick = async () => {
    try {
        const origin = new URL(tab.url).origin + '/*';
        const granted = await chrome.permissions.request({ origins: [origin] });
        if (granted) {
            $('status').textContent = '✅ Izin diberikan! Putar ulang video untuk mendeteksi jaringan.';
            $('request-permission').style.display = 'none';
        } else {
            $('status').textContent = '⚠️ Izin ditolak.';
        }
    } catch(e) {
        $('status').textContent = '❌ Gagal meminta izin: ' + e.message;
    }
};


