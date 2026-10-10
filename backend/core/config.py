import os
from pathlib import Path

# Environment variables
VPS_ROOT = os.environ.get('VPS_ROOT')
if VPS_ROOT:
    VPS_ROOT = Path(VPS_ROOT).resolve()

PORT = int(os.environ.get('PORT', 8080))
RETENTION_SECONDS = int(os.environ.get('RETENTION_SECONDS', 7200))
MAX_CONCURRENT_JOBS = int(os.environ.get('MAX_CONCURRENT_JOBS', 4))

# Paths
BASE_DIR = Path(__file__).parent.parent.parent.resolve()
COOKIES_PATH = os.environ.get('COOKIES_PATH') or str(BASE_DIR / 'cookies.txt')

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
