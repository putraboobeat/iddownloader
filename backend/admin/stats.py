"""Honest Statistics, Health Diagnostics, and Activity Aggregation for OmniFetch Super Admin."""
import time
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core import config
from ..core import library_db
from ..core.job_manager import job_manager, ACTIVE_STAGES
from ..core.downloader import get_downloader_cmd
from ..core.jellyfin import jellyfin_client

def get_target_timezone() -> ZoneInfo:
    tz_str = library_db.get_setting("analytics_timezone", config.ANALYTICS_TIMEZONE)
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo("Asia/Jakarta")

def get_dir_size_bytes(directory: Path) -> int:
    """Calculates total size in bytes of files within a directory without crossing mounts."""
    if not directory.exists() or not directory.is_dir():
        return 0
    total = 0
    try:
        for entry in directory.rglob('*'):
            if entry.is_file() and not entry.is_symlink():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total

_HEALTH_CACHE: Optional[Dict[str, Any]] = None
_HEALTH_CACHE_TIME: float = 0.0

_STORAGE_CACHE: Optional[Dict[str, Any]] = None
_STORAGE_CACHE_TIME: float = 0.0

def get_health_status(force: bool = False) -> Dict[str, Any]:
    """Inspects subsystem health for yt-dlp, FFmpeg, SQLite, and Jellyfin with 60s cache."""
    global _HEALTH_CACHE, _HEALTH_CACHE_TIME
    now = time.time()
    if not force and _HEALTH_CACHE and (now - _HEALTH_CACHE_TIME < 60.0):
        return _HEALTH_CACHE

    # 1. Downloader
    try:
        dl_cmd = get_downloader_cmd()
        proc = subprocess.run(dl_cmd + ['--version'], capture_output=True, text=True, timeout=5)
        dl_ver = proc.stdout.strip() if proc.returncode == 0 else "Error"
        dl_ok = proc.returncode == 0
    except Exception:
        dl_ver = "Tidak terpasang / Error"
        dl_ok = False

    # 2. FFmpeg & ffprobe
    ffmpeg_path = shutil.which('ffmpeg')
    ffprobe_path = shutil.which('ffprobe')
    ffmpeg_ok = bool(ffmpeg_path and ffprobe_path)

    # 3. Database
    db_path = library_db.get_db_path()
    db_size = db_path.stat().st_size if db_path.exists() else 0
    db_ok = db_path.exists()

    # 4. Jellyfin (Optional external service)
    jf_health = jellyfin_client.check_health()

    res = {
        'downloader': {
            'ok': dl_ok,
            'version': dl_ver,
            'name': 'yt-dlp'
        },
        'ffmpeg': {
            'ok': ffmpeg_ok,
            'ffmpeg_path': ffmpeg_path or 'Not found',
            'ffprobe_path': ffprobe_path or 'Not found'
        },
        'database': {
            'ok': db_ok,
            'size_bytes': db_size,
            'path': str(db_path)
        },
        'jellyfin': {
            'configured': bool(config.JELLYFIN_INTERNAL_URL),
            'online': jf_health.get('online', False),
            'message': jf_health.get('message', ''),
            'fallback_note': 'Pemutar web bawaan OmniFetch bekerja mandiri tanpa ketergantungan pada Jellyfin.'
        },
        'timestamp': now
    }
    _HEALTH_CACHE = res
    _HEALTH_CACHE_TIME = now
    return res

def get_storage_breakdown(force: bool = False) -> Dict[str, Any]:
    """Gathers filesystem capacity and individual managed directory sizes with 60s cache."""
    global _STORAGE_CACHE, _STORAGE_CACHE_TIME
    now = time.time()
    if not force and _STORAGE_CACHE and (now - _STORAGE_CACHE_TIME < 60.0):
        return _STORAGE_CACHE

    # Filesystem usage for library mount
    check_path = config.LIBRARY_ROOT if config.LIBRARY_ROOT.exists() else Path.cwd()
    try:
        usage = shutil.disk_usage(check_path)
        total_b = usage.total
        free_b = usage.free
        used_b = total_b - free_b
        percent_used = round((used_b / total_b * 100), 1) if total_b > 0 else 0.0
    except Exception:
        total_b = free_b = used_b = percent_used = 0

    # Individual managed directory sizes
    temp_size = get_dir_size_bytes(config.TEMPORARY_DIR)
    staging_size = get_dir_size_bytes(config.STAGING_DIR)
    library_size = get_dir_size_bytes(config.LIBRARY_ROOT)
    trash_size = get_dir_size_bytes(config.TRASH_DIR)

    warn_threshold = library_db.get_setting("disk_warning_percent", config.DISK_WARNING_PERCENT)
    is_warning = percent_used >= warn_threshold

    res = {
        'filesystem': {
            'total_bytes': total_b,
            'free_bytes': free_b,
            'used_bytes': used_b,
            'percent_used': percent_used,
            'warning_threshold_percent': warn_threshold,
            'is_warning': is_warning
        },
        'directories': {
            'temporary': {
                'path': str(config.TEMPORARY_DIR),
                'size_bytes': temp_size
            },
            'staging': {
                'path': str(config.STAGING_DIR),
                'size_bytes': staging_size
            },
            'library': {
                'path': str(config.LIBRARY_ROOT),
                'size_bytes': library_size
            },
            'trash': {
                'path': str(config.TRASH_DIR),
                'size_bytes': trash_size
            }
        },
        'timestamp': now
    }
    _STORAGE_CACHE = res
    _STORAGE_CACHE_TIME = now
    return res

def get_success_rate(start_time: float) -> Dict[str, Any]:
    """Helper calculating honest success rate via SQL aggregation with display string."""
    val, sample_count = library_db.get_jobs_success_rate_aggregate(start_time)
    return {
        'value': val,
        'display': f"{val}%" if val is not None else "Belum ada data",
        'sample_count': sample_count
    }

def get_dashboard_summary() -> Dict[str, Any]:
    """Generates complete, honest operational metrics for the Super Admin dashboard."""
    tz = get_target_timezone()
    now_ts = time.time()
    dt_now = datetime.fromtimestamp(now_ts, tz=tz)
    # Start of today in configured timezone (Asia/Jakarta)
    dt_today_start = datetime(dt_now.year, dt_now.month, dt_now.day, 0, 0, 0, tzinfo=tz)
    ts_today_start = dt_today_start.timestamp()

    # Active memory worker states
    active_jobs = list(job_manager.jobs.values())
    queued_count = sum(1 for j in active_jobs if j.stage == "queued")
    downloading_count = sum(1 for j in active_jobs if j.stage == "downloading")
    validating_count = sum(1 for j in active_jobs if j.stage == "validating")
    importing_count = sum(1 for j in active_jobs if j.stage == "importing")
    indexing_count = sum(1 for j in active_jobs if j.stage == "indexing")
    total_processing = downloading_count + validating_count + importing_count + indexing_count

    # Downloads completed today strictly calculated by completed_at in DB
    today_completed = library_db.get_completed_jobs_count_today(ts_today_start)
    today_failed = library_db.get_failed_jobs_count_today(ts_today_start)

    # In-memory counts for today's cancelled and interrupted jobs
    today_cancelled = sum(1 for j in active_jobs if j.stage == 'cancelled' and j.created_at >= ts_today_start)
    today_interrupted = sum(1 for j in active_jobs if j.stage == 'interrupted' and j.created_at >= ts_today_start)

    # Honest user activity tracking
    # 1. Unique active sessions today from meaningful activity events and download jobs
    unique_sessions_today = library_db.get_unique_sessions_today(ts_today_start)
    
    # 2. Active sessions in the last 5 minutes (from meaningful activity events, not polling)
    active_now_count = library_db.get_active_sessions_count_window(300)

    # 3. Real registered admin user accounts
    registered_admin_count = library_db.count_admin_users()

    # Collection status
    all_lib_items = library_db.list_library_items(limit=100000)
    lib_ready = sum(1 for it in all_lib_items if it.get('status') == 'ready')
    lib_needs_attention = sum(1 for it in all_lib_items if it.get('status') == 'needs_attention')
    lib_total = len(all_lib_items)

    # Health & Storage (cached)
    health = get_health_status()
    storage = get_storage_breakdown()

    # Actionable alerts
    alerts = []
    if storage['filesystem']['is_warning']:
        alerts.append({
            'type': 'danger',
            'title': 'Kapasitas Disk Kritis',
            'message': f"Ruang penyimpanan terpakai telah mencapai {storage['filesystem']['percent_used']}% (Ambang batas {storage['filesystem']['warning_threshold_percent']}%). Segera periksa pembersihan penyimpanan."
        })
    if not health['downloader']['ok']:
        alerts.append({
            'type': 'danger',
            'title': 'Downloader yt-dlp Bermasalah',
            'message': 'Modul downloader tidak merespons. Unduhan baru mungkin akan gagal.'
        })
    if not health['ffmpeg']['ok']:
        alerts.append({
            'type': 'warning',
            'title': 'FFmpeg / ffprobe Tidak Ditemukan',
            'message': 'Penggabungan video/audio dan verifikasi format media memerlukan FFmpeg di server.'
        })
    if lib_needs_attention > 0:
        alerts.append({
            'type': 'warning',
            'title': 'Koleksi Memerlukan Perhatian',
            'message': f"Ada {lib_needs_attention} item film yang belum siap diputar atau gagal diimpor."
        })

    # Success rate via SQL aggregation without row limits
    success_rate_val, sample_count = library_db.get_jobs_success_rate_aggregate(now_ts - (30 * 86400))
    success_rate_label = f"{success_rate_val}%" if success_rate_val is not None else "Belum ada data"

    return {
        'timestamp': now_ts,
        'timezone': str(tz),
        'jobs_waiting': queued_count,
        'jobs_processing': total_processing,
        'processing_breakdown': {
            'downloading': downloading_count,
            'validating': validating_count,
            'importing': importing_count,
            'indexing': indexing_count
        },
        'today': {
            'completed': today_completed,
            'failed': today_failed,
            'cancelled': today_cancelled,
            'interrupted': today_interrupted
        },
        'sessions': {
            'unique_today': unique_sessions_today,
            'active_5min': active_now_count,
            'registered_users': registered_admin_count,
            'registered_admin_count': registered_admin_count,
            'note': 'Sesi unik dihitung dari aktivitas autentikasi dan unduhan nyata. Pengguna terdaftar menampilkan jumlah Akun Admin.'
        },
        'library': {
            'total': lib_total,
            'ready': lib_ready,
            'needs_attention': lib_needs_attention
        },
        'storage': storage,
        'health': health,
        'alerts': alerts,
        'success_rate': {
            'rate_percent': success_rate_val,
            'rate_display': success_rate_label,
            'total_considered': sample_count,
            'explanation': 'Dihitung dari seluruh tugas selesai dibandingkan kegagalan tanpa limit sampel. Jika tidak ada tugas, menampilkan Belum ada data.'
        }
    }

def get_activity_chart_data(days: int = 7) -> Dict[str, Any]:
    """Generates daily activity chart breakdown for 7, 30, or 90 days.
    Guarantees honest dates based on configured timezone.
    """
    days = max(7, min(days, 90))
    tz = get_target_timezone()
    now_ts = time.time()
    dt_now = datetime.fromtimestamp(now_ts, tz=tz)

    # Date slots
    dates_map = {}
    for i in range(days - 1, -1, -1):
        day_dt = dt_now - timedelta(days=i)
        day_key = day_dt.strftime("%Y-%m-%d")
        day_label = day_dt.strftime("%d %b")
        dates_map[day_key] = {
            'date': day_key,
            'label': day_label,
            'completed': 0,
            'failed': 0,
            'cancelled': 0,
            'interrupted': 0,
            'unique_sessions': set()
        }

    start_ts = (datetime.strptime(min(dates_map.keys()), "%Y-%m-%d").replace(tzinfo=tz)).timestamp()
    all_jobs = library_db.list_persisted_jobs(limit=50000, date_from=start_ts)['items']

    for j in all_jobs:
        stage = j.get('stage')
        created = j.get('created_at')
        completed = j.get('completed_at')
        
        # Completed jobs are grouped by completed_at, others by created_at
        effective_ts = completed if (stage in ('completed', 'ready') and completed) else created
        if not effective_ts:
            continue

        day_key = datetime.fromtimestamp(effective_ts, tz=tz).strftime("%Y-%m-%d")
        if day_key in dates_map:
            if stage in ('completed', 'ready'):
                dates_map[day_key]['completed'] += 1
            elif stage == 'failed':
                dates_map[day_key]['failed'] += 1
            elif stage == 'cancelled':
                dates_map[day_key]['cancelled'] += 1
            elif stage == 'interrupted':
                dates_map[day_key]['interrupted'] += 1
            
            pseudo = j.get('owner_session_pseudonym')
            if pseudo:
                dates_map[day_key]['unique_sessions'].add(pseudo)

    # Format result series
    chart_series = []
    for day_key in sorted(dates_map.keys()):
        item = dates_map[day_key]
        chart_series.append({
            'date': item['date'],
            'label': item['label'],
            'completed': item['completed'],
            'failed': item['failed'],
            'cancelled': item['cancelled'],
            'interrupted': item['interrupted'],
            'total_jobs': item['completed'] + item['failed'] + item['cancelled'] + item['interrupted'],
            'unique_sessions': len(item['unique_sessions'])
        })

    return {
        'days': days,
        'timezone': str(tz),
        'series': chart_series
    }
