"""Media Importer & Validator for OmniFetch Personal Streaming Library.
Handles ffprobe deep inspection, atomic permanent storage placement, Jellyfin naming conventions,
and index triggering with full error recovery.
"""
import os
import re
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from . import config
from . import library_db
from .jellyfin import jellyfin_client

WINDOWS_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
}

def sanitize_title(title: str) -> str:
    """Sanitizes movie title for cross-platform filesystems (Linux, macOS, Windows)."""
    if not title or not isinstance(title, str):
        return "Unknown Movie"
    clean = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', title).strip()
    clean = re.sub(r'^\.+|\.+$', '', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)
    if not clean or clean.upper() in WINDOWS_RESERVED:
        clean = f"Movie_{os.urandom(2).hex()}"
    return clean[:80].strip() or "Movie"

def parse_year_from_title(title: str, explicit_year: Optional[int] = None) -> Tuple[str, Optional[int]]:
    """Extracts year from title if present like 'Inception (2010)' or 'Interstellar 2014'."""
    if explicit_year and 1900 <= int(explicit_year) <= 2100:
        clean = re.sub(r'\(\s*' + str(explicit_year) + r'\s*\)', '', title).strip()
        return sanitize_title(clean), int(explicit_year)

    match = re.search(r'[\(\[]\s*(\b(19\d\d|20\d\d)\b)\s*[\)\]]', title)
    if match:
        year = int(match.group(1))
        clean = title[:match.start()] + title[match.end():]
        return sanitize_title(clean), year

    # Match trailing year: Title 2024
    match2 = re.search(r'\b(19\d\d|20\d\d)\b$', title.strip())
    if match2:
        year = int(match2.group(1))
        clean = title.strip()[:match2.start()]
        return sanitize_title(clean), year

    return sanitize_title(title), None

def run_ffprobe(file_path: Path) -> Dict[str, Any]:
    """Runs ffprobe on a media file to inspect container, streams, and codecs."""
    # Sniff file header for HTML or JSON error responses disguised as media
    try:
        if file_path.exists():
            with open(file_path, 'rb') as f:
                header = f.read(512).strip()
                if header.startswith(b'<!DOCTYPE') or header.startswith(b'<html') or header.startswith(b'<?xml') or b'<body' in header:
                    return {'valid': False, 'error': 'File is an HTML/XML error response, not a valid media file.'}
                if header.startswith(b'{') and (b'"error"' in header or b'"message"' in header or b'"status"' in header):
                    return {'valid': False, 'error': 'File is a JSON error response, not a valid media file.'}
    except Exception:
        pass

    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        # If ffprobe not available, perform basic size validation
        size = file_path.stat().st_size if file_path.exists() else 0
        return {
            'valid': size > 1024,
            'duration': 0.0,
            'video_codec': None,
            'audio_codec': None,
            'resolution': None,
            'has_video': True,
            'has_audio': True,
            'format_name': file_path.suffix.lstrip('.'),
            'warning': 'ffprobe not found in system PATH; basic validation applied.'
        }

    cmd = [
        ffprobe,
        '-v', 'error',
        '-show_entries', 'format=duration,format_name,size:stream=codec_type,codec_name,width,height',
        '-of', 'json',
        str(file_path)
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return {'valid': False, 'error': f"ffprobe error: {proc.stderr.strip()[:200]}"}

        data = json.loads(proc.stdout)
        fmt = data.get('format', {})
        streams = data.get('streams', [])

        has_video = any(s.get('codec_type') == 'video' for s in streams)
        has_audio = any(s.get('codec_type') == 'audio' for s in streams)
        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)

        duration = float(fmt.get('duration') or 0.0)
        width = video_stream.get('width') if video_stream else None
        height = video_stream.get('height') if video_stream else None
        resolution = f"{width}x{height}" if (width and height) else None

        # Check if file is HTML/JSON disguised as video
        format_name = fmt.get('format_name', '')
        if 'html' in format_name.lower() or 'json' in format_name.lower():
            return {'valid': False, 'error': 'File is HTML/JSON response, not a valid media container.'}

        if not has_video and not has_audio:
            return {'valid': False, 'error': 'No video or audio streams detected in media file.'}

        return {
            'valid': True,
            'duration': duration,
            'video_codec': video_stream.get('codec_name') if video_stream else None,
            'audio_codec': audio_stream.get('codec_name') if audio_stream else None,
            'resolution': resolution,
            'has_video': has_video,
            'has_audio': has_audio,
            'format_name': format_name
        }
    except Exception as e:
        return {'valid': False, 'error': str(e)}

def validate_subtitle_file(sub_path: Path) -> Tuple[bool, Optional[str]]:
    """Validates that subtitle file contains valid UTF-8 text and SRT/VTT timecodes."""
    if not sub_path.exists() or sub_path.stat().st_size == 0:
        return False, "File subtitle kosong atau tidak ditemukan."
    try:
        text = sub_path.read_text(encoding='utf-8', errors='replace')
        if not ('-->' in text or 'WEBVTT' in text or '[Script Info]' in text):
            return False, "Konten subtitle tidak memiliki timestamp waktu (bukan file subtitle valid)."
        return True, None
    except Exception as e:
        return False, f"Format encoding subtitle rusak: {e}"

def convert_vtt_to_srt_file(src_vtt: Path, dest_srt: Path) -> bool:
    """Converts a WebVTT file to standard SubRip UTF-8 SRT."""
    try:
        from subtitles import convert_vtt_to_srt
        raw_text = src_vtt.read_text(encoding='utf-8', errors='replace')
        srt_content = convert_vtt_to_srt(raw_text)
        dest_srt.write_text(srt_content, encoding='utf-8')
        return True
    except Exception as e:
        # Fallback regex converter
        try:
            content = src_vtt.read_text(encoding='utf-8', errors='replace')
            content = re.sub(r'^WEBVTT[^\n]*\n', '', content, flags=re.MULTILINE)
            content = re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', content)
            dest_srt.write_text(content.strip() + '\n', encoding='utf-8')
            return True
        except Exception:
            return False

def check_disk_space_sufficient(target_dir: Path, required_bytes: int) -> Tuple[bool, int, str]:
    """Ensures destination filesystem has at least required_bytes + MIN_FREE_SPACE_BYTES."""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        usage = shutil.disk_usage(target_dir)
        needed = required_bytes + config.MIN_FREE_SPACE_BYTES
        if usage.free < needed:
            free_mb = usage.free // (1024 * 1024)
            needed_mb = needed // (1024 * 1024)
            return False, usage.free, f"Ruang disk tidak cukup. Tersisa: {free_mb} MB, Dibutuhkan: {needed_mb} MB (termasuk batas aman 2 GB)."
        return True, usage.free, ""
    except Exception as e:
        return True, 0, f"Peringatan pemeriksaan kapasitas disk: {e}"

def atomic_transfer_file(src: Path, dest: Path) -> None:
    """Transfers file safely and atomically.
    If same filesystem, uses os.replace.
    If cross-filesystem, copies to .importing temp name, verifies, replaces, then unlinks src.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    same_fs = False
    try:
        same_fs = (src.stat().st_dev == dest.parent.stat().st_dev)
    except Exception:
        same_fs = False

    if same_fs:
        # Fast atomic replace on same filesystem
        temp_dest = dest.with_name(f"{dest.name}.importing_{os.urandom(4).hex()}")
        try:
            os.replace(src, temp_dest)
            os.replace(temp_dest, dest)
        finally:
            if temp_dest.exists():
                try: temp_dest.unlink()
                except OSError: pass
    else:
        # Safe cross-filesystem copy
        temp_dest = dest.with_name(f"{dest.name}.importing_{os.urandom(4).hex()}")
        try:
            shutil.copy2(src, temp_dest)
            if temp_dest.stat().st_size != src.stat().st_size:
                raise IOError(f"Copy size mismatch: expected {src.stat().st_size}, got {temp_dest.stat().st_size}")
            os.replace(temp_dest, dest)
            src.unlink(missing_ok=True)
        finally:
            if temp_dest.exists():
                try: temp_dest.unlink()
                except OSError: pass

class LibraryImporter:
    """Orchestrates importing downloaded media into permanent Jellyfin library."""

    @staticmethod
    def import_job_output(
        job_id: str,
        owner_session: str,
        job_folder: Path,
        raw_title: str,
        explicit_year: Optional[int] = None,
        source_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Validates job files, moves them atomically to Jellyfin library, records in SQLite,
        and triggers Jellyfin indexing.
        """
        # Step 1: Locate media files in download folder
        if not job_folder.exists() or not job_folder.is_dir():
            return {'success': False, 'error': f"Folder pekerjaan '{job_folder}' tidak ditemukan."}

        video_files: List[Path] = []
        subtitle_files: List[Path] = []

        for p in job_folder.iterdir():
            if p.is_file() and not p.name.startswith('.'):
                ext = p.suffix.lower()
                if ext in ('.mp4', '.mkv', '.webm', '.mov', '.avi'):
                    video_files.append(p)
                elif ext in ('.srt', '.vtt'):
                    subtitle_files.append(p)

        if not video_files:
            return {'success': False, 'error': "Tidak ada file video utama yang ditemukan untuk diimpor."}

        primary_video = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]

        # Step 2: Validate primary video with ffprobe
        probe_res = run_ffprobe(primary_video)
        if not probe_res.get('valid'):
            return {'success': False, 'error': f"Validasi media gagal: {probe_res.get('error', 'Format tidak terbaca')}"}
        if not probe_res.get('has_video'):
            return {'success': False, 'error': "Berkas media yang diunduh tidak memiliki trek video (hanya audio). Silakan pilih sumber video/stream utama di pemutar."}

        # Step 3: Parse and sanitize naming
        clean_title, year = parse_year_from_title(raw_title, explicit_year)
        folder_display = f"{clean_title} ({year})" if year else clean_title

        # Check for destination directory uniqueness / conflict
        dest_folder = config.LIBRARY_MOVIES_DIR / folder_display
        if dest_folder.exists():
            # Check if existing item is registered in DB
            existing_item = library_db.find_item_by_title_year(clean_title, year)
            if existing_item and existing_item.get('status') == 'ready':
                # Return duplicate awareness
                return {
                    'success': True,
                    'is_duplicate': True,
                    'item': existing_item,
                    'message': f"Film '{clean_title}' sudah ada di perpustakaan."
                }
            elif not existing_item:
                # Folder exists on disk without DB record, make safe alternate folder
                folder_display = f"{folder_display} - Versi {os.urandom(2).hex()}"
                dest_folder = config.LIBRARY_MOVIES_DIR / folder_display

        # Step 4: Check disk space
        total_import_size = primary_video.stat().st_size + sum(s.stat().st_size for s in subtitle_files)
        ok, free_bytes, space_msg = check_disk_space_sufficient(config.LIBRARY_MOVIES_DIR, total_import_size)
        if not ok:
            return {'success': False, 'error': space_msg}

        dest_folder.mkdir(parents=True, exist_ok=True)

        # Step 5: Transfer primary video file
        video_ext = primary_video.suffix.lower()
        final_video_name = f"{folder_display}{video_ext}"
        final_video_path = dest_folder / final_video_name

        try:
            atomic_transfer_file(primary_video, final_video_path)
        except Exception as e:
            return {'success': False, 'error': f"Gagal memindahkan video ke perpustakaan: {e}"}

        # Step 6: Process and transfer subtitles
        imported_subtitles: List[Path] = []
        for idx, sub_path in enumerate(subtitle_files):
            valid_sub, sub_err = validate_subtitle_file(sub_path)
            if not valid_sub:
                continue

            sub_ext = sub_path.suffix.lower()
            # Determine language tag from file name (default to 'id' if Indonesian or single subtitle)
            lang = 'id'
            for known in ('id', 'in', 'indonesia', 'en', 'eng'):
                if f".{known}." in sub_path.name.lower():
                    lang = 'id' if known in ('id', 'in', 'indonesia') else 'en'
                    break

            final_sub_name = f"{folder_display}.{lang}.srt"
            final_sub_path = dest_folder / final_sub_name

            if sub_ext == '.vtt':
                # Convert VTT to SRT
                converted = convert_vtt_to_srt_file(sub_path, final_sub_path)
                if converted:
                    imported_subtitles.append(final_sub_path)
                    sub_path.unlink(missing_ok=True)
            else:
                try:
                    atomic_transfer_file(sub_path, final_sub_path)
                    imported_subtitles.append(final_sub_path)
                except Exception:
                    pass

        # Step 7: Record into SQLite Database
        rel_folder = str(dest_folder.relative_to(config.LIBRARY_ROOT))
        source_key = library_db.normalize_source_key(source_url)
        item = library_db.create_library_item(
            title=clean_title,
            year=year,
            clean_title=clean_title,
            folder_path=rel_folder,
            media_type='movie',
            source_url=source_url,
            source_key=source_key,
            status='importing'
        )
        item_id = item['id']

        # Record primary video file
        rel_video = str(final_video_path.relative_to(config.LIBRARY_ROOT))
        library_db.add_library_file(
            item_id=item_id,
            file_path=rel_video,
            file_type='video',
            file_size=final_video_path.stat().st_size,
            duration=probe_res.get('duration'),
            video_codec=probe_res.get('video_codec'),
            audio_codec=probe_res.get('audio_codec'),
            resolution=probe_res.get('resolution'),
            is_primary=1
        )

        # Record subtitles
        for sub_p in imported_subtitles:
            rel_sub = str(sub_p.relative_to(config.LIBRARY_ROOT))
            library_db.add_library_file(
                item_id=item_id,
                file_path=rel_sub,
                file_type='subtitle',
                file_size=sub_p.stat().st_size,
                subtitle_lang='id' if '.id.' in sub_p.name else 'en',
                is_primary=0
            )

        # Step 8: Trigger Jellyfin Indexing & Poll
        library_db.update_item_status(item_id, 'indexing')
        scan_triggered = jellyfin_client.trigger_scan()

        jelly_item = None
        if scan_triggered:
            jelly_item = jellyfin_client.poll_item_indexing(
                clean_title,
                year=year,
                expected_folder=str(dest_folder),
                max_retries=5,
                initial_delay=1.5
            )

        if jelly_item:
            jelly_id = jelly_item.get('Id')
            updated_item = library_db.update_item_status(item_id, 'ready', jellyfin_item_id=jelly_id)
            final_status = 'ready'
            msg = f"Film '{clean_title}' berhasil diimpor dan terindeks di Jellyfin!"
        else:
            # Files are SAFE, but Jellyfin scan is pending or Jellyfin is offline
            updated_item = library_db.update_item_status(item_id, 'needs_attention')
            final_status = 'needs_attention'
            msg = f"Film '{clean_title}' telah tersimpan aman di perpustakaan permanen. Menunggu pemindaian katalog Jellyfin."

        # Fetch complete recorded item
        full_item = library_db.get_library_item(item_id)

        return {
            'success': True,
            'status': final_status,
            'item': full_item,
            'message': msg,
            'folder': str(dest_folder),
            'video_file': str(final_video_path),
            'subtitles': [str(s) for s in imported_subtitles]
        }
