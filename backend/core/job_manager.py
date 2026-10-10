import threading
import uuid
import time
import subprocess
import re
import json
import shutil
from pathlib import Path
from typing import Dict, Optional, List

from . import config
from .storage import save_metadata, read_metadata, ensure_job_zip, get_job_files
from .downloader import build_ytdlp_command

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
        self.status = existing_status
        self.percent = 0.0 if existing_status != "completed" else 100.0
        self.logs: List[str] = existing_logs or []
        self.created_at = created_at or time.time()
        self.process: Optional[subprocess.Popen] = None
        
        base_name = data.get('name') or "Download"
        if config.VPS_ROOT:
            self.folder = config.VPS_ROOT / self.id
        else:
            self.folder = Path.home() / 'Downloads' / f"{base_name}_{self.id[:8]}"
            
        if not existing_id:
            self.folder.mkdir(parents=True, exist_ok=True)
            save_metadata(self.folder, {
                'job_id': self.id,
                'owner_session': self.owner_session,
                'created_at': self.created_at,
                'expires_at': self.created_at + config.RETENTION_SECONDS if config.VPS_ROOT else None,
                'input_url': data.get('urls') or data.get('url')
            })

    def to_dict(self):
        return {
            "id": self.id,
            "owner_session": self.owner_session,
            "data": self.data,
            "status": self.status,
            "logs": self.logs,
            "created_at": self.created_at
        }

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
        max_retries = 2
        retry_count = 0
        
        while retry_count <= max_retries:
            self.status = "downloading"
            if retry_count > 0:
                self.log(f"🔄 Memulai ulang percobaan ke-{retry_count} / {max_retries}...")
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
                
                current_stage = '⬇️ Mengunduh media...'
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
                        elif '[download] Downloading item' in line: stage = line.replace('[download]', '🎬').strip()
                        elif 'Downloading m3u8' in line: stage = 'Menyiapkan daftar potongan video…'
                        elif 'Retrying' in line: stage = '⚠️ Koneksi tersendat. Mencoba kembali…'
                        elif '[Merger]' in line: stage = '🔄 Menggabungkan video dan audio…'
                        elif '[ExtractAudio]' in line: stage = '🎵 Mengekstrak audio…'
                        
                        if stage:
                            current_stage = stage
                            self.status = stage
                            
                self.process.wait()
                
                if self.status == "cancelled":
                    break
                    
                if self.process.returncode == 0:
                    self.status = "validating"
                    if validate_job_output(self.folder):
                        self.status = "completed"
                        self.percent = 100.0
                        break # Success
                    else:
                        self.status = "failed"
                        self.log("Unduhan selesai tetapi file rusak atau terpotong (Gagal validasi).")
                else:
                    self.status = "failed"
                    self.log(f"Unduhan gagal (Kode keluar: {self.process.returncode})")
                    
                    log_text = "\n".join(self.logs)
                    
                    # Do not retry on explicit Auth or Not Found / URL expiration errors
                    if "HTTP Error 401" in log_text or "HTTP Error 403" in log_text or "Sign in to confirm" in log_text:
                        self.log("🔒 Otentikasi diperlukan. Akses ditolak oleh server sumber.")
                        break
                    elif "Video unavailable" in log_text or "Private video" in log_text or "has been removed" in log_text or "HTTP Error 404" in log_text:
                        self.log("🚫 Video tidak tersedia, pribadi, telah dihapus, atau URL sudah kedaluwarsa.")
                        break
                        
                    if "HTTP Error 429" in log_text or "Rate limit" in log_text:
                        self.log("⚠️ Server media membatasi kecepatan (Rate Limit).")
                    elif "timeout" in log_text.lower() or "502" in log_text or "503" in log_text or "504" in log_text:
                        self.log("⚠️ Gangguan jaringan atau server media (Timeout/5xx).")
                        
            except Exception as e:
                self.status = "failed"
                self.log(f"Kesalahan sistem: {str(e)}")
                break # Don't retry unknown system errors (could be configuration issue)
                
            retry_count += 1
            if retry_count <= max_retries:
                time.sleep(3 * retry_count) # Backoff before retry
                
        # End of retry loop
        if self.folder.exists() and self.status == "completed":
            ensure_job_zip(self.folder, self.data.get('name'))
        job_manager.save_db()

class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.RLock()
        self.load_db()
        
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
            user_jobs = sum(1 for j in self.jobs.values() if j.owner_session == owner_session and j.status in ("queued", "downloading"))
            if user_jobs >= 3:
                raise ValueError("Batas antrean per pengguna tercapai (Maksimal 3 tugas bersamaan).")
                
            active_count = sum(1 for j in self.jobs.values() if j.status in ("queued", "downloading"))
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

job_manager = JobManager()
