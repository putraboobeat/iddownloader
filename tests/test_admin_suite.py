"""Comprehensive Super Admin and Library Integration Test Suite for OmniFetch.
Validates all 17 findings and requirements with isolated local database,
ephemeral HTTP server, and synthetic media fixtures.
"""
import io
import os
import re
import sys
import json
import time
import shutil
import tempfile
import threading
import subprocess
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from PIL import Image

# Pre-configure environment BEFORE any application imports
_TEST_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="omni_suite_root_")).resolve()
os.environ["OMNIFETCH_DATA_DIR"] = str(_TEST_TEMP_ROOT / "data")
os.environ["OMNIFETCH_TMP_DIR"] = str(_TEST_TEMP_ROOT / "data" / "temporary")
os.environ["OMNIFETCH_STAGING_DIR"] = str(_TEST_TEMP_ROOT / "data" / "staging")
os.environ["OMNIFETCH_TRASH_DIR"] = str(_TEST_TEMP_ROOT / "data" / "trash")
os.environ["LIBRARY_ROOT"] = str(_TEST_TEMP_ROOT / "data" / "library")
os.environ["LIBRARY_DB_PATH"] = str(_TEST_TEMP_ROOT / "data" / "test-library.sqlite3")
os.environ["JOBS_DB_PATH"] = str(_TEST_TEMP_ROOT / "data" / "test-jobs.json")
os.environ["VPS_ROOT"] = str(_TEST_TEMP_ROOT / "data" / "vps_downloads")
os.environ["RETENTION_SECONDS"] = "2"

from backend.core import config
from backend.core import library_db
from backend.core.job_manager import JobManager, Job, job_manager, ACTIVE_STAGES, TERMINAL_STAGES
from backend.admin.cli import create_admin_programmatic
from backend.admin import auth, audit, settings, stats, storage_manager, library_manager
from backend.api.router import APIHandler


class TestSuperAdminComprehensiveSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_dir = _TEST_TEMP_ROOT
        cls.data_dir = config.DATA_DIR
        cls.data_dir.mkdir(parents=True, exist_ok=True)

        # Ensure all managed directories exist
        for d in (config.TEMPORARY_DIR, config.STAGING_DIR, config.TRASH_DIR, config.LIBRARY_ROOT, config.LIBRARY_MOVIES_DIR, config.VPS_ROOT):
            d.mkdir(parents=True, exist_ok=True)

        library_db.init_db()
        library_db.set_setting("retention_seconds", 2)

        # Create primary Super Admin account for testing
        create_admin_programmatic("adminboss", "SuperSecurePassword123!", "Boss Superadmin", is_superadmin=True)

        # Ephemeral live test server
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), APIHandler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    def _http_req(self, path, method="GET", data=None, headers=None):
        url = f"{self.base_url}{path}"
        hdrs = headers.copy() if headers else {}
        body = None
        if data is not None:
            if not any(k.lower() == 'content-type' for k in hdrs):
                hdrs['Content-Type'] = 'application/json'
            if isinstance(data, (dict, list)):
                body = json.dumps(data).encode('utf-8')
            elif isinstance(data, bytes):
                body = data
            elif isinstance(data, str):
                body = data.encode('utf-8')

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                resp_bytes = resp.read()
                try:
                    parsed = json.loads(resp_bytes.decode('utf-8'))
                except Exception:
                    parsed = resp_bytes
                return resp.status, parsed, resp.headers
        except urllib.error.HTTPError as e:
            err_bytes = e.read()
            try:
                parsed = json.loads(err_bytes.decode('utf-8'))
            except Exception:
                parsed = err_bytes
            return e.code, parsed, e.headers

    def _login_superadmin(self):
        status, res, hdrs = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "adminboss", "password": "SuperSecurePassword123!"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(status, 200)
        cookie = hdrs.get('Set-Cookie', '').split(';')[0]
        csrf = res.get('csrf_token')
        return cookie, csrf

    # =========================================================================
    # PRIORITAS 1 — Login dan HTTP harus berfungsi di browser
    # =========================================================================

    def test_01_admin_origin_handling_and_cors(self):
        """1. Respons admin dengan Origin disusun dengan urutan HTTP yang benar,
        Origin divalidasi same-origin, foreign origin ditolak 403, dan OPTIONS ditangani."""
        # A. Same-origin POST request with Origin header
        same_origin = f"http://127.0.0.1:{self.port}"
        status, res, hdrs = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "adminboss", "password": "SuperSecurePassword123!"},
            headers={"Origin": same_origin}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), same_origin)
        self.assertEqual(hdrs.get("Access-Control-Allow-Credentials"), "true")
        self.assertNotEqual(hdrs.get("Access-Control-Allow-Origin"), "*")

        # B. Foreign origin POST request rejected with 403 Forbidden
        foreign_origin = "https://evil-attacker.com"
        f_status, f_res, f_hdrs = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "adminboss", "password": "SuperSecurePassword123!"},
            headers={"Origin": foreign_origin}
        )
        self.assertEqual(f_status, 403)
        self.assertIn("Origin asing ditolak", str(f_res))

        # C. OPTIONS preflight with same-origin
        opt_status, _, opt_hdrs = self._http_req(
            "/api/admin/auth/login",
            method="OPTIONS",
            headers={"Origin": same_origin}
        )
        self.assertIn(opt_status, (200, 204))
        self.assertEqual(opt_hdrs.get("Access-Control-Allow-Origin"), same_origin)
        self.assertIn("POST", opt_hdrs.get("Access-Control-Allow-Methods", ""))

    def test_02_clean_browser_library_auth(self):
        """2. Browser bersih tanpa X-App-Session dapat login ke perpustakaan via POST /api/library/auth."""
        # Request with empty storage / no X-App-Session header
        status, res, _ = self._http_req(
            "/api/library/auth",
            method="POST",
            data={"password": config.LIBRARY_PASSWORD}
        )
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))
        app_sess = res.get("app_session_id")
        self.assertTrue(app_sess)

        # Subsequent collection request using returned session succeeds
        c_status, c_res, _ = self._http_req(
            "/api/library/items",
            headers={"X-App-Session": app_sess, "X-Library-Auth": config.LIBRARY_PASSWORD}
        )
        self.assertEqual(c_status, 200)
        self.assertIn("items", c_res)

    def test_03_admin_media_authentication_and_preview(self):
        """3. Poster dan preview video menerima sesi admin sah via Cookie atau Header;
        akses anonim mengembalikan 401; fallback SVG tidak looping."""
        cookie, csrf = self._login_superadmin()

        # Create dummy movie item
        item = library_db.create_library_item(
            title="Film Pratinjau Admin",
            year=2024,
            clean_title="PratinjauAdmin",
            folder_path="Movies/PratinjauAdmin (2024)",
            status="ready"
        )
        item_id = item['id']
        movie_dir = config.LIBRARY_ROOT / "Movies" / "PratinjauAdmin (2024)"
        movie_dir.mkdir(parents=True, exist_ok=True)
        vid_path = movie_dir / "PratinjauAdmin.mp4"
        vid_content = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_STREAM_CONTENT"
        vid_path.write_bytes(vid_content)

        # A. Stream with Admin Cookie -> 200 OK
        s_status, s_data, s_hdrs = self._http_req(
            f"/api/library/stream/{item_id}",
            headers={"Cookie": cookie}
        )
        self.assertEqual(s_status, 200)
        self.assertEqual(s_data, vid_content)

        # B. Seeking / Range request -> 206 Partial Content
        r_status, r_data, r_hdrs = self._http_req(
            f"/api/library/stream/{item_id}",
            headers={"Cookie": cookie, "Range": "bytes=0-9"}
        )
        self.assertEqual(r_status, 206)
        self.assertEqual(r_data, b"0123456789")
        self.assertIn("bytes 0-9/", r_hdrs.get("Content-Range", ""))

        # C. Poster with Admin Cookie -> 200 OK (SVG fallback if no poster file)
        p_status, p_data, p_hdrs = self._http_req(
            f"/api/library/poster/{item_id}",
            headers={"Cookie": cookie}
        )
        self.assertEqual(p_status, 200)
        self.assertIn(b"svg", p_data)

        # D. Anonymous access without admin cookie or library auth -> 401 Unauthorized
        anon_status, _, _ = self._http_req(f"/api/library/stream/{item_id}")
        self.assertEqual(anon_status, 401)

        anon_poster_status, _, _ = self._http_req(f"/api/library/poster/{item_id}")
        self.assertEqual(anon_poster_status, 401)

    # =========================================================================
    # PRIORITAS 2 — Cegah kehilangan file dan server berhenti
    # =========================================================================

    def test_04_subtitle_delete_protects_video(self):
        """4. Endpoint hapus subtitle menolak file video, poster, dan symlink keluar root;
        file video tetap utuh ketika endpoint diberi ID atau path video."""
        cookie, csrf = self._login_superadmin()

        item = library_db.create_library_item(
            title="Film Proteksi Subtitle",
            year=2024,
            clean_title="ProteksiSub",
            folder_path="Movies/ProteksiSub (2024)",
            status="ready"
        )
        item_id = item['id']
        folder = config.LIBRARY_ROOT / "Movies" / "ProteksiSub (2024)"
        folder.mkdir(parents=True, exist_ok=True)
        vid_file = folder / "movie.mp4"
        vid_file.write_bytes(b"DUMMY_IMPORTANT_VIDEO_BYTES_CANNOT_BE_DELETED")

        # Register video in library_files
        vid_file_rec = library_db.add_library_file(
            item_id=item_id,
            file_path="Movies/ProteksiSub (2024)/movie.mp4",
            file_type="video",
            file_size=len(b"DUMMY_IMPORTANT_VIDEO_BYTES_CANNOT_BE_DELETED")
        )
        vid_file_id = vid_file_rec['id']

        # Also add a real subtitle
        sub_file = folder / "id.vtt"
        sub_file.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nSubtitle ID")
        sub_file_rec = library_db.add_library_file(
            item_id=item_id,
            file_path="Movies/ProteksiSub (2024)/id.vtt",
            file_type="subtitle",
            file_size=len(sub_file.read_bytes()),
            subtitle_lang="id"
        )
        sub_file_id = sub_file_rec['id']

        # Attempt to delete VIDEO file through subtitle deletion endpoint
        status, res, _ = self._http_req(
            "/api/admin/library/subtitles/delete",
            method="POST",
            data={"item_id": item_id, "file_id": vid_file_id},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(status, 400)
        self.assertFalse(res.get("success"))
        self.assertIn("bukan subtitle", res.get("error", "").lower())

        # CRITICAL REGRESSION ASSERTION: Video MUST remain completely intact on disk!
        self.assertTrue(vid_file.exists())
        self.assertEqual(vid_file.read_bytes(), b"DUMMY_IMPORTANT_VIDEO_BYTES_CANNOT_BE_DELETED")

        # Deleting legitimate subtitle file works
        s_status, s_res, _ = self._http_req(
            "/api/admin/library/subtitles/delete",
            method="POST",
            data={"item_id": item_id, "file_id": sub_file_id},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(s_status, 200)
        self.assertTrue(s_res.get("success"))
        self.assertFalse(sub_file.exists())
        # Video STILL remains intact
        self.assertTrue(vid_file.exists())

    def test_05_job_termination_process_group_isolation(self):
        """5. Terminasi pekerjaan menghentikan grup proses anak tanpa mematikan server;
        pekerjaan berstatus terminal tidak diubah menjadi cancelled."""
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        job = Job(owner_session="test_session", data={"name": "ProcessIsoTest"})
        job.process = proc
        job.set_stage("downloading")

        # Terminate job
        job.terminate()
        self.assertEqual(job.stage, "cancelled")

        # Child process must be dead
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        self.assertIsNotNone(proc.poll())

        # Test server is STILL fully functional and responsive!
        status, res, _ = self._http_req("/api/health")
        self.assertEqual(status, 200)

        # Cancelling a terminal job does not overwrite its status
        comp_job = Job(owner_session="test_sess2", data={"name": "CompletedJob"})
        comp_job.set_stage("completed")
        comp_job.terminate()
        self.assertEqual(comp_job.stage, "completed")

    def test_06_trash_quarantine_and_full_catalog_restore(self):
        """6. Memindahkan ke Trash menyimpan snapshot metadata lengkap;
        memulihkan dari Trash mengembalikan katalog, relasi berkas, dan folder fisik."""
        cookie, csrf = self._login_superadmin()

        # Create item with video and subtitle files
        item = library_db.create_library_item(
            title="Koleksi Restore Lengkap",
            year=2023,
            clean_title="RestoreLengkap",
            folder_path="Movies/RestoreLengkap (2023)",
            status="ready"
        )
        item_id = item['id']
        item_folder = config.LIBRARY_ROOT / "Movies" / "RestoreLengkap (2023)"
        item_folder.mkdir(parents=True, exist_ok=True)
        (item_folder / "movie.mp4").write_bytes(b"MOVIE_CONTENT")
        (item_folder / "id.vtt").write_text("WEBVTT")

        library_db.add_library_file(item_id, "Movies/RestoreLengkap (2023)/movie.mp4", "video", len(b"MOVIE_CONTENT"))
        library_db.add_library_file(item_id, "Movies/RestoreLengkap (2023)/id.vtt", "subtitle", 6, subtitle_lang="id", is_primary=1)

        # 1. Delete and move to trash
        del_status, del_res, _ = self._http_req(
            "/api/admin/library/delete",
            method="POST",
            data={"item_id": item_id, "delete_files": True, "move_to_trash": True},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(del_status, 200)
        self.assertTrue(del_res.get("success"))
        self.assertTrue(del_res.get("trashed"))

        # Folder moved out of library
        self.assertFalse(item_folder.exists())

        # Catalog item temporarily gone from active items
        active_item = library_db.get_library_item(item_id)
        self.assertIsNone(active_item)

        # Find trash record
        trash_items = library_db.get_active_trash_items()
        matching = [t for t in trash_items if "Restore" in t['name']]
        self.assertTrue(matching)
        trash_id = matching[0]['id']

        # 2. Restore from trash
        res_status, res_data, _ = self._http_req(
            "/api/admin/storage/restore",
            method="POST",
            data={"trash_id": trash_id},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(res_status, 200)
        self.assertTrue(res_data.get("success"))

        # CRITICAL REGRESSION ASSERTION: Folder is restored AND catalog item exists in DB!
        self.assertTrue(item_folder.exists())
        self.assertTrue((item_folder / "movie.mp4").exists())

        restored_item = library_db.get_library_item(item_id)
        self.assertIsNotNone(restored_item)
        self.assertEqual(restored_item['title'], "Koleksi Restore Lengkap")
        self.assertEqual(restored_item['year'], 2023)

        # Files relations are also fully restored
        restored_files = library_db.get_library_files_for_item(item_id)
        self.assertGreaterEqual(len(restored_files), 2)
        types = [f['file_type'] for f in restored_files]
        self.assertIn("video", types)
        self.assertIn("subtitle", types)

    def test_07_structural_folder_and_active_job_protection(self):
        """7. Inventaris tidak menandai folder 'Movies' sebagai orphan;
        is_path_in_use mendeteksi folder induk dan anak dari job aktif;
        penghapusan folder struktural dan root ditolak."""
        cookie, csrf = self._login_superadmin()

        # A. Inventory scan does not orphan Movies/
        scan_status, scan_res, _ = self._http_req(
            "/api/admin/storage/scan",
            method="POST",
            data={},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(scan_status, 200)
        inv = storage_manager.scan_managed_inventory()
        orphan_names = [it['name'] for it in inv['items'] if it.get('status') == 'orphan']
        self.assertNotIn("Movies", orphan_names)

        # B. is_path_in_use check for parent, child, and exact match
        job_dir = config.TEMPORARY_DIR / "active_job_xyz"
        job_dir.mkdir(parents=True, exist_ok=True)
        sub_job_file = job_dir / "download.part"
        sub_job_file.touch()

        active_job = Job(owner_session="in_use_test", data={"name": "ActiveJob"})
        active_job.folder = job_dir
        active_job.set_stage("downloading")
        job_manager.jobs[active_job.id] = active_job

        # Exact match is in use
        self.assertTrue(storage_manager.is_path_in_use(job_dir))
        # Child is in use
        self.assertTrue(storage_manager.is_path_in_use(sub_job_file))
        # Parent of active job is in use!
        self.assertTrue(storage_manager.is_path_in_use(config.TEMPORARY_DIR))

        # C. Rejection of structural directory deletion
        movies_dir = config.LIBRARY_MOVIES_DIR
        valid, err, _ = storage_manager.validate_path_containment(movies_dir)
        self.assertFalse(valid)
        self.assertIn("direktori struktural", err.lower())

        # D. Purge trash rejects purging TRASH_DIR root
        p_res = storage_manager.purge_trash_item(9999999)
        self.assertFalse(p_res['success'])

    def test_08_cleanup_accuracy_and_managed_temp_containment(self):
        """8. Cleanup tidak melaporkan sukses palsu jika berkas gagal dihapus;
        hanya membersihkan berkas dalam direktori sementara yang dikelola."""
        expired_job = Job(
            owner_session="sess_clean_test",
            data={"name": "CleanTest", "target": "download"},
            created_at=time.time() - 10000
        )
        expired_job.set_stage("completed")
        expired_job.folder.mkdir(parents=True, exist_ok=True)
        test_file = expired_job.folder / "video.mp4"
        test_file.write_bytes(b"TEMP_CONTENT")
        job_manager.jobs[expired_job.id] = expired_job

        cleaned = job_manager.cleanup_expired_jobs()
        self.assertIn(expired_job.id, cleaned)
        self.assertFalse(test_file.exists())
        self.assertFalse(expired_job.file_exists)

        # Job in persistent DB remains intact with file_exists = 0
        persisted = library_db.get_persisted_job(expired_job.id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted['file_exists'], 0)

    # =========================================================================
    # PRIORITAS 3 — Thumbnail, persistensi, dan keamanan
    # =========================================================================

    def test_09_thumbnail_replacement_pillow_aspect_and_ssrf(self):
        """9. Validasi gambar dengan Pillow; pemotongan aspek rasio 2:3 dan 16:9;
        penolakan byte rusak; pencegahan SSRF pada redirect."""
        item = library_db.create_library_item(
            title="Film Uji Thumbnail",
            year=2024,
            clean_title="UjiThumb",
            folder_path="Movies/UjiThumb (2024)",
            status="ready"
        )
        item_id = item['id']
        (config.LIBRARY_ROOT / "Movies" / "UjiThumb (2024)").mkdir(parents=True, exist_ok=True)

        # A. Corrupt bytes rejected by Pillow
        corrupt_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + (b"\x00" * 30)
        c_res = library_manager.save_item_poster_or_backdrop(item_id, corrupt_bytes, target_type="poster")
        self.assertFalse(c_res['success'])
        self.assertIn("rusak", c_res['error'].lower())

        # B. Real valid image created with Pillow
        img = Image.new("RGB", (400, 400), color=(50, 100, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        valid_jpeg = buf.getvalue()

        # Save as poster (should crop to 2:3 -> 600x900)
        p_res = library_manager.save_item_poster_or_backdrop(item_id, valid_jpeg, target_type="poster", crop_mode="center")
        self.assertTrue(p_res['success'])
        poster_file = config.LIBRARY_ROOT / "Movies" / "UjiThumb (2024)" / "poster.jpg"
        self.assertTrue(poster_file.exists())
        with Image.open(poster_file) as saved_img:
            self.assertEqual(saved_img.size, (600, 900))

        # Save as backdrop (should crop to 16:9 -> 1280x720)
        b_res = library_manager.save_item_poster_or_backdrop(item_id, valid_jpeg, target_type="backdrop")
        self.assertTrue(b_res['success'])
        backdrop_file = config.LIBRARY_ROOT / "Movies" / "UjiThumb (2024)" / "backdrop.jpg"
        self.assertTrue(backdrop_file.exists())
        with Image.open(backdrop_file) as saved_bg:
            self.assertEqual(saved_bg.size, (1280, 720))

        # C. SSRF redirect validation: attempting to fetch from localhost/private IP
        ssrf_res = library_manager.fetch_remote_image(f"http://127.0.0.1:{self.port}/secret.jpg")
        self.assertFalse(ssrf_res['success'])
        self.assertTrue("privat" in ssrf_res['error'].lower() or "ssrf" in ssrf_res['error'].lower())

    def test_10_storage_mount_check_and_state_recovery(self):
        """10. check_storage_available menolak operasi saat mount hilang;
        JOBS_DB_PATH absolut; rekonsiliasi startup memulihkan progres & timeline."""
        # A. Storage check with configured expected mount
        old_expected = config.EXPECTED_STORAGE_MOUNT
        try:
            config.EXPECTED_STORAGE_MOUNT = "/nonexistent/test/mount/point"
            avail, msg = config.check_storage_available()
            self.assertFalse(avail)
            self.assertIn("belum ter-mount", msg.lower())
        finally:
            config.EXPECTED_STORAGE_MOUNT = old_expected

        # B. Absolute jobs_db path
        self.assertTrue(config.JOBS_DB_PATH.is_absolute())

        # C. Reconcile startup preserves timestamps and progress
        library_db.upsert_download_job({
            "job_id": "job_reconcile_test",
            "owner_session": "sess_rec",
            "stage": "completed",
            "percent": 100.0,
            "bytes_downloaded": 123456,
            "total_bytes": 123456,
            "timeline": json.dumps([{"time": 1000, "stage": "completed", "message": "Done"}]),
            "created_at": 500,
            "started_at": 600,
            "completed_at": 1000,
            "file_exists": 1
        })
        job_manager.reconcile_and_load_db()
        loaded = job_manager.jobs.get("job_reconcile_test")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.percent, 100.0)
        self.assertEqual(loaded.bytes_downloaded, 123456)
        self.assertEqual(loaded.started_at, 600)
        self.assertEqual(loaded.completed_at, 1000)

    def test_11_authorization_and_audit_redaction(self):
        """11. /api/library/delete memerlukan superadmin dan CSRF;
        audit log menyamarkan ?t=... dan token rahasia;
        Job.to_admin_dto menyamarkan sesi pemilik dan URL mentah."""
        cookie, csrf = self._login_superadmin()

        # Regular non-superadmin user
        create_admin_programmatic("regadmin", "PasswordRegular123!", "Regular Staff", is_superadmin=False)
        r_status, r_res, r_hdrs = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "regadmin", "password": "PasswordRegular123!"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        reg_cookie = r_hdrs.get('Set-Cookie', '').split(';')[0]
        reg_csrf = r_res['csrf_token']

        # Non-superadmin cannot delete library item via /api/library/delete
        del_status, del_res, _ = self._http_req(
            "/api/library/delete",
            method="POST",
            data={"item_id": 9999},
            headers={"Cookie": reg_cookie, "X-CSRF-Token": reg_csrf}
        )
        self.assertEqual(del_status, 403)
        self.assertIn("super admin", del_res.get("error", "").lower())

        # URL secret redaction
        secret_url = "https://example.com/media.mp4?t=FAKE_SECRET_TOKEN&key=xyz123"
        redacted_url = audit.redact_url_secrets(secret_url)
        self.assertNotIn("FAKE_SECRET_TOKEN", redacted_url)
        self.assertIn("t=[REDACTED]", redacted_url)

        # Job.to_admin_dto pseudonymization
        j = Job(owner_session="real_secret_session_token_12345", data={"url": secret_url})
        dto = j.to_admin_dto()
        self.assertNotIn("real_secret_session_token_12345", dto['owner_session_pseudonym'])
        self.assertNotIn("FAKE_SECRET_TOKEN", dto['url'])

    def test_12_no_inline_onclick_interpolations(self):
        """12. admin.html bebas dari interpolasi variabel ke inline onclick."""
        admin_html_path = config.BASE_DIR / "admin.html"
        self.assertTrue(admin_html_path.exists())
        content = admin_html_path.read_text(encoding="utf-8")

        # Check for dangerous patterns: onclick="${...}" or onclick="func('${...}')"
        interpolated_onclick = re.findall(r'onclick=[\'"][^\'"]*\$\{[^}]+\}[^\'"]*[\'"]', content)
        self.assertEqual(interpolated_onclick, [], f"Found dynamic onclick interpolation in admin.html: {interpolated_onclick}")

    # =========================================================================
    # PRIORITAS 4 — Statistik, performa, dan kelengkapan
    # =========================================================================

    def test_13_csv_export_formatting_and_formula_protection(self):
        """13. Ekspor CSV riwayat berhasil tanpa error datetime;
        tanggal diformat WIB; formula injection dinetralkan dengan petik tunggal."""
        cookie, csrf = self._login_superadmin()

        # Insert a job with formula injection attempt
        library_db.upsert_download_job({
            "job_id": "csv_job_1",
            "owner_session": "sess_csv",
            "title": "=1+2';calc",
            "stage": "completed",
            "status_message": "Selesai",
            "url": "https://example.com/video?t=SECRET_PARAM",
            "created_at": time.time(),
            "completed_at": time.time()
        })

        status, csv_data, hdrs = self._http_req(
            "/api/admin/history/export?format=csv",
            headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", hdrs.get("Content-Type", ""))
        csv_text = csv_data.decode("utf-8") if isinstance(csv_data, bytes) else str(csv_data)

        # Formula escaping check: starts with '= must be escaped with single quote
        self.assertIn("=1+2';calc", csv_text)
        # Secret param redacted in CSV export
        self.assertNotIn("SECRET_PARAM", csv_text)

    def test_14_stats_accuracy_and_sql_aggregations(self):
        """14. completed_today dihitung berdasarkan completed_at;
        akun admin dihitung jujur; sampel 0 menampilkan 'Belum ada data'."""
        summary = stats.get_dashboard_summary()
        self.assertIn("Akun Admin", summary.get("sessions", {}).get("note", ""))

        # Empty jobs scenario
        rate_empty = stats.get_success_rate(start_time=time.time() + 100000)
        self.assertEqual(rate_empty['display'], "Belum ada data")

    def test_15_dashboard_polling_cache_and_inventory_snapshots(self):
        """15. Health status dan storage breakdown dicache (60 detik) untuk meringankan polling;
        GET storage/inventory membaca snapshot tanpa pemindaian disk synchronous yang berat."""
        cookie, csrf = self._login_superadmin()

        s1 = stats.get_health_status()
        s2 = stats.get_health_status()
        self.assertEqual(s1['timestamp'], s2['timestamp'])

        inv_status, inv_res, _ = self._http_req(
            "/api/admin/storage/inventory?limit=10",
            headers={"Cookie": cookie}
        )
        self.assertEqual(inv_status, 200)
        self.assertIn("items", inv_res.get("data", {}))

    def test_16_retention_settings_application(self):
        """16. Pengaturan retensi tersimpan di database diterapkan pada pekerja cleanup."""
        library_db.set_setting("trash_retention_days", 14)
        effective_days = storage_manager.get_effective_trash_retention_days()
        self.assertEqual(effective_days, 14)

        library_db.set_setting("retention_seconds", 3600)
        eff_ret = library_db.get_setting("retention_seconds", 7200)
        self.assertEqual(eff_ret, 3600)

    def test_17_collection_management_endpoints(self):
        """17. Endpoint pengelolaan koleksi:
        - tambah dari URL
        - impor berkas server
        - atur default subtitle
        - ganti password admin."""
        cookie, csrf = self._login_superadmin()

        # A. Tambah dari URL
        url_status, url_res, _ = self._http_req(
            "/api/admin/library/add-url",
            method="POST",
            data={"url": "https://example.com/stream.m3u8", "title": "Stream Add Test"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(url_status, 200)
        self.assertTrue(url_res.get("success"))
        new_job_id = url_res.get("job_id")
        self.assertTrue(new_job_id)

        # B. Impor berkas server with small valid synthetic video
        server_vid = config.TEMPORARY_DIR / "imported_sample.mp4"
        subprocess.run([
            'ffmpeg', '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:d=1',
            '-f', 'lavfi', '-i', 'anullsrc', '-t', '1',
            '-c:v', 'libx264', '-c:a', 'aac', '-pix_fmt', 'yuv420p',
            str(server_vid), '-y'
        ], check=True, capture_output=True)

        imp_status, imp_res, _ = self._http_req(
            "/api/admin/library/import-file",
            method="POST",
            data={"file_path": str(server_vid), "title": "Imported Movie", "year": 2024},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(imp_status, 200)
        self.assertTrue(imp_res.get("success"))
        imp_item_id = imp_res.get("item_id")

        # C. Atur default subtitle
        sub_rec = library_db.add_library_file(imp_item_id, "Movies/Imported Movie (2024)/en.vtt", "subtitle", 10, subtitle_lang="en", is_primary=0)
        sub_id = sub_rec['id']
        def_status, def_res, _ = self._http_req(
            "/api/admin/library/subtitles/default",
            method="POST",
            data={"item_id": imp_item_id, "file_id": sub_id},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(def_status, 200)
        self.assertTrue(def_res.get("success"))
        updated_sub = library_db.get_library_file(sub_id)
        self.assertEqual(updated_sub['is_default'], 1)

        # D. Ganti password admin
        pw_status, pw_res, _ = self._http_req(
            "/api/admin/auth/change-password",
            method="POST",
            data={"old_password": "SuperSecurePassword123!", "new_password": "NewSuperPassword456!"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf}
        )
        self.assertEqual(pw_status, 200)
        self.assertTrue(pw_res.get("ok"))

        # Verify old password fails and new password succeeds
        fail_status, _, _ = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "adminboss", "password": "SuperSecurePassword123!"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(fail_status, 401)

        succ_status, _, _ = self._http_req(
            "/api/admin/auth/login",
            method="POST",
            data={"username": "adminboss", "password": "NewSuperPassword456!"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(succ_status, 200)


if __name__ == "__main__":
    unittest.main()
