import os
import shutil
import time
import zipfile
import json
from pathlib import Path
from urllib.parse import quote

def save_metadata(folder: Path, metadata: dict):
    """Saves job metadata (including ownership) to a hidden file."""
    meta_path = folder / '.metadata.json'
    meta_path.write_text(json.dumps(metadata, indent=2))

def read_metadata(folder: Path) -> dict:
    """Reads metadata for a job folder."""
    meta_path = folder / '.metadata.json'
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except Exception:
            pass
    return {}

def ensure_job_zip(folder: Path, job_name: str):
    """Packages the folder into a ZIP if it contains multiple media files."""
    valid_files = []
    has_video = False
    for p in folder.iterdir():
        if p.is_file() and not p.name.startswith('.'):
            if p.name.endswith(('.part', '.ytdl', '.temp', '.txt', '.zip')) or '.f' in p.name:
                continue
            valid_files.append(p)
            if p.name.endswith(('.mp4', '.mkv', '.webm', '.m4a', '.mp3')):
                has_video = True
                
    if len(valid_files) > 1 and has_video:
        clean_zip_name = (job_name or folder.name).replace('/', '_')
        zip_path = folder / f'{clean_zip_name}.zip'
        try:
            with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as zf:
                for vf in sorted(valid_files, key=lambda x: x.name):
                    zf.write(vf, arcname=vf.name)
            return zip_path
        except Exception:
            pass
    return None

def get_job_files(folder: Path, job_id: str = '', owner_session: str = ''):
    """Returns a list of files ready for download in a specific job folder."""
    if not folder.exists() or not folder.is_dir():
        return []
        
    results = []
    zip_items = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and not p.name.startswith('.'):
            # Exclude incomplete files
            if p.name.endswith(('.part', '.ytdl', '.temp', '.txt')) or '.f' in p.name:
                continue
                
            if job_id:
                url = f'/download/{quote(job_id)}/{quote(p.name)}'
                if owner_session:
                    url += f'?session={owner_session}'
            else:
                url = f'/download/local/{quote(p.name)}'
                
            item = {
                'name': p.name,
                'size': p.stat().st_size,
                'url': url,
                'is_zip': p.name.endswith('.zip')
            }
            if p.name.endswith('.zip'):
                zip_items.append(item)
            else:
                results.append(item)
                
    return zip_items + results
