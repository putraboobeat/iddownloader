"""Authentication, Password Hashing, Sessions, CSRF, and Rate Limiting for OmniFetch Super Admin."""
import hashlib
import hmac
import secrets
import time
import re
from typing import Optional, Tuple, Dict, Any
from http.cookies import SimpleCookie

from ..core import config
from ..core import library_db

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 32

# In-memory rate limiting for login attempts: {key: [(timestamp)]}
_LOGIN_ATTEMPTS: Dict[str, list] = {}
RATE_LIMIT_WINDOW = 900 # 15 minutes
MAX_FAILED_ATTEMPTS = 5

def hash_password(password: str) -> Tuple[str, str]:
    """Hashes a password with PBKDF2-HMAC-SHA256 and a random salt."""
    if not password or len(password) < 8:
        raise ValueError("Kata sandi harus minimal 8 karakter.")
    salt = secrets.token_hex(SALT_BYTES)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        PBKDF2_ITERATIONS
    )
    return key.hex(), salt

def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Verifies password using constant-time comparison."""
    if not password or not stored_hash or not salt:
        return False
    try:
        key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            PBKDF2_ITERATIONS
        )
        return hmac.compare_digest(key.hex(), stored_hash)
    except Exception:
        return False

def hash_token(token: str) -> str:
    """Computes SHA-256 hash of an auth token for storage and lookup."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def check_login_rate_limit(client_key: str) -> Tuple[bool, int]:
    """Checks if a client has exceeded login failure limits.
    Returns (allowed: bool, retry_after_seconds: int).
    """
    now = time.time()
    attempts = _LOGIN_ATTEMPTS.get(client_key, [])
    # Filter attempts within window
    valid_attempts = [ts for ts in attempts if now - ts < RATE_LIMIT_WINDOW]
    _LOGIN_ATTEMPTS[client_key] = valid_attempts
    if len(valid_attempts) >= MAX_FAILED_ATTEMPTS:
        oldest = valid_attempts[0]
        retry_after = int(RATE_LIMIT_WINDOW - (now - oldest))
        return False, max(1, retry_after)
    return True, 0

def record_login_failure(client_key: str):
    """Records a failed login attempt."""
    now = time.time()
    attempts = _LOGIN_ATTEMPTS.get(client_key, [])
    attempts.append(now)
    _LOGIN_ATTEMPTS[client_key] = [ts for ts in attempts if now - ts < RATE_LIMIT_WINDOW]

def clear_login_failures(client_key: str):
    """Clears failed login attempts upon successful login."""
    if client_key in _LOGIN_ATTEMPTS:
        del _LOGIN_ATTEMPTS[client_key]

def create_session(user_id: int, ip_address: str = "", user_agent: str = "") -> Dict[str, Any]:
    """Generates a secure admin session and CSRF token."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_token(raw_token)
    csrf_token = secrets.token_hex(32)
    max_age = config.ADMIN_SESSION_MAX_AGE
    expires_at = time.time() + max_age

    session_record = library_db.create_admin_session(
        token_hash=token_hash,
        user_id=user_id,
        csrf_token=csrf_token,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent
    )
    return {
        'token': raw_token,
        'csrf_token': csrf_token,
        'expires_at': expires_at,
        'record': session_record
    }

def get_session_from_request(handler) -> Optional[dict]:
    """Extracts and verifies admin session from HTTP request.
    Does NOT accept tokens via URL query parameters.
    """
    raw_token = None
    # 1. Cookie
    cookie_header = handler.headers.get('Cookie')
    if cookie_header:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            if 'omni_admin_session' in cookie:
                raw_token = cookie['omni_admin_session'].value
        except Exception:
            pass

    # 2. Authorization or X-Admin-Session header
    if not raw_token:
        auth_hdr = handler.headers.get('X-Admin-Session') or handler.headers.get('Authorization')
        if auth_hdr:
            if auth_hdr.startswith('Bearer '):
                raw_token = auth_hdr[7:].strip()
            else:
                raw_token = auth_hdr.strip()

    if not raw_token or len(raw_token) < 20:
        return None

    token_hash = hash_token(raw_token)
    session = library_db.get_admin_session(token_hash)
    if session:
        # Touch last seen
        library_db.touch_admin_session(token_hash)
    return session

def validate_csrf(handler, session: dict) -> bool:
    """Validates CSRF token on state-changing requests."""
    expected_csrf = session.get('csrf_token')
    if not expected_csrf:
        return False
    provided_csrf = handler.headers.get('X-CSRF-Token', '').strip()
    if not provided_csrf:
        # Also check custom form header
        provided_csrf = handler.headers.get('X-XSRF-Token', '').strip()
    if not provided_csrf:
        return False
    return hmac.compare_digest(str(provided_csrf), str(expected_csrf))

def get_client_ip(handler) -> str:
    """Safely determines client IP.
    Does not trust arbitrary forwarded headers unless connecting via loopback proxy.
    """
    direct_ip = handler.client_address[0] if hasattr(handler, 'client_address') and handler.client_address else '127.0.0.1'
    # Only trust X-Forwarded-For if request comes from local reverse proxy (127.0.0.1 or ::1)
    if direct_ip in ('127.0.0.1', '::1', 'localhost'):
        fwd = handler.headers.get('X-Forwarded-For')
        if fwd:
            candidate = fwd.split(',')[0].strip()
            if re.match(r'^[0-9a-fA-F:.]+$', candidate):
                return candidate
    return direct_ip

def format_cookie(name: str, value: str, max_age: int, secure: bool = False) -> str:
    """Formats an HttpOnly, SameSite=Lax cookie string."""
    parts = [
        f"{name}={value}",
        f"Max-Age={max_age}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax"
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)

def format_logout_cookie() -> str:
    return "omni_admin_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
