import os
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).parent.parent.parent.resolve()

# Auto-load .env file if present
_env_file = BASE_DIR / '.env'
if _env_file.is_file():
    try:
        with open(_env_file, 'r', encoding='utf-8') as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _v = _line.split('=', 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
    except Exception:
        pass

# Environment variables
VPS_ROOT = os.environ.get('VPS_ROOT')
if VPS_ROOT:
    VPS_ROOT = Path(VPS_ROOT).resolve()

PORT = int(os.environ.get('PORT', 8080))
RETENTION_SECONDS = int(os.environ.get('RETENTION_SECONDS', 7200))
MAX_CONCURRENT_JOBS = int(os.environ.get('MAX_CONCURRENT_JOBS', 4))

COOKIES_PATH = os.environ.get('COOKIES_PATH') or str(BASE_DIR / 'cookies.txt')

# Library & Storage Paths
DATA_DIR = Path(os.environ.get('OMNIFETCH_DATA_DIR') or (BASE_DIR / 'data')).resolve()
TEMPORARY_DIR = Path(os.environ.get('OMNIFETCH_TMP_DIR') or (DATA_DIR / 'temporary')).resolve()
STAGING_DIR = Path(os.environ.get('OMNIFETCH_STAGING_DIR') or (DATA_DIR / 'staging')).resolve()
TRASH_DIR = Path(os.environ.get('OMNIFETCH_TRASH_DIR') or (DATA_DIR / 'trash')).resolve()
JOBS_DB_PATH = Path(os.environ.get('JOBS_DB_PATH') or (DATA_DIR / 'jobs_db.json')).resolve()

# Permanent storage mount configuration
# If EXPECTED_STORAGE_MOUNT is set, storage availability check will verify this mount exists before accessing
EXPECTED_STORAGE_MOUNT = os.environ.get('EXPECTED_STORAGE_MOUNT', '').strip()

_DEFAULT_LIB_ROOT = DATA_DIR / 'library'
if not os.environ.get('LIBRARY_ROOT') and os.path.exists('/mnt/hdd/perpustakaan_film'):
    _DEFAULT_LIB_ROOT = Path('/mnt/hdd/perpustakaan_film')

LIBRARY_ROOT = Path(os.environ.get('LIBRARY_ROOT') or _DEFAULT_LIB_ROOT).resolve()
LIBRARY_MOVIES_DIR = Path(os.environ.get('LIBRARY_MOVIES_DIR') or (LIBRARY_ROOT / 'Movies')).resolve()
LIBRARY_DB_PATH = Path(os.environ.get('LIBRARY_DB_PATH') or (DATA_DIR / 'omnifetch-library.sqlite3')).resolve()

def check_storage_available() -> tuple[bool, str]:
    """Verifies that the configured permanent storage is mounted and accessible.
    Prevents silent fallback to root filesystem or generating empty catalogs when an external volume is detached.
    """
    if EXPECTED_STORAGE_MOUNT:
        exp_p = Path(EXPECTED_STORAGE_MOUNT).resolve()
        if not exp_p.exists() or not exp_p.is_dir():
            return False, f"Volume penyimpanan eksternal '{EXPECTED_STORAGE_MOUNT}' belum ter-mount."
    
    # If LIBRARY_ROOT parent doesn't exist and LIBRARY_ROOT doesn't exist, storage is considered detached
    if not LIBRARY_ROOT.exists():
        # Check if parent is also missing (typical of detached mount point)
        if not LIBRARY_ROOT.parent.exists():
            return False, f"Penyimpanan perpustakaan tidak tersedia ({LIBRARY_ROOT.parent} tidak terpasang)."
    return True, "OK"

# Operational Administration & Retention Config
TRASH_RETENTION_DAYS = int(os.environ.get('TRASH_RETENTION_DAYS', 7))
HISTORY_RETENTION_DAYS = int(os.environ.get('HISTORY_RETENTION_DAYS', 90))
DISK_WARNING_PERCENT = int(os.environ.get('DISK_WARNING_PERCENT', 85))
ANALYTICS_TIMEZONE = os.environ.get('ANALYTICS_TIMEZONE', 'Asia/Jakarta').strip()
ADMIN_SESSION_MAX_AGE = int(os.environ.get('ADMIN_SESSION_MAX_AGE', 86400)) # 24 hours
ADMIN_COOKIE_SECURE = os.environ.get('ADMIN_COOKIE_SECURE', '').lower() in ('true', '1', 'yes')

# Minimum free space required for library imports (default: 2 GB)
MIN_FREE_SPACE_BYTES = int(os.environ.get('MIN_FREE_SPACE_BYTES', 2 * 1024 * 1024 * 1024))

# Personal Library Access Password (Default: 1234567890)
LIBRARY_PASSWORD = os.environ.get('LIBRARY_PASSWORD', '1234567890').strip()

# Jellyfin Integration Config
JELLYFIN_INTERNAL_URL = os.environ.get('JELLYFIN_INTERNAL_URL', 'http://127.0.0.1:8096').rstrip('/')
JELLYFIN_PUBLIC_URL = os.environ.get('JELLYFIN_PUBLIC_URL', JELLYFIN_INTERNAL_URL).rstrip('/')
JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY', '').strip()
JELLYFIN_LIBRARY_ID = os.environ.get('JELLYFIN_LIBRARY_ID', '').strip()
JELLYFIN_MEDIA_ROOT = os.environ.get('JELLYFIN_MEDIA_ROOT', str(LIBRARY_ROOT)).strip()

# SSRF Protection
BLOCKED_IP_RANGES = [
    '127.0.0.0/8',
    '10.0.0.0/8',
    '172.16.0.0/12',
    '192.168.0.0/16',
    '169.254.0.0/16',
    '::1/128',
    'fe80::/10',
    'fc00::/7'
]

