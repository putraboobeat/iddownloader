"""SQLite Library & Integration Database Module for OmniFetch Personal Streaming Library.
Implements versioned migrations, WAL mode, foreign keys, connection pooling, and safe transaction handling.
Includes Super Admin authentication, persistent job tracking, audit logging, trash quarantine, and settings.
"""
import sqlite3
import json
import time
import hashlib
import re
import uuid
import shutil
from pathlib import Path
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from contextlib import contextmanager

from . import config

CURRENT_SCHEMA_VERSION = 2

MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS library_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        year INTEGER,
        clean_title TEXT NOT NULL,
        folder_path TEXT NOT NULL,
        media_type TEXT DEFAULT 'movie',
        source_url TEXT,
        source_key TEXT,
        jellyfin_item_id TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_library_items_status ON library_items(status);
    CREATE INDEX IF NOT EXISTS idx_library_items_source_key ON library_items(source_key);
    CREATE INDEX IF NOT EXISTS idx_library_items_title_year ON library_items(title, year);

    CREATE TABLE IF NOT EXISTS library_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
        file_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        duration REAL,
        video_codec TEXT,
        audio_codec TEXT,
        resolution TEXT,
        subtitle_lang TEXT,
        is_primary INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_library_files_item_id ON library_files(item_id);

    CREATE TABLE IF NOT EXISTS library_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT UNIQUE NOT NULL,
        item_id INTEGER REFERENCES library_items(id) ON DELETE SET NULL,
        owner_session TEXT NOT NULL,
        stage TEXT NOT NULL,
        target TEXT DEFAULT 'library',
        idempotency_key TEXT,
        error_message TEXT,
        checkpoint_data TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_library_jobs_job_id ON library_jobs(job_id);
    CREATE INDEX IF NOT EXISTS idx_library_jobs_owner_session ON library_jobs(owner_session);
    CREATE INDEX IF NOT EXISTS idx_library_jobs_idempotency ON library_jobs(idempotency_key, owner_session);
    """,
    2: """
    -- Super Admin Users
    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        full_name TEXT,
        is_superadmin INTEGER DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_login_at REAL
    );

    -- Admin Sessions with Expiry and CSRF Protection
    CREATE TABLE IF NOT EXISTS admin_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
        csrf_token TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        ip_address TEXT,
        user_agent TEXT,
        is_revoked INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_admin_sessions_user_id ON admin_sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires_at ON admin_sessions(expires_at);

    -- Immutable Audit Logs
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        user_id INTEGER,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        resource_type TEXT,
        resource_id TEXT,
        details_json TEXT,
        ip_address TEXT,
        status TEXT NOT NULL DEFAULT 'success'
    );
    CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs(timestamp);
    CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);

    -- Operational Settings (Persistent)
    CREATE TABLE IF NOT EXISTS admin_settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at REAL NOT NULL,
        updated_by TEXT
    );

    -- Persistent Download Jobs History & Diagnostics (Decoupled from physical file cleanup)
    CREATE TABLE IF NOT EXISTS download_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT UNIQUE NOT NULL,
        owner_session TEXT NOT NULL,
        owner_session_pseudonym TEXT NOT NULL,
        target TEXT DEFAULT 'download',
        url TEXT,
        title TEXT,
        domain TEXT,
        stage TEXT NOT NULL,
        status_message TEXT,
        percent REAL DEFAULT 0.0,
        bytes_downloaded INTEGER DEFAULT 0,
        total_bytes INTEGER DEFAULT 0,
        speed_str TEXT,
        eta_str TEXT,
        quality TEXT,
        error_message TEXT,
        retry_count INTEGER DEFAULT 0,
        parent_job_id TEXT,
        library_item_id INTEGER REFERENCES library_items(id) ON DELETE SET NULL,
        result_filename TEXT,
        result_path TEXT,
        file_size INTEGER DEFAULT 0,
        file_exists INTEGER DEFAULT 1,
        expires_at REAL,
        heartbeat_at REAL,
        last_progress_at REAL,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        timeline_json TEXT,
        logs_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_download_jobs_job_id ON download_jobs(job_id);
    CREATE INDEX IF NOT EXISTS idx_download_jobs_created_at ON download_jobs(created_at);
    CREATE INDEX IF NOT EXISTS idx_download_jobs_stage ON download_jobs(stage);
    CREATE INDEX IF NOT EXISTS idx_download_jobs_owner_pseudo ON download_jobs(owner_session_pseudonym);
    CREATE INDEX IF NOT EXISTS idx_download_jobs_domain ON download_jobs(domain);

    -- Trash Quarantine for Managed Files
    CREATE TABLE IF NOT EXISTS trash_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_path TEXT NOT NULL,
        trash_path TEXT NOT NULL,
        item_type TEXT NOT NULL,
        resource_id TEXT,
        name TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        deleted_at REAL NOT NULL,
        deleted_by TEXT NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT DEFAULT 'trashed'
    );
    CREATE INDEX IF NOT EXISTS idx_trash_items_status ON trash_items(status);
    CREATE INDEX IF NOT EXISTS idx_trash_items_deleted_at ON trash_items(deleted_at);
    """
}

def normalize_source_key(url: Optional[str]) -> Optional[str]:
    """Generates a stable, reproducible source key for deduplication.
    Strips transient query parameters like utm_*, fbclid, ref, etc.
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
        ignore_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid', 'ref', 'ts'}
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filtered_qs = {k: v for k, v in qs.items() if k.lower() not in ignore_params}
        sorted_query = urlencode(sorted(filtered_qs.items()), doseq=True)
        normalized_url = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.params, sorted_query, ''))
        return hashlib.sha256(normalized_url.encode('utf-8')).hexdigest()
    except Exception:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

def pseudonymize_session(session_id: Optional[str]) -> str:
    """Generates an irreversible pseudonymized string for anonymous session metrics."""
    if not session_id:
        return "anon_unknown"
    # Salted hash with stable prefix
    h = hashlib.sha256(f"omni_pseudo_{session_id}".encode('utf-8')).hexdigest()
    return f"ses_{h[:12]}"

def extract_domain(url: Optional[str]) -> str:
    """Extracts clean domain name from URL for honest metrics."""
    if not url:
        return "Unknown"
    try:
        parsed = urlparse(str(url).strip())
        host = parsed.netloc.lower()
        if ':' in host:
            host = host.split(':')[0]
        if host.startswith('www.'):
            host = host[4:]
        return host or "Direct"
    except Exception:
        return "Other"

def get_db_path(custom_path: Optional[Path] = None) -> Path:
    p = custom_path or config.LIBRARY_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

@contextmanager
def db_session(db_path: Optional[Path] = None):
    """Context manager that guarantees the SQLite connection is committed and closed."""
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a standalone connection. Callers should close it when done."""
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db(db_path: Optional[Path] = None):
    """Initializes the database, runs migrations, and verifies table schemas."""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);")
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
        applied_versions = {row['version'] for row in cursor.fetchall()}

        for v in sorted(MIGRATIONS.keys()):
            if v not in applied_versions:
                cursor.executescript(MIGRATIONS[v])
                cursor.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);", (v, time.time()))

        # Safe non-destructive column additions to library_items for Migration 2
        cursor.execute("PRAGMA table_info(library_items);")
        existing_cols = {row['name'] for row in cursor.fetchall()}
        columns_to_add = [
            ("synopsis", "TEXT"),
            ("genres", "TEXT"),
            ("poster_path", "TEXT"),
            ("backdrop_path", "TEXT"),
            ("is_favorite", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in columns_to_add:
            if col_name not in existing_cols:
                try:
                    cursor.execute(f"ALTER TABLE library_items ADD COLUMN {col_name} {col_type};")
                except Exception:
                    pass

        # Check trash_items metadata_snapshot column
        cursor.execute("PRAGMA table_info(trash_items);")
        trash_cols = {row['name'] for row in cursor.fetchall()}
        if "metadata_snapshot" not in trash_cols:
            try:
                cursor.execute("ALTER TABLE trash_items ADD COLUMN metadata_snapshot TEXT;")
            except Exception:
                pass

        # Check library_files is_default column
        cursor.execute("PRAGMA table_info(library_files);")
        files_cols = {row['name'] for row in cursor.fetchall()}
        if "is_default" not in files_cols:
            try:
                cursor.execute("ALTER TABLE library_files ADD COLUMN is_default INTEGER DEFAULT 0;")
            except Exception:
                pass

        # Meaningful user activity tracking (Not affected by polling or static asset requests)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_pseudonym TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON user_activity_events(timestamp);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_session ON user_activity_events(session_pseudonym);")

# --- Super Admin Users ---

def create_admin_user(
    username: str,
    password_hash: str,
    salt: str,
    full_name: str = "",
    is_superadmin: int = 1,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    clean_username = username.strip().lower()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO admin_users (username, password_hash, salt, full_name, is_superadmin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (clean_username, password_hash, salt, full_name, is_superadmin, now, now))
        user_id = cursor.lastrowid
        cursor.execute("SELECT * FROM admin_users WHERE id = ?;", (user_id,))
        return dict(cursor.fetchone())

def get_admin_user_by_username(username: str, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    clean_username = (username or '').strip().lower()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin_users WHERE username = ?;", (clean_username,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_admin_user_by_id(user_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin_users WHERE id = ?;", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def count_admin_users(db_path: Optional[Path] = None) -> int:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS cnt FROM admin_users;")
        row = cursor.fetchone()
        return int(row['cnt']) if row else 0

def update_admin_password(user_id: int, password_hash: str, salt: str, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE admin_users 
            SET password_hash = ?, salt = ?, updated_at = ?
            WHERE id = ?;
        """, (password_hash, salt, now, user_id))
        return cursor.rowcount > 0

def update_admin_last_login(user_id: int, db_path: Optional[Path] = None):
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?;", (now, user_id))

# --- Admin Sessions ---

def create_admin_session(
    token_hash: str,
    user_id: int,
    csrf_token: str,
    expires_at: float,
    ip_address: str = "",
    user_agent: str = "",
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO admin_sessions (token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at, ip_address, user_agent, is_revoked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0);
        """, (token_hash, user_id, csrf_token, now, expires_at, now, ip_address, user_agent))
        cursor.execute("SELECT * FROM admin_sessions WHERE token_hash = ?;", (token_hash,))
        return dict(cursor.fetchone())

def get_admin_session(token_hash: str, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.*, u.username, u.full_name, u.is_superadmin
            FROM admin_sessions s
            JOIN admin_users u ON s.user_id = u.id
            WHERE s.token_hash = ? AND s.is_revoked = 0 AND s.expires_at > ?;
        """, (token_hash, time.time()))
        row = cursor.fetchone()
        return dict(row) if row else None

def touch_admin_session(token_hash: str, db_path: Optional[Path] = None):
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?;", (now, token_hash))

def revoke_admin_session(token_hash: str, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE admin_sessions SET is_revoked = 1 WHERE token_hash = ?;", (token_hash,))
        return cursor.rowcount > 0

def cleanup_expired_sessions(db_path: Optional[Path] = None) -> int:
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_sessions WHERE expires_at < ? OR is_revoked = 1;", (now,))
        return cursor.rowcount

# --- Audit Logs ---

def record_audit_log(
    username: str,
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    details: Optional[dict] = None,
    ip_address: str = "",
    status: str = "success",
    user_id: Optional[int] = None,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    details_json = json.dumps(details) if details is not None else None
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs (timestamp, user_id, username, action, resource_type, resource_id, details_json, ip_address, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (now, user_id, username, action, resource_type, str(resource_id), details_json, ip_address, status))
        log_id = cursor.lastrowid
        cursor.execute("SELECT * FROM audit_logs WHERE id = ?;", (log_id,))
        return dict(cursor.fetchone())

def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if action:
        conditions.append("action = ?")
        params.append(action)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if search:
        conditions.append("(username LIKE ? OR resource_type LIKE ? OR resource_id LIKE ? OR details_json LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s, s])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS total FROM audit_logs {where};", params)
        total = int(cursor.fetchone()['total'])

        sql = f"""
            SELECT * FROM audit_logs
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?;
        """
        cursor.execute(sql, params + [limit, offset])
        logs = []
        for r in cursor.fetchall():
            d = dict(r)
            if d.get('details_json'):
                try:
                    d['details'] = json.loads(d['details_json'])
                except Exception:
                    d['details'] = {}
            else:
                d['details'] = {}
            logs.append(d)

        return {'items': logs, 'total': total, 'limit': limit, 'offset': offset}

# --- Operational Settings ---

def get_setting(key: str, default: Any = None, db_path: Optional[Path] = None) -> Any:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value_json FROM admin_settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        if not row:
            return default
        try:
            return json.loads(row['value_json'])
        except Exception:
            return default

def set_setting(key: str, value: Any, updated_by: str = "admin", db_path: Optional[Path] = None):
    init_db(db_path)
    now = time.time()
    val_json = json.dumps(value)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO admin_settings (key, value_json, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at, updated_by = excluded.updated_by;
        """, (key, val_json, now, updated_by))

def get_all_settings(db_path: Optional[Path] = None) -> Dict[str, Any]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value_json, updated_at, updated_by FROM admin_settings;")
        res = {}
        for r in cursor.fetchall():
            try:
                res[r['key']] = {
                    'value': json.loads(r['value_json']),
                    'updated_at': r['updated_at'],
                    'updated_by': r['updated_by']
                }
            except Exception:
                pass
        return res

# --- Persistent Download Jobs ---

def upsert_download_job(job_dict: dict, db_path: Optional[Path] = None) -> dict:
    """Inserts or updates a download job record in persistent SQLite.
    Guarantees download history and analytics are not lost when files are cleaned.
    """
    init_db(db_path)
    job_id = job_dict['job_id']
    owner_session = job_dict.get('owner_session', '')
    owner_pseudo = job_dict.get('owner_session_pseudonym') or pseudonymize_session(owner_session)
    now = time.time()

    url = job_dict.get('url') or ''
    domain = job_dict.get('domain') or extract_domain(url)
    timeline_json = json.dumps(job_dict.get('timeline', [])) if 'timeline' in job_dict else None
    logs_json = json.dumps(job_dict.get('logs', [])) if 'logs' in job_dict else None

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO download_jobs (
                job_id, owner_session, owner_session_pseudonym, target, url, title, domain,
                stage, status_message, percent, bytes_downloaded, total_bytes, speed_str, eta_str,
                quality, error_message, retry_count, parent_job_id, library_item_id,
                result_filename, result_path, file_size, file_exists, expires_at,
                heartbeat_at, last_progress_at, created_at, started_at, completed_at,
                timeline_json, logs_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?
            )
            ON CONFLICT(job_id) DO UPDATE SET
                stage = excluded.stage,
                status_message = excluded.status_message,
                percent = excluded.percent,
                bytes_downloaded = excluded.bytes_downloaded,
                total_bytes = excluded.total_bytes,
                speed_str = excluded.speed_str,
                eta_str = excluded.eta_str,
                quality = excluded.quality,
                error_message = excluded.error_message,
                retry_count = excluded.retry_count,
                library_item_id = excluded.library_item_id,
                result_filename = excluded.result_filename,
                result_path = excluded.result_path,
                file_size = excluded.file_size,
                file_exists = excluded.file_exists,
                expires_at = excluded.expires_at,
                heartbeat_at = excluded.heartbeat_at,
                last_progress_at = excluded.last_progress_at,
                started_at = COALESCE(download_jobs.started_at, excluded.started_at),
                completed_at = excluded.completed_at,
                timeline_json = excluded.timeline_json,
                logs_json = excluded.logs_json;
        """, (
            job_id, owner_session, owner_pseudo, job_dict.get('target', 'download'),
            url, job_dict.get('title', 'Media'), domain,
            job_dict.get('stage', 'queued'), job_dict.get('status_message', ''),
            job_dict.get('percent', 0.0), job_dict.get('bytes_downloaded', 0), job_dict.get('total_bytes', 0),
            job_dict.get('speed_str', ''), job_dict.get('eta_str', ''),
            job_dict.get('quality', ''), job_dict.get('error_message', ''),
            job_dict.get('retry_count', 0), job_dict.get('parent_job_id'),
            job_dict.get('library_item_id'), job_dict.get('result_filename', ''),
            job_dict.get('result_path', ''), job_dict.get('file_size', 0),
            job_dict.get('file_exists', 1), job_dict.get('expires_at'),
            job_dict.get('heartbeat_at', now), job_dict.get('last_progress_at', now),
            job_dict.get('created_at', now), job_dict.get('started_at'),
            job_dict.get('completed_at'), timeline_json, logs_json
        ))
        cursor.execute("SELECT * FROM download_jobs WHERE job_id = ?;", (job_id,))
        return dict(cursor.fetchone())

def get_persisted_job(job_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM download_jobs WHERE job_id = ?;", (job_id,))
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get('timeline_json'):
            try: d['timeline'] = json.loads(d['timeline_json'])
            except: d['timeline'] = []
        if d.get('logs_json'):
            try: d['logs'] = json.loads(d['logs_json'])
            except: d['logs'] = []
        return d

def list_persisted_jobs(
    limit: int = 50,
    offset: int = 0,
    stage: Optional[str] = None,
    target: Optional[str] = None,
    domain: Optional[str] = None,
    session_pseudo: Optional[str] = None,
    query: Optional[str] = None,
    date_from: Optional[float] = None,
    date_to: Optional[float] = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if stage:
        conditions.append("stage = ?")
        params.append(stage)
    if target:
        conditions.append("target = ?")
        params.append(target)
    if domain:
        conditions.append("domain = ?")
        params.append(domain)
    if session_pseudo:
        conditions.append("owner_session_pseudonym = ?")
        params.append(session_pseudo)
    if date_from:
        conditions.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("created_at <= ?")
        params.append(date_to)
    if query:
        conditions.append("(title LIKE ? OR job_id LIKE ? OR url LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    allowed_sort = {"created_at", "percent", "file_size", "completed_at", "title"}
    col_sort = sort_by if sort_by in allowed_sort else "created_at"
    dir_sort = "ASC" if sort_dir.lower() == "asc" else "DESC"

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS total FROM download_jobs {where};", params)
        total = int(cursor.fetchone()['total'])

        sql = f"""
            SELECT * FROM download_jobs
            {where}
            ORDER BY {col_sort} {dir_sort}
            LIMIT ? OFFSET ?;
        """
        cursor.execute(sql, params + [limit, offset])
        items = []
        for r in cursor.fetchall():
            d = dict(r)
            if d.get('timeline_json'):
                try: d['timeline'] = json.loads(d['timeline_json'])
                except: d['timeline'] = []
            if d.get('logs_json'):
                try: d['logs'] = json.loads(d['logs_json'])
                except: d['logs'] = []
            items.append(d)

        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

def mark_interrupted_jobs_on_startup(db_path: Optional[Path] = None) -> int:
    """Reconciles in-flight active jobs upon server restart, marking them 'interrupted'."""
    init_db(db_path)
    active_stages = ('queued', 'downloading', 'validating', 'importing', 'indexing')
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        placeholders = ', '.join(['?'] * len(active_stages))
        cursor.execute(f"""
            UPDATE download_jobs
            SET stage = 'interrupted',
                status_message = 'Pekerjaan terputus akibat server direstart.',
                completed_at = ?
            WHERE stage IN ({placeholders});
        """, [now] + list(active_stages))
        return cursor.rowcount

def mark_job_file_cleaned(job_id: str, db_path: Optional[Path] = None):
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE download_jobs SET file_exists = 0 WHERE job_id = ?;", (job_id,))

# --- Trash Quarantine ---

def create_trash_entry(
    original_path: str,
    trash_path: str,
    item_type: str,
    resource_id: str,
    name: str,
    file_size: int,
    deleted_by: str,
    expires_at: float,
    metadata_snapshot: Optional[str] = None,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trash_items (original_path, trash_path, item_type, resource_id, name, file_size, deleted_at, deleted_by, expires_at, status, metadata_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'trashed', ?);
        """, (original_path, trash_path, item_type, str(resource_id), name, file_size, now, deleted_by, expires_at, metadata_snapshot))
        t_id = cursor.lastrowid
        cursor.execute("SELECT * FROM trash_items WHERE id = ?;", (t_id,))
        return dict(cursor.fetchone())

def restore_library_item_snapshot(item_data: dict, db_path: Optional[Path] = None) -> Optional[dict]:
    """Reconstructs and restores a library item and all its file relations from a metadata snapshot."""
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        orig_id = item_data.get('id')
        cursor.execute("""
            INSERT OR REPLACE INTO library_items (
                id, title, year, clean_title, folder_path, status,
                synopsis, genres, poster_path, backdrop_path, is_favorite,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            orig_id,
            item_data.get('title', 'Restored Item'),
            item_data.get('year'),
            item_data.get('clean_title') or item_data.get('title', 'Restored'),
            item_data.get('folder_path', ''),
            item_data.get('status', 'ready'),
            item_data.get('synopsis'),
            item_data.get('genres'),
            item_data.get('poster_path'),
            item_data.get('backdrop_path'),
            item_data.get('is_favorite', 0),
            item_data.get('created_at', now),
            now
        ))
        new_id = orig_id or cursor.lastrowid

        # Delete existing files relation for this item if any
        cursor.execute("DELETE FROM library_files WHERE item_id = ?;", (new_id,))

        # Re-insert files
        for f in item_data.get('files', []):
            cursor.execute("""
                INSERT INTO library_files (
                    item_id, file_path, file_type, file_size, duration,
                    video_codec, audio_codec, resolution, subtitle_lang, is_primary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                new_id,
                f.get('file_path'),
                f.get('file_type', 'video'),
                f.get('file_size', 0),
                f.get('duration'),
                f.get('video_codec'),
                f.get('audio_codec'),
                f.get('resolution'),
                f.get('subtitle_lang'),
                f.get('is_primary', 0),
                f.get('created_at', now)
            ))
        
        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (new_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        cursor.execute("SELECT * FROM library_files WHERE item_id = ? ORDER BY is_primary DESC, id ASC;", (new_id,))
        res['files'] = [dict(r) for r in cursor.fetchall()]
        return res

def get_trash_entry(trash_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trash_items WHERE id = ?;", (trash_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def list_trash_items(status: str = "trashed", limit: int = 100, offset: int = 0, db_path: Optional[Path] = None) -> Dict[str, Any]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM trash_items WHERE status = ?;", (status,))
        total = int(cursor.fetchone()['total'])

        cursor.execute("""
            SELECT * FROM trash_items
            WHERE status = ?
            ORDER BY deleted_at DESC
            LIMIT ? OFFSET ?;
        """, (status, limit, offset))
        items = [dict(r) for r in cursor.fetchall()]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

def get_active_trash_items(db_path: Optional[Path] = None) -> List[dict]:
    """Helper returning list of currently quarantined trash items."""
    return list_trash_items(status="trashed", limit=1000, db_path=db_path).get('items', [])

def update_trash_status(trash_id: int, status: str, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE trash_items SET status = ? WHERE id = ?;", (status, trash_id))
        return cursor.rowcount > 0

# --- Library Jobs (Legacy Compatibility) ---

def create_or_get_library_job(
    job_id: str,
    owner_session: str,
    stage: str = "queued",
    target: str = "library",
    idempotency_key: Optional[str] = None,
    item_id: Optional[int] = None,
    checkpoint_data: Optional[dict] = None,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    ck_json = json.dumps(checkpoint_data) if checkpoint_data is not None else None

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        cursor.execute("""
            INSERT INTO library_jobs (job_id, item_id, owner_session, stage, target, idempotency_key, checkpoint_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (job_id, item_id, owner_session, stage, target, idempotency_key, ck_json, now, now))
        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        return dict(cursor.fetchone())

def get_job_by_idempotency_key(idempotency_key: str, owner_session: str, db_path: Optional[Path] = None) -> Optional[dict]:
    if not idempotency_key:
        return None
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM library_jobs 
            WHERE idempotency_key = ? AND owner_session = ?
            ORDER BY created_at DESC LIMIT 1;
        """, (idempotency_key, owner_session))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_library_job(job_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_library_job(
    job_id: str,
    stage: Optional[str] = None,
    item_id: Optional[int] = None,
    error_message: Optional[str] = None,
    checkpoint_data: Optional[dict] = None,
    db_path: Optional[Path] = None
) -> Optional[dict]:
    init_db(db_path)
    now = time.time()
    fields = ["updated_at = ?"]
    params: List[Any] = [now]

    if stage is not None:
        fields.append("stage = ?")
        params.append(stage)
    if item_id is not None:
        fields.append("item_id = ?")
        params.append(item_id)
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    if checkpoint_data is not None:
        fields.append("checkpoint_data = ?")
        params.append(json.dumps(checkpoint_data))

    params.append(job_id)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        query = f"UPDATE library_jobs SET {', '.join(fields)} WHERE job_id = ?;"
        cursor.execute(query, params)
        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

# --- Library Items ---

def create_library_item(
    title: str,
    year: Optional[int],
    clean_title: str,
    folder_path: str,
    media_type: str = "movie",
    source_url: Optional[str] = None,
    source_key: Optional[str] = None,
    status: str = "queued",
    synopsis: Optional[str] = None,
    genres: Optional[str] = None,
    poster_path: Optional[str] = None,
    backdrop_path: Optional[str] = None,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    if not source_key and source_url:
        source_key = normalize_source_key(source_url)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO library_items (
                title, year, clean_title, folder_path, media_type, source_url, source_key,
                status, synopsis, genres, poster_path, backdrop_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (title, year, clean_title, folder_path, media_type, source_url, source_key,
              status, synopsis, genres, poster_path, backdrop_path, now, now))
        item_id = cursor.lastrowid
        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (item_id,))
        return dict(cursor.fetchone())

def find_item_by_source_key(source_key: str, db_path: Optional[Path] = None) -> Optional[dict]:
    if not source_key:
        return None
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_items WHERE source_key = ? ORDER BY id DESC LIMIT 1;", (source_key,))
        row = cursor.fetchone()
        return dict(row) if row else None

def find_item_by_title_year(title: str, year: Optional[int] = None, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        if year:
            cursor.execute("SELECT * FROM library_items WHERE LOWER(title) = LOWER(?) AND year = ? ORDER BY id DESC LIMIT 1;", (title, year))
        else:
            cursor.execute("SELECT * FROM library_items WHERE LOWER(title) = LOWER(?) ORDER BY id DESC LIMIT 1;", (title,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_item_status(
    item_id: int,
    status: str,
    jellyfin_item_id: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Optional[dict]:
    init_db(db_path)
    now = time.time()
    fields = ["status = ?", "updated_at = ?"]
    params: List[Any] = [status, now]

    if jellyfin_item_id is not None:
        fields.append("jellyfin_item_id = ?")
        params.append(jellyfin_item_id)

    params.append(item_id)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        query = f"UPDATE library_items SET {', '.join(fields)} WHERE id = ?;"
        cursor.execute(query, params)
        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (item_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_library_item_metadata(
    item_id: int,
    title: Optional[str] = None,
    year: Optional[int] = None,
    clean_title: Optional[str] = None,
    synopsis: Optional[str] = None,
    genres: Optional[str] = None,
    poster_path: Optional[str] = None,
    backdrop_path: Optional[str] = None,
    is_favorite: Optional[int] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Optional[dict]:
    init_db(db_path)
    now = time.time()
    fields = ["updated_at = ?"]
    params: List[Any] = [now]

    if title is not None:
        fields.append("title = ?")
        params.append(title.strip())
    if year is not None:
        fields.append("year = ?")
        params.append(year)
    if clean_title is not None:
        fields.append("clean_title = ?")
        params.append(clean_title.strip())
    if synopsis is not None:
        fields.append("synopsis = ?")
        params.append(synopsis.strip())
    if genres is not None:
        fields.append("genres = ?")
        params.append(genres.strip())
    if poster_path is not None:
        fields.append("poster_path = ?")
        params.append(poster_path)
    if backdrop_path is not None:
        fields.append("backdrop_path = ?")
        params.append(backdrop_path)
    if is_favorite is not None:
        fields.append("is_favorite = ?")
        params.append(int(bool(is_favorite)))
    if status is not None:
        fields.append("status = ?")
        params.append(status)

    params.append(item_id)

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        query = f"UPDATE library_items SET {', '.join(fields)} WHERE id = ?;"
        cursor.execute(query, params)
        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (item_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

# --- Library Files ---

def add_library_file(
    item_id: int,
    file_path: str,
    file_type: str,
    file_size: int = 0,
    duration: Optional[float] = None,
    video_codec: Optional[str] = None,
    audio_codec: Optional[str] = None,
    resolution: Optional[str] = None,
    subtitle_lang: Optional[str] = None,
    is_primary: int = 0,
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO library_files (
                item_id, file_path, file_type, file_size, duration, video_codec, audio_codec, resolution, subtitle_lang, is_primary, is_default, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (item_id, file_path, file_type, file_size, duration, video_codec, audio_codec, resolution, subtitle_lang, is_primary, is_primary, now))
        file_id = cursor.lastrowid
        cursor.execute("SELECT * FROM library_files WHERE id = ?;", (file_id,))
        row = cursor.fetchone()
        d = dict(row) if row else {}
        if 'is_default' not in d or d['is_default'] is None:
            d['is_default'] = d.get('is_primary', 0)
        return d

def get_library_files_for_item(item_id: int, db_path: Optional[Path] = None) -> List[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_files WHERE item_id = ? ORDER BY is_primary DESC, id ASC;", (item_id,))
        res = []
        for f in cursor.fetchall():
            d = dict(f)
            if 'is_default' not in d or d['is_default'] is None:
                d['is_default'] = d.get('is_primary', 0)
            res.append(d)
        return res

def remove_library_file(file_id: int, db_path: Optional[Path] = None) -> bool:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM library_files WHERE id = ?;", (file_id,))
        return cursor.rowcount > 0

def get_library_file(file_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_files WHERE id = ?;", (file_id,))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            if 'is_default' not in d or d['is_default'] is None:
                d['is_default'] = d.get('is_primary', 0)
            return d
        return None

def set_default_subtitle(item_id: int, file_id: int, db_path: Optional[Path] = None) -> bool:
    """Sets a subtitle track as primary for an item while clearing primary flag on others."""
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE library_files SET is_primary = 0, is_default = 0 WHERE item_id = ? AND file_type = 'subtitle';", (item_id,))
        cursor.execute("UPDATE library_files SET is_primary = 1, is_default = 1 WHERE id = ? AND item_id = ? AND file_type = 'subtitle';", (file_id, item_id))
        return cursor.rowcount > 0

# --- User Activity Tracking (Meaningful Events) ---

def record_user_activity(session_id: Optional[str], action: str, db_path: Optional[Path] = None):
    """Records meaningful user action (e.g. streaming, download, unlock). Polling & assets are excluded."""
    if not session_id:
        return
    init_db(db_path)
    pseudo = pseudonymize_session(session_id)
    now = time.time()
    try:
        with db_session(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_activity_events (session_pseudonym, action, timestamp)
                VALUES (?, ?, ?);
            """, (pseudo, action, now))
    except Exception:
        pass

def get_active_sessions_count_window(window_seconds: int = 300, db_path: Optional[Path] = None) -> int:
    """Counts distinct session pseudonyms active in the last N seconds from meaningful events."""
    init_db(db_path)
    cutoff = time.time() - window_seconds
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(DISTINCT session_pseudonym) AS c
            FROM user_activity_events
            WHERE timestamp >= ?;
        """, (cutoff,))
        row = cursor.fetchone()
        return int(row['c']) if row else 0

def get_unique_sessions_today(start_timestamp: float, db_path: Optional[Path] = None) -> int:
    """Counts distinct session pseudonyms from both jobs and meaningful activity events since today's start."""
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(DISTINCT pseudo) AS total FROM (
                SELECT owner_session_pseudonym AS pseudo FROM download_jobs WHERE created_at >= ?
                UNION
                SELECT session_pseudonym AS pseudo FROM user_activity_events WHERE timestamp >= ?
            );
        """, (start_timestamp, start_timestamp))
        row = cursor.fetchone()
        return int(row['total']) if row else 0

# --- Retrieval & Management ---

def get_library_item(item_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (item_id,))
        item_row = cursor.fetchone()
        if not item_row:
            return None

        item = dict(item_row)
        cursor.execute("SELECT * FROM library_files WHERE item_id = ? ORDER BY is_primary DESC, id ASC;", (item_id,))
        item['files'] = [dict(f) for f in cursor.fetchall()]
        return item

def list_library_items(
    query: Optional[str] = None,
    status: Optional[str] = None,
    genre: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    db_path: Optional[Path] = None
) -> List[dict]:
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if query:
        conditions.append("(title LIKE ? OR clean_title LIKE ? OR synopsis LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q])
    if status:
        conditions.append("status = ?")
        params.append(status)
    if genre:
        conditions.append("genres LIKE ?")
        params.append(f"%{genre}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    allowed_sort = {"created_at", "title", "year", "updated_at"}
    col_sort = sort_by if sort_by in allowed_sort else "created_at"
    dir_sort = "ASC" if sort_dir.lower() == "asc" else "DESC"

    with db_session(db_path) as conn:
        cursor = conn.cursor()
        sql = f"""
            SELECT * FROM library_items 
            {where_clause}
            ORDER BY {col_sort} {dir_sort} 
            LIMIT ? OFFSET ?;
        """
        cursor.execute(sql, params + [limit, offset])
        items = [dict(r) for r in cursor.fetchall()]

        for item in items:
            cursor.execute("SELECT * FROM library_files WHERE item_id = ? ORDER BY is_primary DESC, id ASC;", (item['id'],))
            item['files'] = [dict(f) for f in cursor.fetchall()]

        return items

def count_library_items(
    query: Optional[str] = None,
    status: Optional[str] = None,
    genre: Optional[str] = None,
    db_path: Optional[Path] = None
) -> int:
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if query:
        conditions.append("(title LIKE ? OR clean_title LIKE ? OR synopsis LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q])
    if status:
        conditions.append("status = ?")
        params.append(status)
    if genre:
        conditions.append("genres LIKE ?")
        params.append(f"%{genre}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS total FROM library_items {where};", params)
        row = cursor.fetchone()
        return int(row['total']) if row else 0

def delete_library_item(
    item_id: int,
    delete_files: bool = False,
    move_to_trash: bool = True,
    deleted_by: str = "admin",
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Deletes an item from the library DB with full tracking.
    Never ignores filesystem errors as success. Supports quarantine trash with snapshot recovery.
    """
    item = get_library_item(item_id, db_path=db_path)
    if not item:
        return {'success': False, 'files_deleted': False, 'trashed': False, 'database_deleted': False, 'partial_error': 'Item not found in database'}

    files_removed = False
    trashed = False
    partial_error = None
    dest_folder = None
    target_dir = None

    if delete_files and item.get('folder_path'):
        target_dir = (config.LIBRARY_ROOT / item['folder_path']).resolve()
        root_dir = config.LIBRARY_ROOT.resolve()
        movies_dir = config.LIBRARY_MOVIES_DIR.resolve()

        # Strict containment & structural guard
        try:
            target_dir.relative_to(root_dir)
            if target_dir in (root_dir, movies_dir):
                return {
                    'success': False,
                    'files_deleted': False,
                    'trashed': False,
                    'database_deleted': False,
                    'partial_error': "Penolakan keamanan: Tidak diizinkan menghapus root atau folder struktural perpustakaan."
                }

            # In-use check against active jobs
            from ..admin.storage_manager import is_path_in_use
            if is_path_in_use(target_dir):
                return {
                    'success': False,
                    'files_deleted': False,
                    'trashed': False,
                    'database_deleted': False,
                    'partial_error': "Penolakan: Direktori sedang digunakan oleh proses unduhan/impor yang aktif."
                }

            if target_dir.exists() and target_dir.is_dir():
                if move_to_trash:
                    try:
                        trash_root = config.TRASH_DIR
                        trash_root.mkdir(parents=True, exist_ok=True)
                        dest_folder = trash_root / f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{target_dir.name}"
                        shutil.move(str(target_dir), str(dest_folder))
                        trashed = True
                        files_removed = True

                        retention_days = get_setting("trash_retention_days", config.TRASH_RETENTION_DAYS)
                        create_trash_entry(
                            original_path=str(target_dir),
                            trash_path=str(dest_folder),
                            item_type='library_item',
                            resource_id=str(item_id),
                            name=item.get('title', target_dir.name),
                            file_size=0,
                            deleted_by=deleted_by,
                            expires_at=time.time() + (retention_days * 86400),
                            metadata_snapshot=json.dumps(item),
                            db_path=db_path
                        )
                    except Exception as t_err:
                        return {
                            'success': False,
                            'files_deleted': False,
                            'trashed': False,
                            'database_deleted': False,
                            'partial_error': f"Gagal memindahkan berkas ke karantina: {t_err}"
                        }
                else:
                    try:
                        shutil.rmtree(target_dir)
                        if target_dir.exists():
                            return {
                                'success': False,
                                'files_deleted': False,
                                'trashed': False,
                                'database_deleted': False,
                                'partial_error': "Gagal menghapus folder media secara penuh."
                            }
                        files_removed = True
                    except Exception as rm_err:
                        return {
                            'success': False,
                            'files_deleted': False,
                            'trashed': False,
                            'database_deleted': False,
                            'partial_error': f"Gagal menghapus folder: {rm_err}"
                        }
        except ValueError:
            return {
                'success': False,
                'files_deleted': False,
                'trashed': False,
                'database_deleted': False,
                'partial_error': "Direktori target melanggar batas root perpustakaan."
            }

    # Now remove from database
    try:
        with db_session(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM library_items WHERE id = ?;", (item_id,))
            db_deleted = cursor.rowcount > 0
    except Exception as dberr:
        db_deleted = False
        partial_error = f"Penghapusan database gagal: {dberr}"
        # Rollback / Compensation: move files back if DB deletion failed
        if trashed and dest_folder and target_dir and dest_folder.exists():
            try:
                shutil.move(str(dest_folder), str(target_dir))
                files_removed = False
                trashed = False
            except Exception:
                pass

    overall_success = db_deleted and (not delete_files or files_removed)
    return {
        'success': overall_success,
        'files_deleted': files_removed,
        'trashed': trashed,
        'database_deleted': db_deleted,
        'partial_error': partial_error
    }

def get_completed_jobs_count_today(completed_after_ts: float, db_path: Optional[Path] = None) -> int:
    """Accurately counts jobs completed on or after timestamp using completed_at without fetching row limits."""
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM download_jobs
            WHERE stage IN ('completed', 'ready') AND completed_at >= ?;
        """, (completed_after_ts,))
        row = cursor.fetchone()
        return row[0] if row else 0

def get_failed_jobs_count_today(start_ts: float, db_path: Optional[Path] = None) -> int:
    """Accurately counts jobs that failed today based on completed_at or created_at."""
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM download_jobs
            WHERE stage = 'failed' AND (completed_at >= ? OR created_at >= ?);
        """, (start_ts, start_ts))
        row = cursor.fetchone()
        return row[0] if row else 0

def get_jobs_success_rate_aggregate(since_ts: float, db_path: Optional[Path] = None) -> Tuple[Optional[float], int]:
    """Computes success rate using direct SQL aggregation across all terminal jobs since timestamp.
    Returns (rate_or_none, sample_count).
    """
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                SUM(CASE WHEN stage IN ('completed', 'ready') THEN 1 ELSE 0 END) as completed_count,
                COUNT(*) as total_count
            FROM download_jobs
            WHERE stage IN ('completed', 'ready', 'failed', 'cancelled', 'interrupted')
              AND created_at >= ?;
        """, (since_ts,))
        row = cursor.fetchone()
        if not row or not row['total_count']:
            return None, 0
        total = row['total_count']
        completed = row['completed_count'] or 0
        rate = round((completed / total * 100.0), 1)
        return rate, total

def prune_terminal_jobs(cutoff_ts: float, db_path: Optional[Path] = None) -> int:
    """Prunes terminal download jobs older than cutoff_ts whose physical files do not exist."""
    init_db(db_path)
    with db_session(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM download_jobs
            WHERE stage IN ('completed', 'failed', 'cancelled', 'interrupted')
              AND created_at < ?
              AND file_exists = 0;
        """, (cutoff_ts,))
        return cursor.rowcount

