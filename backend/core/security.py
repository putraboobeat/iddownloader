import re
import socket
import ipaddress
import secrets
from urllib.parse import urlparse

from .config import BLOCKED_IP_RANGES

def is_safe_url(url: str) -> bool:
    """Validates if a URL is safe from SSRF attacks."""
    if not url:
        return False
        
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
            
        hostname = parsed.hostname
        if not hostname:
            return False

        # Resolve IP
        try:
            ip_str = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip_str)
        except (socket.gaierror, ValueError):
            return False

        # Check against blocked ranges
        for blocked_range in BLOCKED_IP_RANGES:
            if ip_obj in ipaddress.ip_network(blocked_range):
                return False
                
        return True
    except Exception:
        return False

def clean_url(value: str) -> str:
    """Extracts and cleans a URL safely."""
    value = str(value).strip()
    match = re.fullmatch(r'\[.*?\]\((.*)\)', value, re.S)
    if match:
        value = match.group(1)
        
    value = value.replace('\\&', '&').replace('\\_', '_').strip('<> \t\r\n')
    
    if not is_safe_url(value):
        raise ValueError("URL tidak valid atau diblokir (Potensi SSRF / Local Network).")
        
    parts = urlparse(value)
    if parts.username or parts.password:
        raise ValueError("URL dengan nama pengguna atau sandi tidak didukung.")
        
    return value

def generate_session_id() -> str:
    """Generates a secure random session ID."""
    return secrets.token_hex(32)

def generate_app_token() -> str:
    """Generates a secure random API token for frontend."""
    return secrets.token_urlsafe(64)
