"""Personal Library Management, Metadata Editing, Subtitle Manager, and Poster Replacer for OmniFetch Super Admin."""
import os
import shutil
import subprocess
import time
import re
import uuid
import io
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image

from ..core import config
from ..core import library_db
from ..core.security import is_safe_url
from ..core.importer import run_ffprobe
from .audit import log_admin_action

MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024 # 10 MB

def validate_image_bytes(data: bytes) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validates image data using Pillow to verify header, structure, dimensions, and pixel limit.
    Returns (valid: bool, mime: Optional[str], error: Optional[str]).
    """
    if not data or len(data) < 16:
        return False, None, "Ukuran berkas gambar terlalu kecil atau kosong."
    if len(data) > MAX_IMAGE_SIZE_BYTES:
        return False, None, f"Ukuran gambar melebihi batas maksimum ({MAX_IMAGE_SIZE_BYTES // (1024*1024)}MB)."

    try:
        # Step 1: Verify image integrity
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
            fmt = (img.format or '').lower()
            if fmt not in ('jpeg', 'jpg', 'png', 'webp'):
                return False, None, f"Format gambar '{fmt}' tidak didukung (Gunakan format JPEG, PNG, atau WebP)."

        # Step 2: Reload image to check dimensions and pixel limits
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if w * h > 25_000_000:
                return False, None, f"Dimensi piksel gambar terlalu besar ({w}x{h} > 25MP). Batasi ukuran gambar."
            if w < 50 or h < 50:
                return False, None, f"Dimensi gambar terlalu kecil ({w}x{h})."
            mime = 'image/jpeg' if fmt in ('jpeg', 'jpg') else f'image/{fmt}'
            return True, mime, None
    except Exception as e:
        return False, None, f"Berkas gambar rusak atau tidak dapat didekode: {e}"

class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom HTTP redirect handler that strictly verifies network safety on every redirect hop."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        clean_url = newurl.strip()
        if not is_safe_url(clean_url):
            raise urllib.error.HTTPError(newurl, 403, "Pengalihan (Redirect) ke alamat jaringan privat diblokir demi keamanan (SSRF Guard).", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch_image_from_url(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Fetches an image from an external URL with SSRF protection on initial request AND all redirect hops."""
    clean_u = url.strip()
    if not is_safe_url(clean_u):
        return None, "URL gambar tidak aman atau mengarah ke jaringan privat (SSRF Blocked)."

    opener = urllib.request.build_opener(SafeRedirectHandler())
    req = urllib.request.Request(
        clean_u,
        headers={'User-Agent': 'Mozilla/5.0 (OmniFetch Cinema Studio / Image Bot)'}
    )
    try:
        with opener.open(req, timeout=10) as resp:
            final_url = resp.geturl()
            if not is_safe_url(final_url):
                return None, "Pengalihan akhir URL mengarah ke jaringan privat yang diblokir."

            content_length = resp.headers.get('Content-Length')
            if content_length and int(content_length) > MAX_IMAGE_SIZE_BYTES:
                return None, "Ukuran gambar di server sumber melebihi batas 10MB."

            data = resp.read(MAX_IMAGE_SIZE_BYTES + 1024)
            if len(data) > MAX_IMAGE_SIZE_BYTES:
                return None, "Ukuran unduhan gambar melebihi batas 10MB."

            valid, mime, err = validate_image_bytes(data)
            if not valid:
                return None, err
            return data, None
    except urllib.error.HTTPError as he:
        return None, f"Gagal mengunduh gambar (HTTP {he.code}): {he.reason}"
    except Exception as e:
        return None, f"Gagal mengunduh gambar dari URL: {e}"

def fetch_remote_image(url: str) -> Dict[str, Any]:
    """Helper wrapper for fetch_image_from_url returning standardized dictionary result."""
    data, err = fetch_image_from_url(url)
    return {'success': data is not None, 'data': data, 'error': err or ''}

def extract_video_frame(video_path: Path, timestamp_sec: float = 10.0) -> Tuple[Optional[bytes], Optional[str]]:
    """Extracts a frame from a video file using ffmpeg as a JPEG snapshot with strict path containment."""
    try:
        res_vid = video_path.resolve()
        res_vid.relative_to(config.LIBRARY_ROOT.resolve())
    except Exception:
        return None, "Berkas video sumber berada di luar batas direktori perpustakaan."

    if not res_vid.exists() or not res_vid.is_file():
        return None, "Berkas video sumber tidak ditemukan di server."

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return None, "Perangkat lunak FFmpeg tidak terpasang di server untuk mengekstrak frame."

    # Format timestamp: HH:MM:SS.xxx
    m, s = divmod(max(0.0, timestamp_sec), 60)
    h, m = divmod(m, 60)
    time_str = f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

    cmd = [
        ffmpeg, '-y',
        '-ss', time_str,
        '-i', str(res_vid),
        '-vframes', '1',
        '-q:v', '2',
        '-f', 'image2pipe',
        '-vcodec', 'mjpeg',
        'pipe:1'
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15)
        if proc.returncode == 0 and proc.stdout:
            valid, _, err = validate_image_bytes(proc.stdout)
            if valid:
                return proc.stdout, None
            return None, err or "Frame yang diekstrak tidak valid."
        return None, f"FFmpeg gagal mengekstrak frame (Kode keluar {proc.returncode})."
    except Exception as e:
        return None, f"Kesalahan saat menjalankan FFmpeg: {e}"

def save_item_poster_or_backdrop(
    item_id: int,
    image_data: bytes,
    target_type: str = "poster", # 'poster' (2:3) or 'backdrop' (16:9)
    crop_mode: Optional[str] = "center", # 'center', 'top'
    username: str = "admin",
    ip_address: str = ""
) -> Dict[str, Any]:
    """Processes, crops, normalizes, and saves an item's poster or backdrop atomically with backup preservation."""
    item = library_db.get_library_item(item_id)
    if not item:
        return {'success': False, 'error': "Item perpustakaan tidak ditemukan."}

    folder_path = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
    try:
        folder_path.relative_to(config.LIBRARY_ROOT.resolve())
    except ValueError:
        return {'success': False, 'error': "Folder media melanggar batas direktori perpustakaan."}

    if not folder_path.exists() or not folder_path.is_dir():
        folder_path.mkdir(parents=True, exist_ok=True)

    valid, mime, err = validate_image_bytes(image_data)
    if not valid:
        return {'success': False, 'error': err or "Data gambar tidak valid."}

    filename = "poster.jpg" if target_type == "poster" else "backdrop.jpg"
    final_dest = folder_path / filename
    temp_dest = folder_path / f".temp_{uuid.uuid4().hex[:8]}_{filename}"

    try:
        # Use Pillow for crop and format normalization
        with Image.open(io.BytesIO(image_data)) as img:
            # Convert palette / RGBA to RGB for clean JPEG saving
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (15, 23, 42)) # Cinematic slate dark bg
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            orig_w, orig_h = img.size
            # Target aspect ratio: 2:3 for poster, 16:9 for backdrop
            target_ratio = (2.0 / 3.0) if target_type == "poster" else (16.0 / 9.0)
            current_ratio = orig_w / orig_h

            # Crop box calculation
            if current_ratio > target_ratio:
                # Image wider than target aspect ratio: crop width
                new_w = int(orig_h * target_ratio)
                left = int((orig_w - new_w) / 2)
                box = (left, 0, left + new_w, orig_h)
            else:
                # Image taller than target aspect ratio: crop height
                new_h = int(orig_w / target_ratio)
                if crop_mode == 'top':
                    top = 0
                else:
                    top = int((orig_h - new_h) / 2)
                box = (0, top, orig_w, top + new_h)

            cropped = img.crop(box)
            target_dimensions = (600, 900) if target_type == "poster" else (1280, 720)
            resized = cropped.resize(target_dimensions, Image.Resampling.LANCZOS)
            resized.save(str(temp_dest), format='JPEG', quality=92, optimize=True)

        # Atomic replacement: old image is preserved until new image is ready
        shutil.move(str(temp_dest), str(final_dest))
    except Exception as write_err:
        temp_dest.unlink(missing_ok=True)
        return {'success': False, 'error': f"Gagal memproses dan menyimpan berkas gambar: {write_err}"}

    rel_path = f"{item['folder_path']}/{filename}"
    now = time.time()
    if target_type == "poster":
        library_db.update_library_item_metadata(item_id, poster_path=rel_path)
    else:
        library_db.update_library_item_metadata(item_id, backdrop_path=rel_path)

    log_admin_action(
        username=username,
        action="library.update_poster" if target_type == "poster" else "library.update_backdrop",
        resource_type="library_item",
        resource_id=str(item_id),
        details={'target_type': target_type, 'filename': filename, 'size_bytes': len(image_data), 'crop_mode': crop_mode},
        ip_address=ip_address,
        status="success"
    )

    return {
        'success': True,
        'message': f"Gambar {'poster (2:3)' if target_type == 'poster' else 'backdrop (16:9)'} berhasil diperbarui.",
        'relative_path': rel_path,
        'cache_buster': int(now)
    }

def update_item_metadata(
    item_id: int,
    data: dict,
    username: str = "admin",
    ip_address: str = ""
) -> Dict[str, Any]:
    """Updates metadata fields for a library collection item with media verification before ready status."""
    item = library_db.get_library_item(item_id)
    if not item:
        return {'success': False, 'error': "Item tidak ditemukan."}

    year_val = None
    if data.get('year') is not None and str(data.get('year')).strip():
        try: year_val = int(data['year'])
        except ValueError: pass

    desired_status = data.get('status')
    if desired_status == 'ready' and item.get('status') != 'ready':
        # Verify media file presence and validity before allowing status change to 'ready'
        folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
        video_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')] if folder.exists() else []
        if not video_files:
            return {'success': False, 'error': "Tidak dapat menandai status 'ready': Berkas video tidak ditemukan dalam folder koleksi."}
        primary_vid = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]
        probe = run_ffprobe(primary_vid)
        if not probe.get('valid') or not probe.get('has_video'):
            return {'success': False, 'error': f"Validasi media gagal: {probe.get('error', 'Berkas video tidak valid')}. Status ready ditolak."}

    updated = library_db.update_library_item_metadata(
        item_id=item_id,
        title=data.get('title'),
        year=year_val,
        clean_title=data.get('clean_title'),
        synopsis=data.get('synopsis'),
        genres=data.get('genres'),
        is_favorite=data.get('is_favorite'),
        status=desired_status
    )

    log_admin_action(
        username=username,
        action="library.update_metadata",
        resource_type="library_item",
        resource_id=str(item_id),
        details={'old_title': item.get('title'), 'new_title': data.get('title')},
        ip_address=ip_address,
        status="success"
    )

    return {'success': True, 'item': updated, 'message': 'Metadata koleksi berhasil diperbarui.'}

def add_subtitle_file(
    item_id: int,
    subtitle_data: bytes,
    language: str = "id",
    is_default: bool = True,
    username: str = "admin",
    ip_address: str = ""
) -> Dict[str, Any]:
    """Adds or updates a subtitle file, converting SRT to WebVTT automatically with atomic upsert."""
    item = library_db.get_library_item(item_id)
    if not item:
        return {'success': False, 'error': "Item perpustakaan tidak ditemukan."}

    folder_path = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
    try:
        folder_path.relative_to(config.LIBRARY_ROOT.resolve())
    except ValueError:
        return {'success': False, 'error': "Folder media melanggar batas perpustakaan."}

    if not folder_path.exists() or not folder_path.is_dir():
        folder_path.mkdir(parents=True, exist_ok=True)

    text_content = subtitle_data.decode('utf-8', errors='ignore')
    # If SRT, convert to WebVTT
    if not text_content.startswith('WEBVTT'):
        vtt_body = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', text_content)
        vtt_content = f"WEBVTT\n\n{vtt_body}"
    else:
        vtt_content = text_content

    clean_lang = re.sub(r'[^a-zA-Z0-9_-]', '', language)[:10] or "id"
    sub_filename = f"{clean_lang}.vtt"
    dest_path = folder_path / sub_filename

    dest_path.write_text(vtt_content, encoding='utf-8')
    vtt_bytes_len = len(vtt_content.encode('utf-8'))
    file_rel = f"{item['folder_path']}/{sub_filename}"

    # Atomic upsert check: see if a subtitle record already exists for this language
    existing_files = library_db.get_library_files_for_item(item_id)
    matched_sub = None
    for f in existing_files:
        if f.get('file_type') == 'subtitle' and (f.get('subtitle_lang') == clean_lang or Path(f.get('file_path', '')).name == sub_filename):
            matched_sub = f
            break

    if matched_sub:
        with library_db.db_session() as conn:
            conn.execute("UPDATE library_files SET file_size = ? WHERE id = ?;", (vtt_bytes_len, matched_sub['id']))
        sub_id = matched_sub['id']
    else:
        sub_record = library_db.add_library_file(
            item_id=item_id,
            file_path=file_rel,
            file_type='subtitle',
            file_size=vtt_bytes_len,
            subtitle_lang=clean_lang,
            is_primary=1 if is_default else 0
        )
        sub_id = sub_record['id']

    if is_default:
        library_db.set_default_subtitle(item_id, sub_id)

    log_admin_action(
        username=username,
        action="library.add_subtitle",
        resource_type="library_item",
        resource_id=str(item_id),
        details={'filename': sub_filename, 'language': clean_lang, 'is_default': is_default},
        ip_address=ip_address,
        status="success"
    )

    return {'success': True, 'file_id': sub_id, 'message': f"Subtitle '{clean_lang}' berhasil disimpan dan ditetapkan."}

def delete_subtitle_file(
    item_id: int,
    filename: Optional[str] = None,
    file_id: Optional[int] = None,
    username: str = "admin",
    ip_address: str = ""
) -> Dict[str, Any]:
    """Safely deletes a subtitle track by verifying exact server-side record, path containment, and subtitle type.
    Strictly prevents deletion of videos, posters, config files, or external files.
    """
    item = library_db.get_library_item(item_id)
    if not item:
        return {'success': False, 'error': "Item perpustakaan tidak ditemukan."}

    # Step 1: Look up record server-side
    record = None
    if file_id:
        rec = library_db.get_library_file(int(file_id))
        if rec and rec.get('item_id') == item_id:
            record = rec

    if not record and filename:
        clean_name = Path(filename).name
        files = library_db.get_library_files_for_item(item_id)
        for f in files:
            if Path(f.get('file_path', '')).name == clean_name:
                record = f
                break

    if not record:
        return {'success': False, 'error': "Catatan berkas subtitle tidak ditemukan pada koleksi ini."}

    # Step 2: Strict Type Check - Must be a subtitle record
    if record.get('file_type') != 'subtitle':
        return {
            'success': False,
            'error': f"Penolakan keamanan: Berkas '{record.get('file_path')}' bukan subtitle (Tipe: {record.get('file_type')}). Penghapusan dibatalkan untuk melindungi berkas media."
        }

    # Step 3: Strict Path Containment Check
    folder_path = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
    sub_path = (config.LIBRARY_ROOT / record['file_path']).resolve()
    try:
        sub_path.relative_to(folder_path)
        sub_path.relative_to(config.LIBRARY_ROOT.resolve())
    except ValueError:
        return {'success': False, 'error': "Penolakan keamanan: Jalur berkas berada di luar batas folder koleksi."}

    # Step 4: Extension & Symlink Check
    if sub_path.suffix.lower() not in ('.vtt', '.srt', '.sub', '.ass', '.ssa'):
        return {'success': False, 'error': f"Penolakan keamanan: Ekstensi '{sub_path.suffix}' bukan merupakan berkas subtitle yang sah."}

    if sub_path.is_symlink():
        return {'success': False, 'error': "Penolakan keamanan: Symlink tidak diizinkan."}

    # Step 5: Unlink physical file only after passing all checks
    if sub_path.exists() and sub_path.is_file():
        try:
            sub_path.unlink()
        except Exception as unl_err:
            return {'success': False, 'error': f"Gagal menghapus berkas fisik subtitle: {unl_err}"}

    # Step 6: Remove database record by exact ID
    library_db.remove_library_file(record['id'])

    log_admin_action(
        username=username,
        action="library.delete_subtitle",
        resource_type="library_item",
        resource_id=str(item_id),
        details={'file_id': record['id'], 'file_path': record['file_path']},
        ip_address=ip_address,
        status="success"
    )

    return {'success': True, 'message': f"Berkas subtitle '{sub_path.name}' berhasil dihapus secara aman."}
