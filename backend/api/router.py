import json
import time
import mimetypes
import shutil
import re
import secrets
import hmac
import html
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

from ..core.security import generate_session_id, clean_url
from ..core.job_manager import job_manager
from ..core.storage import get_job_files, read_metadata
from ..core import config
from ..core import library_db
from ..core.jellyfin import jellyfin_client
from ..core.importer import run_ffprobe

LIBRARY_AUTH_TOKENS = set()

class APIHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        if not self.path.startswith('/api/admin/'):
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-App-Session, X-Library-Auth, Range')
            self.send_header('Access-Control-Expose-Headers', 'Content-Range, Accept-Ranges, Content-Length')
        # Security headers with media & blob support for video streaming
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline' https: data: blob:; media-src 'self' blob: https: data:; img-src 'self' https: data: blob:; frame-ancestors 'none'")
        super().end_headers()

    def is_library_authorized(self) -> bool:
        # Check admin session (cookie or header) - valid admin session is always authorized for media
        try:
            from ..admin.auth import get_session_from_request
            admin_sess = get_session_from_request(self)
            if admin_sess:
                return True
        except Exception:
            pass

        token = self.headers.get('X-Library-Auth', '').strip()
        if not token:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            token = (qs.get('library_auth') or qs.get('auth') or [''])[0].strip()
        if not token:
            return False
        if token in LIBRARY_AUTH_TOKENS:
            return True
        return hmac.compare_digest(str(token), str(config.LIBRARY_PASSWORD))

    def do_OPTIONS(self):
        if self.path.startswith('/api/admin/'):
            from ..admin.router import AdminRouter
            if AdminRouter.handle_request(self, 'OPTIONS'):
                return
        self.send_response(200)
        self.end_headers()

    def get_session(self):
        session = self.headers.get('X-App-Session')
        if not session:
            qs = parse_qs(urlparse(self.path).query)
            session = (qs.get('session') or [''])[0].strip()
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
        
        if path.startswith('/api/admin/'):
            from ..admin.router import AdminRouter
            if AdminRouter.handle_request(self, 'POST'):
                return

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

        session = self.get_session()
        if not session:
            try:
                from ..admin.auth import get_session_from_request
                admin_sess = get_session_from_request(self)
                if admin_sess:
                    session = f"admin_{admin_sess.get('username')}"
            except Exception:
                pass

        # Public authentication for library mode (clean browser without prior session allowed)
        if path == '/api/library/auth':
            provided_pw = str(data.get('password') or '').strip()
            if hmac.compare_digest(provided_pw, str(config.LIBRARY_PASSWORD)):
                token = secrets.token_hex(16)
                LIBRARY_AUTH_TOKENS.add(token)
                effective_session = session or generate_session_id()
                library_db.record_user_activity(effective_session, 'library_auth')
                return self.send_json(200, {
                    'ok': True,
                    'token': token,
                    'session_id': effective_session,
                    'app_session_id': effective_session,
                    'message': 'Kata sandi benar'
                })
            else:
                return self.send_json(401, {'ok': False, 'error': 'Kata sandi salah. Silakan coba lagi.'})

        # All other POST endpoints require valid X-App-Session
        if not session:
            return self.send_json(401, {'error': 'Unauthorized. Missing valid X-App-Session.'})

        if path == '/api/start':
            try:
                # Basic validation
                url = data.get('url') or data.get('urls')
                if not url:
                    return self.send_json(400, {'error': 'URL wajib diisi'})
                    
                # Clean URL validates against SSRF
                clean_url(url.splitlines()[0])
                
                job = job_manager.add_job(session, data)
                self.send_json(200, {
                    'ok': True,
                    'job_id': job.id,
                    'status': job.status,
                    'target': getattr(job, 'target', 'download')
                })
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

        # Handled above
        elif path == '/api/library/auth':
            pass

        elif path == '/api/library/retry-import':
            if not self.is_library_authorized():
                return self.send_json(401, {'ok': False, 'auth_required': True, 'error': 'Kata sandi perpustakaan diperlukan'})
            job_id = data.get('job_id')
            item_id = data.get('item_id')
            if not job_id and not item_id:
                return self.send_json(400, {'error': 'item_id atau job_id wajib diisi'})
            try:
                if item_id:
                    item = library_db.get_library_item(int(item_id))
                    if not item:
                        return self.send_json(404, {'error': 'Item tidak ditemukan'})
                    folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
                    video_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')] if folder.exists() and folder.is_dir() else []
                    if video_files:
                        primary_vid = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]
                        probe_res = run_ffprobe(primary_vid)
                        if probe_res.get('valid'):
                            library_db.update_item_status(item['id'], 'ready')
                            return self.send_json(200, {'ok': True, 'message': f"Item '{item['title']}' berhasil diverifikasi dan siap ditonton!"})
                        else:
                            return self.send_json(400, {'error': f"Validasi media gagal: {probe_res.get('error', 'Format tidak terbaca')}"})
                    else:
                        return self.send_json(404, {'error': 'Berkas video tidak ditemukan di folder perpustakaan'})
                else:
                    res = job_manager.retry_library_import(job_id, session)
                    self.send_json(200, res)
            except ValueError as e:
                self.send_json(400, {'error': str(e)})
            except Exception as e:
                self.send_json(500, {'error': str(e)})

        elif path == '/api/library/retry-index':
            if not self.is_library_authorized():
                return self.send_json(401, {'ok': False, 'auth_required': True, 'error': 'Kata sandi perpustakaan diperlukan'})
            item_id = data.get('item_id')
            if not item_id:
                return self.send_json(400, {'error': 'item_id wajib diisi'})
            try:
                res = job_manager.retry_library_indexing(int(item_id), session)
                msg = res.get('message', '')
                if not res.get('success') and any(k in msg for k in ['Connection refused', 'Errno 111', 'Errno 61', '10061', 'tidak dapat dihubungi']):
                    res['jellyfin_offline'] = True
                    res['ok'] = False
                    res['message'] = 'Server Jellyfin eksternal belum aktif. Video dapat langsung ditonton menggunakan pemutar web bawaan.'
                else:
                    res['ok'] = res.get('success', False)
                self.send_json(200, res)
            except Exception as e:
                err_str = str(e)
                if any(k in err_str for k in ['Connection refused', 'Errno 111', 'Errno 61', '10061', 'tidak dapat dihubungi']):
                    self.send_json(200, {
                        'ok': False,
                        'jellyfin_offline': True,
                        'message': 'Server Jellyfin eksternal belum aktif di port 8096. Video dapat langsung ditonton menggunakan pemutar web bawaan.'
                    })
                else:
                    self.send_json(200, {'ok': False, 'error': err_str})

        elif path == '/api/library/scan':
            if not self.is_library_authorized():
                return self.send_json(401, {'ok': False, 'auth_required': True, 'error': 'Kata sandi perpustakaan diperlukan'})
            try:
                ok = jellyfin_client.trigger_scan()
                if ok:
                    self.send_json(200, {'ok': True, 'message': 'Pemindaian katalog Jellyfin berhasil dipicu.'})
                else:
                    self.send_json(200, {
                        'ok': False,
                        'jellyfin_offline': True,
                        'message': 'Server Jellyfin eksternal belum aktif. Pemutaran web mandiri tetap berfungsi penuh.'
                    })
            except Exception:
                self.send_json(200, {
                    'ok': False,
                    'jellyfin_offline': True,
                    'message': 'Server Jellyfin eksternal belum aktif di port 8096. Video dapat ditonton langsung di pemutar web.'
                })

        elif path == '/api/library/delete':
            from ..admin.auth import get_session_from_request, validate_csrf
            from ..admin.audit import log_admin_action
            admin_sess = get_session_from_request(self)
            if not admin_sess:
                return self.send_json(403, {
                    'ok': False,
                    'error': 'Hak menonton perpustakaan tidak memberi izin menghapus koleksi. Diperlukan hak akses Super Admin.'
                })
            if not admin_sess.get('is_superadmin'):
                return self.send_json(403, {
                    'ok': False,
                    'error': 'Diperlukan hak peran Super Admin untuk menghapus koleksi.'
                })
            if not validate_csrf(self, admin_sess):
                return self.send_json(403, {
                    'ok': False,
                    'error': 'Token CSRF tidak valid atau hilang.'
                })

            item_id = data.get('item_id')
            delete_files = bool(data.get('delete_files', False))
            if not item_id:
                return self.send_json(400, {'error': 'item_id wajib diisi'})
            try:
                username = admin_sess.get('username', 'admin')
                res = library_db.delete_library_item(int(item_id), delete_files=delete_files, move_to_trash=True, deleted_by=username)
                log_admin_action(
                    username=username,
                    action="library.delete_item",
                    resource_type="library_item",
                    resource_id=str(item_id),
                    details=res,
                    ip_address=self.client_address[0] if hasattr(self, 'client_address') else '127.0.0.1',
                    status="success" if res.get('success') else "failure",
                    user_id=admin_sess.get('user_id')
                )
                if res.get('success'):
                    self.send_json(200, {'ok': True, 'status': 'Item berhasil dihapus', **res})
                else:
                    self.send_json(400, {'error': res.get('partial_error') or 'Item tidak ditemukan'})
            except Exception as e:
                self.send_json(500, {'error': str(e)})
            
        else:
            self.send_json(404, {'error': 'API endpoint not found'})

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        
        # Admin API Routes
        if path.startswith('/api/admin/'):
            from ..admin.router import AdminRouter
            if AdminRouter.handle_request(self, 'GET'):
                return

        # Health & Monitoring API
        if path in ('/api/health', '/api/ping'):
            return self.send_json(200, {'ok': True, 'status': 'healthy', 'time': time.time()})

        # API Routes
        if path.startswith('/api/'):
            if path.startswith('/api/library/'):
                if not self.is_library_authorized():
                    return self.send_json(401, {'ok': False, 'auth_required': True, 'error': 'Kata sandi perpustakaan diperlukan'})
            else:
                session = self.get_session()
                if not session:
                    return self.send_json(401, {'error': 'Unauthorized'})

            if path.startswith('/api/library/stream/'):
                item_id_str = path.split('/')[-1]
                try:
                    return self.serve_library_media(int(item_id_str))
                except (ValueError, TypeError):
                    return self.send_error(400, 'Invalid item ID')

            if path.startswith('/api/library/subtitle/'):
                item_id_str = path.split('/')[-1]
                try:
                    return self.serve_library_subtitle(int(item_id_str))
                except (ValueError, TypeError):
                    return self.send_error(400, 'Invalid item ID')

            if path.startswith('/api/library/poster/'):
                item_id_str = path.split('/')[-1]
                try:
                    return self.serve_library_poster(int(item_id_str))
                except (ValueError, TypeError):
                    return self.send_error(400, 'Invalid item ID')

            if path.startswith('/api/library/backdrop/'):
                item_id_str = path.split('/')[-1]
                try:
                    return self.serve_library_backdrop(int(item_id_str))
                except (ValueError, TypeError):
                    return self.send_error(400, 'Invalid item ID')

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
                    'running': job.status in ('queued', 'downloading', 'validating', 'importing', 'indexing'),
                    'target': getattr(job, 'target', 'download'),
                    'library_item_id': getattr(job, 'library_item_id', None),
                    'jellyfin_item_id': getattr(job, 'jellyfin_item_id', None),
                    'jellyfin_url': getattr(job, 'jellyfin_url', None),
                    'logs': job.logs,
                    'files': files
                })
                return

            if path == '/api/library/status':
                health = jellyfin_client.check_health()
                try:
                    if not config.LIBRARY_ROOT.exists():
                        config.LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
                    usage = shutil.disk_usage(config.LIBRARY_ROOT)
                    free_bytes = usage.free
                    total_bytes = usage.total
                except Exception:
                    try:
                        usage = shutil.disk_usage(Path.cwd())
                        free_bytes = usage.free
                        total_bytes = usage.total
                    except Exception:
                        free_bytes = 0
                        total_bytes = 0
                try:
                    all_items = library_db.list_library_items()
                    counts = {
                        'total': len(all_items),
                        'ready': len([it for it in all_items if it.get('status') == 'ready']),
                        'needs_attention': len([it for it in all_items if it.get('status') == 'needs_attention'])
                    }
                except Exception:
                    counts = {'total': 0, 'ready': 0, 'needs_attention': 0}

                return self.send_json(200, {
                    'ok': True,
                    'jellyfin': health,
                    'library_root': str(config.LIBRARY_ROOT),
                    'movies_dir': str(config.LIBRARY_MOVIES_DIR),
                    'disk': {
                        'free_bytes': free_bytes,
                        'total_bytes': total_bytes,
                        'free_gb': round(free_bytes / (1024**3), 2),
                        'total_gb': round(total_bytes / (1024**3), 2),
                        'percent_used': round(((total_bytes - free_bytes) / total_bytes * 100), 1) if total_bytes > 0 else 0.0
                    },
                    'counts': counts
                })

            if path == '/api/library/items':
                qs = parse_qs(parts.query)
                q = (qs.get('q') or [''])[0].strip() or None
                st = (qs.get('status') or [''])[0].strip() or None
                limit = int((qs.get('limit') or [50])[0])
                offset = int((qs.get('offset') or [0])[0])
                items = library_db.list_library_items(query=q, status=st, limit=limit, offset=offset)
                for it in items:
                    if it.get('jellyfin_item_id'):
                        it['jellyfin_url'] = jellyfin_client.get_playback_web_url(it['jellyfin_item_id'])
                return self.send_json(200, {'ok': True, 'items': items, 'total': len(items)})

            if path == '/api/library/item':
                qs = parse_qs(parts.query)
                item_id = (qs.get('id') or [None])[0]
                if not item_id:
                    return self.send_json(400, {'error': 'id required'})
                item = library_db.get_library_item(int(item_id))
                if not item:
                    return self.send_json(404, {'error': 'Item not found'})
                if item.get('jellyfin_item_id'):
                    item['jellyfin_url'] = jellyfin_client.get_playback_web_url(item['jellyfin_item_id'])
                return self.send_json(200, {'item': item})
                
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

    def serve_library_media(self, item_id: int):
        item = library_db.get_library_item(item_id)
        if not item:
            return self.send_error(404, 'Item perpustakaan tidak ditemukan')
        
        folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
        try:
            folder.relative_to(config.LIBRARY_ROOT.resolve())
        except ValueError:
            return self.send_error(403, 'Path traversal detected')

        if not folder.exists() or not folder.is_dir():
            return self.send_error(404, 'Folder media tidak ditemukan')

        # Find primary video file
        video_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')]
        if not video_files:
            return self.send_error(404, 'Berkas video tidak ditemukan di perpustakaan')

        target_file = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]
        file_size = target_file.stat().st_size
        mime = mimetypes.guess_type(target_file.name)[0] or 'video/mp4'

        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            try:
                ranges = range_header.replace('bytes=', '').strip().split('-')
                start = int(ranges[0]) if ranges[0] else 0
                end = int(ranges[1]) if len(ranges) > 1 and ranges[1] else file_size - 1
                if start >= file_size or end >= file_size or start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{file_size}')
                    self.end_headers()
                    return

                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Disposition', 'inline')
                self.end_headers()

                with open(target_file, 'rb') as f:
                    f.seek(start)
                    bytes_remaining = length
                    chunk_size = 256 * 1024
                    while bytes_remaining > 0:
                        chunk = f.read(min(chunk_size, bytes_remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        bytes_remaining -= len(chunk)
                return
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                return self.send_error(500, f'Streaming error: {e}')

        # Full file stream
        try:
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Disposition', 'inline')
            self.end_headers()

            with open(target_file, 'rb') as f:
                chunk_size = 256 * 1024
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.send_error(500, 'Streaming error')

    def serve_library_subtitle(self, item_id: int):
        item = library_db.get_library_item(item_id)
        if not item:
            return self.send_error(404, 'Item tidak ditemukan')
        
        folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
        sub_content = "WEBVTT\n\n"
        if folder.exists() and folder.is_dir():
            sub_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.vtt', '.srt')]
            if sub_files:
                vtt_files = [p for p in sub_files if p.suffix.lower() == '.vtt']
                chosen_sub = vtt_files[0] if vtt_files else sub_files[0]
                try:
                    raw_text = chosen_sub.read_text(encoding='utf-8', errors='ignore')
                    if chosen_sub.suffix.lower() == '.srt':
                        vtt_body = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', raw_text)
                        sub_content = "WEBVTT\n\n" + vtt_body
                    else:
                        sub_content = raw_text if raw_text.startswith('WEBVTT') else f"WEBVTT\n\n{raw_text}"
                except Exception:
                    pass

        body = sub_content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/vtt; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _serve_image_file(self, file_path: Path):
        stat = file_path.stat()
        etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
        if_none_match = self.headers.get('If-None-Match', '').strip()
        if if_none_match == etag:
            self.send_response(304)
            self.end_headers()
            return

        content = file_path.read_bytes()
        mime = mimetypes.guess_type(file_path.name)[0] or 'image/jpeg'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        self.wfile.write(content)

    def serve_library_poster(self, item_id: int):
        item = library_db.get_library_item(item_id)
        if item and item.get('poster_path'):
            target_img = (config.LIBRARY_ROOT / item['poster_path']).resolve()
            if target_img.exists() and target_img.is_file():
                return self._serve_image_file(target_img)

        # Fallback to poster.* in folder
        if item:
            folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
            if folder.exists() and folder.is_dir():
                for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                    candidate = folder / f"poster{ext}"
                    if candidate.exists() and candidate.is_file():
                        return self._serve_image_file(candidate)

        # If no custom image exists, generate SVG card
        title = item.get('title', 'Media Film') if item else 'OmniFetch Media'
        year = item.get('year', '') if item else ''
        resolution = item.get('resolution') or item.get('quality') or ''
        return self._serve_svg_poster(title, year, resolution, has_subtitles=bool(item.get('has_subtitles') if item else False))

    def serve_library_backdrop(self, item_id: int):
        item = library_db.get_library_item(item_id)
        if item and item.get('backdrop_path'):
            target_img = (config.LIBRARY_ROOT / item['backdrop_path']).resolve()
            if target_img.exists() and target_img.is_file():
                return self._serve_image_file(target_img)

        # Fallback to backdrop.* in folder
        if item:
            folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
            if folder.exists() and folder.is_dir():
                for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                    candidate = folder / f"backdrop{ext}"
                    if candidate.exists() and candidate.is_file():
                        return self._serve_image_file(candidate)

        # Fallback to poster
        return self.serve_library_poster(item_id)

    def _serve_svg_poster(self, title: str, year: Any = '', resolution: str = '', has_subtitles: bool = False):
        year_str = f" • {year}" if year else ""
        safe_title = html.escape(title[:45] + ('...' if len(title) > 45 else ''))

        # Only render quality badge if known, never assume WEB-DL
        quality_badge = ""
        if resolution:
            safe_q = html.escape(str(resolution))
            quality_badge = f"""
  <g transform="translate(24, 30)">
    <rect width="78" height="22" rx="4" fill="rgba(15,23,42,0.85)" stroke="rgba(255,255,255,0.15)" />
    <text x="39" y="15" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="11" font-weight="700" text-anchor="middle" letter-spacing="1">{safe_q}</text>
  </g>"""

        subtitle_badge = ""
        if has_subtitles:
            subtitle_badge = """
  <g transform="translate(296, 30)">
    <rect width="80" height="22" rx="4" fill="rgba(14,116,144,0.8)" stroke="rgba(6,182,212,0.4)" />
    <text x="336" y="15" fill="#cffafe" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="10" font-weight="700" text-anchor="middle" letter-spacing="0.5">SUB INDO</text>
  </g>"""

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a" />
      <stop offset="50%" stop-color="#1e1b4b" />
      <stop offset="100%" stop-color="#090d16" />
    </linearGradient>
    <linearGradient id="overlay" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="40%" stop-color="rgba(15,23,42,0)" />
      <stop offset="100%" stop-color="rgba(10,14,26,0.95)" />
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="30%" r="50%">
      <stop offset="0%" stop-color="rgba(99,102,241,0.25)" />
      <stop offset="100%" stop-color="rgba(99,102,241,0)" />
    </radialGradient>
  </defs>
  <rect width="400" height="600" fill="url(#bg)" />
  <circle cx="200" cy="220" r="160" fill="url(#glow)" />
  <path d="M40 0 V600 M360 0 V600 M0 500 H400" stroke="rgba(255,255,255,0.04)" stroke-width="1" />
  <g transform="translate(160, 180)" stroke="#818cf8" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round">
    <rect x="0" y="10" width="80" height="60" rx="6" stroke="#6366f1" fill="rgba(30,27,75,0.6)" />
    <polygon points="32,28 32,52 54,40" fill="#a5b4fc" stroke="#a5b4fc" />
    <path d="M10 10 L20 0 H70 L80 10" stroke="#818cf8" fill="rgba(99,102,241,0.2)" />
    <line x1="26" y1="0" x2="30" y2="10" />
    <line x1="44" y1="0" x2="48" y2="10" />
    <line x1="62" y1="0" x2="66" y2="10" />
  </g>
  <rect width="400" height="600" fill="url(#overlay)" />
  {quality_badge}
  {subtitle_badge}
  <g transform="translate(24, 520)">
    <text x="0" y="0" fill="#e2e8f0" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="18" font-weight="700">{safe_title}</text>
    <text x="0" y="24" fill="#94a3b8" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="12" font-weight="500">OmniFetch Cinema Studio{year_str}</text>
  </g>
</svg>"""
        body = svg.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

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
        try:
            safe_path.relative_to(job.folder.resolve())
        except ValueError:
            return self.send_error(403, 'Path traversal detected')
            
        if not safe_path.exists() or not safe_path.is_file():
            return self.send_error(404, 'File not found')
            
        try:
            content = safe_path.read_bytes()
            if safe_path.name.lower().endswith('.srt'):
                mime = 'application/x-subrip'
            elif safe_path.name.lower().endswith('.vtt'):
                mime = 'text/vtt'
            else:
                mime = mimetypes.guess_type(str(safe_path))[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            
            from urllib.parse import quote
            clean_ascii = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
            disposition = 'inline' if stream else f'attachment; filename="{clean_ascii}"; filename*=UTF-8\'\'{quote(filename)}'
            self.send_header('Content-Disposition', disposition)
            
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, 'Error reading file')

    def serve_static(self, path: str):
        if path == '/':
            path = '/library.html'
        elif path == '/downloader':
            path = '/downloader.html'
        elif path == '/admin':
            path = '/admin.html'
        elif path == '/features':
            path = '/features.html'
            
        # Redirect /extension.zip to versioned package URL so the link has the version
        if path == '/extension.zip':
            manifest_file = config.BASE_DIR / 'chrome-extension' / 'manifest.json'
            v = '2.1.4'
            if manifest_file.exists():
                try:
                    v = json.loads(manifest_file.read_text()).get('version', v)
                except Exception:
                    pass
            self.send_response(302)
            self.send_header('Location', f'/OmniFetch-Companion-v{v}.zip')
            self.end_headers()
            return

        safe_path = Path(config.BASE_DIR / path.lstrip('/')).resolve()
        
        # Directory traversal protection using strict relative_to
        try:
            safe_path.relative_to(config.BASE_DIR)
        except ValueError:
            self.send_error(403, 'Forbidden')
            return

        # Security protection: never serve database, cookies, environment file, secrets, or python code
        blocked_names = {'cookies.txt', 'jobs_db.json', 'omnifetch-library.sqlite3'}
        if (safe_path.name.startswith('.') and safe_path.name != '.well-known') or \
           safe_path.suffix.lower() in ('.sqlite3', '.sqlite', '.db', '.env', '.key', '.pem', '.py') or \
           safe_path.name in blocked_names or \
           'data' in safe_path.parts:
            self.send_error(403, 'Forbidden')
            return
            
        if not safe_path.exists() or not safe_path.is_file():
            # Fallback for versioned companion zip if specific file doesn't exist on disk
            if 'companion' in path.lower() and path.endswith('.zip'):
                candidates = sorted(config.BASE_DIR.glob('OmniFetch-Companion-*.zip'), reverse=True)
                for cand in candidates:
                    if cand.exists() and cand.is_file():
                        safe_path = cand
                        break

            if not safe_path.exists() or not safe_path.is_file():
                self.send_error(404, 'File not found')
                return
            
        try:
            content = safe_path.read_bytes()
            mime = mimetypes.guess_type(str(safe_path))[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            if safe_path.suffix == '.zip':
                self.send_header('Content-Disposition', f'attachment; filename="{safe_path.name}"')
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_error(500, 'Internal Server Error')
