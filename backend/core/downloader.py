import shutil
import sys
import re
from pathlib import Path

def get_downloader_cmd():
    try:
        import importlib.util
        if importlib.util.find_spec('yt_dlp'):
            return [sys.executable, '-m', 'yt_dlp']
    except ImportError:
        pass
    binary = shutil.which('yt-dlp')
    return [binary] if binary else ['yt-dlp']

def build_ytdlp_command(data: dict, folder: Path, cookies_path: str = None) -> list:
    """Builds a secure yt-dlp command using an allowlist of arguments."""
    cmd = get_downloader_cmd()
    
    cmd += [
        '--no-cache-dir', '--ignore-config', '--newline', '--no-colors', '--progress', '--no-quiet',
        '--progress-template', 'download:APP_PROGRESS:%(progress._percent_str)s | %(progress._speed_str)s | ETA %(progress._eta_str)s',
        '--print', 'after_move:APP_FILE:%(filepath)s', '--no-simulate',
        '--continue', '--no-abort-on-unavailable-fragments',
        '--retries', '20', '--fragment-retries', '20', '--file-access-retries', '5',
        '--extractor-retries', '10',
        '--retry-sleep', 'http:3', '--retry-sleep', 'fragment:3',
        '--socket-timeout', '30', '--no-update',
        '-P', str(folder)
    ]
    
    # Turbo mode
    if shutil.which('aria2c'):
        cmd += ['--external-downloader', 'aria2c', '--external-downloader-args', 'aria2c:-x 16 -s 16 -k 1M']
        
    try:
        concurrent = int(data.get('concurrent_fragments') or 4)
        if 1 <= concurrent <= 16:
            cmd += ['-N', str(concurrent)]
    except (ValueError, TypeError):
        pass

    # Extract URLs securely
    raw_urls = data.get('urls') or [data.get('url', '')]
    if isinstance(raw_urls, str):
        parsed_urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    else:
        parsed_urls = [str(u).strip() for u in raw_urls if str(u).strip()]
        
    is_instagram = any('instagram.com' in u.lower() for u in parsed_urls)
    
    # User session cookies integration
    if cookies_path and Path(cookies_path).is_file():
        cmd += ['--cookies', str(cookies_path)]
        
    # Safe User-Agent
    cmd += ['--add-header', 'User-Agent:Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36']
    
    if is_instagram:
        cmd += ['--add-header', 'Referer:https://www.instagram.com/']
        cmd += ['--yes-playlist', '--write-thumbnail']
        
    # Batch delays
    if len(parsed_urls) > 1 or is_instagram:
        cmd += ['--sleep-requests', '1.5', '--sleep-interval', '2', '--max-sleep-interval', '5']
        
    # Time limits
    s = str(data.get('start_time', '')).strip()
    e = str(data.get('end_time', '')).strip()
    if s or e:
        cmd += ['--download-sections', f'*{s or "00:00:00"}-{e or "inf"}', '--force-keyframes-at-cuts']

    # Subtitles
    if data.get('embed_subs') or data.get('write_subs'):
        cmd += ['--write-subs']
        if data.get('auto_subs'):
            cmd += ['--write-auto-subs']
        if data.get('embed_subs'):
            cmd += ['--embed-subs']
        lang = str(data.get('sub_lang', 'id,en')).strip()
        if re.match(r'^[a-zA-Z0-9,-]+$', lang):
            cmd += ['--sub-langs', lang]

    # Preset cerdas
    preset = data.get('preset', '')
    if preset == 'audio_only':
        data['media_type'] = 'audio'
    elif preset == 'mobile':
        data['resolution'] = '720'
        data['video_format'] = 'mp4'
    elif preset == 'smallest':
        data['resolution'] = '480'
    elif preset == 'subtitle_only':
        data['write_subs'] = True
        cmd += ['--skip-download']
        
    # Media selection
    if data.get('media_type') == 'audio':
        cmd.append('-x')
        audio_fmt = data.get('audio_format', 'mp3')
        if audio_fmt in ('mp3', 'm4a', 'flac', 'wav', 'opus', 'aac', 'best'):
            cmd += ['--audio-format', audio_fmt]
        audio_q = data.get('audio_quality', '0')
        if str(audio_q) in ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9'):
            cmd += ['--audio-quality', str(audio_q)]
        if data.get('embed_thumb'):
            cmd += ['--embed-thumbnail']
    elif preset != 'subtitle_only':
        res = str(data.get('resolution', 'best'))
        # Updated res_map to be more compatible
        res_map = {
            'best': 'bv*+ba/b',
            '1080': 'bv*[height<=1080]+ba/b[height<=1080]',
            '720': 'bv*[height<=720]+ba/b[height<=720]',
            '480': 'bv*[height<=480]+ba/b[height<=480]'
        }
        cmd += ['-f', res_map.get(res, res_map['best'])]
        fmt = data.get('video_format', 'mp4')
        if fmt in ('mp4', 'mkv', 'webm', 'mov'):
            cmd += ['--merge-output-format', fmt, '--remux-video', fmt]

    if data.get('embed_metadata'):
        cmd += ['--embed-metadata']
    if data.get('embed_thumb'):
        cmd += ['--embed-thumbnail']
        
    # Strictly allowlisted custom arguments (No more generic denylist)
    custom_args = data.get('custom_args', '').strip()
    if custom_args:
        allowed = {'--no-check-certificates', '--geo-bypass', '--yes-playlist', '--no-playlist'}
        for arg in custom_args.split():
            if arg in allowed:
                cmd.append(arg)
                
    if len(parsed_urls) > 1:
        batch_file = folder / '.batch.txt'
        batch_file.write_text('\n'.join(parsed_urls))
        cmd += ['-i', '-a', str(batch_file)]
        if data.get('playlist_start'):
            cmd += ['--playlist-start', str(int(data.get('playlist_start')))]
        if data.get('playlist_end'):
            cmd += ['--playlist-end', str(int(data.get('playlist_end')))]
    else:
        cmd += ['--', parsed_urls[0]]
        
    return cmd
