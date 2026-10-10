const $ = id => document.getElementById(id);
let tab, items = [];

const SOCIAL_DOMAINS = ['youtube.com', 'instagram.com', 'tiktok.com', 'twitter.com', 'x.com'];

async function getApp() {
  const d = await chrome.storage.local.get(['app','token']);
  return { app: d.app || null, token: d.token || '' };
}

// Format Label
function label(item) {
  const u = new URL(item.url);
  return `${item.kind === 'config' ? '★ Config' : item.kind} · ${u.pathname.split('/').pop()} · ${u.hostname}`;
}

// Refresh Sniffer
async function refresh() {
  const stored = (await chrome.storage.session.get('tab:'+tab.id))['tab:'+tab.id] || [];
  items = stored.filter(x => Date.now() - x.time < 3600000);
  items.sort((a,b) => (b.kind==='config'?3:b.kind==='playlist'?2:1) - (a.kind==='config'?3:a.kind==='playlist'?2:1) || b.time - a.time);
  
  $('media').replaceChildren(new Option('Pilih playlist film…', ''));
  $('subtitle').replaceChildren(new Option('Tanpa subtitle tambahan', ''));
  
  for (const item of items) {
    const opt = new Option(label(item), item.url);
    $(item.kind === 'subtitle' ? 'subtitle' : 'media').append(opt);
  }
  
  const best = items.find(x => x.kind !== 'subtitle');
  if (best) $('media').value = best.url;
  
  const source = items.find(x => x.url === $('media').value);
  if (source) {
    const group = new URL(source.url).pathname.match(/^\/v\/[^/]+\/[^/]+\//)?.[0];
    const subs = items.filter(x => x.kind === 'subtitle' && group && new URL(x.url).origin === new URL(source.url).origin && new URL(x.url).pathname.startsWith(group));
    subs.sort((a,b) => Number(/\/i18n\/id\//.test(b.url)) - Number(/\/i18n\/id\//.test(a.url)) || b.time - a.time);
    if (subs[0]) $('subtitle').value = subs[0].url;
  }
  
  $('status').textContent = items.length ? '🎬 Sumber berhasil ditangkap.' : 'Belum ada sumber tertangkap. Putar video dulu.';
  $('send').disabled = !best;
}

// Send from Sniffer
async function sendSniffer(subOnly) {
  try {
    const { app } = await getApp();
    if (!app) throw Error('Silakan atur alamat Dashboard OmniFetch terlebih dahulu.');
    const selected = items.find(x => x.url === $(subOnly ? 'subtitle' : 'media').value);
    if (!selected) throw Error(subOnly ? 'Pilih subtitle dulu.' : 'Pilih playlist dulu.');
    
    const payload = {
      url: selected.url,
      mode: subOnly ? 'subtitle' : 'direct',
      name: $('name').value || tab.title.slice(0,100),
      referer: tab.url,
      subtitle_url: subOnly ? '' : $('subtitle').value
    };
    
    await chrome.tabs.create({ url: new URL(app).origin + '/#import=' + encodeURIComponent(JSON.stringify(payload)) });
    $('status').textContent = '✅ Dikirim ke Dashboard.';
  } catch(e) {
    $('status').textContent = '❌ ' + e.message;
  }
}

// Initializer
(async () => {
  [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  
  // Decide which UI to show
  const host = new URL(tab.url).hostname;
  const isSocial = SOCIAL_DOMAINS.some(d => host.endsWith(d));
  
  if (isSocial) {
    $('direct-dl-section').style.display = 'block';
    $('sniffer-section').style.display = 'none';
    $('current-url').textContent = tab.url.length > 50 ? tab.url.substring(0, 47) + '...' : tab.url;
  } else {
    $('direct-dl-section').style.display = 'none';
    $('sniffer-section').style.display = 'block';
    refresh();
  }
  
  $('open-app').onclick = async () => {
    const { app } = await getApp();
    if (app) chrome.tabs.create({ url: new URL(app).origin });
    else chrome.runtime.openOptionsPage();
  };
})();

// Direct DL Click
$('btn-dl-direct').onclick = async () => {
  try {
    const { app, token } = await getApp();
    if (!app) throw Error('Atur Dashboard OmniFetch dulu.');
    
    const btn = $('btn-dl-direct');
    btn.innerHTML = '⏳ Mengirim...';
    btn.disabled = true;
    
    const payload = { urls: tab.url, mode: 'ytdlp', media_type: 'video', resolution: 'best' };
    const res = await fetch(new URL(app).origin + '/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-App-Token': token },
      body: JSON.stringify(payload)
    });
    
    const data = await res.json();
    if (data.ok) {
      btn.innerHTML = '✅ Sukses Terkirim';
      btn.style.background = '#15803d';
      $('status').textContent = 'Video sedang diunduh di VPS!';
    } else {
      throw Error(data.error || 'Gagal');
    }
  } catch(e) {
    $('status').textContent = '❌ ' + e.message;
    $('btn-dl-direct').innerHTML = '🚀 Coba Lagi';
    $('btn-dl-direct').disabled = false;
  }
};

// Listeners for Sniffer
$('refresh').onclick = refresh;
$('clear').onclick = async () => {
  await chrome.storage.session.remove('tab:'+tab.id);
  await chrome.action.setBadgeText({tabId:tab.id, text:''});
  refresh();
};
$('send').onclick = () => sendSniffer(false);
$('send-sub').onclick = () => sendSniffer(true);

// Settings
$('settings').onclick = () => chrome.runtime.openOptionsPage();

// Cookie Sync
$('sync-cookies').onclick = async () => {
  const btn = $('sync-cookies');
  btn.disabled = true;
  try {
    $('status').textContent = 'Mengambil cookie media sosial...';
    const { app, token } = await getApp();
    if (!app) throw Error('Atur Dashboard OmniFetch dulu.');
    
    let allCookies = [];
    for (const d of ['.instagram.com','instagram.com','.youtube.com','youtube.com','.tiktok.com','tiktok.com']) {
      try { allCookies = allCookies.concat(await chrome.cookies.getAll({domain: d})); } catch(e) {}
    }
    
    if (allCookies.length === 0) throw Error('Tidak ada cookie ditemukan.');
    
    let netscape = "# Netscape HTTP Cookie File\n# Generated by OmniFetch Companion\n";
    const seen = new Set();
    for (const c of allCookies) {
      const key = `${c.domain}|${c.name}|${c.path}`;
      if (seen.has(key)) continue; seen.add(key);
      netscape += `${c.domain}\t${c.domain.startsWith('.')?'TRUE':'FALSE'}\t${c.path}\t${c.secure?'TRUE':'FALSE'}\t${c.expirationDate?Math.floor(c.expirationDate):0}\t${c.name}\t${c.value}\n`;
    }
    
    const res = await fetch(new URL(app).origin + '/upload-cookies', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain', 'X-App-Token': token },
      body: netscape
    });
    const data = await res.json();
    if (!res.ok) throw Error(data.error || 'Gagal server');
    
    $('status').textContent = '✅ Sinkronisasi sukses!';
  } catch(e) {
    $('status').textContent = '❌ ' + e.message;
  } finally {
    btn.disabled = false;
  }
};
