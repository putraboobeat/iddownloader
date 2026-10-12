from __future__ import annotations
import threading
import uuid
import time
import subprocess
import re
import json
import shutil
import os
import signal
from pathlib import Path
from typing import Dict, Optional, List, Any

from . import config
from .storage import save_metadata, read_metadata, ensure_job_zip, get_job_files
from .downloader import build_ytdlp_command
from . import library_db
from .jellyfin import jellyfin_client
from .importer import LibraryImporter

# Definisi Stage Mesin Unduhan OmniFetch
ACTIVE_STAGES = {'queued', 'downloading', 'validating', 'importing', 'indexing'}
TERMINAL_STAGES = {'completed', 'ready', 'failed', 'cancelled', 'interrupted', 'needs_attention'}
ALL_STAGES = ACTIVE_STAGES | TERMINAL_STAGES

def get_db_path() -> Path:
    p = config.JOBS_DB_PATH if not config.VPS_ROOT else config.VPS_ROOT / "jobs_db.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def validate_job_output(folder: Path) -> bool:
    """Validates the output files using ffprobe to ensure they are complete and valid."""
    if not folder.exists():
        return False
    media_files = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.wav', '.flac') and not p.name.startswith('.'):
            media_files.append(p)
    
    if not media_files:
        return True # Non-media outputs or zip file check
        
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return True # Can't validate without ffprobe
        
    all_valid = True
    for file in media_files:
        cmd = [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(file)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                all_valid = False
                break
        except Exception:
            all_valid = False
            break
            
    return all_valid


class Job:
    def __init__(
        self,
        owner_session: str,
        data: dict,
        existing_id: Optional[str] = None,
        existing_stage: str = "queued",
        existing_status: Optional[str] = None,
        existing_status_message: str = "",
        existing_logs: Optional[list] = None,
        created_at: Optional[float] = None,
        parent_job_id: Optional[str] = None,
        retry_count: int = 0,
        started_at: Optional[float] = None,
        completed_at: Optional[float] = None,
        percent: Optional[float] = None,
        bytes_downloaded: int = 0,
        total_bytes: int = 0,
        existing_timeline: Optional[list] = None,
        file_exists: Optional[bool] = None,
        existing_folder: Optional[str] = None
    ):
        self.id = existing_id or str(uuid.uuid4())
        self.owner_session = owner_session
        self.owner_pseudonym = library_db.pseudonymize_session(owner_session)
        self.data = data
        self.target = data.get('target', 'download')
        
        # State Machine: Stage vs Display Message
        stage_cand = existing_status or existing_stage or "queued"
        if stage_cand in ALL_STAGES:
            self._stage = stage_cand
        else:
            self._stage = "queued"
            
        self.status_message = existing_status_message or ("Dalam antrean..." if self._stage == "queued" else self._stage)
        self.percent = percent if percent is not None else (100.0 if self._stage in ("completed", "ready") else 0.0)
        self.bytes_downloaded = bytes_downloaded
        self.total_bytes = total_bytes
        self.speed_str = ""
        self.eta_str = ""
        self.quality = data.get('quality', '')
        self.error_message = ""
        self.retry_count = retry_count
        self.parent_job_id = parent_job_id
        
        self.logs: List[str] = existing_logs or []
        self.timeline: List[dict] = existing_timeline or []
        now = time.time()
        self.created_at = created_at or now
        self.started_at: Optional[float] = started_at if started_at is not None else (now if self._stage == "downloading" else None)
        self.completed_at: Optional[float] = completed_at if completed_at is not None else (now if self._stage in TERMINAL_STAGES else None)
        self.heartbeat_at = now
        self.last_progress_at = now
        self.file_exists = file_exists if file_exists is not None else True
        
        self.process: Optional[subprocess.Popen] = None
        self.library_item_id = data.get('library_item_id')
        self.jellyfin_item_id = data.get('jellyfin_item_id')
        self.jellyfin_url = data.get('jellyfin_url')
        
        raw_url = data.get('urls') or data.get('url') or ''
        if isinstance(raw_url, list):
            raw_url = raw_url[0] if raw_url else ''
        self.url = str(raw_url).strip()
        self.domain = library_db.extract_domain(self.url)
        self.title = data.get('title') or data.get('name') or "Media"

        base_name = re.sub(r'[^a-zA-Z0-9_-]', '_', self.title)[:30] or "Download"
        if existing_folder:
            self.folder = Path(existing_folder)
        elif self.target == 'library':
            self.folder = config.TEMPORARY_DIR / f"staging_{self.id[:8]}"
        elif config.VPS_ROOT:
            self.folder = config.VPS_ROOT / self.id
        elif config.TEMPORARY_DIR:
            self.folder = config.TEMPORARY_DIR / f"{base_name}_{self.id[:8]}"
        else:
            self.folder = Path.home() / 'Downloads' / f"{base_name}_{self.id[:8]}"
            
        if not existing_timeline:
            self._record_timeline(self._stage, self.status_message)

        if not existing_id:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                save_metadata(self.folder, {
                    'job_id': self.id,
                    'owner_session': self.owner_session,
                    'target': self.target,
                    'created_at': self.created_at,
                    'expires_at': (self.created_at + config.RETENTION_SECONDS) if (config.VPS_ROOT and self.target != 'library') else None,
                    'input_url': self.url
                })
            except Exception as e:
                self.logs.append(f"Storage Note: {e}")
                
            if self.target == 'library':
                try:
                    library_db.create_or_get_library_job(
                        job_id=self.id,
                        owner_session=self.owner_session,
                        stage=self._stage,
                        target='library',
                        idempotency_key=data.get('idempotency_key')
                    )
                except Exception as db_err:
                    self.logs.append(f"DB Note: {db_err}")

            self.sync_persistent_db()

    @property
    def stage(self) -> str:
        return self._stage

    @stage.setter
    def stage(self, new_stage: str):
        if new_stage in ALL_STAGES:
            self._stage = new_stage
            self.heartbeat_at = time.time()
            if new_stage in TERMINAL_STAGES and not self.completed_at:
                self.completed_at = time.time()
            self._record_timeline(new_stage, self.status_message)
            self.sync_persistent_db()

    @property
    def status(self) -> str:
        """Backwards compatibility for python tests and callers: returns structured stage."""
        return self._stage

    @status.setter
    def status(self, val: str):
        """Backwards compatibility for legacy assignment `job.status = ...`."""
        if val in ALL_STAGES:
            self.stage = val
            if not self.status_message:
                self.status_message = val
        else:
            self.status_message = val
            self.heartbeat_at = time.time()
        self.sync_persistent_db()

    def set_stage(self, new_stage: str, message: Optional[str] = None):
        """Structured state machine transition."""
        if new_stage in ALL_STAGES:
            self._stage = new_stage
        if message:
            self.status_message = message
        now = time.time()
        self.heartbeat_at = now
        if new_stage == "downloading" and not self.started_at:
            self.started_at = now
        if new_stage in TERMINAL_STAGES:
            self.completed_at = now
        self._record_timeline(new_stage, self.status_message)
        self.sync_persistent_db()

    def update_progress(self, percent: Optional[float] = None, speed: Optional[str] = None, eta: Optional[str] = None, message: Optional[str] = None):
        """Updates progress metrics without changing the core state machine stage."""
        now = time.time()
        self.heartbeat_at = now
        if percent is not None and percent > self.percent:
            self.percent = float(percent)
            self.last_progress_at = now
        if speed:
            self.speed_str = speed
        if eta:
            self.eta_str = eta
        if message:
            self.status_message = message
        self.sync_persistent_db(throttle=True)

    def _record_timeline(self, stage: str, message: str):
        if not self.timeline or self.timeline[-1].get('stage') != stage:
            self.timeline.append({
                'stage': stage,
                'time': time.time(),
                'message': message
            })
            if len(self.timeline) > 30:
                self.timeline = self.timeline[-30:]

    def sync_persistent_db(self, throttle: bool = False):
        """Synchronizes current job record to SQLite download_jobs table."""
        # Optional throttle on high-frequency progress line reporting
        if throttle and hasattr(self, '_last_sync_time'):
            if time.time() - self._last_sync_time < 0.5:
                return
        self._last_sync_time = time.time()

        res_file = ""
        res_size = 0
        if self.folder.exists() and self.folder.is_dir():
            files = [p for p in self.folder.iterdir() if p.is_file() and not p.name.startswith('.')]
            if files:
                primary = sorted(files, key=lambda f: f.stat().st_size, reverse=True)[0]
                res_file = primary.name
                res_size = primary.stat().st_size

        try:
            library_db.upsert_download_job({
                'job_id': self.id,
                'owner_session': self.owner_session,
                'owner_session_pseudonym': self.owner_pseudonym,
                'target': self.target,
                'url': self.url,
                'title': self.title,
                'domain': self.domain,
                'stage': self._stage,
                'status_message': self.status_message,
                'percent': self.percent,
                'bytes_downloaded': self.bytes_downloaded,
                'total_bytes': self.total_bytes,
                'speed_str': self.speed_str,
                'eta_str': self.eta_str,
                'quality': self.quality,
                'error_message': self.error_message,
                'retry_count': self.retry_count,
                'parent_job_id': self.parent_job_id,
                'library_item_id': self.library_item_id,
                'result_filename': res_file,
                'result_path': str(self.folder),
                'file_size': res_size,
                'file_exists': 1 if self.file_exists and self.folder.exists() else 0,
                'expires_at': (self.created_at + config.RETENTION_SECONDS) if (config.VPS_ROOT and self.target != 'library') else None,
                'heartbeat_at': self.heartbeat_at,
                'last_progress_at': self.last_progress_at,
                'created_at': self.created_at,
                'started_at': self.started_at,
                'completed_at': self.completed_at,
                'timeline': self.timeline,
                'logs': self.logs[-50:]
            })
        except Exception:
            pass

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "job_id": self.id,
            "owner_session": self.owner_session,
            "owner_pseudonym": self.owner_pseudonym,
            "data": self.data,
            "stage": self._stage,
            "status": self.status_message or self._stage,
            "status_message": self.status_message,
            "target": self.target,
            "percent": self.percent,
            "speed_str": self.speed_str,
            "eta_str": self.eta_str,
            "quality": self.quality,
            "domain": self.domain,
            "title": self.title,
            "url": self.url,
            "logs": self.logs,
            "timeline": self.timeline,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "heartbeat_at": self.heartbeat_at,
            "last_progress_at": self.last_progress_at,
            "folder": str(self.folder),
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "retry_count": self.retry_count,
            "parent_job_id": self.parent_job_id,
            "is_active": self._stage in ACTIVE_STAGES
        }
        if self.target == 'library':
            d["library_item_id"] = self.library_item_id
            d["jellyfin_item_id"] = self.jellyfin_item_id
            d["jellyfin_url"] = self.jellyfin_url
        return d

    def to_admin_dto(self) -> dict:
        """Admin DTO that pseudonymizes owner and redacts secret tokens in URLs and logs."""
        from ..admin import audit
        return {
            "id": self.id,
            "job_id": self.id,
            "owner_pseudonym": self.owner_pseudonym,
            "owner_session_pseudonym": self.owner_pseudonym,
            "stage": self._stage,
            "status": self.status_message or self._stage,
            "status_message": self.status_message,
            "target": self.target,
            "percent": self.percent,
            "speed_str": self.speed_str,
            "eta_str": self.eta_str,
            "quality": self.quality,
            "domain": self.domain,
            "title": self.title,
            "url": audit.redact_sensitive_text(self.url),
            "logs": audit.redact_secrets(self.logs),
            "timeline": audit.redact_secrets(self.timeline),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "heartbeat_at": self.heartbeat_at,
            "last_progress_at": self.last_progress_at,
            "file_exists": bool(self.file_exists and self.folder.exists()),
            "retry_count": self.retry_count,
            "parent_job_id": self.parent_job_id,
            "is_active": self._stage in ACTIVE_STAGES,
            "library_item_id": self.library_item_id if self.target == 'library' else None,
            "jellyfin_item_id": self.jellyfin_item_id if self.target == 'library' else None,
            "jellyfin_url": audit.redact_sensitive_text(self.jellyfin_url) if self.jellyfin_url else None
        }

    def log(self, message: str):
        self.logs.append(message)
        if len(self.logs) > 150:
            self.logs = self.logs[-150:]
        job_manager.save_db()
        self.sync_persistent_db()

    def terminate(self):
        """Safely stops the worker process tree and marks job cancelled."""
        if self._stage in TERMINAL_STAGES:
            return  # Terminal jobs (completed/ready/failed) must never be overwritten as cancelled

        proc = self.process
        if proc and proc.poll() is None:
            pid = proc.pid
            try:
                # Terminate only the child's dedicated process group
                if hasattr(os, 'killpg'):
                    try:
                        pgid = os.getpgid(pid)
                        # STRICT GUARD: Ensure pgid != server's pgid
                        if pgid != os.getpgid(0):
                            os.killpg(pgid, signal.SIGTERM)
                        else:
                            proc.terminate()
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                else:
                    proc.terminate()

                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    if hasattr(os, 'killpg') and pgid != os.getpgid(0):
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except Exception:
                            proc.kill()
                    else:
                        proc.kill()
                    proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self.set_stage("cancelled", "Proses dihentikan oleh pengguna.")
        self.log("Proses dihentikan oleh pengguna.")
        job_manager.save_db()
        self.sync_persistent_db()

    def run(self):
        target_url = self.url
        is_subtitle_mode = self.data.get('mode') == 'subtitle' or any(target_url.lower().split('?')[0].endswith(ext) for ext in ('.vtt', '.srt'))

        if is_subtitle_mode:
            self.set_stage("downloading", "downloading")
            job_manager.save_db()
            try:
                from subtitles import save_subtitle
                sub_name = self.data.get('name') or "Subtitle"
                cookie_arg = config.COOKIES_PATH if Path(config.COOKIES_PATH).is_file() else None
                saved_path = save_subtitle(
                    track={'url': target_url, 'referer': self.data.get('referer', '')},
                    folder=self.folder,
                    name=sub_name,
                    cancelled=lambda: self._stage == 'cancelled',
                    cookie_file=cookie_arg
                )
                if self._stage == "cancelled":
                    return
                self.percent = 100.0
                self.set_stage("completed", f"Subtitle berhasil disimpan: {saved_path.name}")
                self.log(f"Subtitle berhasil disimpan: {saved_path.name}")
                ensure_job_zip(self.folder, sub_name)
            except Exception as e:
                if self._stage != "cancelled":
                    self.error_message = str(e)
                    self.set_stage("failed", f"Gagal mengunduh subtitle: {str(e)}")
                    self.log(f"Gagal mengunduh subtitle: {str(e)}")
            job_manager.save_db()
            return

        # 2. Pengunduhan Subtitle Tambahan jika disertakan bersama video
        if self.data.get('subtitle_url'):
            try:
                from subtitles import save_subtitle
                sub_url = str(self.data['subtitle_url']).strip()
                self.log("Mengunduh subtitle eksternal pendamping...")
                cookie_arg = config.COOKIES_PATH if Path(config.COOKIES_PATH).is_file() else None
                saved_sub = save_subtitle(
                    track={'url': sub_url, 'referer': self.data.get('referer', '')},
                    folder=self.folder,
                    name=self.data.get('name') or "Subtitle",
                    cancelled=lambda: self._stage == 'cancelled',
                    cookie_file=cookie_arg
                )
                self.log(f"Subtitle eksternal tersimpan: {saved_sub.name}")
            except Exception as sub_err:
                self.log(f"Peringatan subtitle eksternal: {str(sub_err)}")

        max_retries = 2
        retry_count = 0
        
        while retry_count <= max_retries:
            if self._stage == "cancelled":
                break

            self.set_stage("downloading", "downloading")
            if retry_count > 0:
                self.log(f"Memulai ulang percobaan ke-{retry_count} / {max_retries}...")
            job_manager.save_db()
            
            try:
                # Check disk space (Need at least 1GB free)
                free_bytes = shutil.disk_usage(self.folder.parent).free
                if free_bytes < 1024 * 1024 * 1024:
                    self.log("Ruang penyimpanan VPS tidak cukup (< 1GB).")
                    self.error_message = "Ruang penyimpanan tidak cukup (< 1GB)"
                    self.set_stage("failed", "Ruang penyimpanan tidak cukup")
                    break

                cmd = build_ytdlp_command(self.data, self.folder, cookies_path=config.COOKIES_PATH)
                # start_new_session=True creates an isolated process group on POSIX (pgid == pid)
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace',
                    start_new_session=True
                )
                
                current_stage_label = 'Mengunduh media...'
                for raw in self.process.stdout:
                    if self._stage == "cancelled":
                        break
                    line = raw.strip()
                    if line.startswith('APP_PROGRESS:'):
                        text = line.split(':', 1)[1].strip()
                        pct = None
                        match = re.search(r'(\d+(?:\.\d+)?)%', text)
                        if match:
                            pct = float(match.group(1))
                        
                        speed_eta = text.split('|', 1)[1] if '|' in text else text
                        sp_m = re.search(r'([0-9.]+\s*[KMGT]?i?B/s)', text)
                        speed = sp_m.group(1) if sp_m else ""
                        eta_m = re.search(r'ETA\s+([0-9:]+)', text)
                        eta = eta_m.group(1) if eta_m else ""
                        
                        msg = f"{current_stage_label} — {pct if pct is not None else self.percent:.1f}% | {speed_eta.strip()}"
                        self.update_progress(percent=pct, speed=speed, eta=eta, message=msg)
                    elif line.startswith('APP_FILE:'):
                        self.log('File: ' + line.split(':', 1)[1])
                    elif line:
                        self.log(line)
                        stage_hint = None
                        if 'Downloading webpage' in line: stage_hint = 'Menghubungi server media…'
                        elif '[download] Downloading item' in line: stage_hint = line.replace('[download]', '').strip()
                        elif 'Downloading m3u8' in line: stage_hint = 'Menyiapkan daftar potongan video…'
                        elif 'Retrying' in line: stage_hint = 'Koneksi tersendat. Mencoba kembali…'
                        elif '[Merger]' in line: stage_hint = 'Menggabungkan video dan audio…'
                        elif '[ExtractAudio]' in line: stage_hint = 'Mengekstrak audio…'
                        
                        if stage_hint:
                            current_stage_label = stage_hint
                            self.status_message = stage_hint
                            self.heartbeat_at = time.time()
                            
                self.process.wait()
                
                if self._stage == "cancelled":
                    break
                    
                if self.process.returncode == 0:
                    if self._stage == "cancelled":
                        break
                    self.set_stage("validating", "Memvalidasi kelengkapan berkas...")
                    if self._stage == "cancelled":
                        break
                    if not validate_job_output(self.folder):
                        self.error_message = "Gagal validasi media (file rusak atau tidak lengkap)"
                        self.set_stage("failed", "Unduhan selesai tetapi file rusak atau terpotong.")
                        self.log("Unduhan selesai tetapi file rusak atau terpotong (Gagal validasi).")
                        if self.target == 'library':
                            library_db.update_library_job(self.id, stage="failed", error_message="Gagal validasi media")
                        break

                    if self._stage == "cancelled":
                        break

                    if self.target == 'library':
                        self.set_stage("importing", "Memasukkan media ke perpustakaan Jellyfin permanen…")
                        self.percent = 95.0
                        self.log("Memasukkan media ke perpustakaan Jellyfin permanen…")
                        library_db.update_library_job(self.id, stage="importing")
                        job_manager.save_db()

                        if self._stage == "cancelled":
                            break

                        res = LibraryImporter.import_job_output(
                            job_id=self.id,
                            owner_session=self.owner_session,
                            job_folder=self.folder,
                            raw_title=self.data.get('title') or self.data.get('name') or 'Media',
                            explicit_year=self.data.get('year'),
                            source_url=self.url
                        )

                        if self._stage == "cancelled":
                            break

                        if res.get('success'):
                            final_stage = res.get('status', 'ready')
                            self.percent = 100.0
                            item = res.get('item') or {}
                            self.library_item_id = item.get('id')
                            self.jellyfin_item_id = item.get('jellyfin_item_id')
                            if self.jellyfin_item_id:
                                self.jellyfin_url = jellyfin_client.get_playback_web_url(self.jellyfin_item_id)
                            msg = res.get('message', 'Media berhasil diimpor ke perpustakaan.')
                            self.set_stage(final_stage, msg)
                            self.log(msg)
                            library_db.update_library_job(
                                self.id,
                                stage=final_stage,
                                item_id=self.library_item_id,
                                checkpoint_data={'library_item_id': self.library_item_id, 'jellyfin_item_id': self.jellyfin_item_id}
                            )
                        else:
                            err_msg = res.get('error', 'Gagal memproses impor ke perpustakaan.')
                            self.error_message = err_msg
                            self.set_stage("needs_attention", f"Impor perpustakaan tertunda: {err_msg}")
                            self.log(f"Impor perpustakaan tertunda: {err_msg}")
                            library_db.update_library_job(self.id, stage="needs_attention", error_message=err_msg)
                        break
                    else:
                        if self._stage == "cancelled":
                            break
                        self.percent = 100.0
                        self.set_stage("completed", "Unduhan selesai dan siap diambil.")
                        break # Success
                else:
                    if self._stage == "cancelled":
                        break
                    self.log(f"Unduhan gagal (Kode keluar: {self.process.returncode})")
                    log_text = "\n".join(self.logs)
                    
                    if "HTTP Error 401" in log_text or "HTTP Error 403" in log_text or "Sign in to confirm" in log_text:
                        self.error_message = "Akses ditolak oleh server sumber (Perlu autentikasi/cookie)."
                        self.set_stage("failed", self.error_message)
                        self.log(self.error_message)
                        break
                    elif "Video unavailable" in log_text or "Private video" in log_text or "has been removed" in log_text or "HTTP Error 404" in log_text:
                        self.error_message = "Video tidak tersedia atau URL sudah kedaluwarsa."
                        self.set_stage("failed", self.error_message)
                        self.log(self.error_message)
                        break
                        
                    if "HTTP Error 429" in log_text or "Rate limit" in log_text:
                        self.log("Server media membatasi kecepatan (Rate Limit).")
                    elif "timeout" in log_text.lower() or "502" in log_text or "503" in log_text or "504" in log_text:
                        self.log("Gangguan jaringan atau server media (Timeout/5xx).")
                    
                    self.error_message = f"Unduhan gagal (exit code {self.process.returncode})"
                    self.set_stage("failed", "Unduhan gagal. Silakan coba kembali.")
                        
            except Exception as e:
                self.error_message = str(e)
                self.set_stage("failed", f"Kesalahan sistem: {str(e)}")
                self.log(f"Kesalahan sistem: {str(e)}")
                break
                
            retry_count += 1
            if retry_count <= max_retries:
                time.sleep(3 * retry_count)
                
        # End of retry loop
        if self.folder.exists() and self._stage == "completed" and self.target != 'library':
            ensure_job_zip(self.folder, self.data.get('name'))
        job_manager.save_db()
        self.sync_persistent_db()


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.RLock()
        self.reconcile_and_load_db()
        threading.Thread(target=self._periodic_retention_worker, daemon=True).start()

    def reconcile_and_load_db(self):
        """Reconciles interrupted jobs from past server runs and loads persisted DB."""
        try:
            library_db.mark_interrupted_jobs_on_startup()
        except Exception:
            pass

        db_path = get_db_path()
        if not db_path.exists():
            return
        try:
            with open(db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for j_id, j_data in data.items():
                stage = j_data.get("stage") or j_data.get("status", "failed")
                status_msg = j_data.get("status_message") or j_data.get("status", "")
                
                # Reconcile any in-flight active stage as 'interrupted' on server startup
                if stage in ACTIVE_STAGES:
                    stage = "interrupted"
                    status_msg = "Pekerjaan terputus akibat server direstart."
                    logs = j_data.get("logs", [])
                    logs.append("Pekerjaan terputus akibat server direstart.")
                else:
                    logs = j_data.get("logs", [])

                job = Job(
                    j_data["owner_session"],
                    j_data.get("data", {}),
                    existing_id=j_id,
                    existing_stage=stage,
                    existing_status_message=status_msg,
                    existing_logs=logs,
                    created_at=j_data.get("created_at"),
                    parent_job_id=j_data.get("parent_job_id"),
                    retry_count=j_data.get("retry_count", 0),
                    started_at=j_data.get("started_at"),
                    completed_at=j_data.get("completed_at"),
                    percent=j_data.get("percent"),
                    bytes_downloaded=j_data.get("bytes_downloaded", 0),
                    total_bytes=j_data.get("total_bytes", 0),
                    existing_timeline=j_data.get("timeline"),
                    file_exists=j_data.get("file_exists"),
                    existing_folder=j_data.get("folder") or j_data.get("result_path")
                )
                self.jobs[j_id] = job
        except Exception as e:
            print("Error loading jobs DB:", e)

        # Also load from SQLite persistent download_jobs table
        try:
            persisted = library_db.list_persisted_jobs(limit=1000)
            for j_data in (persisted.get('items') or persisted.get('jobs') or []):
                j_id = j_data['job_id']
                if j_id not in self.jobs:
                    stage = j_data.get("stage") or "completed"
                    status_msg = j_data.get("status_message") or ""
                    logs = j_data.get("logs") or []
                    timeline = j_data.get("timeline") or []
                    job = Job(
                        j_data.get("owner_session") or "admin",
                        {"title": j_data.get("title"), "url": j_data.get("url")},
                        existing_id=j_id,
                        existing_stage=stage,
                        existing_status_message=status_msg,
                        existing_logs=logs,
                        created_at=j_data.get("created_at"),
                        parent_job_id=j_data.get("parent_job_id"),
                        retry_count=j_data.get("retry_count", 0),
                        started_at=j_data.get("started_at"),
                        completed_at=j_data.get("completed_at"),
                        percent=j_data.get("percent"),
                        bytes_downloaded=j_data.get("bytes_downloaded", 0),
                        total_bytes=j_data.get("total_bytes", 0),
                        existing_timeline=timeline,
                        file_exists=bool(j_data.get("file_exists")),
                        existing_folder=j_data.get("result_path")
                    )
                    self.jobs[j_id] = job
        except Exception:
            pass

    def save_db(self):
        with self.lock:
            try:
                data = {j_id: j.to_dict() for j_id, j in self.jobs.items()}
                with open(get_db_path(), 'w', encoding='utf-8') as f:
                    json.dump(data, f)
            except Exception as e:
                print("Error saving DB:", e)

    def is_intake_allowed(self) -> bool:
        """Checks if server maintenance mode is active."""
        try:
            is_maint = library_db.get_setting("maintenance_mode", False)
            return not bool(is_maint)
        except Exception:
            return True

    def add_job(self, owner_session: str, data: dict) -> Job:
        with self.lock:
            if not self.is_intake_allowed():
                raise ValueError("Server sedang dalam mode pemeliharaan (tidak menerima antrean unduhan baru).")

            target = data.get('target', 'download')

            # Idempotency and Deduplication for Library Mode
            if target == 'library':
                idempotency_key = data.get('idempotency_key')
                if idempotency_key:
                    existing_lib_job = library_db.get_job_by_idempotency_key(idempotency_key, owner_session)
                    if existing_lib_job:
                        j_id = existing_lib_job['job_id']
                        if j_id in self.jobs:
                            return self.jobs[j_id]

                # Deduplication by source URL for active library jobs
                target_url = data.get('urls') or data.get('url')
                source_key = library_db.normalize_source_key(target_url)
                if source_key:
                    for j in self.jobs.values():
                        if j.owner_session == owner_session and getattr(j, 'target', 'download') == 'library':
                            if j.stage in ACTIVE_STAGES:
                                j_url = j.data.get('urls') or j.data.get('url')
                                if library_db.normalize_source_key(j_url) == source_key:
                                    return j

            # Per-user active concurrency check (Strictly evaluated using stage enum)
            user_jobs = sum(1 for j in self.jobs.values() if j.owner_session == owner_session and j.stage in ACTIVE_STAGES)
            if user_jobs >= 3:
                raise ValueError("Batas antrean per pengguna tercapai (Maksimal 3 tugas bersamaan).")

            # Global server concurrency check
            max_limit = library_db.get_setting("max_concurrent_jobs", config.MAX_CONCURRENT_JOBS)
            active_count = sum(1 for j in self.jobs.values() if j.stage in ACTIVE_STAGES)
            if active_count >= max_limit:
                raise ValueError("Server sedang sibuk mencapai batas kapasitas antrean. Harap tunggu beberapa saat.")

            job = Job(owner_session, data)
            self.jobs[job.id] = job
            self.save_db()

            threading.Thread(target=job.run, daemon=True).start()
            return job

    def retry_job(self, old_job_id: str, owner_session: str, new_url: Optional[str] = None) -> Job:
        """Creates a new linked attempt without destroying the evidence of the previous failure."""
        with self.lock:
            old_job = self.jobs.get(old_job_id)
            if not old_job:
                # Try loading from persistent DB
                old_db_job = library_db.get_persisted_job(old_job_id)
                if not old_db_job:
                    raise ValueError("Pekerjaan tidak ditemukan.")
                data_copy = {
                    'title': old_db_job.get('title'),
                    'target': old_db_job.get('target', 'download'),
                    'url': new_url or old_db_job.get('url'),
                    'quality': old_db_job.get('quality')
                }
                parent_id = old_job_id
                r_count = old_db_job.get('retry_count', 0) + 1
            else:
                data_copy = dict(old_job.data)
                if new_url:
                    data_copy['url'] = new_url
                    data_copy['urls'] = [new_url]
                parent_id = old_job.id
                r_count = old_job.retry_count + 1

            if not self.is_intake_allowed():
                raise ValueError("Server sedang dalam mode pemeliharaan.")

            new_job = Job(
                owner_session,
                data_copy,
                parent_job_id=parent_id,
                retry_count=r_count
            )
            self.jobs[new_job.id] = new_job
            self.save_db()
            threading.Thread(target=new_job.run, daemon=True).start()
            return new_job

    def get_job(self, job_id: str, owner_session: str) -> Optional[Job]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.owner_session == owner_session:
                return job
            return None

    def get_job_admin(self, job_id: str) -> Optional[Job]:
        """Admin access without session check."""
        with self.lock:
            return self.jobs.get(job_id)

    def get_user_jobs(self, owner_session: str) -> List[Job]:
        with self.lock:
            return [j for j in self.jobs.values() if j.owner_session == owner_session]

    def remove_job(self, job_id: str):
        with self.lock:
            if job_id in self.jobs:
                del self.jobs[job_id]
                self.save_db()

    def cleanup_expired_jobs(self) -> List[str]:
        """Cleans up temporary physical files that have passed the retention period.
        STRICT PERSISTENCE GUARANTEE:
        - NEVER removes the job history/diagnostics from SQLite `download_jobs`.
        - ONLY cleans temporary physical files on disk and marks `file_exists = 0`.
        - NEVER touches jobs with target == 'library' or any folder in LIBRARY_ROOT.
        - NEVER touches active jobs in ACTIVE_STAGES.
        """
        now = time.time()
        cleaned_ids = []
        retention = library_db.get_setting("retention_seconds", config.RETENTION_SECONDS)

        with self.lock:
            for j_id, job in list(self.jobs.items()):
                # Exclude library jobs from automatic expiration cleanup
                if getattr(job, 'target', 'download') == 'library':
                    continue

                # Exclude active jobs
                if job.stage in ACTIVE_STAGES:
                    continue

                # Check if expired
                is_expired = (now - job.created_at) >= retention
                if is_expired:
                    resolved_folder = job.folder.resolve()
                    if resolved_folder.exists():
                        # Strict safety guard: target folder must be strictly inside managed temporary directories
                        is_in_managed_temp = False
                        for root in (config.TEMPORARY_DIR, config.VPS_ROOT):
                            if root:
                                try:
                                    resolved_folder.relative_to(Path(root).resolve())
                                    is_in_managed_temp = True
                                    break
                                except ValueError:
                                    pass

                        if not is_in_managed_temp:
                            # Not in managed temp directory, skip physical removal to protect user files
                            continue

                        # Never delete the root directory itself!
                        if resolved_folder in (config.TEMPORARY_DIR.resolve(), Path(config.VPS_ROOT).resolve() if config.VPS_ROOT else None):
                            continue

                        # Never delete if active or in use
                        from ..admin.storage_manager import is_path_in_use
                        if is_path_in_use(resolved_folder):
                            continue

                        try:
                            shutil.rmtree(resolved_folder)
                            job.file_exists = False
                            job.log("File fisik sementara telah kedaluwarsa dan dibersihkan dari server.")
                        except Exception as rm_err:
                            job.log(f"Gagal membersihkan berkas fisik: {rm_err}")
                            job.file_exists = resolved_folder.exists()
                    else:
                        job.file_exists = False

                    if not job.file_exists:
                        library_db.mark_job_file_cleaned(j_id)
                        cleaned_ids.append(j_id)
                    job.sync_persistent_db()

            if cleaned_ids:
                self.save_db()

        # Also clean expired quarantine trash
        try:
            from ..admin.storage_manager import clean_expired_trash
            clean_expired_trash("system_retention")
        except Exception:
            pass
        return cleaned_ids

    def _periodic_retention_worker(self):
        while True:
            try:
                time.sleep(600)  # Check every 10 minutes
                self.cleanup_expired_jobs()
            except Exception:
                pass

    def retry_library_import(self, job_id: str, owner_session: str) -> Dict[str, Any]:
        """Retries import stage for an existing downloaded job without re-downloading video."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.owner_session != owner_session:
                raise ValueError("Pekerjaan tidak ditemukan atau tidak diizinkan.")
            if job.target != 'library':
                raise ValueError("Bukan pekerjaan mode perpustakaan.")
            if not job.folder.exists():
                raise ValueError("Folder sementara unduhan sudah tidak tersedia. Silakan unduh ulang.")

        job.set_stage("importing", "Mengulang proses impor ke perpustakaan Jellyfin…")
        job.percent = 95.0
        job.log("Mengulang proses impor ke perpustakaan Jellyfin…")
        library_db.update_library_job(job.id, stage="importing")
        self.save_db()

        res = LibraryImporter.import_job_output(
            job_id=job.id,
            owner_session=job.owner_session,
            job_folder=job.folder,
            raw_title=job.data.get('title') or job.data.get('name') or 'Media',
            explicit_year=job.data.get('year'),
            source_url=job.url
        )

        with self.lock:
            if res.get('success'):
                final_stage = res.get('status', 'ready')
                job.percent = 100.0
                item = res.get('item') or {}
                job.library_item_id = item.get('id')
                job.jellyfin_item_id = item.get('jellyfin_item_id')
                if job.jellyfin_item_id:
                    job.jellyfin_url = jellyfin_client.get_playback_web_url(job.jellyfin_item_id)
                msg = res.get('message', 'Media berhasil diimpor.')
                job.set_stage(final_stage, msg)
                job.log(msg)
                library_db.update_library_job(
                    job.id,
                    stage=final_stage,
                    item_id=job.library_item_id,
                    checkpoint_data={'library_item_id': job.library_item_id, 'jellyfin_item_id': job.jellyfin_item_id}
                )
            else:
                err_msg = res.get('error', 'Gagal memproses ulang impor.')
                job.error_message = err_msg
                job.set_stage("needs_attention", f"Impor gagal: {err_msg}")
                job.log(f"Impor gagal: {err_msg}")
                library_db.update_library_job(job.id, stage="needs_attention", error_message=err_msg)
            self.save_db()
        return res

    def retry_library_indexing(self, item_id: int, owner_session: str) -> Dict[str, Any]:
        """Retries Jellyfin catalog scanning and indexing for an item already safely imported."""
        item = library_db.get_library_item(item_id)
        if not item:
            raise ValueError("Item perpustakaan tidak ditemukan.")

        scan_triggered = jellyfin_client.trigger_scan()
        if not scan_triggered:
            health = jellyfin_client.check_health()
            return {'success': False, 'message': f"Jellyfin tidak dapat memindai: {health.get('message', 'Server offline')}"}

        folder_full = str(config.LIBRARY_ROOT / item['folder_path'])
        jelly_item = jellyfin_client.poll_item_indexing(
            item['title'],
            year=item.get('year'),
            expected_folder=folder_full,
            max_retries=5,
            initial_delay=1.5
        )

        if jelly_item:
            j_id = jelly_item.get('Id')
            library_db.update_item_status(item_id, 'ready', jellyfin_item_id=j_id)
            web_url = jellyfin_client.get_playback_web_url(j_id)
            return {'success': True, 'status': 'ready', 'jellyfin_item_id': j_id, 'jellyfin_url': web_url, 'message': 'Item berhasil terindeks di Jellyfin!'}
        else:
            return {'success': False, 'status': 'needs_attention', 'message': 'Pemindaian terpicu, namun item belum muncul dalam polling Jellyfin. Silakan periksa dashboard Jellyfin.'}


job_manager = JobManager()
