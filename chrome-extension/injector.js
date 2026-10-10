// OmniFetch Injector — Injects download buttons into YouTube, Instagram, TikTok
(function() {
  'use strict';

  // Prevent running twice
  if (window.__omnifetch_injected) return;
  window.__omnifetch_injected = true;

  const HOST = location.hostname;
  const IS_YOUTUBE = HOST.includes('youtube.com');
  const IS_INSTAGRAM = HOST.includes('instagram.com');
  const IS_TIKTOK = HOST.includes('tiktok.com');
  const IS_TWITTER = HOST.includes('twitter.com') || HOST.includes('x.com');

  // === Styles ===
  const style = document.createElement('style');
  style.textContent = `
    .omni-btn {
      display: inline-flex; align-items: center; gap: 5px;
      background: linear-gradient(135deg, #c9153d, #ff3d58);
      color: #fff !important; border: none; border-radius: 20px;
      padding: 6px 13px; font-size: 12px; font-weight: 700;
      cursor: pointer; margin-left: 8px;
      font-family: -apple-system, sans-serif;
      text-decoration: none !important;
      transition: transform 0.1s, opacity 0.1s; z-index: 9999;
      box-shadow: 0 2px 8px rgba(201,21,61,0.35);
      vertical-align: middle; letter-spacing: 0.3px;
      position: relative;
    }
    .omni-btn:hover { transform: scale(1.05); opacity: 0.93; }
    .omni-btn:active { transform: scale(0.97); }
    .omni-btn svg { width: 12px; height: 12px; fill: currentColor; flex-shrink: 0; }

    #omni-sniffer-toast {
      position: fixed; bottom: 24px; right: 24px; z-index: 2147483647;
      background: #160a0d; border: 1px solid #c9153d; border-radius: 14px;
      padding: 14px 18px; max-width: 320px; display: flex; align-items: center; gap: 12px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.7), 0 0 0 1px #c9153d22;
      animation: omni-slide-in 0.4s cubic-bezier(.2,.8,.4,1) forwards;
      font-family: -apple-system, sans-serif;
    }
    @keyframes omni-slide-in {
      from { transform: translateY(30px); opacity: 0; }
      to   { transform: translateY(0);    opacity: 1; }
    }
    .omni-pulse { width: 10px; height: 10px; border-radius: 50%; background: #ff3d58; flex-shrink: 0; animation: omni-pulse 1.2s infinite; }
    @keyframes omni-pulse { 0%,100%{box-shadow:0 0 0 0 rgba(255,61,88,0.7)} 50%{box-shadow:0 0 0 7px rgba(255,61,88,0)} }
    #omni-sniffer-toast p { margin: 0; font-size: 12px; color: #ffb3c1; font-weight: 600; }
    #omni-sniffer-toast small { color: #888; font-size: 10px; display: block; margin-top: 2px; }
    #omni-sniffer-toast button {
      margin-left: auto; background: #c9153d; color: #fff; border: none;
      border-radius: 8px; padding: 6px 12px; font-size: 11px; font-weight: 700; cursor: pointer;
    }
    #omni-sniffer-close {
      background: transparent !important; color: #888 !important;
      padding: 4px 8px !important; font-size: 14px !important;
    }
  `;
  document.head.appendChild(style);

  // === Arrow SVG icon ===
  const arrowSVG = `<svg viewBox="0 0 24 24"><path d="M12 2v13m0 0l-4-4m4 4l4-4M4 19h16"/></svg>`;

  // === Get app URL ===
  async function getApp() {
    const d = await chrome.storage.local.get(['app','token']);
    return { app: d.app || null, token: d.token || '' };
  }

  // === Send URL to OmniFetch VPS ===
  async function sendToOmniFetch(url, btn) {
    const { app, token } = await getApp();
    if (!app) {
      btn && (btn.textContent = '⚙️ Atur Pengaturan Dulu');
      chrome.runtime.openOptionsPage();
      return;
    }
    if (btn) { btn.innerHTML = '⏳ Mengirim…'; btn.disabled = true; }
    try {
      const payload = { urls: url, mode: 'ytdlp', media_type: 'video', resolution: 'best' };
      const appOrigin = new URL(app).origin;
      const res = await fetch(appOrigin + '/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-App-Token': token },
        body: JSON.stringify(payload)
      });
      const j = await res.json();
      if (j.ok) {
        if (btn) { btn.innerHTML = arrowSVG + ' Terkirim!'; btn.style.background = '#15803d'; }
        setTimeout(() => {
          if (btn) { btn.innerHTML = arrowSVG + ' Unduh'; btn.style.background = ''; btn.disabled = false; }
        }, 3000);
      } else {
        throw new Error(j.error || 'Gagal');
      }
    } catch(e) {
      if (btn) { btn.innerHTML = '❌ Gagal'; btn.disabled = false; }
      setTimeout(() => { if (btn) { btn.innerHTML = arrowSVG + ' Unduh'; } }, 2500);
    }
  }

  // === Helper: create OmniFetch button ===
  function createBtn(url) {
    const btn = document.createElement('button');
    btn.className = 'omni-btn';
    btn.innerHTML = arrowSVG + ' Unduh';
    btn.title = 'Unduh dengan OmniFetch';
    btn.onclick = (e) => { e.stopPropagation(); e.preventDefault(); sendToOmniFetch(url, btn); };
    return btn;
  }

  // ========================
  // === YOUTUBE INJECTOR ===
  // ========================
  function injectYouTube() {
    // Only on watch pages
    if (!location.pathname.startsWith('/watch') && !location.pathname.startsWith('/shorts')) return;
    if (document.querySelector('.omni-btn[data-yt]')) return;

    const url = location.href;
    const actionRow = document.querySelector('#actions-inner #top-level-buttons-computed, ytd-watch-metadata #actions');
    if (!actionRow) return;

    const btn = createBtn(url);
    btn.setAttribute('data-yt','1');
    actionRow.appendChild(btn);
  }

  // ========================
  // === INSTAGRAM INJECTOR ==
  // ========================
  function injectInstagram() {
    // Target all article posts
    document.querySelectorAll('article').forEach(article => {
      if (article.querySelector('.omni-btn[data-ig]')) return;
      // Try to find the post URL
      const link = article.querySelector('a[href*="/p/"], a[href*="/reel/"]');
      if (!link) return;
      const postUrl = 'https://www.instagram.com' + link.getAttribute('href').split('?')[0];

      // Find the action row (like/comment/share bar)
      const actionBar = article.querySelector('section, div[role="toolbar"]');
      if (!actionBar) return;

      const btn = createBtn(postUrl);
      btn.setAttribute('data-ig', '1');
      btn.style.borderRadius = '8px';
      btn.style.padding = '5px 10px';
      actionBar.appendChild(btn);
    });
  }

  // ========================
  // === TIKTOK INJECTOR ===
  // ========================
  function injectTikTok() {
    document.querySelectorAll('[class*="DivActionItemContainer"], [data-e2e="comment-icon"]').forEach(container => {
      const parent = container.closest('[class*="DivActionItemGroup"]') || container.parentElement;
      if (!parent || parent.querySelector('.omni-btn[data-tt]')) return;
      const videoWrap = parent.closest('div[class*="DivVideoWrapper"], article, [data-e2e="recommend-list-item-container"]');
      // Extract TikTok URL from the current page or nearby link
      const a = parent.closest('[class*="DivItemContainer"]')?.querySelector('a[href*="/@"]') || document.querySelector('a[href*="/@"]');
      const postUrl = a ? 'https://www.tiktok.com' + a.getAttribute('href').split('?')[0] : location.href;

      const btn = createBtn(postUrl);
      btn.setAttribute('data-tt', '1');
      btn.style.display = 'block';
      btn.style.borderRadius = '8px';
      btn.style.margin = '8px auto 0';
      btn.style.fontSize = '11px';
      parent.appendChild(btn);
    });
  }

  // ========================
  // === TWITTER/X INJECTOR =
  // ========================
  function injectTwitter() {
    document.querySelectorAll('article[data-testid="tweet"]').forEach(tweet => {
      if (tweet.querySelector('.omni-btn[data-tw]')) return;
      const timeEl = tweet.querySelector('time');
      if (!timeEl) return;
      const tweetLink = timeEl.closest('a');
      if (!tweetLink) return;
      const postUrl = 'https://twitter.com' + tweetLink.getAttribute('href');
      const actionRow = tweet.querySelector('[role="group"]');
      if (!actionRow) return;
      const btn = createBtn(postUrl);
      btn.setAttribute('data-tw','1');
      btn.style.borderRadius = '6px';
      btn.style.padding = '5px 10px';
      actionRow.appendChild(btn);
    });
  }

  // ========================
  // === SMART SNIFFER ======
  // ========================
  let snifferShown = false;
  function showSnifferToast(mediaCount, firstUrl) {
    if (snifferShown || document.getElementById('omni-sniffer-toast')) return;
    snifferShown = true;
    const toast = document.createElement('div');
    toast.id = 'omni-sniffer-toast';
    toast.innerHTML = `
      <div class="omni-pulse"></div>
      <div>
        <p>🎬 ${mediaCount} Video Tersembunyi Ditemukan!</p>
        <small>${location.hostname}</small>
      </div>
      <button id="omni-sniffer-dl">⬇ Unduh</button>
      <button id="omni-sniffer-close" title="Tutup">✕</button>
    `;
    document.body.appendChild(toast);
    document.getElementById('omni-sniffer-dl').onclick = () => {
      sendToOmniFetch(firstUrl, document.getElementById('omni-sniffer-dl'));
    };
    document.getElementById('omni-sniffer-close').onclick = () => toast.remove();
    setTimeout(() => toast.remove(), 15000);
  }

  // Listen for sniffer signals from background
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'omni-sniffer' && msg.count > 0) {
      showSnifferToast(msg.count, msg.url);
    }
  });

  // ========================
  // === OBSERVER (SPA) =====
  // ========================
  function runInjectors() {
    if (IS_YOUTUBE) injectYouTube();
    if (IS_INSTAGRAM) injectInstagram();
    if (IS_TIKTOK) injectTikTok();
    if (IS_TWITTER) injectTwitter();
  }

  runInjectors();

  // Watch for SPA navigation changes (React apps)
  let lastUrl = location.href;
  const navObserver = new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      snifferShown = false;
      setTimeout(runInjectors, 1000);
      setTimeout(runInjectors, 2500);
    }
    runInjectors();
  });
  navObserver.observe(document.body, { childList: true, subtree: true });
})();
