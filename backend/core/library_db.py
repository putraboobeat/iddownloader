"""SQLite Library & Integration Database Module for OmniFetch Personal Streaming Library.
Implements versioned migrations, WAL mode, foreign keys, and safe transaction handling.
"""
import sqlite3
import json
import time
import hashlib
import re
from pathlib import Path
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from . import config

CURRENT_SCHEMA_VERSION = 1

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
        # Drop transient parameters
        ignore_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid', 'ref', 'ts'}
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filtered_qs = {k: v for k, v in qs.items() if k.lower() not in ignore_params}
        sorted_query = urlencode(sorted(filtered_qs.items()), doseq=True)
        normalized_url = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), parsed.params, sorted_query, ''))
        return hashlib.sha256(normalized_url.encode('utf-8')).hexdigest()
    except Exception:
        return hashlib.sha256(url.encode('utf-8')).hexdigest()

def get_db_path(custom_path: Optional[Path] = None) -> Path:
    p = custom_path or config.LIBRARY_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = get_db_path(db_path)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db(db_path: Optional[Path] = None):
    """Initializes the database and runs any pending schema migrations."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);")
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version ASC;")
        applied_versions = {row['version'] for row in cursor.fetchall()}

        for v in sorted(MIGRATIONS.keys()):
            if v not in applied_versions:
                cursor.executescript(MIGRATIONS[v])
                cursor.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);", (v, time.time()))
        conn.commit()

# --- Library Jobs ---

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

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        cursor.execute("""
            INSERT INTO library_jobs (job_id, item_id, owner_session, stage, target, idempotency_key, checkpoint_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (job_id, item_id, owner_session, stage, target, idempotency_key, ck_json, now, now))
        conn.commit()

        cursor.execute("SELECT * FROM library_jobs WHERE job_id = ?;", (job_id,))
        return dict(cursor.fetchone())

def get_job_by_idempotency_key(idempotency_key: str, owner_session: str, db_path: Optional[Path] = None) -> Optional[dict]:
    if not idempotency_key:
        return None
    init_db(db_path)
    with get_connection(db_path) as conn:
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
    with get_connection(db_path) as conn:
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

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        query = f"UPDATE library_jobs SET {', '.join(fields)} WHERE job_id = ?;"
        cursor.execute(query, params)
        conn.commit()

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
    db_path: Optional[Path] = None
) -> dict:
    init_db(db_path)
    now = time.time()
    if not source_key and source_url:
        source_key = normalize_source_key(source_url)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO library_items (title, year, clean_title, folder_path, media_type, source_url, source_key, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (title, year, clean_title, folder_path, media_type, source_url, source_key, status, now, now))
        conn.commit()
        item_id = cursor.lastrowid

        cursor.execute("SELECT * FROM library_items WHERE id = ?;", (item_id,))
        return dict(cursor.fetchone())

def find_item_by_source_key(source_key: str, db_path: Optional[Path] = None) -> Optional[dict]:
    if not source_key:
        return None
    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM library_items WHERE source_key = ? ORDER BY id DESC LIMIT 1;", (source_key,))
        row = cursor.fetchone()
        return dict(row) if row else None

def find_item_by_title_year(title: str, year: Optional[int] = None, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_connection(db_path) as conn:
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

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        query = f"UPDATE library_items SET {', '.join(fields)} WHERE id = ?;"
        cursor.execute(query, params)
        conn.commit()

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
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO library_files (
                item_id, file_path, file_type, file_size, duration, video_codec, audio_codec, resolution, subtitle_lang, is_primary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (item_id, file_path, file_type, file_size, duration, video_codec, audio_codec, resolution, subtitle_lang, is_primary, now))
        conn.commit()
        file_id = cursor.lastrowid

        cursor.execute("SELECT * FROM library_files WHERE id = ?;", (file_id,))
        return dict(cursor.fetchone())

# --- Retrieval & Management ---

def get_library_item(item_id: int, db_path: Optional[Path] = None) -> Optional[dict]:
    init_db(db_path)
    with get_connection(db_path) as conn:
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
    limit: int = 50,
    offset: int = 0,
    db_path: Optional[Path] = None
) -> List[dict]:
    init_db(db_path)
    conditions = []
    params: List[Any] = []

    if query:
        conditions.append("(title LIKE ? OR clean_title LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if status:
        conditions.append("status = ?")
        params.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        sql = f"""
            SELECT * FROM library_items 
            {where_clause}
            ORDER BY created_at DESC 
            LIMIT ? OFFSET ?;
        """
        params.extend([limit, offset])
        cursor.execute(sql, params)
        items = [dict(r) for r in cursor.fetchall()]

        for item in items:
            cursor.execute("SELECT * FROM library_files WHERE item_id = ? ORDER BY is_primary DESC, id ASC;", (item['id'],))
            item['files'] = [dict(f) for f in cursor.fetchall()]

        return items

def delete_library_item(item_id: int, delete_files: bool = False, db_path: Optional[Path] = None) -> bool:
    """Deletes an item from the library DB and optionally deletes its physical directory."""
    item = get_library_item(item_id, db_path=db_path)
    if not item:
        return False

    if delete_files and item.get('folder_path'):
        target_dir = (config.LIBRARY_ROOT / item['folder_path']).resolve()
        root_dir = config.LIBRARY_ROOT.resolve()
        # Path traversal guard: must be strictly inside LIBRARY_ROOT
        try:
            target_dir.relative_to(root_dir)
            if target_dir != root_dir and target_dir.exists() and target_dir.is_dir():
                import shutil
                shutil.rmtree(target_dir, ignore_errors=True)
        except ValueError:
            # target_dir is not relative to LIBRARY_ROOT! Refuse deletion.
            pass

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM library_items WHERE id = ?;", (item_id,))
        conn.commit()
    return True
