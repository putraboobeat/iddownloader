"""Immutable Audit Logging Module for OmniFetch Super Admin Actions."""
import re
from typing import Optional, Dict, Any

from ..core import library_db

EXACT_SENSITIVE_KEYS = {
    'password', 'passwd', 'pass', 'token', 'csrf_token', 'secret',
    'cookie', 'cookies', 'auth', 'authorization', 'credential', 'credentials',
    'api_key', 'secret_key', 'private_key', 'access_key', 'owner_session',
    'session_token', 'raw_token', 't', 'sig', 'signature'
}

PARTIAL_SENSITIVE_PATTERNS = ('password', 'passwd', 'token', 'secret', 'cookie', 'credential', 'session')

URL_SECRET_REGEX = re.compile(
    r'([?&](?:token|auth|key|password|t|sig|signature|secret|session|access_token|credential)=)[^&\s]+',
    re.IGNORECASE
)
BEARER_REGEX = re.compile(r'(Bearer\s+)[a-zA-Z0-9_\-\.]+', re.IGNORECASE)

def is_sensitive_key(k: str) -> bool:
    lk = str(k).lower()
    if lk in EXACT_SENSITIVE_KEYS:
        return True
    return any(p in lk for p in PARTIAL_SENSITIVE_PATTERNS)

def redact_sensitive_text(text: str) -> str:
    """Redacts query secret parameters (including ?t=) and bearer tokens from strings."""
    if not isinstance(text, str):
        return text
    s = URL_SECRET_REGEX.sub(r'\1[REDACTED]', text)
    s = BEARER_REGEX.sub(r'\1[REDACTED]', s)
    return s

def redact_url_secrets(url: str) -> str:
    """Helper alias for redact_sensitive_text on URLs."""
    return redact_sensitive_text(url)

def redact_secrets(obj: Any) -> Any:
    """Recursively redacts sensitive values from dictionaries and lists."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if is_sensitive_key(k):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_secrets(v)
        return cleaned
    elif isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    elif isinstance(obj, str):
        return redact_sensitive_text(obj)
    return obj

def log_admin_action(
    username: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    details: Optional[dict] = None,
    ip_address: str = "",
    status: str = "success",
    user_id: Optional[int] = None
) -> dict:
    """Records an admin action to persistent SQLite audit_logs with automatic redaction."""
    safe_details = redact_secrets(details or {})
    return library_db.record_audit_log(
        username=username or "system",
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        details=safe_details,
        ip_address=ip_address,
        status=status,
        user_id=user_id
    )
