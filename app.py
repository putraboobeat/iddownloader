#!/usr/bin/env python3
"""Local video downloader. Python standard library + installed yt-dlp/FFmpeg."""
import argparse
import json
import mimetypes
import os
import re
import secrets
import shlex
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
from urllib.parse import urlsplit, unquote, quote
from detect_video import discover
from subtitles import save_subtitle
from retention import Retention

os.environ['PATH'] = os.pathsep.join([os.environ.get('PATH', ''), '/opt/homebrew/bin', '/usr/local/bin', str(Path.home() / '.local/bin')])
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
STATE = {'running': False, 'status': 'Siap mengunduh', 'percent': 0, 'logs': [], 'folder': str(Path.home() / 'Downloads'), 'cancelled': False, 'files': []}
PROCESS = None

def get_job_files(folder_path, job_id=''):
    folder = Path(folder_path).resolve()
    if not folder.exists() or not folder.is_dir():
        return []
    results = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and not p.name.startswith('.'):
            # Exclude incomplete temporary files or unmerged format fragments (e.g. .part, .ytdl, .f1242.mp4, batch files)
            if p.name.endswith(('.part', '.ytdl', '.temp', '.txt')) or re.search(r'\.f[0-9a-zA-Z_-]+\.(mp4|m4a|webm|mkv)$', p.name):
                continue
            if job_id:
                url = f'/download/{quote(job_id)}/{quote(p.name)}'
            else:
                url = f'/download/local/{quote(p.name)}'
            results.append({
                'name': p.name,
                'size': p.stat().st_size,
                'url': url
            })
    return results

# Retention is enabled only in VPS mode, inside its dedicated output root.
RETENTION = None
VPS_ROOT = None

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
            '--continue', '--no-abort-on-unavailable-fragments',
            '--retries', '25', '--fragment-retries', '25', '--file-access-retries', '5',
            '--extractor-retries', '10',
            '--retry-sleep', 'http:3', '--retry-sleep', 'fragment:3', '--retry-sleep', 'extractor:3',
            '--socket-timeout', '30', '--no-update',
            '-f', formats.get(quality, formats['best']),
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
    name = re.sub(r'(?i)\.(mp4|vtt|srt|mkv|webm|mp3|m4a|flac|wav|opus|aac)$', '', str(data.get('name', 'Video')).strip())
    return re.sub(r'[^\w .-]', '_', name).strip(' .')[:100] or 'Video'

def build_ytdlp_command(data):
    folder = Path(str(data.get('folder', '')).strip() or '~/Downloads').expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [data.get('yt_dlp') or downloader() or 'yt-dlp']
    cmd += [
        '--ignore-config', '--newline', '--no-colors', '--progress', '--no-quiet',
        '--progress-template', 'download:APP_PROGRESS:%(progress._percent_str)s | %(progress._speed_str)s | ETA %(progress._eta_str)s',
        '--print', 'after_move:APP_FILE:%(filepath)s', '--no-simulate',
        '--continue', '--no-abort-on-unavailable-fragments',
        '--retries', '20', '--fragment-retries', '20', '--file-access-retries', '5',
        '--extractor-retries', '10',
        '--retry-sleep', 'http:3', '--retry-sleep', 'fragment:3',
        '--socket-timeout', '30', '--no-update',
        '-P', str(folder)
    ]
    try:
        concurrent = int(data.get('concurrent_fragments') or 4)
        if 1 <= concurrent <= 16:
            cmd += ['-N', str(concurrent)]
    except (ValueError, TypeError):
        pass

    media_type = data.get('media_type', 'video')
    if media_type == 'audio':
        cmd.append('-x')
        audio_fmt = data.get('audio_format', 'mp3')
        if audio_fmt in ('mp3', 'm4a', 'flac', 'wav', 'opus', 'aac', 'best'):
            cmd += ['--audio-format', audio_fmt]
        audio_q = data.get('audio_quality', '0')
        cmd += ['--audio-quality', str(audio_q)]
    else:
        res = str(data.get('resolution', 'best'))
        res_map = {
            '2160': 'bv*[height<=2160]+ba/b[height<=2160]',
            '1440': 'bv*[height<=1440]+ba/b[height<=1440]',
            '1080': 'bv*[height<=1080]+ba/b[height<=1080]',
            '720': 'bv*[height<=720]+ba/b[height<=720]',
            '480': 'bv*[height<=480]+ba/b[height<=480]',
            '360': 'bv*[height<=360]+ba/b[height<=360]',
        }
        fmt = res_map.get(res, 'bv*+ba/b')
        cmd += ['-f', fmt]
        vid_fmt = data.get('video_format', 'mp4')
        if vid_fmt in ('mp4', 'mkv', 'webm'):
            cmd += ['--merge-output-format', vid_fmt]

    if data.get('write_subs'):
        cmd.append('--write-subs')
    if data.get('auto_subs'):
        cmd.append('--write-auto-subs')
    if data.get('embed_subs') and media_type == 'video':
        cmd.append('--embed-subs')
    if data.get('write_subs') or data.get('auto_subs') or data.get('embed_subs'):
        sub_lang = re.sub(r'[^a-zA-Z0-9,_-]', '', str(data.get('sub_lang', 'id,en'))).strip() or 'id,en'
        cmd += ['--sub-langs', sub_lang]

    if data.get('embed_thumb'):
        cmd.append('--embed-thumbnail')
    if data.get('write_thumb'):
        cmd.append('--write-thumbnail')
    if data.get('embed_metadata'):
        cmd.append('--embed-metadata')
    if data.get('embed_chapters'):
        cmd.append('--embed-chapters')

    if data.get('ignore_errors', True):
        cmd.append('-i')
    if data.get('playlist_start'):
        try:
            cmd += ['--playlist-start', str(int(data['playlist_start']))]
        except (ValueError, TypeError):
            pass
    if data.get('playlist_end'):
        try:
            cmd += ['--playlist-end', str(int(data['playlist_end']))]
        except (ValueError, TypeError):
            pass
    if data.get('max_downloads'):
        try:
            cmd += ['--max-downloads', str(int(data['max_downloads']))]
        except (ValueError, TypeError):
            pass

    raw_name = str(data.get('name', '')).strip()
    clean_name = re.sub(r'[^\w .-]', '_', raw_name).strip(' .')[:100]
    urls = data.get('urls') or [data.get('url', '')]
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
    urls = [clean_url(u) for u in urls if str(u).strip()]
    if not urls:
        raise ValueError('Masukkan minimal satu tautan URL.')

    if len(urls) == 1 and clean_name and clean_name != 'Video':
        cmd += ['-o', f'{clean_name}.%(ext)s']
    else:
        cmd += ['-o', '%(title)s [%(id)s].%(ext)s']

    if data.get('custom_args'):
        extra = shlex.split(str(data['custom_args']))
        disallowed = {'--exec', '--exec-before-download'}
        extra = [arg for arg in extra if not any(arg.startswith(d) for d in disallowed)]
        cmd += extra

    batch_file = None
    if len(urls) > 1:
        fd, batch_path = tempfile.mkstemp(prefix='ytdlp-batch-', suffix='.txt', dir=str(folder))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for u in urls:
                f.write(u + '\n')
        batch_file = batch_path
        cmd += ['--batch-file', batch_path]
    else:
        cmd += ['--', urls[0]]

    return cmd, folder, batch_file

def run_job(data):
    global PROCESS
    def cancelled():
        with LOCK:
            return STATE['cancelled']
    def status(message):
        with LOCK:
            if not STATE['cancelled']:
                STATE['status'] = message
    managed_folder = None
    try:
        if data.get('mode') == 'ytdlp':
            urls = data.get('urls') or [data.get('url', '')]
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.splitlines() if u.strip()]
            urls = [clean_url(u) for u in urls if str(u).strip()]
            with LOCK:
                STATE.update(input_url='\n'.join(urls), video_url='', subtitle_urls=[])
            folder = Path(data.get('folder') or '~/Downloads').expanduser().resolve()
            job_name = output_name(data) if data.get('name') else ('YT_DLP_Batch' if len(urls) > 1 else 'YT_DLP_Download')
            if RETENTION:
                folder = RETENTION.create(job_name)
                managed_folder = folder
            else:
                folder = folder / job_name
            folder.mkdir(parents=True, exist_ok=True)
            data['folder'] = str(folder)
            with LOCK:
                STATE['folder'] = str(folder)
                STATE['target_folder'] = str(folder)
            add_log('Folder hasil: ' + str(folder))
            status('Memulai proses unduhan yt-dlp…')
            cmd, folder, batch_file = build_ytdlp_command(data)
            try:
                run_download(cmd)
            finally:
                if batch_file and os.path.exists(batch_file):
                    try:
                        os.unlink(batch_file)
                    except OSError:
                        pass
            return
        subtitle_only = data.get('mode') == 'subtitle' or urlsplit(data['url']).path.lower().endswith(('.vtt', '.srt'))
        direct = urlsplit(data['url']).path.lower().endswith(('.mp4', '.webm', '.m3u8', '.mpd', '.json'))
        with LOCK:
            STATE.update(input_url=data['url'],
                         video_url=data['url'] if not subtitle_only and (data.get('mode') == 'direct' or (data.get('mode', 'auto') == 'auto' and direct)) else '',
                         subtitle_urls=[data['url']] if subtitle_only else [])
        folder = Path(data.get('folder') or '~/Downloads').expanduser().resolve()
        name = output_name(data)
        if RETENTION:
            folder = RETENTION.create(name)
            managed_folder = folder
        else:
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
        with LOCK:
            STATE['folder'] = str(folder)
            STATE['target_folder'] = str(folder)
        add_log('Folder hasil: ' + str(folder))
        if urlsplit(data['url']).path.lower().endswith(('.vtt', '.srt')) or data.get('mode') == 'subtitle':
            status('Mengunduh subtitle…')
            saved = save_subtitle(dict(url=data['url'], referer=data.get('referer')), folder, name, cancelled)
            add_log('Subtitle: ' + str(saved))
            status('Selesai — subtitle tersimpan di folder tujuan' + (' (akan dihapus otomatis dalam 1 jam).' if RETENTION else '.'))
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
        if folder.exists():
            media_files = get_job_files(folder, managed_folder.name if managed_folder else '')
        else:
            media_files = []
        if managed_folder:
            try:
                deadline = RETENTION.finish(managed_folder)
                add_log('Hasil dan file sementara dihapus otomatis pada ' + time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(deadline)))
            except OSError as exc:
                add_log('Gagal menyimpan jadwal pembersihan: ' + str(exc))
        with LOCK:
            STATE['files'] = media_files
            if STATE['cancelled']:
                STATE['status'] = 'Dihentikan.'
            elif (STATE['status'].startswith('Unduhan') or STATE['status'].startswith('Pencarian')) and any(f['name'].endswith(('.srt', '.vtt')) for f in media_files):
                if not any(f['name'].endswith(('.mp4', '.mkv', '.webm')) for f in media_files):
                    STATE['status'] = 'Unduhan video terputus, tetapi subtitle berhasil disimpan. Unduh subtitle di bawah.'
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
        target_dir = None
        with LOCK:
            if STATE['cancelled']:
                STATE['status'] = 'Dihentikan. File sementara disimpan untuk melanjutkan.'
            elif code == 0:
                STATE['status'] = 'Selesai — file tersimpan di folder tujuan' + (' (akan dihapus otomatis dalam 1 jam).' if RETENTION else '.')
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

    def valid_host(self):
        host = self.headers.get('Host', '')
        if host == '127.0.0.1:' + str(self.server.server_port):
            return True
        if host in ('localhost:' + str(self.server.server_port), '127.0.0.1', 'localhost'):
            return True
        if VPS_ROOT is not None or os.environ.get('ALLOW_ANY_HOST') == '1':
            return True
        return False

    def do_GET(self):
        if not self.valid_host():
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
            snapshot['vps'] = bool(VPS_ROOT)
            snapshot['retention_seconds'] = 3600 if VPS_ROOT else 0
            snapshot['tools'] = {'yt': bool(downloader()), 'ffmpeg': bool(shutil.which('ffmpeg'))}
            self.send(snapshot)
        elif self.path.startswith('/download/'):
            raw_path = self.path[len('/download/'):].split('?', 1)[0]
            parts = raw_path.split('/', 1)
            if len(parts) != 2:
                return self.send({'error': 'Format URL unduhan tidak valid'}, 400)
            job_id = unquote(parts[0])
            filename = unquote(parts[1])
            if '/' in filename or '\\' in filename or filename.startswith('.'):
                return self.send({'error': 'Nama file tidak valid'}, 403)
            if job_id == 'local':
                with LOCK:
                    cur_folder = Path(STATE.get('folder', '')).resolve()
                target = (cur_folder / filename).resolve()
                if not cur_folder or not target.is_relative_to(cur_folder):
                    return self.send({'error': 'Akses ditolak'}, 403)
            else:
                if not job_id.startswith('job-') or '/' in job_id or '\\' in job_id:
                    return self.send({'error': 'ID pekerjaan tidak valid'}, 403)
                if not VPS_ROOT:
                    return self.send({'error': 'Unduhan mode VPS tidak aktif'}, 400)
                vps_base = VPS_ROOT.resolve()
                target = (vps_base / job_id / filename).resolve()
                if not target.is_relative_to(vps_base):
                    return self.send({'error': 'Akses ditolak'}, 403)
            if not target.is_file():
                return self.send({'error': 'File tidak ditemukan atau telah kedaluwarsa'}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
            if filename.lower().endswith(('.srt', '.vtt')):
                ctype = 'text/plain; charset=utf-8'
            file_size = target.stat().st_size
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(file_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                with open(target, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile, length=64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        else:
            self.send({'error': 'Tidak tersedia'}, 403)

    def do_POST(self):
        if not self.valid_host() or not self.authorized():
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
                allowed_keys = (
                    'url', 'name', 'quality', 'folder', 'referer', 'forward', 'mode',
                    'subtitles', 'subtitle_url',
                    'urls', 'media_type', 'video_format', 'audio_format', 'audio_quality',
                    'resolution', 'write_subs', 'auto_subs', 'embed_subs', 'sub_lang',
                    'embed_thumb', 'write_thumb', 'embed_metadata', 'embed_chapters',
                    'playlist_start', 'playlist_end', 'max_downloads', 'concurrent_fragments',
                    'ignore_errors', 'custom_args'
                )
                data = {key: data[key] for key in allowed_keys if key in data}
                if VPS_ROOT:
                    data['folder'] = str(VPS_ROOT)
                if data.get('mode') == 'ytdlp':
                    urls = data.get('urls') or [data.get('url', '')]
                    if isinstance(urls, str):
                        urls = [u.strip() for u in urls.splitlines() if u.strip()]
                    urls = [clean_url(u) for u in urls if str(u).strip()]
                    if not urls:
                        raise ValueError('Masukkan minimal satu tautan URL.')
                    data['urls'] = urls
                    data['url'] = urls[0]
                    with LOCK:
                        if STATE['running']:
                            raise ValueError('Masih ada unduhan yang berjalan.')
                        folder = str(Path(data.get('folder') or '~/Downloads').expanduser().resolve())
                        STATE.update(running=True, cancelled=False, percent=0, logs=[], folder=folder,
                                     status='Menyiapkan unduhan yt-dlp…', input_url='\n'.join(urls),
                                     video_url='', subtitle_urls=[], files=[])
                        threading.Thread(target=run_job, args=(data,), daemon=True).start()
                    return self.send({'ok': True})

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
                    STATE.update(running=True, cancelled=False, percent=0, logs=[], folder=folder, status='Menghubungkan…', input_url=data['url'], video_url='', subtitle_urls=[], files=[])
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
    global RETENTION, VPS_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--vps', action='store_true', help='Isolated downloads, automatically removed after one hour')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--download-dir', default=str(Path.home() / 'video-downloader-data'))
    args = parser.parse_args()
    if args.vps:
        VPS_ROOT = Path(args.download_dir).expanduser().resolve()
        RETENTION = Retention(VPS_ROOT, seconds=3600, log=add_log)
        STATE['folder'] = str(VPS_ROOT)
        def cleanup_worker():
            while True:
                RETENTION.sweep()
                time.sleep(15)
        threading.Thread(target=cleanup_worker, daemon=True).start()
    port = args.port or (6666 if args.vps else 0)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('Video Downloader: ' + url, flush=True)
    if args.vps:
        print(f'Mode VPS aktif (Port: {server.server_port}). File unduhan akan dihapus otomatis dalam 1 jam.', flush=True)
    print('Biarkan Terminal terbuka. Tekan Control+C untuk menutup program.', flush=True)
    if not args.no_browser and not args.vps:
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
