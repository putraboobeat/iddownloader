"""Find media in a temporary browser session, including HLS served as .json."""
import time
from contextlib import contextmanager
from http.cookiejar import Cookie, MozillaCookieJar
from urllib.parse import urlsplit
from subtitles import subtitle_format, language


def media_kind(body, content_type=''):
    text = body.lstrip('\ufeff \r\n\t')
    if text.startswith('#EXTM3U'):
        if '#EXT-X-STREAM-INF:' in text:
            return 'master', 100
        if '#EXTINF:' in text:
            return 'hls', 50
    if '<MPD' in text[:2000]:
        return 'dash', 90
    if content_type.split(';')[0].strip() in ('video/mp4', 'video/webm'):
        return 'file', 40
    return None


def write_cookies(cookies, filename):
    jar = MozillaCookieJar(str(filename))
    for c in cookies:
        domain = c['domain']
        jar.set_cookie(Cookie(0, c['name'], c['value'], None, False,
                              domain, domain.startswith('.'), domain.startswith('.'),
                              c['path'], True, c['secure'],
                              int(c['expires']) if c['expires'] > 0 else None,
                              c['expires'] <= 0, None, None, {}, False))
    jar.save(ignore_discard=True, ignore_expires=True)
    filename.chmod(0o600)


@contextmanager
def discover(url, cookie_file, cancelled, status, timeout=90, headless=False, subtitles=False):
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError:
        raise ValueError('Pendeteksi belum terpasang. Jalankan Pasang.command terlebih dahulu.') from None
    candidates = {}
    pending = []
    tracks = {}
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel='chrome', headless=headless,
                args=['--autoplay-policy=no-user-gesture-required'])
        except Error:
            raise ValueError('Google Chrome tidak dapat dibuka. Pastikan Chrome sudah terpasang.') from None
        try:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()
            context.on('page', lambda popup: popup.close() if popup != page else None)
            page.on('dialog', lambda dialog: dialog.dismiss())

            def response_info(response):
                request = response.request
                headers = request.all_headers()
                try:
                    referer = headers.get('referer') or request.frame.url
                except Error:
                    referer = url
                return dict(url=response.url,
                    referer=referer if referer.startswith('http') else url,
                    headers={key: headers[key] for key in ('user-agent', 'origin') if key in headers})

            def record(response, kind):
                candidates[response.url] = dict(response_info(response), kind=kind[0], rank=kind[1])

            def on_response(response):
                if response.status not in (200, 206):
                    return
                mime = response.headers.get('content-type', '').lower()
                kind = media_kind('', mime)
                if kind:
                    record(response, kind)

            def on_finished(request):
                if request.resource_type in ('fetch', 'xhr', 'media', 'document', 'other'):
                    pending.append(request)

            context.on('response', on_response)
            context.on('requestfinished', on_finished)
            status('Membuka halaman dan mencari video…')
            try:
                page.goto(url, wait_until='commit', timeout=20000)
            except Error:
                if page.is_closed():
                    raise ValueError('Jendela pencarian ditutup.') from None
            deadline = time.monotonic() + timeout
            first_found = None
            clicked = set()
            while time.monotonic() < deadline:
                if cancelled():
                    raise InterruptedError('Dihentikan.')
                if page.is_closed():
                    raise ValueError('Jendela pencarian ditutup sebelum video ditemukan.')
                page.wait_for_timeout(250)
                for request in pending[:]:
                    pending.remove(request)
                    try:
                        response = request.response()
                        if not response or response.status != 200:
                            continue
                        mime = response.headers.get('content-type', '').lower()
                        path = urlsplit(response.url).path.lower()
                        if not (any(x in mime for x in ('mpegurl', 'dash+xml', 'json', 'text/', 'octet-stream'))
                                or path.endswith(('.m3u8', '.mpd', '.json', '.vtt', '.srt'))):
                            continue
                        if int(response.headers.get('content-length', '0')) > 2_000_000:
                            continue
                        body = response.body()
                        if len(body) > 2_000_000:
                            continue
                        if subtitles:
                            try:
                                subtitle_format(body)
                                tracks[response.url] = dict(response_info(response), body=body, lang=language(response.url))
                            except (ValueError, UnicodeError):
                                pass
                        kind = media_kind(body.decode('utf-8', errors='replace'), mime)
                        if kind:
                            record(response, kind)
                    except (Error, ValueError):
                        continue
                if candidates:
                    first_found = first_found or time.monotonic()
                    best = max(candidates.values(), key=lambda item: item['rank'])
                    ready = best['rank'] >= 90 or time.monotonic() - first_found > 5
                    if ready and (not subtitles or time.monotonic() - first_found >= 4):
                        if subtitles:
                            for frame in page.frames:
                                try:
                                    for track in frame.locator('track[src]').evaluate_all('els => els.map(t => ({url:t.src, lang:t.srclang, kind:t.kind}))'):
                                        if track['kind'] in ('subtitles', 'captions') and track['url'].startswith(('http://', 'https://')):
                                            tracks.setdefault(track['url'], dict(track, referer=frame.url, headers=best['headers']))
                                            if track['lang']:
                                                tracks[track['url']]['lang'] = track['lang']
                                except Error:
                                    pass
                        best['subtitles'] = list(tracks.values())
                        write_cookies(context.cookies(), cookie_file)
                        status('Video ditemukan. Menyiapkan unduhan…')
                        yield best
                        return
                # Only activate recognizable player controls, including embedded players.
                for frame in page.frames:
                    try:
                        frame.evaluate('''() => { for (const v of document.querySelectorAll('video')) {
                            v.muted = true; v.play().catch(() => {}); } }''')
                        controls = frame.locator('.vjs-big-play-button, .jw-display-icon-container, '
                            '.plyr__control--overlaid, button[aria-label="Play"], button[title="Play"]')
                        if frame.url not in clicked and controls.count() and controls.first.is_visible():
                            controls.first.click(timeout=700)
                            clicked.add(frame.url)
                    except Error:
                        pass
                if not candidates:
                    status('Mencari video… Selesaikan verifikasi situs atau klik Play di jendela Chrome jika diminta.')
            raise ValueError(f'Video belum ditemukan dalam {timeout} detik. Coba lagi, selesaikan verifikasi situs, lalu putar video di jendela Chrome yang terbuka.')
        finally:
            browser.close()
