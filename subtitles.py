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


def decompress_body(body):
    if not body:
        return b''
    if body[:2] == b'\x1f\x8b':
        import gzip
        try:
            return gzip.decompress(body)
        except Exception:
            pass
    if len(body) > 2 and body[:2] in (b'\x78\x9c', b'\x78\xda', b'\x78\x01', b'\x78\x5e'):
        import zlib
        try:
            return zlib.decompress(body)
        except Exception:
            pass
    return body


def py_vtt_to_srt(vtt_text):
    text = re.sub(r'^\uFEFF', '', vtt_text).strip()
    text = re.sub(r'(?i)^WEBVTT[^\n\r]*[\n\r]*', '', text)
    text = re.sub(r'(?is)(?:NOTE|STYLE|REGION)(?:[ \t\r\n][\s\S]*?)?(?=(?:\r?\n\r?\n|\Z))', '', text)
    blocks = re.split(r'\r?\n\r?\n+', text)
    cues = []
    idx = 1
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        time_idx = -1
        for i, l in enumerate(lines):
            if '-->' in l:
                time_idx = i
                break
        if time_idx == -1:
            continue
        time_line = lines[time_idx]
        match = re.search(r'((?:\d{1,2}:)?\d{1,2}:\d{2}[\.,]\d{1,3})\s*-->\s*((?:\d{1,2}:)?\d{1,2}:\d{2}[\.,]\d{1,3})', time_line)
        if not match:
            continue
        def fmt_t(t):
            parts = re.split(r'[\.,]', t)
            hms = parts[0]
            ms = parts[1] if len(parts) > 1 else '000'
            hms_parts = hms.split(':')
            if len(hms_parts) == 3:
                h, m, s = hms_parts
            elif len(hms_parts) == 2:
                h, m, s = '00', hms_parts[0], hms_parts[1]
            else:
                h, m, s = '00', '00', hms_parts[0]
            return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms.ljust(3, '0')[:3]}"
        start_t = fmt_t(match.group(1))
        end_t = fmt_t(match.group(2))
        text_lines = [re.sub(r'<[^>]+>', '', l).strip() for l in lines[time_idx + 1:] if l.strip()]
        if not text_lines:
            continue
        cues.append(f"{idx}\n{start_t} --> {end_t}\n" + "\n".join(text_lines))
        idx += 1
    return "\n\n".join(cues) + "\n" if cues else vtt_text


def subtitle_format(body):
    body = decompress_body(body)
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = body.decode('latin-1', errors='replace')
    text_clean = text.lstrip()

    # 1. Cek WebVTT
    if re.search(r'(?i)\bWEBVTT\b', text_clean[:512]):
        return 'vtt'

    # 2. Cek Timestamp Subtitle (00:00:01.000 --> 00:00:04.000 / koma / jam 1 digit / tanpa jam)
    if re.search(r'(?m)(?:\d{1,2}:)?\d{1,2}:\d{2}[,\.]\d{1,3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,\.]\d{1,3}', text_clean):
        if re.search(r'\d{2}:\d{2}\.\d{3}', text_clean) or re.search(r'<c\.|<v |align:', text_clean):
            return 'vtt'
        return 'srt'

    # 3. Cek format ASS / SSA
    if re.search(r'(?i)\[Script Info\]|Dialogue:\s*\d+', text_clean[:1024]):
        return 'ass'

    # 4. Cek format SAMI / SMI
    if re.search(r'(?i)<SAMI>|<SYNC\s+Start=', text_clean[:1024]):
        return 'smi'

    # 5. Deteksi jika server mengembalikan HTML (anti-bot / token kedaluwarsa)
    if re.search(r'(?i)<!DOCTYPE html|<html|<head|<body', text_clean[:512]):
        raise ValueError('Respons server berupa halaman HTML (tautan proteksi/kedaluwarsa atau akses diblokir oleh host video).')

    raise ValueError('Respons bukan subtitle VTT/SRT yang valid; tautan mungkin kedaluwarsa.')


def convert_to_srt(body, cancelled):
    body = decompress_body(body)
    binary = shutil.which('ffmpeg')
    if binary:
        with tempfile.TemporaryDirectory(prefix='subtitle-convert-') as directory:
            source = Path(directory) / 'source.vtt'
            target = Path(directory) / 'subtitle.srt'
            source.write_bytes(body)
            process = subprocess.Popen([binary, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-i', str(source), '-c:s', 'srt', str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            try:
                while process.poll() is None:
                    if cancelled():
                        raise InterruptedError('Dihentikan.')
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                if target.exists() and target.stat().st_size > 0:
                    return target.read_bytes()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    # Python fallback jika FFmpeg tidak tersedia atau gagal
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = body.decode('latin-1', errors='replace')
    converted = py_vtt_to_srt(text)
    return converted.encode('utf-8')


def language(url):
    match = re.search(r'/i18n/([\w-]+)/', urlsplit(url).path)
    return match.group(1)[:20] if match else 'sub'


def save_subtitle(track, folder, name, cancelled, cookie_file=None):
    if cancelled():
        raise InterruptedError('Dihentikan.')
    body = track.get('body')
    if body is None:
        try:
            from yt_dlp.utils import std_headers
            user_agent = std_headers.get('User-Agent')
        except Exception:
            user_agent = None
        if not user_agent:
            user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'

        headers = {
            'User-Agent': user_agent,
            'Accept': '*/*',
            'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site'
        }
        headers.update({k: v for k, v in track.get('headers', {}).items() if k.lower() in ('user-agent', 'origin', 'referer')})
        if track.get('referer'):
            headers['Referer'] = track['referer']
            try:
                ref_p = urlsplit(track['referer'])
                if ref_p.scheme and ref_p.netloc:
                    headers.setdefault('Origin', f"{ref_p.scheme}://{ref_p.netloc}")
            except Exception:
                pass
        else:
            try:
                parsed_u = urlsplit(track['url'])
                if parsed_u.scheme and parsed_u.netloc:
                    headers['Referer'] = f"{parsed_u.scheme}://{parsed_u.netloc}/"
                    headers.setdefault('Origin', f"{parsed_u.scheme}://{parsed_u.netloc}")
            except Exception:
                pass

        jar = MozillaCookieJar()
        if cookie_file and os.path.isfile(cookie_file):
            try:
                jar.load(cookie_file, ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
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

    body = decompress_body(body)
    if len(body) > MAX_SIZE:
        raise ValueError('Subtitle melebihi batas 5 MB.')
    try:
        extension = subtitle_format(body)
    except UnicodeError:
        raise ValueError('Subtitle bukan teks UTF-8 yang valid.') from None
    if extension in ('vtt', 'ass', 'smi'):
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
