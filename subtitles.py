"""Small, validated subtitle downloads with session cookies and atomic output."""
import os
import re
import tempfile
import shutil
import subprocess
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

MAX_SIZE = 5_000_000


def subtitle_format(body):
    text = body.decode('utf-8-sig').lstrip()
    if re.match(r'WEBVTT(?:[ \t\r\n]|$)', text):
        return 'vtt'
    if re.search(r'(?m)^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}', text):
        return 'srt'
    raise ValueError('Respons bukan subtitle VTT/SRT yang valid; tautan mungkin kedaluwarsa.')


def convert_to_srt(body, cancelled):
    binary = shutil.which('ffmpeg')
    if not binary:
        raise ValueError('FFmpeg diperlukan untuk mengubah VTT menjadi SRT.')
    with tempfile.TemporaryDirectory(prefix='subtitle-convert-') as directory:
        source = Path(directory) / 'source.vtt'
        target = Path(directory) / 'subtitle.srt'
        source.write_bytes(body)
        process = subprocess.Popen([binary, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-i', str(source), '-map', '0:s:0', '-c:s', 'srt', str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        try:
            while process.poll() is None:
                if cancelled():
                    raise InterruptedError('Dihentikan.')
                if time.monotonic() >= deadline:
                    raise ValueError('Konversi subtitle melewati batas waktu.')
                time.sleep(0.05)
            if process.returncode or not target.exists():
                raise ValueError('Subtitle VTT tidak dapat dikonversi menjadi SRT.')
            result = target.read_bytes()
            if subtitle_format(result) != 'srt':
                raise ValueError('Hasil konversi tidak berisi subtitle SRT yang valid.')
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def language(url):
    match = re.search(r'/i18n/([\w-]+)/', urlsplit(url).path)
    return match.group(1)[:20] if match else 'sub'


def save_subtitle(track, folder, name, cancelled, cookie_file=None):
    if cancelled():
        raise InterruptedError('Dihentikan.')
    body = track.get('body')
    if body is None:
        # Match the downloader's browser identification unless the captured request
        # provides its own. urllib's default identification is rejected by some hosts.
        try:
            from yt_dlp.utils import std_headers
            user_agent = std_headers['User-Agent']
        except ImportError:
            user_agent = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
        headers = {'user-agent': user_agent}
        headers.update({k: v for k, v in track.get('headers', {}).items() if k in ('user-agent', 'origin')})
        if track.get('referer'):
            headers['Referer'] = track['referer']
        jar = MozillaCookieJar()
        if cookie_file:
            jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
        try:
            with build_opener(HTTPCookieProcessor(jar)).open(Request(track['url'], headers=headers), timeout=15) as response:
                chunks, size = [], 0
                while True:
                    if cancelled():
                        raise InterruptedError('Dihentikan.')
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise ValueError('Subtitle melebihi batas 5 MB.')
                    chunks.append(chunk)
                body = b''.join(chunks)
        except HTTPError as exc:
            raise ValueError(f'Subtitle gagal diunduh (HTTP {exc.code}). Coba tautan baru atau isi alamat halaman asal.') from None
        except URLError:
            raise ValueError('Server subtitle tidak dapat dihubungi.') from None
    if len(body) > MAX_SIZE:
        raise ValueError('Subtitle melebihi batas 5 MB.')
    try:
        extension = subtitle_format(body)
    except UnicodeError:
        raise ValueError('Subtitle bukan teks UTF-8 yang valid.') from None
    if extension == 'vtt':
        body = convert_to_srt(body, cancelled)
        extension = 'srt'
    lang = re.sub(r'[^\w-]', '_', track.get('lang') or language(track['url']))[:20] or 'sub'
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    # Link a completed temporary file exclusively, so repeated downloads never overwrite.
    fd, temporary = tempfile.mkstemp(prefix='.subtitle-', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(body)
        count = 1
        while True:
            if cancelled():
                raise InterruptedError('Dihentikan.')
            suffix = '' if count == 1 else f'-{count}'
            target = folder / (f'{name}.{extension}' if count == 1 else f'{name}.{lang}{suffix}.{extension}')
            try:
                os.link(temporary, target)
                return target
            except FileExistsError:
                if target.read_bytes() == body:
                    return target
                count += 1
    finally:
        os.unlink(temporary)
