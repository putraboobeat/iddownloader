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

# Auto-detect secondary HDD mount (/mnt/hdd) if available on host server
_DEFAULT_LIB_ROOT = DATA_DIR / 'library'
if os.path.exists('/mnt/hdd') and os.path.isdir('/mnt/hdd'):
    _DEFAULT_LIB_ROOT = Path('/mnt/hdd/perpustakaan_film')

LIBRARY_ROOT = Path(os.environ.get('LIBRARY_ROOT') or _DEFAULT_LIB_ROOT).resolve()
LIBRARY_MOVIES_DIR = Path(os.environ.get('LIBRARY_MOVIES_DIR') or (LIBRARY_ROOT / 'Movies')).resolve()
LIBRARY_DB_PATH = Path(os.environ.get('LIBRARY_DB_PATH') or (DATA_DIR / 'omnifetch-library.sqlite3')).resolve()

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

