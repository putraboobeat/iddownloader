from __future__ import annotations
import threading
import uuid
import time
import subprocess
import re
import json
import shutil
from pathlib import Path
from typing import Dict, Optional, List, Any

from . import config
from .storage import save_metadata, read_metadata, ensure_job_zip, get_job_files
from .downloader import build_ytdlp_command
from . import library_db
from .jellyfin import jellyfin_client
from .importer import LibraryImporter

def get_db_path():
    return Path("jobs_db.json") if not config.VPS_ROOT else config.VPS_ROOT / "jobs_db.json"

def validate_job_output(folder: Path) -> bool:
    """Validates the output files using ffprobe to ensure they are complete and valid."""
    media_files = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.wav', '.flac') and not p.name.startswith('.'):
            media_files.append(p)
    
    if not media_files:
        return True # Non-media outputs or zip file check
        
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return True # Can't validate
        
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
    def __init__(self, owner_session: str, data: dict, existing_id: str = None, existing_status: str = "queued", existing_logs: list = None, created_at: float = None):
        self.id = existing_id or str(uuid.uuid4())
        self.owner_session = owner_session
        self.data = data
        self.target = data.get('target', 'download')
        self.status = existing_status
        self.percent = 0.0 if existing_status != "completed" else 100.0
        self.logs: List[str] = existing_logs or []
        self.created_at = created_at or time.time()
        self.process: Optional[subprocess.Popen] = None
        self.library_item_id = data.get('library_item_id')
        self.jellyfin_item_id = data.get('jellyfin_item_id')
        self.jellyfin_url = data.get('jellyfin_url')
        
        base_name = data.get('name') or data.get('title') or "Download"
        if self.target == 'library':
            self.folder = config.TEMPORARY_DIR / f"staging_{self.id[:8]}"
        elif config.VPS_ROOT:
            self.folder = config.VPS_ROOT / self.id
        else:
            self.folder = Path.home() / 'Downloads' / f"{base_name}_{self.id[:8]}"
            
        if not existing_id:
            self.folder.mkdir(parents=True, exist_ok=True)
            save_metadata(self.folder, {
                'job_id': self.id,
                'owner_session': self.owner_session,
                'target': self.target,
                'created_at': self.created_at,
                'expires_at': (self.created_at + config.RETENTION_SECONDS) if (config.VPS_ROOT and self.target != 'library') else None,
                'input_url': data.get('urls') or data.get('url')
            })
            if self.target == 'library':
                try:
                    library_db.create_or_get_library_job(
                        job_id=self.id,
                        owner_session=self.owner_session,
                        stage=self.status,
                        target='library',
                        idempotency_key=data.get('idempotency_key')
                    )
                except Exception as db_err:
                    self.logs.append(f"DB Note: {db_err}")

    def to_dict(self):
        d = {
            "id": self.id,
            "owner_session": self.owner_session,
            "data": self.data,
            "status": self.status,
            "target": self.target,
            "percent": self.percent,
            "logs": self.logs,
            "created_at": self.created_at
        }
        if self.target == 'library':
            d["library_item_id"] = self.library_item_id
            d["jellyfin_item_id"] = self.jellyfin_item_id
            d["jellyfin_url"] = self.jellyfin_url
        return d


    def log(self, message: str):
        self.logs.append(message)
        if len(self.logs) > 150:
            self.logs = self.logs[-150:]
        job_manager.save_db()

    def terminate(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status = "cancelled"
            self.log("Proses dihentikan oleh pengguna.")
            job_manager.save_db()

    def run(self):
        # 1. Penanganan Khusus Pekerjaan Subtitle Saja
        target_url = self.data.get('urls') or self.data.get('url') or ''
        if isinstance(target_url, list):
            target_url = target_url[0] if target_url else ''
        target_url = str(target_url).strip()

        is_subtitle_mode = self.data.get('mode') == 'subtitle' or any(target_url.lower().split('?')[0].endswith(ext) for ext in ('.vtt', '.srt'))

        if is_subtitle_mode:
            self.status = "downloading"
            self.log("Memulai pengunduhan subtitle...")
            job_manager.save_db()
            try:
                from subtitles import save_subtitle
                sub_name = self.data.get('name') or "Subtitle"
                cookie_arg = config.COOKIES_PATH if Path(config.COOKIES_PATH).is_file() else None
                saved_path = save_subtitle(
                    track={'url': target_url, 'referer': self.data.get('referer', '')},
                    folder=self.folder,
                    name=sub_name,
                    cancelled=lambda: self.status == 'cancelled',
                    cookie_file=cookie_arg
                )
                self.status = "completed"
                self.percent = 100.0
                self.log(f"Subtitle berhasil disimpan: {saved_path.name}")
                ensure_job_zip(self.folder, sub_name)
            except Exception as e:
                self.status = "failed"
                self.log(f"Gagal mengunduh subtitle: {str(e)}")
            job_manager.save_db()
            return

        # 2. Pengunduhan Subtitle Tambahan jika disertakan bersama video
        if self.data.get('subtitle_url'):
            try:
                from subtitles import save_subtitle
                sub_url = str(self.data['subtitle_url']).strip()
                self.log(f"Mengunduh subtitle eksternal pendamping...")
                cookie_arg = config.COOKIES_PATH if Path(config.COOKIES_PATH).is_file() else None
                saved_sub = save_subtitle(
                    track={'url': sub_url, 'referer': self.data.get('referer', '')},
                    folder=self.folder,
                    name=self.data.get('name') or "Subtitle",
                    cancelled=lambda: self.status == 'cancelled',
                    cookie_file=cookie_arg
                )
                self.log(f"Subtitle eksternal tersimpan: {saved_sub.name}")
            except Exception as sub_err:
                self.log(f"Peringatan subtitle eksternal: {str(sub_err)}")

        max_retries = 2
        retry_count = 0
        
        while retry_count <= max_retries:
            self.status = "downloading"
            if retry_count > 0:
                self.log(f"Memulai ulang percobaan ke-{retry_count} / {max_retries}...")
            job_manager.save_db()
            
            try:
                # Check disk space (Need at least 1GB free)
                free_bytes = shutil.disk_usage(self.folder.parent).free
                if free_bytes < 1024 * 1024 * 1024:
                    self.log("Ruang penyimpanan VPS tidak cukup (< 1GB).")
                    self.status = "failed"
                    break # Fatal error, no retry

                from .config import COOKIES_PATH
                cmd = build_ytdlp_command(self.data, self.folder, cookies_path=config.COOKIES_PATH)
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace'
                )
                
                current_stage = 'Mengunduh media...'
                for raw in self.process.stdout:
                    line = raw.strip()
                    if line.startswith('APP_PROGRESS:'):
                        text = line.split(':', 1)[1].strip()
                        match = re.search(r'(\d+(?:\.\d+)?)%', text)
                        if match:
                            self.percent = float(match.group(1))
                        speed_eta = text.split('|', 1)[1] if '|' in text else text
                        self.status = f"{current_stage} — {self.percent}% | {speed_eta}"
                    elif line.startswith('APP_FILE:'):
                        self.log('File: ' + line.split(':', 1)[1])
                    elif line:
                        self.log(line)
                        stage = None
                        if 'Downloading webpage' in line: stage = 'Menghubungi server media…'
                        elif '[download] Downloading item' in line: stage = line.replace('[download]', '').strip()
                        elif 'Downloading m3u8' in line: stage = 'Menyiapkan daftar potongan video…'
                        elif 'Retrying' in line: stage = 'Koneksi tersendat. Mencoba kembali…'
                        elif '[Merger]' in line: stage = 'Menggabungkan video dan audio…'
                        elif '[ExtractAudio]' in line: stage = 'Mengekstrak audio…'
                        
                        if stage:
                            current_stage = stage
                            self.status = stage
                            
                self.process.wait()
                
                if self.status == "cancelled":
                    break
                    
                if self.process.returncode == 0:
                    self.status = "validating"
                    if not validate_job_output(self.folder):
                        self.status = "failed"
                        self.log("Unduhan selesai tetapi file rusak atau terpotong (Gagal validasi).")
                        if self.target == 'library':
                            library_db.update_library_job(self.id, stage="failed", error_message="Gagal validasi media")
                        break

                    if self.target == 'library':
                        self.status = "importing"
                        self.percent = 95.0
                        self.log("Memasukkan media ke perpustakaan Jellyfin permanen…")
                        library_db.update_library_job(self.id, stage="importing")
                        job_manager.save_db()

                        res = LibraryImporter.import_job_output(
                            job_id=self.id,
                            owner_session=self.owner_session,
                            job_folder=self.folder,
                            raw_title=self.data.get('title') or self.data.get('name') or 'Media',
                            explicit_year=self.data.get('year'),
                            source_url=self.data.get('urls') or self.data.get('url')
                        )

                        if res.get('success'):
                            self.status = res.get('status', 'ready')
                            self.percent = 100.0
                            item = res.get('item') or {}
                            self.library_item_id = item.get('id')
                            self.jellyfin_item_id = item.get('jellyfin_item_id')
                            if self.jellyfin_item_id:
                                self.jellyfin_url = jellyfin_client.get_playback_web_url(self.jellyfin_item_id)
                            self.log(res.get('message', 'Media berhasil diimpor ke perpustakaan.'))
                            library_db.update_library_job(
                                self.id,
                                stage=self.status,
                                item_id=self.library_item_id,
                                checkpoint_data={'library_item_id': self.library_item_id, 'jellyfin_item_id': self.jellyfin_item_id}
                            )
                        else:
                            self.status = "needs_attention"
                            err_msg = res.get('error', 'Gagal memproses impor ke perpustakaan.')
                            self.log(f"Impor perpustakaan tertunda: {err_msg}")
                            library_db.update_library_job(self.id, stage="needs_attention", error_message=err_msg)
                        break
                    else:
                        self.status = "completed"
                        self.percent = 100.0
                        break # Success
                else:
                    self.log(f"Unduhan gagal (Kode keluar: {self.process.returncode})")
                    
                    log_text = "\n".join(self.logs)
                    
                    # Do not retry on explicit Auth or Not Found / URL expiration errors
                    if "HTTP Error 401" in log_text or "HTTP Error 403" in log_text or "Sign in to confirm" in log_text:
                        self.log("Otentikasi diperlukan. Akses ditolak oleh server sumber.")
                        self.status = "failed"
                        break
                    elif "Video unavailable" in log_text or "Private video" in log_text or "has been removed" in log_text or "HTTP Error 404" in log_text:
                        self.log("Video tidak tersedia, pribadi, telah dihapus, atau URL sudah kedaluwarsa.")
                        self.status = "failed"
                        break
                        
                    if "HTTP Error 429" in log_text or "Rate limit" in log_text:
                        self.log("Server media membatasi kecepatan (Rate Limit).")
                    elif "timeout" in log_text.lower() or "502" in log_text or "503" in log_text or "504" in log_text:
                        self.log("Gangguan jaringan atau server media (Timeout/5xx).")
                    
                    self.status = "failed"
                        
            except Exception as e:
                self.status = "failed"
                self.log(f"Kesalahan sistem: {str(e)}")
                break # Don't retry unknown system errors (could be configuration issue)
                
            retry_count += 1
            if retry_count <= max_retries:
                time.sleep(3 * retry_count) # Backoff before retry
                
        # End of retry loop
        if self.folder.exists() and self.status == "completed" and self.target != 'library':
            ensure_job_zip(self.folder, self.data.get('name'))
        job_manager.save_db()


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.RLock()
        self.load_db()
        threading.Thread(target=self._periodic_retention_worker, daemon=True).start()
        
    def load_db(self):
        db_path = get_db_path()
        if not db_path.exists():
            return
        try:
            with open(db_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for j_id, j_data in data.items():
                status = j_data.get("status", "failed")
                if status in ("queued", "downloading", "validating"):
                    status = "failed" # Mark interrupted jobs as failed on restart
                    j_data["logs"].append("Pekerjaan terputus akibat server direstart.")
                
                self.jobs[j_id] = Job(
                    j_data["owner_session"],
                    j_data["data"],
                    existing_id=j_id,
                    existing_status=status,
                    existing_logs=j_data.get("logs", []),
                    created_at=j_data.get("created_at")
                )
        except Exception as e:
            print("Error loading DB:", e)
            
    def save_db(self):
        with self.lock:
            try:
                data = {j_id: j.to_dict() for j_id, j in self.jobs.items()}
                with open(get_db_path(), 'w', encoding='utf-8') as f:
                    json.dump(data, f)
            except Exception as e:
                print("Error saving DB:", e)
        
    def add_job(self, owner_session: str, data: dict) -> Job:
        with self.lock:
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
                            if j.status in ("queued", "downloading", "validating", "importing", "indexing"):
                                j_url = j.data.get('urls') or j.data.get('url')
                                if library_db.normalize_source_key(j_url) == source_key:
                                    return j

            user_jobs = sum(1 for j in self.jobs.values() if j.owner_session == owner_session and j.status in ("queued", "downloading", "importing", "indexing"))
            if user_jobs >= 3:
                raise ValueError("Batas antrean per pengguna tercapai (Maksimal 3 tugas bersamaan).")
                
            active_count = sum(1 for j in self.jobs.values() if j.status in ("queued", "downloading", "importing", "indexing"))
            if active_count >= config.MAX_CONCURRENT_JOBS:
                raise ValueError("Server sedang sibuk. Harap tunggu beberapa saat.")
                
            job = Job(owner_session, data)
            self.jobs[job.id] = job
            self.save_db()
            
            threading.Thread(target=job.run, daemon=True).start()
            return job

    def get_job(self, job_id: str, owner_session: str) -> Optional[Job]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job.owner_session == owner_session:
                return job
            return None
            
    def get_user_jobs(self, owner_session: str) -> List[Job]:
        with self.lock:
            return [j for j in self.jobs.values() if j.owner_session == owner_session]
            
    def remove_job(self, job_id: str):
        with self.lock:
            if job_id in self.jobs:
                del self.jobs[job_id]
                self.save_db()

    def cleanup_expired_jobs(self) -> List[str]:
        """Cleans up temporary jobs that have passed the retention period.
        STRICT SAFETY:
        - NEVER touches jobs with target == 'library'
        - NEVER touches any folder located within config.LIBRARY_ROOT
        - NEVER touches active jobs in queued/downloading/validating/importing/indexing stages.
        """
        now = time.time()
        cleaned_ids = []
        with self.lock:
            for j_id, job in list(self.jobs.items()):
                # Exclude library jobs from automatic expiration cleanup
                if getattr(job, 'target', 'download') == 'library':
                    continue

                # Exclude any active job
                if job.status in ("queued", "downloading", "validating", "importing", "indexing"):
                    continue

                # Check if expired (or force if Retention seconds elapsed)
                is_expired = (now - job.created_at) >= config.RETENTION_SECONDS
                if is_expired:
                    if job.folder.exists():
                        # Strict safety guard: folder must not be inside LIBRARY_ROOT
                        try:
                            job.folder.resolve().relative_to(config.LIBRARY_ROOT.resolve())
                            # If inside LIBRARY_ROOT, abort deletion of this folder!
                            continue
                        except ValueError:
                            # Not inside LIBRARY_ROOT, safe to remove
                            shutil.rmtree(job.folder, ignore_errors=True)
                    del self.jobs[j_id]
                    cleaned_ids.append(j_id)

            if cleaned_ids:
                self.save_db()
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

        job.status = "importing"
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
            source_url=job.data.get('urls') or job.data.get('url')
        )

        with self.lock:
            if res.get('success'):
                job.status = res.get('status', 'ready')
                job.percent = 100.0
                item = res.get('item') or {}
                job.library_item_id = item.get('id')
                job.jellyfin_item_id = item.get('jellyfin_item_id')
                if job.jellyfin_item_id:
                    job.jellyfin_url = jellyfin_client.get_playback_web_url(job.jellyfin_item_id)
                job.log(res.get('message', 'Media berhasil diimpor.'))
                library_db.update_library_job(
                    job.id,
                    stage=job.status,
                    item_id=job.library_item_id,
                    checkpoint_data={'library_item_id': job.library_item_id, 'jellyfin_item_id': job.jellyfin_item_id}
                )
            else:
                job.status = "needs_attention"
                err_msg = res.get('error', 'Gagal memproses ulang impor.')
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
