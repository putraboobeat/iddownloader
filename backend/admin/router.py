"""HTTP Dispatcher and API Router for OmniFetch Super Admin endpoints."""
import json
import shutil
import csv
import io
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from typing import Optional, Dict, Any, List

from ..core import config
from ..core import library_db
from ..core.security import clean_url
from ..core.job_manager import job_manager, ACTIVE_STAGES
from ..core.importer import run_ffprobe, LibraryImporter
from . import auth
from . import audit
from . import settings
from . import stats
from . import storage_manager
from . import library_manager

WIB = timezone(timedelta(hours=7))

def sanitize_csv_cell(val: Any) -> str:
    """Defends against CSV Formula Injection (DDE injection) by escaping special prefixes."""
    s = str(val if val is not None else '').strip()
    if s and s[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return s

def is_valid_admin_origin(handler, origin: str) -> bool:
    """Verifies that the Origin matches the request Host or local loopback.
    Rejects foreign cross-origins to protect Super Admin panel.
    """
    if not origin:
        return True
    parsed_origin = urlparse(origin)
    origin_netloc = (parsed_origin.netloc or '').lower()
    host_hdr = (handler.headers.get('Host') or '').lower()
    if host_hdr and origin_netloc == host_hdr:
        return True
    origin_hostname = (parsed_origin.hostname or '').lower()
    if origin_hostname in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
        return True
    return False

class AdminRouter:
    """Handles dispatching of /api/admin/... requests."""

    @staticmethod
    def handle_request(handler, method: str) -> bool:
        """Entry point called from main HTTP handler.
        Returns True if the request was an admin route and handled.
        """
        parts = urlparse(handler.path)
        path = parts.path

        if not path.startswith('/api/admin/'):
            return False

        origin = handler.headers.get('Origin', '')
        if origin and not is_valid_admin_origin(handler, origin):
            AdminRouter._send_json(handler, 403, {'ok': False, 'error': 'Origin asing ditolak untuk panel Super Admin.'})
            return True

        if method == 'OPTIONS':
            handler.send_response(200)
            if origin and is_valid_admin_origin(handler, origin):
                handler.send_header('Access-Control-Allow-Origin', origin)
                handler.send_header('Access-Control-Allow-Credentials', 'true')
                handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                handler.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Admin-Session, X-CSRF-Token, Range')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return True

        # Public Admin Route: Login
        if path == '/api/admin/auth/login' and method == 'POST':
            AdminRouter._handle_login(handler)
            return True

        # All other admin routes REQUIRE authenticated admin session
        session = auth.get_session_from_request(handler)
        if not session:
            AdminRouter._send_json(handler, 401, {
                'ok': False,
                'error': 'Sesi administrator tidak sah atau telah kedaluwarsa. Silakan masuk kembali.'
            })
            return True

        # State-changing methods (POST, PUT, DELETE) require CSRF token
        if method in ('POST', 'PUT', 'DELETE'):
            if not auth.validate_csrf(handler, session):
                audit.log_admin_action(
                    username=session.get('username', 'unknown'),
                    action="auth.csrf_failure",
                    ip_address=auth.get_client_ip(handler),
                    status="failure",
                    user_id=session.get('user_id')
                )
                AdminRouter._send_json(handler, 403, {
                    'ok': False,
                    'error': 'Token proteksi CSRF tidak valid atau hilang. Muat ulang halaman.'
                })
                return True

        # Dispatch based on route and method
        try:
            if method == 'GET':
                AdminRouter._dispatch_get(handler, path, parts, session)
            elif method == 'POST':
                AdminRouter._dispatch_post(handler, path, parts, session)
            else:
                AdminRouter._send_json(handler, 405, {'error': 'Method not allowed'})
        except Exception as e:
            AdminRouter._send_json(handler, 500, {'error': f"Kesalahan internal server admin: {str(e)}"})

        return True

    @staticmethod
    def _send_json(handler, code: int, data: dict, extra_headers: Optional[list] = None):
        body = json.dumps(data).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        origin = handler.headers.get('Origin', '')
        if origin and is_valid_admin_origin(handler, origin):
            handler.send_header('Access-Control-Allow-Origin', origin)
            handler.send_header('Access-Control-Allow-Credentials', 'true')
            handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            handler.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Admin-Session, X-CSRF-Token, Range')
        if extra_headers:
            for k, v in extra_headers:
                handler.send_header(k, v)
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _read_json_body(handler) -> dict:
        content_length = int(handler.headers.get('Content-Length', 0))
        if content_length > 15 * 1024 * 1024: # 15 MB cap
            raise ValueError("Ukuran muatan data melebihi batas maksimum (15MB).")
        body = handler.rfile.read(content_length)
        if not body:
            return {}
        return json.loads(body.decode('utf-8'))

    # --- Authentication Handlers ---

    @staticmethod
    def _handle_login(handler):
        ip = auth.get_client_ip(handler)
        allowed, retry_after = auth.check_login_rate_limit(ip)
        if not allowed:
            AdminRouter._send_json(handler, 429, {
                'ok': False,
                'error': f"Terlalu banyak percobaan masuk yang gagal. Silakan coba kembali dalam {retry_after} detik."
            })
            return

        try:
            data = AdminRouter._read_json_body(handler)
        except Exception:
            AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Format data JSON tidak valid'})
            return

        username = str(data.get('username') or '').strip().lower()
        password = str(data.get('password') or '')

        if not username or not password:
            AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Username dan kata sandi wajib diisi'})
            return

        user = library_db.get_admin_user_by_username(username)
        if not user or not auth.verify_password(password, user['password_hash'], user['salt']):
            auth.record_login_failure(ip)
            audit.log_admin_action(
                username=username,
                action="auth.login_failed",
                ip_address=ip,
                status="failure"
            )
            AdminRouter._send_json(handler, 401, {'ok': False, 'error': 'Username atau kata sandi salah.'})
            return

        # Login success
        auth.clear_login_failures(ip)
        library_db.update_admin_last_login(user['id'])
        sess_info = auth.create_session(
            user_id=user['id'],
            ip_address=ip,
            user_agent=handler.headers.get('User-Agent', '')
        )

        audit.log_admin_action(
            username=user['username'],
            action="auth.login_success",
            ip_address=ip,
            status="success",
            user_id=user['id']
        )

        cookie_str = auth.format_cookie('omni_admin_session', sess_info['token'], config.ADMIN_SESSION_MAX_AGE, config.ADMIN_COOKIE_SECURE)
        AdminRouter._send_json(handler, 200, {
            'ok': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'full_name': user.get('full_name'),
                'is_superadmin': bool(user.get('is_superadmin'))
            },
            'csrf_token': sess_info['csrf_token'],
            'expires_at': sess_info['expires_at']
        }, extra_headers=[('Set-Cookie', cookie_str)])

    # --- GET Dispatcher ---

    @staticmethod
    def _dispatch_get(handler, path: str, parts, session: dict):
        qs = parse_qs(parts.query)

        # 1. Auth Me
        if path == '/api/admin/auth/me':
            AdminRouter._send_json(handler, 200, {
                'ok': True,
                'user': {
                    'id': session['user_id'],
                    'username': session['username'],
                    'full_name': session.get('full_name'),
                    'is_superadmin': bool(session.get('is_superadmin'))
                },
                'csrf_token': session['csrf_token']
            })

        # 2. Dashboard Summary
        elif path == '/api/admin/dashboard':
            data = stats.get_dashboard_summary()
            AdminRouter._send_json(handler, 200, {'ok': True, 'data': data})

        # 3. Activity Chart Data
        elif path == '/api/admin/stats/chart':
            days = int((qs.get('days') or [7])[0])
            data = stats.get_activity_chart_data(days=days)
            AdminRouter._send_json(handler, 200, {'ok': True, 'data': data})

        # 4. Active Worker Queue
        elif path == '/api/admin/jobs/active':
            active_list = [j.to_admin_dto() for j in job_manager.jobs.values() if j.stage in ACTIVE_STAGES]
            AdminRouter._send_json(handler, 200, {
                'ok': True,
                'jobs': sorted(active_list, key=lambda x: x['created_at'], reverse=True),
                'maintenance_mode': not job_manager.is_intake_allowed()
            })

        # 5. Job Log
        elif path == '/api/admin/jobs/log':
            job_id = (qs.get('job_id') or [''])[0]
            job = job_manager.get_job_admin(job_id)
            if job:
                logs = job.logs
            else:
                p_job = library_db.get_persisted_job(job_id)
                logs = p_job.get('logs', []) if p_job else []
            safe_logs = audit.redact_secrets(logs)
            AdminRouter._send_json(handler, 200, {'ok': True, 'logs': safe_logs})

        # 6. Job Diagnostic
        elif path == '/api/admin/jobs/diagnose':
            job_id = (qs.get('job_id') or [''])[0]
            job = job_manager.get_job_admin(job_id)
            if not job:
                p_job = library_db.get_persisted_job(job_id)
                if not p_job:
                    return AdminRouter._send_json(handler, 404, {'ok': False, 'error': 'Pekerjaan tidak ditemukan'})
                dto = audit.redact_secrets(p_job)
                dto['owner_pseudonym'] = dto.get('owner_session_pseudonym') or 'user_anon'
                dto.pop('owner_session', None)
            else:
                dto = job.to_admin_dto()
            AdminRouter._send_json(handler, 200, {'ok': True, 'diagnostic': dto})

        # 7. Download History
        elif path == '/api/admin/history':
            limit = int((qs.get('limit') or [25])[0])
            offset = int((qs.get('offset') or [0])[0])
            stage = (qs.get('stage') or [''])[0] or None
            target = (qs.get('target') or [''])[0] or None
            domain = (qs.get('domain') or [''])[0] or None
            query = (qs.get('q') or [''])[0] or None
            sort_by = (qs.get('sort_by') or ['created_at'])[0]
            sort_dir = (qs.get('sort_dir') or ['desc'])[0]

            res = library_db.list_persisted_jobs(
                limit=limit,
                offset=offset,
                stage=stage,
                target=target,
                domain=domain,
                query=query,
                sort_by=sort_by,
                sort_dir=sort_dir
            )
            AdminRouter._send_json(handler, 200, {'ok': True, **res})

        # 8. Export History (CSV / JSON)
        elif path == '/api/admin/history/export':
            fmt = (qs.get('format') or ['csv'])[0].lower()
            stage = (qs.get('stage') or [''])[0] or None
            target = (qs.get('target') or [''])[0] or None
            domain = (qs.get('domain') or [''])[0] or None
            query = (qs.get('q') or [''])[0] or None
            sort_by = (qs.get('sort_by') or ['created_at'])[0]
            sort_dir = (qs.get('sort_dir') or ['desc'])[0]

            res = library_db.list_persisted_jobs(
                limit=10000,
                offset=0,
                stage=stage,
                target=target,
                domain=domain,
                query=query,
                sort_by=sort_by,
                sort_dir=sort_dir
            )
            items = res.get('items', [])

            if fmt == 'json':
                safe_items = audit.redact_secrets(items)
                for it in safe_items:
                    it.pop('owner_session', None)
                body = json.dumps({'history': safe_items}, indent=2).encode('utf-8')
                handler.send_response(200)
                handler.send_header('Content-Type', 'application/json')
                handler.send_header('Content-Disposition', 'attachment; filename="omnifetch-history.json"')
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            else:
                # CSV Export with Formula Injection Guard, Secret Redaction, and Asia/Jakarta Timezone
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['ID Pekerjaan', 'Domain Sumber', 'Judul', 'Tahap', 'Progres (%)', 'Ukuran File', 'Dibuat Pada (WIB)', 'Selesai Pada (WIB)', 'Sesi Pseudonim'])
                for it in items:
                    c_ts = it.get('created_at')
                    cmp_ts = it.get('completed_at')
                    c_str = datetime.fromtimestamp(c_ts, tz=WIB).strftime('%Y-%m-%d %H:%M:%S') if c_ts else ''
                    cmp_str = datetime.fromtimestamp(cmp_ts, tz=WIB).strftime('%Y-%m-%d %H:%M:%S') if cmp_ts else ''
                    writer.writerow([
                        sanitize_csv_cell(it.get('job_id')),
                        sanitize_csv_cell(it.get('domain')),
                        sanitize_csv_cell(audit.redact_sensitive_text(str(it.get('title') or ''))),
                        sanitize_csv_cell(it.get('stage')),
                        sanitize_csv_cell(it.get('percent')),
                        sanitize_csv_cell(it.get('file_size')),
                        sanitize_csv_cell(c_str),
                        sanitize_csv_cell(cmp_str),
                        sanitize_csv_cell(it.get('owner_session_pseudonym'))
                    ])
                csv_bytes = output.getvalue().encode('utf-8-sig')
                handler.send_response(200)
                handler.send_header('Content-Type', 'text/csv; charset=utf-8')
                handler.send_header('Content-Disposition', 'attachment; filename="omnifetch-history.csv"')
                handler.send_header('Content-Length', str(len(csv_bytes)))
                handler.end_headers()
                handler.wfile.write(csv_bytes)

        # 9. Storage Inventory (Paginated Snapshot)
        elif path == '/api/admin/storage/inventory':
            limit = int((qs.get('limit') or [50])[0])
            offset = int((qs.get('offset') or [0])[0])
            inv = storage_manager.get_latest_inventory_snapshot(limit=limit, offset=offset)
            AdminRouter._send_json(handler, 200, {'ok': True, 'data': inv})

        # 9b. Subtitles for Item
        elif path == '/api/admin/library/subtitles':
            item_id = int((qs.get('item_id') or [0])[0])
            files = library_db.get_library_files_for_item(item_id)
            sub_files = []
            for f in files:
                if f.get('file_type') == 'subtitle':
                    lang = f.get('subtitle_lang') or 'und'
                    f_name = Path(f.get('file_path') or '').name
                    sub_files.append({
                        **f,
                        'language': lang,
                        'file_name': f_name,
                        'is_default': bool(f.get('is_default') or f.get('is_primary'))
                    })
            AdminRouter._send_json(handler, 200, {'ok': True, 'subtitles': sub_files})

        # 10. Trash Items
        elif path == '/api/admin/storage/trash':
            t_res = library_db.list_trash_items(status='trashed')
            AdminRouter._send_json(handler, 200, {'ok': True, **t_res})

        # 11. Library Collections (Admin View)
        elif path == '/api/admin/library/items':
            q = (qs.get('q') or [''])[0] or None
            st = (qs.get('status') or [''])[0] or None
            limit = int((qs.get('limit') or [50])[0])
            offset = int((qs.get('offset') or [0])[0])
            items = library_db.list_library_items(query=q, status=st, limit=limit, offset=offset)
            total = library_db.count_library_items(query=q, status=st)
            AdminRouter._send_json(handler, 200, {'ok': True, 'items': items, 'total': total})

        # 12. Single Library Item
        elif path == '/api/admin/library/item':
            item_id = int((qs.get('id') or [0])[0])
            item = library_db.get_library_item(item_id)
            if not item:
                return AdminRouter._send_json(handler, 404, {'ok': False, 'error': 'Item tidak ditemukan'})
            AdminRouter._send_json(handler, 200, {'ok': True, 'item': item})

        # 13. Audit Logs
        elif path == '/api/admin/audit':
            limit = int((qs.get('limit') or [50])[0])
            offset = int((qs.get('offset') or [0])[0])
            action = (qs.get('action') or [''])[0] or None
            status = (qs.get('status') or [''])[0] or None
            search = (qs.get('q') or [''])[0] or None
            res = library_db.list_audit_logs(limit=limit, offset=offset, action=action, status=status, search=search)
            AdminRouter._send_json(handler, 200, {'ok': True, **res})

        # 14. Settings
        elif path == '/api/admin/settings':
            all_s = settings.get_effective_settings()
            AdminRouter._send_json(handler, 200, {'ok': True, 'settings': all_s})

        else:
            AdminRouter._send_json(handler, 404, {'error': 'Endpoint admin tidak ditemukan'})

    # --- POST Dispatcher ---

    @staticmethod
    def _dispatch_post(handler, path: str, parts, session: dict):
        ip = auth.get_client_ip(handler)
        username = session['username']

        # 1. Logout
        if path == '/api/admin/auth/logout':
            auth_token = handler.headers.get('Cookie', '')
            token_val = None
            if 'omni_admin_session=' in auth_token:
                m = auth_token.split('omni_admin_session=')[1].split(';')[0].strip()
                token_val = m
            if token_val:
                library_db.revoke_admin_session(auth.hash_token(token_val))

            audit.log_admin_action(
                username=username,
                action="auth.logout",
                ip_address=ip,
                status="success",
                user_id=session['user_id']
            )
            logout_cookie = auth.format_logout_cookie()
            AdminRouter._send_json(handler, 200, {'ok': True, 'message': 'Berhasil keluar.'}, extra_headers=[('Set-Cookie', logout_cookie)])
            return

        # 2. Change Password
        elif path == '/api/admin/auth/change-password':
            data = AdminRouter._read_json_body(handler)
            old_pw = str(data.get('old_password') or '')
            new_pw = str(data.get('new_password') or '')
            user = library_db.get_admin_user_by_id(session['user_id'])
            if not user or not auth.verify_password(old_pw, user['password_hash'], user['salt']):
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Kata sandi saat ini tidak cocok.'})
            if len(new_pw) < 8:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Kata sandi baru minimal 8 karakter.'})

            new_hash, new_salt = auth.hash_password(new_pw)
            library_db.update_admin_password(user['id'], new_hash, new_salt)
            audit.log_admin_action(
                username=username,
                action="auth.change_password",
                resource_type="user",
                resource_id=str(user['id']),
                ip_address=ip,
                status="success",
                user_id=user['id']
            )
            AdminRouter._send_json(handler, 200, {'ok': True, 'message': 'Kata sandi berhasil diperbarui.'})
            return

        # 3. Stop / Cancel Job
        elif path == '/api/admin/jobs/cancel':
            data = AdminRouter._read_json_body(handler)
            job_id = data.get('job_id')
            job = job_manager.get_job_admin(job_id)
            if not job:
                return AdminRouter._send_json(handler, 404, {'ok': False, 'error': 'Pekerjaan tidak ditemukan'})
            job.terminate()
            audit.log_admin_action(
                username=username,
                action="job.cancel",
                resource_type="job",
                resource_id=job_id,
                ip_address=ip,
                status="success",
                user_id=session['user_id']
            )
            AdminRouter._send_json(handler, 200, {'ok': True, 'message': 'Pekerjaan berhasil dihentikan.'})

        # 4. Retry Job
        elif path == '/api/admin/jobs/retry':
            data = AdminRouter._read_json_body(handler)
            job_id = data.get('job_id')
            new_url = data.get('new_url')
            try:
                new_j = job_manager.retry_job(job_id, session.get('owner_session', 'admin_session'), new_url=new_url)
                audit.log_admin_action(
                    username=username,
                    action="job.retry",
                    resource_type="job",
                    resource_id=new_j.id,
                    details={'parent_job_id': job_id},
                    ip_address=ip,
                    status="success",
                    user_id=session['user_id']
                )
                AdminRouter._send_json(handler, 200, {'ok': True, 'new_job_id': new_j.id, 'message': 'Percobaan ulang dimulai.'})
            except Exception as e:
                AdminRouter._send_json(handler, 400, {'ok': False, 'error': str(e)})

        # 5. Maintenance Mode Toggle
        elif path == '/api/admin/maintenance':
            data = AdminRouter._read_json_body(handler)
            enable = bool(data.get('enable'))
            settings.update_setting('maintenance_mode', enable, username=username, ip_address=ip)
            status_text = 'diaktifkan' if enable else 'dinonaktifkan'
            AdminRouter._send_json(handler, 200, {'ok': True, 'maintenance_mode': enable, 'message': f"Mode pemeliharaan berhasil {status_text}."})

        # 6. Trigger Storage Scan
        elif path == '/api/admin/storage/scan':
            storage_manager.trigger_background_inventory_scan()
            audit.log_admin_action(username=username, action="storage.scan", ip_address=ip, user_id=session['user_id'])
            AdminRouter._send_json(handler, 200, {'ok': True, 'message': 'Pemindaian penyimpanan dimulai di latar belakang.'})

        # 7. Quarantine Storage File
        elif path == '/api/admin/storage/quarantine':
            data = AdminRouter._read_json_body(handler)
            cat = str(data.get('category') or '')
            rel_p = str(data.get('relative_path') or '')
            res = storage_manager.quarantine_managed_path(cat, rel_p, username=username, ip_address=ip)
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 8. Restore from Trash
        elif path == '/api/admin/storage/restore':
            data = AdminRouter._read_json_body(handler)
            t_id = int(data.get('trash_id') or 0)
            res = storage_manager.restore_from_trash(t_id, username=username, ip_address=ip)
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 9. Purge Trash Item
        elif path == '/api/admin/storage/purge':
            data = AdminRouter._read_json_body(handler)
            t_id = int(data.get('trash_id') or 0)
            res = storage_manager.purge_trash_item(t_id, username=username, ip_address=ip)
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 10. Update Library Metadata
        elif path == '/api/admin/library/update':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            res = library_manager.update_item_metadata(item_id, data, username=username, ip_address=ip)
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 11. Delete Library Item (Safe delete with trash quarantine)
        elif path == '/api/admin/library/delete':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            delete_files = bool(data.get('delete_files', False))
            move_to_trash = bool(data.get('move_to_trash', True))

            res = library_db.delete_library_item(
                item_id=item_id,
                delete_files=delete_files,
                move_to_trash=move_to_trash,
                deleted_by=username
            )
            audit.log_admin_action(
                username=username,
                action="library.delete",
                resource_type="library_item",
                resource_id=str(item_id),
                details=res,
                ip_address=ip,
                status="success" if res.get('success') else "failure",
                user_id=session['user_id']
            )
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 12. Poster Replacer: Upload Image
        elif path == '/api/admin/library/poster/upload':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            target_type = data.get('target_type', 'poster')
            b64_content = data.get('image_base64', '')
            import base64
            try:
                if ',' in b64_content:
                    b64_content = b64_content.split(',', 1)[1]
                raw_bytes = base64.b64decode(b64_content)
            except Exception:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Format base64 gambar tidak valid'})

            res = library_manager.save_item_poster_or_backdrop(
                item_id=item_id,
                image_data=raw_bytes,
                target_type=target_type,
                username=username,
                ip_address=ip
            )
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 13. Poster Replacer: From External URL
        elif path == '/api/admin/library/poster/url':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            img_url = str(data.get('image_url') or '')
            target_type = data.get('target_type', 'poster')

            raw_bytes, err = library_manager.fetch_image_from_url(img_url)
            if err or not raw_bytes:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': err or 'Gagal mengunduh gambar'})

            res = library_manager.save_item_poster_or_backdrop(
                item_id=item_id,
                image_data=raw_bytes,
                target_type=target_type,
                username=username,
                ip_address=ip
            )
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 14. Poster Replacer: Extract Video Frame
        elif path == '/api/admin/library/poster/frame':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            target_type = data.get('target_type', 'poster')
            ts_sec = float(data.get('timestamp_seconds', 15.0))

            item = library_db.get_library_item(item_id)
            if not item:
                return AdminRouter._send_json(handler, 404, {'ok': False, 'error': 'Item tidak ditemukan'})

            folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
            video_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')] if folder.exists() else []
            if not video_files:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Berkas video tidak ditemukan untuk mengekstrak frame'})

            primary_video = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]
            raw_bytes, err = library_manager.extract_video_frame(primary_video, timestamp_sec=ts_sec)
            if err or not raw_bytes:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': err or 'Gagal mengekstrak frame video'})

            res = library_manager.save_item_poster_or_backdrop(
                item_id=item_id,
                image_data=raw_bytes,
                target_type=target_type,
                username=username,
                ip_address=ip
            )
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 15. Subtitle: Upload
        elif path == '/api/admin/library/subtitles/upload':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            lang = str(data.get('language') or 'id')
            sub_text = str(data.get('subtitle_text') or '')
            if not sub_text:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Teks subtitle kosong'})
            res = library_manager.add_subtitle_file(
                item_id=item_id,
                subtitle_data=sub_text.encode('utf-8'),
                language=lang,
                username=username,
                ip_address=ip
            )
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 16. Subtitle: Delete
        elif path == '/api/admin/library/subtitles/delete':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            file_id = data.get('library_file_id') or data.get('file_id')
            filename = str(data.get('filename') or '')
            res = library_manager.delete_subtitle_file(item_id, filename=filename, file_id=file_id, username=username, ip_address=ip)
            code = 200 if res.get('success') else 400
            AdminRouter._send_json(handler, code, res)

        # 16b. Subtitle: Set Default
        elif path == '/api/admin/library/subtitles/default':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            file_id = int(data.get('file_id') or data.get('library_file_id') or 0)
            if not item_id or not file_id:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'item_id dan file_id wajib diisi'})
            rec = library_db.get_library_file(file_id)
            if not rec or rec.get('item_id') != item_id or rec.get('file_type') != 'subtitle':
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Berkas subtitle tidak valid untuk item ini'})
            library_db.set_default_subtitle(item_id, file_id)
            audit.log_admin_action(username=username, action="library.set_default_subtitle", resource_type="library_file", resource_id=str(file_id), user_id=session['user_id'])
            AdminRouter._send_json(handler, 200, {'ok': True, 'success': True, 'message': 'Subtitle bawaan berhasil ditetapkan.'})

        # 16c. Add Collection from URL
        elif path == '/api/admin/library/add-url':
            data = AdminRouter._read_json_body(handler)
            url = str(data.get('url') or '').strip()
            if not url:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'URL wajib diisi'})
            try:
                clean_url(url)
                job_data = {
                    'url': url,
                    'urls': [url],
                    'target': 'library',
                    'title': str(data.get('title') or '').strip() or 'Media',
                    'quality': data.get('quality') or 'best',
                    'year': data.get('year')
                }
                job = job_manager.add_job(session.get('token', 'admin_session'), job_data)
                audit.log_admin_action(
                    username=username,
                    action="library.add_url",
                    resource_type="job",
                    resource_id=job.id,
                    details={'url': audit.redact_sensitive_text(url)},
                    ip_address=ip,
                    user_id=session['user_id']
                )
                AdminRouter._send_json(handler, 200, {'ok': True, 'success': True, 'job_id': job.id, 'message': 'Unduhan koleksi perpustakaan berhasil dimulai.'})
            except Exception as e:
                AdminRouter._send_json(handler, 400, {'ok': False, 'error': str(e)})

        # 16d. Import Server File
        elif path == '/api/admin/library/import-file':
            try:
                data = AdminRouter._read_json_body(handler)
                rel_path = str(data.get('relative_path') or data.get('file_path') or '').strip()
                title = str(data.get('title') or '').strip()
                year = data.get('year')
                if not rel_path:
                    return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'relative_path atau file_path wajib diisi'})

                cand_p = Path(rel_path)
                if cand_p.is_absolute():
                    source_p = cand_p.resolve()
                else:
                    source_p = (config.TEMPORARY_DIR / rel_path).resolve()
                    if not source_p.exists():
                        source_p = (config.STAGING_DIR / rel_path).resolve()

                if not source_p.exists() or not source_p.is_file():
                    return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Berkas sumber tidak ditemukan di direktori sementara server.'})

                from .storage_manager import validate_path_containment
                valid, err, _ = validate_path_containment(source_p)
                if not valid:
                    return AdminRouter._send_json(handler, 400, {'ok': False, 'error': err or 'Path berkas tidak diizinkan'})

                # Check probe
                probe = run_ffprobe(source_p)
                if not probe.get('valid') or not probe.get('has_video'):
                    return AdminRouter._send_json(handler, 400, {'ok': False, 'error': f"Validasi media gagal: {probe.get('error', 'Format tidak didukung')}"})

                # Create destination folder inside Movies
                from ..core.importer import sanitize_title, parse_year_from_title
                raw_title = title or source_p.stem
                clean_t, detected_year = parse_year_from_title(raw_title, int(year) if year else None)
                yr = detected_year or year
                dest_dir = f"{clean_t} ({yr})" if yr else clean_t
                final_folder = config.LIBRARY_MOVIES_DIR / dest_dir
                final_folder.mkdir(parents=True, exist_ok=True)
                target_file = final_folder / source_p.name
                shutil.copy2(str(source_p), str(target_file))

                item_rec = library_db.create_library_item(
                    title=clean_t,
                    year=int(year) if year else None,
                    clean_title=clean_t,
                    folder_path=f"Movies/{dest_dir}",
                    media_type="movie",
                    status="ready"
                )
                library_db.add_library_file(
                    item_id=item_rec['id'],
                    file_path=f"Movies/{dest_dir}/{target_file.name}",
                    file_type="video",
                    file_size=target_file.stat().st_size,
                    is_primary=1
                )
                audit.log_admin_action(
                    username=username,
                    action="library.import_file",
                    resource_type="library_item",
                    resource_id=str(item_rec['id']),
                    details={'imported_file': source_p.name},
                    ip_address=ip,
                    user_id=session['user_id']
                )
                AdminRouter._send_json(handler, 200, {'ok': True, 'success': True, 'item_id': item_rec['id'], 'message': f"Berkas '{clean_t}' berhasil diimpor ke perpustakaan."})
            except Exception as e:
                import traceback
                traceback.print_exc()
                AdminRouter._send_json(handler, 500, {'ok': False, 'error': str(e)})

        # 16e. Bulk Action
        elif path == '/api/admin/library/bulk-action':
            data = AdminRouter._read_json_body(handler)
            action = str(data.get('action') or '')
            item_ids = data.get('item_ids') or []
            results = []
            for i_id in item_ids:
                try:
                    num_id = int(i_id)
                    if action == 'delete':
                        d_res = library_db.delete_library_item(num_id, delete_files=True, move_to_trash=True, deleted_by=username)
                        results.append({'id': num_id, 'success': d_res.get('success', False), 'message': 'Dihapus'})
                    elif action == 'revalidate':
                        it = library_db.get_library_item(num_id)
                        if it:
                            f_path = (config.LIBRARY_ROOT / it.get('folder_path', '')).resolve()
                            v_files = [p for p in f_path.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')] if f_path.exists() else []
                            if v_files:
                                pb = run_ffprobe(v_files[0])
                                if pb.get('valid'):
                                    library_db.update_item_status(num_id, 'ready')
                                    results.append({'id': num_id, 'success': True, 'message': 'Valid'})
                                    continue
                        results.append({'id': num_id, 'success': False, 'message': 'Media tidak valid'})
                    else:
                        results.append({'id': num_id, 'success': False, 'message': f"Aksi '{action}' tidak dikenal"})
                except Exception as e:
                    results.append({'id': i_id, 'success': False, 'message': str(e)})

            audit.log_admin_action(username=username, action=f"library.bulk_{action}", details={'total': len(item_ids), 'results': results}, ip_address=ip, user_id=session['user_id'])
            AdminRouter._send_json(handler, 200, {'ok': True, 'results': results})

        # 17. Update Setting
        elif path == '/api/admin/settings/update':
            data = AdminRouter._read_json_body(handler)
            key = str(data.get('key') or '')
            val = data.get('value')
            ok, msg, parsed = settings.update_setting(key, val, username=username, ip_address=ip)
            code = 200 if ok else 400
            AdminRouter._send_json(handler, code, {'ok': ok, 'message': msg, 'value': parsed})

        # 18. Revalidate / Index item
        elif path == '/api/admin/library/revalidate':
            data = AdminRouter._read_json_body(handler)
            item_id = int(data.get('item_id') or 0)
            item = library_db.get_library_item(item_id)
            if not item:
                return AdminRouter._send_json(handler, 404, {'ok': False, 'error': 'Item tidak ditemukan'})
            folder = (config.LIBRARY_ROOT / item.get('folder_path', '')).resolve()
            video_files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mkv', '.webm', '.mov', '.avi')] if folder.exists() else []
            if not video_files:
                return AdminRouter._send_json(handler, 400, {'ok': False, 'error': 'Berkas video tidak ditemukan di direktori'})

            primary_video = sorted(video_files, key=lambda f: f.stat().st_size, reverse=True)[0]
            probe = run_ffprobe(primary_video)
            if probe.get('valid') and probe.get('has_video'):
                library_db.update_item_status(item_id, 'ready')
                audit.log_admin_action(username=username, action="library.revalidate", resource_type="library_item", resource_id=str(item_id), ip_address=ip, user_id=session['user_id'])
                AdminRouter._send_json(handler, 200, {'ok': True, 'message': f"Item '{item['title']}' tervalidasi dan siap ditonton!"})
            else:
                AdminRouter._send_json(handler, 400, {'ok': False, 'error': f"Validasi gagal: {probe.get('error', 'Tidak memiliki stream video valid')}"})

        else:
            AdminRouter._send_json(handler, 404, {'error': 'Endpoint admin tidak ditemukan'})
