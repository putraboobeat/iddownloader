import json
import mimetypes
import shutil
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from ..core.security import generate_session_id, clean_url
from ..core.job_manager import job_manager
from ..core.storage import get_job_files, read_metadata
from ..core import config

class APIHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-App-Session')
        # Security headers
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline' https:; frame-ancestors 'none'")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def get_session(self):
        session = self.headers.get('X-App-Session')
        if not session or not session.isalnum():
            return None
        return session

    def send_json(self, code: int, data: dict):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parts = urlparse(self.path)
        path = parts.path
        
        session = self.get_session()
        if not session:
            return self.send_json(401, {'error': 'Unauthorized. Missing valid X-App-Session.'})

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 1024 * 1024:
            return self.send_json(413, {'error': 'Payload too large'})

        try:
            body = self.rfile.read(content_length)
            if path == '/api/upload-cookies':
                data = {}
            else:
                data = json.loads(body) if body else {}
        except Exception:
            return self.send_json(400, {'error': 'Invalid JSON data'})

        if path == '/api/start':
            try:
                # Basic validation
                url = data.get('url') or data.get('urls')
                if not url:
                    return self.send_json(400, {'error': 'URL wajib diisi'})
                    
                # Clean URL validates against SSRF
                clean_url(url.splitlines()[0])
                
                job = job_manager.add_job(session, data)
                self.send_json(200, {'job_id': job.id, 'status': 'queued'})
            except ValueError as e:
                self.send_json(400, {'error': str(e)})
            except Exception as e:
                self.send_json(500, {'error': 'Server error: ' + str(e)})
                
        elif path == '/api/stop':
            job_id = data.get('job_id')
            job = job_manager.get_job(job_id, session)
            if not job:
                return self.send_json(404, {'error': 'Job not found or unauthorized'})
            job.terminate()
            self.send_json(200, {'status': 'cancelled'})
            
        elif path == '/api/clear':
            # Only clear jobs owned by this session
            jobs = job_manager.get_user_jobs(session)
            deleted = 0
            for job in jobs:
                if job.folder.exists():
                    shutil.rmtree(job.folder, ignore_errors=True)
                job_manager.remove_job(job.id)
                deleted += 1
            self.send_json(200, {'status': f'{deleted} jobs cleared'})
            
        elif path == '/api/upload-cookies':
            try:
                if config.COOKIES_PATH:
                    cookie_file = Path(config.COOKIES_PATH)
                    cookie_file.parent.mkdir(parents=True, exist_ok=True)
                    text_content = body.decode('utf-8')
                    cookie_file.write_text(text_content, encoding='utf-8')
                    self.send_json(200, {'ok': True, 'status': 'Cookies tersimpan'})
                else:
                    self.send_json(400, {'error': 'Cookie path tidak dikonfigurasi'})
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            
        else:
            self.send_json(404, {'error': 'API endpoint not found'})

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        
        # API Routes
        if path.startswith('/api/'):
            session = self.get_session()
            if not session:
                return self.send_json(401, {'error': 'Unauthorized'})

            if path == '/api/status':
                jobs = job_manager.get_user_jobs(session)
                if not jobs:
                    return self.send_json(200, {'jobs': []})
                    
                # For compatibility with legacy UI, return the latest job's status if no ID provided
                # In the new UI, we should send job_id
                qs = parse_qs(parts.query)
                job_id = qs.get('job_id', [None])[0]
                
                if job_id:
                    job = job_manager.get_job(job_id, session)
                    target_jobs = [job] if job else []
                else:
                    target_jobs = [sorted(jobs, key=lambda j: j.created_at, reverse=True)[0]]
                    
                if not target_jobs:
                    return self.send_json(404, {'error': 'Job not found'})
                    
                job = target_jobs[0]
                files = get_job_files(job.folder, job.id, session)
                
                self.send_json(200, {
                    'job_id': job.id,
                    'status': job.status,
                    'percent': job.percent,
                    'running': job.status in ('queued', 'downloading'),
                    'logs': job.logs,
                    'files': files
                })
                return
                
            if path == '/api/inspect':
                qs = parse_qs(parts.query)
                target_url = (qs.get('url') or [''])[0]
                try:
                    target_url = clean_url(target_url)
                except ValueError as e:
                    return self.send_json(400, {'error': str(e)})
                    
                import subprocess, sys
                from ..core.downloader import get_downloader_cmd
                from ..core.config import COOKIES_PATH
                
                cmd = get_downloader_cmd() + ['--no-cache-dir', '--no-update', '--dump-single-json', '--socket-timeout', '15']
                ua = self.headers.get('User-Agent') or 'Mozilla/5.0'
                cmd += ['--add-header', f'User-Agent:{ua}']
                if 'instagram.com' in target_url.lower():
                    cmd += ['--add-header', 'Referer:https://www.instagram.com/']
                if COOKIES_PATH and Path(COOKIES_PATH).is_file():
                    cmd += ['--cookies', str(COOKIES_PATH)]
                cmd.append(target_url)
                
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                    info = None
                    if proc.stdout:
                        try:
                            info = json.loads(proc.stdout)
                        except json.JSONDecodeError:
                            pass
                            
                    if (proc.returncode == 0 or info) and info:
                        entries = info.get('entries') or []
                        res_set = set()
                        has_audio = False
                        has_subs = bool(info.get('subtitles') or info.get('automatic_captions'))
                        filesize_approx = 0
                        
                        all_formats = []
                        if entries:
                            for entry in entries:
                                if not entry: continue
                                has_subs = has_subs or bool(entry.get('subtitles') or entry.get('automatic_captions'))
                                all_formats.extend(entry.get('formats') or [])
                        else:
                            all_formats = info.get('formats', [])
                            
                        for f in all_formats:
                            h = f.get('height')
                            if h: res_set.add(h)
                            if f.get('acodec') and f.get('acodec') != 'none':
                                has_audio = True
                            size = f.get('filesize_approx') or f.get('filesize') or 0
                            filesize_approx = max(filesize_approx, size)
                                
                        thumb = info.get('thumbnail')
                        if not thumb and entries:
                            thumb = next((e.get('thumbnail') for e in entries if e), None)
                            
                        detected_formats = sorted(list(res_set), reverse=True)
                        if not detected_formats:
                            detected_formats = [1080, 720, 480]
                            
                        self.send_json(200, {'ok': True, 'info': {
                            'title': info.get('title') or 'Media Video',
                            'duration': info.get('duration_string') or info.get('duration'),
                            'thumbnail': thumb,
                            'uploader': info.get('uploader') or info.get('channel'),
                            'formats': detected_formats,
                            'has_audio': has_audio,
                            'has_subs': has_subs,
                            'filesize_approx': filesize_approx,
                            'needs_merge': bool(not has_audio and info.get('ext') != 'mp3'),
                            'processing_location': 'VPS' if config.VPS_ROOT else 'Lokal',
                            'retention_seconds': config.RETENTION_SECONDS if config.VPS_ROOT else None,
                            'is_carousel': bool(entries and len(entries) > 1),
                            'carousel_count': len(entries) if entries else 0,
                            'is_instagram': 'instagram.com' in target_url.lower()
                        }})
                    else:
                        self.send_json(400, {'error': 'Gagal menganalisis metadata URL.'})
                except Exception as exc:
                    self.send_json(500, {'error': str(exc)})
                return
                
            if path == '/api/diagnostic':
                qs = parse_qs(parts.query)
                job_id = qs.get('job_id', [None])[0]
                if not job_id:
                    return self.send_json(400, {'error': 'job_id required'})
                    
                job = job_manager.get_job(job_id, session)
                if not job:
                    return self.send_json(404, {'error': 'Job not found'})
                    
                import platform, sys, subprocess
                from ..core.downloader import get_downloader_cmd
                try:
                    dl_v = subprocess.run(get_downloader_cmd() + ['--version'], capture_output=True, text=True, timeout=5).stdout.strip()
                except:
                    dl_v = "Unknown"
                    
                # Redact logs
                clean_logs = []
                for line in job.logs:
                    # Redact URLs and cookies
                    line = re.sub(r'https?://[^\s"\']+', '[REDACTED_URL]', line)
                    line = re.sub(r'(cookie|token|auth)=[^&;\s]+', r'\1=[REDACTED]', line, flags=re.I)
                    clean_logs.append(line)
                    
                report = f"""=== OmniFetch Diagnostic Report ===
Time: {__import__('datetime').datetime.now().isoformat()}
App Version: 2.0 (Universal Discovery Engine)
Environment: {platform.system()} {platform.release()} / Python {sys.version.split()[0]}
Downloader: {dl_v}
Processing Location: {'VPS' if config.VPS_ROOT else 'Lokal'}

-- Job Status --
Job ID: {job.id}
Status: {job.status}
Percent: {job.percent}%
Return Code: {job.process.returncode if job.process else 'N/A'}

-- Redacted Logs --
""" + "\n".join(clean_logs)
                
                return self.send_json(200, {'report': report})

        if path.startswith('/download/'):
            return self.serve_job_file(path, stream=False)
            
        if path.startswith('/stream/'):
            return self.serve_job_file(path, stream=True)

        # Serve static files and templates
        self.serve_static(path)

    def serve_job_file(self, path: str, stream: bool = False):
        parts = path.strip('/').split('/')
        if len(parts) < 3:
            return self.send_error(400, 'Invalid URL format')
            
        job_id = unquote(parts[1])
        filename = unquote(parts[2])
        
        session = self.get_session()
        # In a real app we might allow public streaming via a signed URL, 
        # but for now, we enforce session ownership for isolation.
        if not session:
            # We can also check query params for a signed token, but session is safest
            session = parse_qs(urlparse(self.path).query).get('session', [None])[0]
            if not session:
                return self.send_error(401, 'Unauthorized access to job files')

        job = job_manager.get_job(job_id, session)
        if not job:
            return self.send_error(404, 'Job not found or unauthorized')
            
        safe_path = (job.folder / filename).resolve()
        if not str(safe_path).startswith(str(job.folder)):
            return self.send_error(403, 'Path traversal detected')
            
        if not safe_path.exists() or not safe_path.is_file():
            return self.send_error(404, 'File not found')
            
        try:
            content = safe_path.read_bytes()
            mime = mimetypes.guess_type(str(safe_path))[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            
            from urllib.parse import quote
            disposition = 'inline' if stream else f"attachment; filename*=UTF-8''{quote(filename)}"
            self.send_header('Content-Disposition', disposition)
            
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, 'Error reading file')

    def serve_static(self, path: str):
        if path == '/':
            path = '/index.html'
        elif path == '/features':
            path = '/features.html'
            
        safe_path = Path(config.BASE_DIR / path.lstrip('/')).resolve()
        
        # Directory traversal protection
        if not str(safe_path).startswith(str(config.BASE_DIR)):
            self.send_error(403, 'Forbidden')
            return
            
        if not safe_path.exists() or not safe_path.is_file():
            self.send_error(404, 'File not found')
            return
            
        try:
            content = safe_path.read_bytes()
            mime = mimetypes.guess_type(str(safe_path))[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_error(500, 'Internal Server Error')
