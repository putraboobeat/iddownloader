import os
import shutil
import time
import uuid
import json
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from ..core import config
from ..core import library_db
from ..core.job_manager import job_manager, ACTIVE_STAGES
from .audit import log_admin_action

# Cached scan result to avoid rescanning on every HTTP request
_LAST_SCAN_RESULT: Dict[str, Any] = {
    'scanning': False,
    'progress': 0,
    'last_scanned_at': None,
    'items': [],
    'summary': {
        'total_files': 0,
        'total_bytes': 0,
        'registered_available': 0,
        'missing_file': 0,
        'orphan_file': 0,
        'partial_file': 0,
        'leftover_staging': 0,
        'in_use': 0
    }
}
_SCAN_LOCK = threading.Lock()

def get_managed_roots() -> Dict[str, Path]:
    """Returns canonical roots strictly managed by OmniFetch."""
    roots = {
        'temporary': config.TEMPORARY_DIR.resolve(),
        'staging': config.STAGING_DIR.resolve(),
        'library': config.LIBRARY_ROOT.resolve(),
        'trash': config.TRASH_DIR.resolve()
    }
    if config.VPS_ROOT and config.VPS_ROOT.exists():
        roots['legacy_vps'] = config.VPS_ROOT.resolve()
    return roots

def validate_path_containment(target_path: Path) -> Tuple[bool, Optional[str], Optional[Path]]:
    """Validates that a path resides strictly within one of the managed roots.
    Rejects:
    - Traversal outside managed roots
    - Root of managed directory itself
    - Structural directories (like Movies/)
    - Symlinks that resolve outside managed roots
    - Secret files (.env, cookies, raw sqlite)
    """
    try:
        resolved = target_path.resolve()
    except Exception as e:
        return False, f"Path tidak dapat diverifikasi: {e}", None

    managed_roots = get_managed_roots()
    matched_root = None
    for r_name, r_path in managed_roots.items():
        try:
            resolved.relative_to(r_path)
            # Cannot manipulate the managed root itself
            if resolved == r_path:
                return False, f"Penolakan keamanan: Tidak diizinkan memanipulasi root direktori penyimpanan '{r_name}'.", None
            matched_root = r_path
            break
        except ValueError:
            continue

    if not matched_root:
        return False, "Penolakan keamanan: Jalur berada di luar batas direktori yang dikelola aplikasi.", None

    # Structural directory guard: Movies folder itself must never be manipulated or quarantined
    try:
        movies_dir = config.LIBRARY_MOVIES_DIR.resolve()
        if resolved == movies_dir:
            return False, "Penolakan keamanan: Tidak diizinkan memanipulasi direktori struktural Movies.", None
    except Exception:
        pass

    # Secret file guard
    blocked_names = {'cookies.txt', 'jobs_db.json', 'omnifetch-library.sqlite3', '.env'}
    if resolved.name in blocked_names or resolved.suffix.lower() in ('.sqlite3', '.sqlite', '.db', '.env', '.key', '.pem', '.py'):
        return False, "Penolakan keamanan: Berkas konfigurasi atau database sistem tidak boleh dimodifikasi melalui modul ini.", None

    return True, None, resolved

def is_path_in_use(target_path: Path) -> bool:
    """Checks if a path is actively being written to by any in-flight download job,
    or is a parent or child of an active job directory.
    """
    try:
        resolved = target_path.resolve()
    except Exception:
        return True

    for job in job_manager.jobs.values():
        if job.stage in ACTIVE_STAGES:
            try:
                job_folder = job.folder.resolve()
            except Exception:
                continue

            if resolved == job_folder:
                return True
            try:
                resolved.relative_to(job_folder)
                return True
            except ValueError:
                pass
            try:
                job_folder.relative_to(resolved)
                return True
            except ValueError:
                pass
    return False

def scan_managed_inventory() -> Dict[str, Any]:
    """Performs a full managed directory inventory classification."""
    with _SCAN_LOCK:
        if _LAST_SCAN_RESULT['scanning']:
            return _LAST_SCAN_RESULT
        _LAST_SCAN_RESULT['scanning'] = True

    try:
        is_ok, storage_err = config.check_storage_available()
        if not is_ok:
            _LAST_SCAN_RESULT['storage_available'] = False
            _LAST_SCAN_RESULT['storage_error'] = storage_err
            return _LAST_SCAN_RESULT

        managed = get_managed_roots()
        for p in managed.values():
            if p.exists() or p.parent.exists():
                p.mkdir(parents=True, exist_ok=True)

        # 1. Gather all known database paths
        all_lib_items = library_db.list_library_items(limit=100000)
        lib_folders_map = {}
        for it in all_lib_items:
            f_rel = it.get('folder_path', '')
            if f_rel:
                abs_f = (config.LIBRARY_ROOT / f_rel).resolve()
                lib_folders_map[abs_f] = it

        # 2. Gather active jobs
        active_job_folders = {j.folder.resolve(): j for j in job_manager.jobs.values() if j.stage in ACTIVE_STAGES}

        inventory_items = []
        counts = {
            'total_files': 0,
            'total_bytes': 0,
            'registered_available': 0,
            'missing_file': 0,
            'orphan_file': 0,
            'partial_file': 0,
            'leftover_staging': 0,
            'in_use': 0
        }

        # Check for missing database files
        for abs_folder, it in lib_folders_map.items():
            if not abs_folder.exists() or not abs_folder.is_dir():
                counts['missing_file'] += 1
                inventory_items.append({
                    'id': f"item_missing_{it['id']}",
                    'name': it['title'],
                    'relative_path': it.get('folder_path'),
                    'category': 'library',
                    'size_bytes': 0,
                    'mtime': it.get('updated_at', 0),
                    'related_item_id': it['id'],
                    'related_title': it['title'],
                    'status': 'missing_file',
                    'status_label': 'Catatan Database Tanpa File',
                    'is_active': False
                })

        # Scan files in managed roots
        for cat_name, root_path in managed.items():
            if not root_path.exists():
                continue

            entries_to_check = []
            if cat_name == 'library':
                # Traverse inside Movies directory to inspect actual film collection folders
                movies_dir = config.LIBRARY_MOVIES_DIR
                if movies_dir.exists():
                    for m_child in movies_dir.iterdir():
                        if not m_child.name.startswith('.'):
                            entries_to_check.append(m_child)
                # Also check direct entries in LIBRARY_ROOT (excluding Movies folder itself)
                for r_child in root_path.iterdir():
                    if not r_child.name.startswith('.') and r_child != movies_dir:
                        entries_to_check.append(r_child)
            else:
                entries_to_check = [e for e in root_path.iterdir() if not e.name.startswith('.')]

            for entry in entries_to_check:
                # Symlink security check: reject symlinks pointing outside root
                if entry.is_symlink():
                    try:
                        resolved_symlink = entry.resolve()
                        resolved_symlink.relative_to(root_path)
                    except ValueError:
                        continue # Skip symlinks escaping root

                res_entry = entry.resolve()
                is_active = res_entry in active_job_folders or is_path_in_use(res_entry)

                if res_entry.is_file():
                    size = res_entry.stat().st_size
                    mtime = res_entry.stat().st_mtime
                    counts['total_files'] += 1
                    counts['total_bytes'] += size

                    # Status classification
                    if is_active:
                        status = 'in_use'
                        label = 'Sedang Dipakai'
                        counts['in_use'] += 1
                    elif entry.name.endswith(('.part', '.ytdl', '.temp')):
                        status = 'partial_file'
                        label = 'File Parsial'
                        counts['partial_file'] += 1
                    elif cat_name == 'trash':
                        status = 'trashed'
                        label = 'Dalam Karantina'
                    else:
                        status = 'orphan_file'
                        label = 'File Tanpa Catatan'
                        counts['orphan_file'] += 1

                    inventory_items.append({
                        'id': f"{cat_name}_{entry.name}",
                        'name': entry.name,
                        'relative_path': str(entry.relative_to(root_path)),
                        'category': cat_name,
                        'size_bytes': size,
                        'mtime': mtime,
                        'related_item_id': None,
                        'related_title': None,
                        'status': status,
                        'status_label': label,
                        'is_active': is_active
                    })

                elif res_entry.is_dir():
                    dir_size = 0
                    file_count = 0
                    has_partial = False
                    for child in res_entry.rglob('*'):
                        if child.is_file():
                            file_count += 1
                            try:
                                dir_size += child.stat().st_size
                            except OSError:
                                pass
                            if child.name.endswith(('.part', '.ytdl', '.temp')):
                                has_partial = True

                    counts['total_files'] += file_count
                    counts['total_bytes'] += dir_size
                    mtime = res_entry.stat().st_mtime

                    # Classify directory
                    related_it = lib_folders_map.get(res_entry)
                    if is_active:
                        status = 'in_use'
                        label = 'Sedang Diproses'
                        counts['in_use'] += 1
                    elif cat_name == 'staging':
                        status = 'leftover_staging'
                        label = 'Staging Tertinggal'
                        counts['leftover_staging'] += 1
                    elif has_partial:
                        status = 'partial_file'
                        label = 'Unduhan Parsial'
                        counts['partial_file'] += 1
                    elif related_it:
                        # Verify that primary video file actually exists in folder
                        video_files = [p for p in res_entry.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')]
                        if video_files:
                            status = 'registered_available'
                            label = 'Terdaftar & Tersedia'
                            counts['registered_available'] += 1
                        else:
                            status = 'missing_file'
                            label = 'Berkas Video Hilang'
                            counts['missing_file'] += 1
                    elif cat_name == 'trash':
                        status = 'trashed'
                        label = 'Dalam Karantina'
                    else:
                        status = 'orphan_file'
                        label = 'Folder Tanpa Catatan'
                        counts['orphan_file'] += 1

                    inventory_items.append({
                        'id': f"{cat_name}_{entry.name}",
                        'name': entry.name,
                        'relative_path': str(entry.relative_to(root_path)),
                        'category': cat_name,
                        'size_bytes': dir_size,
                        'mtime': mtime,
                        'related_item_id': related_it['id'] if related_it else None,
                        'related_title': related_it['title'] if related_it else None,
                        'status': status,
                        'status_label': label,
                        'is_active': is_active
                    })

        _LAST_SCAN_RESULT['items'] = sorted(inventory_items, key=lambda x: x['mtime'], reverse=True)
        _LAST_SCAN_RESULT['summary'] = counts
        _LAST_SCAN_RESULT['last_scanned_at'] = time.time()
    finally:
        _LAST_SCAN_RESULT['scanning'] = False

    return _LAST_SCAN_RESULT

def get_latest_inventory_snapshot(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Returns the cached inventory snapshot without triggering a blocking filesystem crawl.
    Supports pagination via limit and offset.
    """
    if _LAST_SCAN_RESULT['last_scanned_at'] is None and not _LAST_SCAN_RESULT['scanning']:
        scan_managed_inventory()

    all_items = _LAST_SCAN_RESULT.get('items', [])
    total_count = len(all_items)
    paginated_items = all_items[offset:offset + limit]

    return {
        'scanning': _LAST_SCAN_RESULT.get('scanning', False),
        'last_scanned_at': _LAST_SCAN_RESULT.get('last_scanned_at'),
        'summary': _LAST_SCAN_RESULT.get('summary', {}),
        'total': total_count,
        'limit': limit,
        'offset': offset,
        'items': paginated_items
    }

def trigger_background_inventory_scan():
    """Triggers scan in a background thread."""
    if not _LAST_SCAN_RESULT['scanning']:
        threading.Thread(target=scan_managed_inventory, daemon=True).start()

def quarantine_managed_path(category: str, relative_path: str, username: str = "admin", ip_address: str = "") -> Dict[str, Any]:
    """Safely moves a managed file/directory to quarantine trash.
    Strictly verifies path containment, rejects files in use, and logs audit trail.
    """
    managed = get_managed_roots()
    if category not in managed:
        return {'success': False, 'error': f"Kategori '{category}' tidak valid."}

    root_dir = managed[category]
    target_path = (root_dir / relative_path).resolve()

    # 1. Path Containment Check
    valid, err_msg, safe_path = validate_path_containment(target_path)
    if not valid or not safe_path:
        return {'success': False, 'error': err_msg or "Path tidak valid"}

    if not safe_path.exists():
        return {'success': False, 'error': "Berkas atau folder tidak ditemukan di disk."}

    # 2. Check if currently in use
    if is_path_in_use(safe_path):
        return {'success': False, 'error': "Berkas sedang digunakan oleh proses unduhan/impor yang aktif."}

    # 3. Calculate size before moving
    file_size = 0
    if safe_path.is_file():
        file_size = safe_path.stat().st_size
    else:
        for f in safe_path.rglob('*'):
            if f.is_file():
                try: file_size += f.stat().st_size
                except OSError: pass

    # 4. Move to TRASH_DIR with unique name
    trash_root = config.TRASH_DIR
    trash_root.mkdir(parents=True, exist_ok=True)
    trash_dest = trash_root / f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_path.name}"

    try:
        shutil.move(str(safe_path), str(trash_dest))
    except Exception as move_err:
        return {'success': False, 'error': f"Gagal memindahkan ke karantina: {move_err}"}

    # 5. Record in SQLite trash_items
    retention_days = library_db.get_setting("trash_retention_days", config.TRASH_RETENTION_DAYS)
    trash_entry = library_db.create_trash_entry(
        original_path=str(safe_path),
        trash_path=str(trash_dest),
        item_type=category,
        resource_id=safe_path.name,
        name=safe_path.name,
        file_size=file_size,
        deleted_by=username,
        expires_at=time.time() + (retention_days * 86400)
    )

    log_admin_action(
        username=username,
        action="storage.quarantine",
        resource_type=category,
        resource_id=safe_path.name,
        details={'original_path': str(safe_path), 'trash_path': str(trash_dest), 'size_bytes': file_size},
        ip_address=ip_address,
        status="success"
    )

    # Refresh inventory cache in background
    trigger_background_inventory_scan()

    return {
        'success': True,
        'message': f"Berkas '{safe_path.name}' berhasil dipindahkan ke karantina (Trash).",
        'trash_entry': trash_entry
    }

def restore_from_trash(trash_id: int, username: str = "admin", ip_address: str = "") -> Dict[str, Any]:
    """Restores a quarantined file/folder back to its original location and restores catalog metadata."""
    entry = library_db.get_trash_entry(trash_id)
    if not entry or entry.get('status') != 'trashed':
        return {'success': False, 'error': "Item karantina tidak ditemukan atau sudah dipulihkan."}

    trash_path = Path(entry['trash_path'])
    orig_path = Path(entry['original_path'])

    if not trash_path.exists():
        return {'success': False, 'error': "Berkas fisik di folder karantina sudah tidak ada."}

    # Validate destination containment
    valid, err_msg, safe_orig = validate_path_containment(orig_path)
    if not valid or not safe_orig:
        return {'success': False, 'error': f"Lokasi pemulihan tidak sah: {err_msg}"}

    if safe_orig.exists():
        return {'success': False, 'error': "Lokasi asli sudah memiliki berkas dengan nama yang sama. Ganti nama terlebih dahulu."}

    try:
        safe_orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_path), str(safe_orig))
    except Exception as e:
        return {'success': False, 'error': f"Gagal memulihkan berkas: {e}"}

    # Restore database metadata if snapshot exists
    if entry.get('metadata_snapshot'):
        try:
            snapshot_data = json.loads(entry['metadata_snapshot'])
            library_db.restore_library_item_snapshot(snapshot_data)
        except Exception as snap_err:
            print(f"Peringatan pemulihan metadata snapshot: {snap_err}")

    library_db.update_trash_status(trash_id, 'restored')
    log_admin_action(
        username=username,
        action="storage.restore",
        resource_type="trash",
        resource_id=str(trash_id),
        details={'restored_to': str(safe_orig)},
        ip_address=ip_address,
        status="success"
    )
    trigger_background_inventory_scan()
    return {'success': True, 'message': f"Item '{entry['name']}' berhasil dipulihkan ke lokasi semula."}

def purge_trash_item(trash_id: int, username: str = "admin", ip_address: str = "") -> Dict[str, Any]:
    """Permanently deletes an item from quarantine trash."""
    entry = library_db.get_trash_entry(trash_id)
    if not entry:
        return {'success': False, 'error': "Item karantina tidak ditemukan."}

    trash_path = Path(entry['trash_path']).resolve()
    # Confirm it is inside TRASH_DIR and is not the root of TRASH_DIR itself
    try:
        trash_root = config.TRASH_DIR.resolve()
        trash_path.relative_to(trash_root)
        if trash_path == trash_root:
            return {'success': False, 'error': "Penolakan keamanan: Tidak diizinkan menghapus root folder karantina."}
    except ValueError:
        return {'success': False, 'error': "Penolakan keamanan: Path bukan di dalam folder karantina."}

    if trash_path.exists():
        try:
            if trash_path.is_file():
                trash_path.unlink()
            else:
                shutil.rmtree(trash_path)
        except Exception as e:
            return {'success': False, 'error': f"Gagal menghapus berkas fisik: {e}"}

    library_db.update_trash_status(trash_id, 'purged')
    log_admin_action(
        username=username,
        action="storage.purge",
        resource_type="trash",
        resource_id=str(trash_id),
        details={'purged_path': str(trash_path)},
        ip_address=ip_address,
        status="success"
    )
    trigger_background_inventory_scan()
    return {'success': True, 'message': f"Item '{entry['name']}' berhasil dihapus permanen."}

def clean_expired_trash(username: str = "system") -> int:
    """Purges quarantine trash entries whose retention has expired."""
    now = time.time()
    trashed_items = library_db.list_trash_items(status='trashed', limit=1000)
    purged_count = 0
    for item in trashed_items.get('items', []):
        if item.get('expires_at') and item['expires_at'] <= now:
            res = purge_trash_item(item['id'], username=username)
            if res.get('success'):
                purged_count += 1
    return purged_count

def clean_expired_history(username: str = "system") -> int:
    """Prunes terminal job history entries whose retention period has passed and file does not exist."""
    history_days = library_db.get_setting("history_retention_days", config.HISTORY_RETENTION_DAYS)
    cutoff_ts = time.time() - (history_days * 86400)
    pruned = library_db.prune_terminal_jobs(cutoff_ts)
    if pruned > 0:
        log_admin_action(
            username=username,
            action="storage.clean_expired_history",
            details={'pruned_count': pruned, 'cutoff_ts': cutoff_ts}
        )
    return pruned

def get_effective_trash_retention_days() -> int:
    """Returns effective quarantine trash retention days from persistent settings."""
    return int(library_db.get_setting("trash_retention_days", config.TRASH_RETENTION_DAYS))
