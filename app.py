#!/usr/bin/env python3
"""Local video downloader. Python standard library + installed yt-dlp/FFmpeg."""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from detect_video import discover
from subtitles import save_subtitle

os.environ['PATH'] = os.pathsep.join([os.environ.get('PATH', ''), '/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local/bin')])
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
STATE = {'running': False, 'status': 'Siap mengunduh', 'percent': 0, 'logs': [], 'folder': str(Path.home() / 'Downloads'), 'cancelled': False}
PROCESS = None

def clean_url(value):
    value = str(value).strip()
    match = re.fullmatch(r'\[.*?\]\((.*)\)', value, re.S)
    if match:
        value = match.group(1)
    value = value.replace('\\&', '&').replace('\\_', '_').strip('<> \t\r\n')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or any(c.isspace() for c in value):
        raise ValueError('Tempel alamat halaman atau tautan video HTTP/HTTPS yang lengkap.')
    if parts.username or parts.password:
        raise ValueError('URL dengan nama pengguna atau sandi tidak didukung.')
    return value

def downloader():
    try:
        import importlib.util
        if importlib.util.find_spec('yt_dlp'):
            return [sys.executable, '-m', 'yt_dlp']
    except ImportError:
        pass
    binary = shutil.which('yt-dlp')
    return [binary] if binary else None

def add_log(line):
    # Do not expose signed URL tokens in the displayed log.
    line = re.sub(r'https?://[^\s\"\']+', '[URL]', line)
    line = re.sub(r'(?i)([?&]t=)[^\s&]+', r'\1[disembunyikan]', line)
    with LOCK:
        STATE['logs'] = (STATE['logs'] + [line])[-120:]

def build_command(data):
    cmd = downloader()
    if not cmd:
        raise ValueError('yt-dlp belum ditemukan. Jalankan: python3 -m pip install -U yt-dlp')
    if not shutil.which('ffmpeg'):
        raise ValueError('FFmpeg belum ditemukan. Jika memakai Homebrew, jalankan: brew install ffmpeg')
    url = clean_url(data.get('url', ''))
    folder = Path(str(data.get('folder', '')).strip() or '~/Downloads').expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    name = data.get('_name') or output_name(data)
    output = name + '.%(ext)s'
    # Keep completed files intact; a repeat download gets a different name.
    if not data.get('_name') and (folder / (name + '.mp4')).exists():
        output = name + '-' + time.strftime('%Y%m%d-%H%M%S') + '.%(ext)s'
    quality = str(data.get('quality', 'best'))
    formats = {'best': 'bv+ba/b', '720': 'bv[height<=720]+ba/b[height<=720]', '480': 'bv[height<=480]+ba/b[height<=480]'}
    cmd += ['--ignore-config', '--no-playlist', '--newline', '--no-colors', '--progress', '--no-quiet',
            '--progress-template', 'download:APP_PROGRESS:%(progress._percent_str)s | %(progress._speed_str)s | ETA %(progress._eta_str)s',
            '--print', 'after_move:APP_FILE:%(filepath)s', '--no-simulate',
            '--abort-on-unavailable-fragments', '--retries', '5', '--fragment-retries', '5',
            '--retry-sleep', 'http:5', '--retry-sleep', 'fragment:5',
            '--socket-timeout', '60', '--no-overwrites', '-f', formats.get(quality, formats['best']),
            '--merge-output-format', 'mp4', '--remux-video', 'mp4', '-P', str(folder), '-o', output]
    if data.get('forward', True):
        cmd += ['--extractor-args', 'generic:variant_query;fragment_query']
    if str(data.get('referer', '')).strip():
        cmd += ['--referer', clean_url(data['referer'])]
    if data.get('_cookies'):
        cmd += ['--cookies', data['_cookies']]
    for key, value in data.get('_headers', {}).items():
        if key in ('user-agent', 'origin'):
            cmd += ['--add-header', key + ':' + value]
    return cmd + ['--', url], str(folder)

def output_name(data):
    name = re.sub(r'(?i)\.(mp4|vtt|srt)$', '', str(data.get('name', 'Video')).strip())
    return re.sub(r'[^\w .-]', '_', name).strip(' .')[:100] or 'Video'

def run_job(data):
    global PROCESS
    def cancelled():
        with LOCK:
            return STATE['cancelled']
    def status(message):
        with LOCK:
            if not STATE['cancelled']:
                STATE['status'] = message
    try:
        subtitle_only = data.get('mode') == 'subtitle' or urlsplit(data['url']).path.lower().endswith(('.vtt', '.srt'))
        direct = urlsplit(data['url']).path.lower().endswith(('.mp4', '.webm', '.m3u8', '.mpd', '.json'))
        with LOCK:
            STATE.update(input_url=data['url'],
                         video_url=data['url'] if not subtitle_only and (data.get('mode') == 'direct' or (data.get('mode', 'auto') == 'auto' and direct)) else '',
                         subtitle_urls=[data['url']] if subtitle_only else [])
        folder = Path(data.get('folder') or '~/Downloads').expanduser().resolve()
        name = output_name(data)
        folder = folder / name
        if not subtitle_only and (folder / (name + '.mp4')).exists():
            index = 2
            parent = folder.parent
            while (parent / f'{name} ({index})').exists():
                index += 1
            folder = parent / f'{name} ({index})'
        folder.mkdir(parents=True, exist_ok=True)
        data['folder'] = str(folder)
        data['_name'] = name
        add_log('Folder hasil: ' + str(folder))
        if urlsplit(data['url']).path.lower().endswith(('.vtt', '.srt')) or data.get('mode') == 'subtitle':
            status('Mengunduh subtitle…')
            saved = save_subtitle(dict(url=data['url'], referer=data.get('referer')), folder, name, cancelled)
            add_log('Subtitle: ' + str(saved))
            status('Selesai — subtitle tersimpan di folder tujuan.')
            with LOCK:
                STATE['percent'] = 100
            return
        direct = urlsplit(data['url']).path.lower().endswith(('.mp4', '.webm', '.m3u8', '.mpd', '.json'))
        subtitle_warnings = []
        def download_subtitles(tracks, cookies=None):
            for track in tracks:
                with LOCK:
                    if track['url'] not in STATE['subtitle_urls']:
                        STATE['subtitle_urls'] = STATE['subtitle_urls'] + [track['url']]
                try:
                    status('Mengunduh subtitle…')
                    saved = save_subtitle(track, folder, name, cancelled, cookies)
                    add_log('Subtitle: ' + str(saved))
                except InterruptedError:
                    raise
                except Exception as exc:
                    subtitle_warnings.append(str(exc))
                    add_log('Subtitle gagal: ' + str(exc))
        manual = data.get('subtitle_url')
        if data.get('mode') == 'direct' or (data.get('mode', 'auto') == 'auto' and direct):
            if manual:
                download_subtitles([dict(url=manual, referer=data.get('referer'))])
            run_download(build_command(data)[0])
        else:
            with tempfile.TemporaryDirectory(prefix='video-downloader-') as temporary:
                cookies = Path(temporary) / 'cookies.txt'
                with discover(data['url'], cookies, cancelled, status, subtitles=data.get('subtitles', True)) as media:
                    if cancelled():
                        raise InterruptedError('Dihentikan.')
                    with LOCK:
                        STATE['video_url'] = media['url']
                    resolved = dict(data, url=media['url'],
                        referer=data.get('referer') or media['referer'],
                        _headers=media['headers'], _cookies=str(cookies))
                    add_log('Playlist video ditemukan otomatis. Memulai unduhan.')
                    if manual:
                        supplied = next((t for t in media.get('subtitles', []) if t['url'] == manual),
                            dict(url=manual, referer=resolved['referer'], headers=media['headers']))
                        download_subtitles([supplied], str(cookies))
                    if data.get('subtitles', True):
                        tracks = [track for track in media.get('subtitles', []) if track['url'] != manual]
                        download_subtitles(tracks, str(cookies))
                        if not tracks and not manual:
                            subtitle_warnings.append('Subtitle otomatis tidak ditemukan.')
                            add_log('Subtitle belum ditemukan. Aktifkan subtitle pada pemutar saat pencarian, atau tempel tautan VTT/SRT.')
                    run_download(build_command(resolved)[0])
        if subtitle_warnings:
            with LOCK:
                if STATE['status'].startswith('Selesai'):
                    STATE['status'] = 'Video selesai — sebagian subtitle gagal atau tidak ditemukan. Lihat detail proses.'
    except InterruptedError:
        status('Dihentikan.')
    except Exception as exc:
        add_log(str(exc))
        status('Pencarian atau unduhan gagal. Lihat detail proses.')
    finally:
        with LOCK:
            if STATE['cancelled']:
                STATE['status'] = 'Dihentikan.'
            PROCESS = None
            STATE['running'] = False

def run_download(cmd):
    global PROCESS
    try:
        with LOCK:
            if STATE['cancelled']:
                STATE['status'] = 'Dihentikan.'
                return
            PROCESS = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors='replace', start_new_session=True)
            proc = PROCESS
            STATE['status'] = 'Membaca playlist dan daftar kualitas… Tunggu hingga proses selesai.'
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith('APP_PROGRESS:'):
                text = line.split(':', 1)[1].strip()
                match = re.search(r'(\d+(?:\.\d+)?)%', text)
                with LOCK:
                    STATE['status'] = text
                    if match:
                        STATE['percent'] = float(match.group(1))
            elif line.startswith('APP_FILE:'):
                add_log('File: ' + line.split(':', 1)[1])
            elif line:
                add_log(line)
                stage = None
                if 'Downloading webpage' in line or 'Extracting URL:' in line:
                    stage = 'Menghubungi server video…'
                elif 'Downloading m3u8 information' in line or 'Checking m3u8 live status' in line:
                    stage = 'Membaca playlist dan memeriksa kualitas video…'
                elif 'Downloading m3u8 manifest' in line:
                    stage = 'Menyiapkan daftar potongan video / audio…'
                elif '[download] Destination:' in line:
                    stage = 'Mengambil potongan pertama… Progres akan muncul setelah data diterima.'
                elif 'Retrying' in line:
                    stage = 'Koneksi tersendat. Mencoba kembali…'
                elif '[Merger]' in line or '[VideoRemuxer]' in line:
                    stage = 'Menggabungkan video dan audio…'
                if stage:
                    with LOCK:
                        if not STATE['cancelled']:
                            STATE['status'] = stage
        proc.stdout.close()
        code = proc.wait()
        with LOCK:
            if STATE['cancelled']:
                STATE['status'] = 'Dihentikan. File sementara disimpan untuk melanjutkan.'
            elif code == 0:
                STATE['status'] = 'Selesai — file tersimpan di folder tujuan.'
                STATE['percent'] = 100
            else:
                STATE['status'] = 'Unduhan gagal. Lihat detail di bawah.'
                logs = '\n'.join(STATE['logs'])
                if '401' in logs or '403' in logs:
                    add_log('Akses ditolak: coba lagi menggunakan alamat halaman film untuk mencari tautan baru.')
    except Exception as exc:
        add_log(str(exc))
        with LOCK:
            STATE['status'] = 'Gagal menjalankan pengunduh.'

def stop():
    with LOCK:
        STATE['cancelled'] = True
        STATE['status'] = 'Menghentikan…'
        proc = PROCESS
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            def force_stop():
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            threading.Thread(target=force_stop, daemon=True).start()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return self.headers.get('X-App-Token') == TOKEN

    def do_GET(self):
        if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
            return self.send({'error': 'Akses ditolak'}, 403)
        if self.path == '/':
            body = Path(__file__).with_name('index.html').read_text().replace('__APP_TOKEN__', TOKEN).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/extension.zip':
            archive = Path(__file__).with_name('Video-Downloader-Extension.zip')
            if not archive.exists():
                return self.send({'error': 'Paket ekstensi belum tersedia.'}, 404)
            body = archive.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="Video-Downloader-Extension.zip"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/status' and self.authorized():
            with LOCK:
                snapshot = dict(STATE)
            snapshot['tools'] = {'yt': bool(downloader()), 'ffmpeg': bool(shutil.which('ffmpeg'))}
            self.send(snapshot)
        else:
            self.send({'error': 'Tidak tersedia'}, 403)

    def do_POST(self):
        if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port) or not self.authorized():
            return self.send({'error': 'Akses ditolak'}, 403)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 <= size <= 32768:
                raise ValueError('Input terlalu besar.')
            data = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data, dict):
                raise ValueError('Input tidak valid.')
            if self.path == '/start':
                # Never accept internal cookie paths or captured headers from HTTP clients.
                data = {key: data[key] for key in ('url', 'name', 'quality', 'folder', 'referer', 'forward', 'mode', 'subtitles', 'subtitle_url') if key in data}
                data['url'] = clean_url(data.get('url', ''))
                if str(data.get('subtitle_url', '')).strip():
                    data['subtitle_url'] = clean_url(data['subtitle_url'])
                else:
                    data['subtitle_url'] = ''
                if str(data.get('referer', '')).strip():
                    data['referer'] = clean_url(data['referer'])
                if data.get('mode', 'auto') not in ('auto', 'page', 'direct', 'subtitle'):
                    raise ValueError('Pilihan sumber tidak valid.')
                with LOCK:
                    if STATE['running']:
                        raise ValueError('Masih ada unduhan yang berjalan.')
                    if data.get('mode') == 'subtitle' or urlsplit(data['url']).path.lower().endswith(('.vtt', '.srt')):
                        folder = str(Path(data.get('folder') or '~/Downloads').expanduser().resolve())
                    else:
                        cmd, folder = build_command(data)
                    STATE.update(running=True, cancelled=False, percent=0, logs=[], folder=folder, status='Menghubungkan…', input_url=data['url'], video_url='', subtitle_urls=[])
                    threading.Thread(target=run_job, args=(data,), daemon=True).start()
                self.send({'ok': True})
            elif self.path == '/stop':
                stop()
                self.send({'ok': True})
            elif self.path == '/folder':
                if sys.platform != 'darwin':
                    raise ValueError('Tuliskan lokasi folder secara manual pada perangkat ini.')
                result = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "Pilih folder penyimpanan")'], capture_output=True, text=True, timeout=120)
                self.send({'folder': result.stdout.strip() if result.returncode == 0 else ''})
            else:
                self.send({'error': 'Tidak tersedia'}, 404)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.send({'error': str(exc)}, 400)

def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('Video Downloader: ' + url, flush=True)
    print('Biarkan Terminal terbuka. Tekan Control+C untuk menutup program.', flush=True)
    if '--no-browser' not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        server.server_close()

if __name__ == '__main__':
    main()
