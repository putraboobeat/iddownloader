import unittest
import os
import shutil
import tempfile
import sqlite3
import json
import time
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error

# Module imports
from backend.core import config
from backend.core import library_db
from backend.core.library_db import (
    init_db,
    get_connection,
    normalize_source_key,
    create_library_item,
    find_item_by_source_key,
    find_item_by_title_year,
    update_item_status,
    add_library_file,
    get_library_item,
    list_library_items,
    delete_library_item,
    create_or_get_library_job,
    get_job_by_idempotency_key,
    get_library_job,
    update_library_job,
)
from backend.core.importer import (
    sanitize_title,
    parse_year_from_title,
    run_ffprobe,
    validate_subtitle_file,
    convert_vtt_to_srt_file,
    check_disk_space_sufficient,
    atomic_transfer_file,
    LibraryImporter,
)
from backend.core.jellyfin import JellyfinClient
from backend.core.job_manager import JobManager, Job
from backend.api.router import APIHandler


class MockJellyfinHandler(BaseHTTPRequestHandler):
    """Mock server simulating official Jellyfin REST API responses."""
    api_key_valid = True
    simulate_delay = 0

    def log_message(self, format, *args):
        pass  # Quiet logging during tests

    def do_GET(self):
        if self.simulate_delay > 0:
            time.sleep(self.simulate_delay)

        auth_header = self.headers.get("X-Emby-Token", "")
        if not auth_header and "MediaBrowser Client=" in self.headers.get("Authorization", ""):
            auth_header = "present"

        if self.path == "/System/Info/Public":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ServerName": "Jellyfin-TestServer",
                "Version": "10.9.11",
                "Id": "test-system-id-12345"
            }).encode())
            return

        if self.path == "/System/Info":
            if not self.api_key_valid or not auth_header:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message": "Unauthorized"}')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ServerName": "Jellyfin-TestServer",
                "Version": "10.9.11",
                "Id": "test-system-id-12345"
            }).encode())
            return

        if self.path.startswith("/Items?"):
            if not self.api_key_valid:
                self.send_response(401)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            items_payload = {
                "Items": [
                    {
                        "Id": "mock-jellyfin-item-777",
                        "Name": "Keluarga Cemara",
                        "ProductionYear": 2019,
                        "Type": "Movie",
                        "Path": "/media/Movies/Keluarga Cemara (2019)/Keluarga Cemara (2019).mp4"
                    }
                ],
                "TotalRecordCount": 1
            }
            self.wfile.write(json.dumps(items_payload).encode())
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        auth_header = self.headers.get("X-Emby-Token", "")
        if not self.api_key_valid or not auth_header:
            self.send_response(401)
            self.end_headers()
            return

        if "/Refresh" in self.path:
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


class TestPersonalLibrarySuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Setup temporary directories for tests
        cls.test_dir = tempfile.mkdtemp(prefix="omnifetch_test_")
        cls.orig_data_dir = config.DATA_DIR
        cls.orig_temp_dir = config.TEMPORARY_DIR
        cls.orig_staging_dir = config.STAGING_DIR
        cls.orig_library_root = config.LIBRARY_ROOT
        cls.orig_library_movies_dir = config.LIBRARY_MOVIES_DIR
        cls.orig_library_db_path = config.LIBRARY_DB_PATH

        config.DATA_DIR = Path(cls.test_dir) / "data"
        config.TEMPORARY_DIR = config.DATA_DIR / "temporary"
        config.STAGING_DIR = config.DATA_DIR / "staging"
        config.LIBRARY_ROOT = config.DATA_DIR / "library"
        config.LIBRARY_MOVIES_DIR = config.LIBRARY_ROOT / "Movies"
        config.LIBRARY_DB_PATH = config.DATA_DIR / "omnifetch-library.sqlite3"

        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.TEMPORARY_DIR.mkdir(parents=True, exist_ok=True)
        config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        config.LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
        config.LIBRARY_MOVIES_DIR.mkdir(parents=True, exist_ok=True)

        # Start Mock Jellyfin HTTP Server
        cls.mock_jellyfin_server = ThreadingHTTPServer(("127.0.0.1", 0), MockJellyfinHandler)
        cls.mock_jellyfin_port = cls.mock_jellyfin_server.server_port
        cls.mock_jellyfin_thread = threading.Thread(target=cls.mock_jellyfin_server.serve_forever)
        cls.mock_jellyfin_thread.daemon = True
        cls.mock_jellyfin_thread.start()

        # Configure test Jellyfin client settings
        config.JELLYFIN_INTERNAL_URL = f"http://127.0.0.1:{cls.mock_jellyfin_port}"
        config.JELLYFIN_PUBLIC_URL = f"http://127.0.0.1:{cls.mock_jellyfin_port}"
        config.JELLYFIN_API_KEY = "valid-test-key-12345"
        config.JELLYFIN_LIBRARY_ID = "mock-library-id-999"

        from backend.core.jellyfin import jellyfin_client
        jellyfin_client.internal_url = f"http://127.0.0.1:{cls.mock_jellyfin_port}"
        jellyfin_client.public_url = f"http://127.0.0.1:{cls.mock_jellyfin_port}"
        jellyfin_client.api_key = "valid-test-key-12345"
        jellyfin_client.library_id = "mock-library-id-999"

        # Start API server for endpoint integration tests
        cls.api_server = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        cls.api_port = cls.api_server.server_port
        cls.api_thread = threading.Thread(target=cls.api_server.serve_forever)
        cls.api_thread.daemon = True
        cls.api_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        # Stop servers
        cls.mock_jellyfin_server.shutdown()
        cls.mock_jellyfin_server.server_close()
        cls.mock_jellyfin_thread.join(timeout=2)

        cls.api_server.shutdown()
        cls.api_server.server_close()
        cls.api_thread.join(timeout=2)

        # Restore original config
        config.DATA_DIR = cls.orig_data_dir
        config.TEMPORARY_DIR = cls.orig_temp_dir
        config.STAGING_DIR = cls.orig_staging_dir
        config.LIBRARY_ROOT = cls.orig_library_root
        config.LIBRARY_MOVIES_DIR = cls.orig_library_movies_dir
        config.LIBRARY_DB_PATH = cls.orig_library_db_path

        # Cleanup test dir
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        init_db(config.LIBRARY_DB_PATH)
        MockJellyfinHandler.api_key_valid = True

    # =========================================================================
    # 1. DATABASE SCHEMA, MIGRATIONS & DEDUPLICATION TESTS
    # =========================================================================
    def test_database_initialization_and_wal(self):
        """Verifies database initializes with versioned migration and WAL mode."""
        with get_connection(config.LIBRARY_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            mode = cursor.fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

            cursor.execute("SELECT version FROM schema_migrations")
            versions = [row[0] for row in cursor.fetchall()]
            self.assertIn(1, versions)

    def test_normalize_source_key(self):
        """Tests stable source key normalization stripping volatile tokens while keeping path/ID."""
        url1 = "https://example.com/stream/movie123/master.m3u8?utm_source=fb&token=xyz123&t=456&sig=abcdef"
        url2 = "https://example.com/stream/movie123/master.m3u8?token=xyz123&t=456&sig=abcdef"
        self.assertEqual(normalize_source_key(url1), normalize_source_key(url2))

        # Different media path produces different key
        url3 = "https://example.com/stream/movie456/master.m3u8"
        self.assertNotEqual(normalize_source_key(url1), normalize_source_key(url3))

    def test_library_jobs_idempotency(self):
        """Tests that submitting the same idempotency_key resolves to the existing job."""
        job1 = create_or_get_library_job(
            job_id="test_job_1",
            owner_session="user_session_abc",
            stage="queued",
            target="library",
            idempotency_key="idemp_key_123"
        )
        self.assertEqual(job1["job_id"], "test_job_1")

        # Lookup by idempotency key
        found = get_job_by_idempotency_key("idemp_key_123", "user_session_abc")
        self.assertIsNotNone(found)
        self.assertEqual(found["job_id"], "test_job_1")

        # Second job with same ID returns existing
        job1_dup = create_or_get_library_job(
            job_id="test_job_1",
            owner_session="user_session_abc"
        )
        self.assertEqual(job1_dup["id"], job1["id"])

    # =========================================================================
    # 2. VALIDATION & IMPORTER TESTS (ffprobe, subtitles, atomicity)
    # =========================================================================
    def test_sanitize_title(self):
        """Verifies special characters illegal in Linux/Mac/Windows are sanitized."""
        dirty = 'Film: "Keluarga" / <Cemara>? *Special* | Edition!'
        clean = sanitize_title(dirty)
        self.assertNotIn(":", clean)
        self.assertNotIn("/", clean)
        self.assertNotIn("<", clean)
        self.assertNotIn(">", clean)
        self.assertNotIn("?", clean)
        self.assertNotIn("*", clean)
        self.assertNotIn("|", clean)
        self.assertNotIn('"', clean)

    def test_parse_year_from_title(self):
        """Tests parsing release year embedded in movie title."""
        title, year = parse_year_from_title("Laskar Pelangi (2008)")
        self.assertEqual(title, "Laskar Pelangi")
        self.assertEqual(year, 2008)

        title2, year2 = parse_year_from_title("Dilan 1990", explicit_year=2018)
        self.assertEqual(year2, 2018)

    def test_subtitle_vtt_to_srt_conversion(self):
        """Tests WebVTT to SubRip UTF-8 conversion."""
        vtt_file = Path(self.test_dir) / "test_sub.vtt"
        srt_file = Path(self.test_dir) / "test_sub.srt"

        vtt_content = """WEBVTT
Kind: captions
Language: id

00:00:01.000 --> 00:00:04.500
Halo, selamat datang di bioskop pribadi.

00:00:05.100 --> 00:00:08.200 line:90%
Nikmati tayangan favorit Anda dengan kualitas terbaik.
"""
        vtt_file.write_text(vtt_content, encoding="utf-8")
        ok = convert_vtt_to_srt_file(vtt_file, srt_file)
        self.assertTrue(ok)
        self.assertTrue(srt_file.exists())

        srt_content = srt_file.read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:04,500", srt_content)
        self.assertIn("Halo, selamat datang di bioskop pribadi.", srt_content)
        self.assertIn("00:00:05,100 --> 00:00:08,200", srt_content)
        self.assertNotIn("WEBVTT", srt_content)

    def test_html_error_response_detected_as_invalid_video(self):
        """Ensures HTML/JSON error responses saved with video extensions are caught and rejected."""
        fake_video = Path(self.test_dir) / "fake_video.mp4"
        fake_video.write_text("<!DOCTYPE html><html><body>404 Not Found Cloudflare</body></html>", encoding="utf-8")

        res = run_ffprobe(fake_video)
        self.assertFalse(res["valid"])
        self.assertIn("HTML", res["error"])

        fake_json = Path(self.test_dir) / "fake_json.mp4"
        fake_json.write_text('{"error": "Forbidden", "status": 403}', encoding="utf-8")
        res_json = run_ffprobe(fake_json)
        self.assertFalse(res_json["valid"])
        self.assertIn("JSON", res_json["error"])

    def test_disk_space_guard_enospc(self):
        """Tests disk space minimum safety check."""
        sufficient, free_gb, msg = check_disk_space_sufficient(config.LIBRARY_ROOT, required_bytes=100)
        self.assertTrue(sufficient)
        self.assertGreater(free_gb, 0)

        # Huge requirement (e.g. 50,000 GB) should fail
        huge_bytes = 50_000 * 1024 * 1024 * 1024
        sufficient_huge, free_gb_h, msg_huge = check_disk_space_sufficient(config.LIBRARY_ROOT, required_bytes=huge_bytes)
        self.assertFalse(sufficient_huge)
        self.assertIn("Ruang disk tidak cukup", msg_huge)

    def test_import_job_output_atomic_placement(self):
        """Tests atomic import of valid video and subtitle files into Jellyfin Movies folder structure."""
        job_staging_dir = Path(self.test_dir) / "staging_job_777"
        job_staging_dir.mkdir(parents=True, exist_ok=True)

        video_src = job_staging_dir / "movie_stream.mp4"
        sub_src = job_staging_dir / "movie_sub.vtt"

        # Generate a genuine tiny MP4 using ffmpeg for authentic container validation
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=0.5", "-c:v", "libx264", str(video_src)],
            check=True,
            capture_output=True
        )

        sub_src.write_text("""WEBVTT

00:00:01.000 --> 00:00:03.000
Subtitle Indonesia uji coba.
""", encoding="utf-8")

        import_res = LibraryImporter.import_job_output(
            job_id="job_777",
            owner_session="testsessionagent",
            job_folder=job_staging_dir,
            raw_title="Keluarga Cemara (2019)",
            explicit_year=2019,
            source_url="https://example.com/cemara.m3u8"
        )

        self.assertTrue(import_res["success"])
        item = import_res["item"]
        self.assertEqual(item["clean_title"], "Keluarga Cemara")
        self.assertEqual(item["year"], 2019)

        movie_folder = config.LIBRARY_ROOT / item["folder_path"]
        self.assertTrue(movie_folder.exists())
        self.assertTrue(movie_folder.is_dir())

        # Verify final names match Jellyfin guidelines:
        expected_video = movie_folder / "Keluarga Cemara (2019).mp4"
        expected_sub = movie_folder / "Keluarga Cemara (2019).id.srt"
        self.assertTrue(expected_video.exists())
        self.assertTrue(expected_sub.exists())

        # Verify subtitle was converted to SRT
        sub_content = expected_sub.read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:03,000", sub_content)

        # Verify staging files were safely transferred/cleaned
        self.assertFalse(video_src.exists())
        self.assertFalse(sub_src.exists())

    # =========================================================================
    # 3. JELLYFIN INTEGRATION CLIENT TESTS
    # =========================================================================
    def test_jellyfin_health_check_online_and_unauthorized(self):
        """Tests Jellyfin client health check for online success and invalid API key rejection."""
        client = JellyfinClient()
        health = client.check_health()
        self.assertTrue(health["configured"])
        self.assertTrue(health["healthy"])
        self.assertEqual(health["server_name"], "Jellyfin-TestServer")
        self.assertEqual(health["version"], "10.9.11")

        # Test invalid API key
        MockJellyfinHandler.api_key_valid = False
        health_invalid = client.check_health()
        self.assertFalse(health_invalid["healthy"])
        self.assertIn("401", health_invalid["error"])
        MockJellyfinHandler.api_key_valid = True

    def test_jellyfin_trigger_scan_and_indexing(self):
        """Tests triggering library scan and polling for item indexing."""
        client = JellyfinClient()
        scan_ok = client.trigger_scan(library_id=config.JELLYFIN_LIBRARY_ID)
        self.assertTrue(scan_ok)

        # Polling find item by title/year
        item = client.poll_item_indexing(
            title="Keluarga Cemara",
            year=2019,
            max_retries=2,
            initial_delay=0.05,
            backoff_factor=1.0
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.get("Id"), "mock-jellyfin-item-777")

    def test_jellyfin_public_playback_url_does_not_leak_admin_key(self):
        """Verifies public playback URL never includes the admin API key or secret tokens."""
        client = JellyfinClient()
        web_url = client.get_playback_web_url("mock-jellyfin-item-777")
        self.assertIn("mock-jellyfin-item-777", web_url)
        self.assertNotIn(config.JELLYFIN_API_KEY, web_url)
        self.assertNotIn("api_key", web_url.lower())
        self.assertNotIn("token", web_url.lower())

    # =========================================================================
    # 4. RETENTION CLEANUP IMMUNITY TEST
    # =========================================================================
    def test_retention_cleanup_never_touches_permanent_library(self):
        """Verifies that expired temporary download jobs are cleaned up, but library files stay untouched."""
        mgr = JobManager()
        
        # 1. Create expired temporary download job
        temp_job = Job(
            owner_session="testsessionagent",
            data={"name": "TempDownload", "target": "download"},
            existing_id="temp-job-expired-1",
            existing_status="completed",
            created_at=time.time() - (config.RETENTION_SECONDS + 500)
        )
        temp_job.folder.mkdir(parents=True, exist_ok=True)
        (temp_job.folder / "download.mp4").write_bytes(b"temp video content")
        mgr.jobs[temp_job.id] = temp_job

        # 2. Create permanent library movie in LIBRARY_ROOT
        permanent_movie_dir = config.LIBRARY_MOVIES_DIR / "Test Film (2025)"
        permanent_movie_dir.mkdir(parents=True, exist_ok=True)
        perm_file = permanent_movie_dir / "Test Film (2025).mp4"
        perm_file.write_bytes(b"permanent video content")

        # 3. Create a library job
        lib_job = Job(
            owner_session="testsessionagent",
            data={"name": "LibMovie", "target": "library"},
            existing_id="lib-job-1",
            existing_status="ready",
            created_at=time.time() - (config.RETENTION_SECONDS + 500)
        )
        mgr.jobs[lib_job.id] = lib_job

        # 4. Run retention cleanup
        cleaned = mgr.cleanup_expired_jobs()

        self.assertIn("temp-job-expired-1", cleaned)
        self.assertNotIn("lib-job-1", cleaned)
        self.assertFalse(temp_job.folder.exists())

        # 5. Verify permanent library file remains completely intact
        self.assertTrue(permanent_movie_dir.exists())
        self.assertTrue(perm_file.exists())
        self.assertEqual(perm_file.read_bytes(), b"permanent video content")

    # =========================================================================
    # 5. API ROUTER & SECURITY TESTS
    # =========================================================================
    def _api_request(self, endpoint, data=None, library_auth=True):
        url = f"http://127.0.0.1:{self.api_port}{endpoint}"
        headers = {"X-App-Session": "testsessionagent99"}
        if library_auth:
            headers["X-Library-Auth"] = config.LIBRARY_PASSWORD
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers)
        else:
            req = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_library_password_authentication(self):
        """Tests that library endpoints require password and that 1234567890 unlocks access."""
        # 1. Unauthenticated request to library status is rejected
        url = f"http://127.0.0.1:{self.api_port}/api/library/status"
        req_unauth = urllib.request.Request(url, headers={"X-App-Session": "testsessionagent99"})
        try:
            urllib.request.urlopen(req_unauth)
            self.fail("Should have rejected unauthenticated library access")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)
            err_body = json.loads(e.read().decode())
            self.assertTrue(err_body.get("auth_required"))

        # 2. Authenticate with wrong password -> rejected
        url_auth = f"http://127.0.0.1:{self.api_port}/api/library/auth"
        req_wrong = urllib.request.Request(
            url_auth,
            data=json.dumps({"password": "salah"}).encode(),
            headers={"X-App-Session": "testsessionagent99", "Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req_wrong)
            self.fail("Should have rejected wrong password")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

        # 3. Authenticate with correct password 1234567890 -> accepted and token returned
        req_correct = urllib.request.Request(
            url_auth,
            data=json.dumps({"password": "1234567890"}).encode(),
            headers={"X-App-Session": "testsessionagent99", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_correct) as resp:
            self.assertEqual(resp.status, 200)
            auth_res = json.loads(resp.read().decode())
            self.assertTrue(auth_res.get("ok"))
            token = auth_res.get("token")
            self.assertTrue(bool(token))

        # 4. Access library status using token in X-Library-Auth header
        req_with_token = urllib.request.Request(
            url,
            headers={"X-App-Session": "testsessionagent99", "X-Library-Auth": token}
        )
        with urllib.request.urlopen(req_with_token) as resp_token:
            self.assertEqual(resp_token.status, 200)
            res = json.loads(resp_token.read().decode())
            self.assertTrue(res.get("ok"))

    def test_api_library_status(self):
        """Tests GET /api/library/status returns health, disk space and item counts."""
        res = self._api_request("/api/library/status")
        self.assertTrue(res["ok"])
        self.assertIn("jellyfin", res)
        self.assertIn("disk", res)
        self.assertIn("counts", res)
        self.assertTrue(res["jellyfin"]["healthy"])
        self.assertGreater(res["disk"]["free_gb"], 0)
        self.assertIn("percent_used", res["disk"])

    def test_api_library_items_and_delete(self):
        """Tests listing library items and deleting item with/without permanent files."""
        # Insert a dummy item directly
        item = create_library_item(
            title="Laskar Pelangi 2",
            year=2013,
            clean_title="Laskar Pelangi 2",
            folder_path="Movies/Laskar Pelangi 2 (2013)",
            source_url="https://example.com/lp2.m3u8",
            status="ready"
        )

        # Create physical folder for item
        item_folder = config.LIBRARY_ROOT / "Movies/Laskar Pelangi 2 (2013)"
        item_folder.mkdir(parents=True, exist_ok=True)
        dummy_video = item_folder / "Laskar Pelangi 2 (2013).mp4"
        dummy_video.write_bytes(b"lp2 video")

        # Query items
        res = self._api_request("/api/library/items")
        self.assertTrue(res["ok"])
        found = any(it["id"] == item["id"] for it in res["items"])
        self.assertTrue(found)

        # Test delete with delete_files=False: folder remains
        del_res = self._api_request("/api/library/delete", {"item_id": item["id"], "delete_files": False})
        self.assertTrue(del_res["ok"])
        self.assertTrue(dummy_video.exists())

        # Test delete with delete_files=True on a second item
        item2 = create_library_item(
            title="Film Dihapus",
            year=2020,
            clean_title="Film Dihapus",
            folder_path="Movies/Film Dihapus (2020)",
            source_url="https://example.com/dihapus.m3u8",
            status="ready"
        )
        del_folder = config.LIBRARY_ROOT / "Movies/Film Dihapus (2020)"
        del_folder.mkdir(parents=True, exist_ok=True)
        (del_folder / "Film Dihapus (2020).mp4").write_bytes(b"dummy")

        del_res2 = self._api_request("/api/library/delete", {"item_id": item2["id"], "delete_files": True})
        self.assertTrue(del_res2["ok"])
        self.assertFalse(del_folder.exists())

    def test_security_blocked_sensitive_files(self):
        """Tests that sqlite databases, cookies.txt, jobs_db.json, and data directory are blocked from static serve."""
        # Try fetching sqlite db directly
        url = f"http://127.0.0.1:{self.api_port}/data/omnifetch-library.sqlite3"
        req = urllib.request.Request(url, headers={"X-App-Session": "test-session"})
        try:
            urllib.request.urlopen(req)
            self.fail("Should have blocked access to sqlite database")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

        # Try directory traversal
        url_traversal = f"http://127.0.0.1:{self.api_port}/../../etc/passwd"
        req_trav = urllib.request.Request(url_traversal, headers={"X-App-Session": "test-session"})
        try:
            urllib.request.urlopen(req_trav)
            self.fail("Should have blocked directory traversal")
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, [400, 403, 404])

    def test_backward_compatibility_normal_download_starts_cleanly(self):
        """Ensures legacy /api/start payload without library target maintains default download behavior."""
        res = self._api_request("/api/start", {
            "url": "https://example.com/test_video.mp4",
            "mode": "direct",
            "name": "Standard Test Download"
        })
        self.assertTrue(res.get("ok"))
        job_id = res.get("job_id")
        self.assertIsNotNone(job_id)

        # Query status
        status = self._api_request(f"/api/status?job_id={job_id}")
        self.assertEqual(status.get("target", "download"), "download")

    def test_custom_hdd_library_directory_configuration(self):
        """Tests that custom HDD paths can be configured and disk usage properly measures the target mount."""
        import tempfile
        from backend.core.importer import check_disk_space_sufficient

        with tempfile.TemporaryDirectory() as custom_hdd_dir:
            custom_hdd_path = Path(custom_hdd_dir) / "perpustakaan_film"
            custom_hdd_path.mkdir(parents=True, exist_ok=True)

            sufficient, free_gb, msg = check_disk_space_sufficient(custom_hdd_path, required_bytes=1024)
            self.assertTrue(sufficient)
            self.assertGreater(free_gb, 0.0)

    def test_library_streaming_chunked_and_range_requests(self):
        """Tests that /api/library/stream/<id> supports full 200 download and RFC 7233 206 partial range streaming."""
        # Setup item with mock video
        folder_rel = "movies/Test Stream Movie (2024)"
        item_folder = config.LIBRARY_ROOT / folder_rel
        item_folder.mkdir(parents=True, exist_ok=True)
        vid_file = item_folder / "Test Stream Movie (2024).mp4"
        dummy_video_data = b"OmniFetchMockVideoChunk" * 100  # 2300 bytes
        vid_file.write_bytes(dummy_video_data)

        item = create_library_item("Test Stream Movie", 2024, "test stream movie", folder_rel, db_path=config.LIBRARY_DB_PATH)
        item_id = item["id"]

        # 1. Full stream request (200 OK)
        url = f"http://127.0.0.1:{self.api_port}/api/library/stream/{item_id}?library_auth={config.LIBRARY_PASSWORD}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Accept-Ranges"), "bytes")
            self.assertEqual(resp.headers.get("Content-Type"), "video/mp4")
            data = resp.read()
            self.assertEqual(len(data), len(dummy_video_data))
            self.assertEqual(data, dummy_video_data)

        # 2. Partial content request (206 Range)
        req_range = urllib.request.Request(url, headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req_range) as resp_range:
            self.assertEqual(resp_range.status, 206)
            self.assertEqual(resp_range.headers.get("Content-Range"), f"bytes 0-99/{len(dummy_video_data)}")
            partial_data = resp_range.read()
            self.assertEqual(len(partial_data), 100)
            self.assertEqual(partial_data, dummy_video_data[:100])

    def test_library_subtitle_serving_and_srt_vtt_conversion(self):
        """Tests that /api/library/subtitle/<id> serves WebVTT and converts SRT timestamps on the fly."""
        folder_rel = "movies/Test Subtitle Movie (2024)"
        item_folder = config.LIBRARY_ROOT / folder_rel
        item_folder.mkdir(parents=True, exist_ok=True)
        srt_file = item_folder / "Test Subtitle Movie.id.srt"
        srt_content = "1\n00:00:01,000 --> 00:00:04,500\nHalo dunia subtitle!\n\n"
        srt_file.write_text(srt_content, encoding="utf-8")

        item = create_library_item("Test Subtitle Movie", 2024, "test subtitle movie", folder_rel, db_path=config.LIBRARY_DB_PATH)
        item_id = item["id"]

        url = f"http://127.0.0.1:{self.api_port}/api/library/subtitle/{item_id}?library_auth={config.LIBRARY_PASSWORD}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/vtt", resp.headers.get("Content-Type"))
            content = resp.read().decode("utf-8")
            self.assertTrue(content.startswith("WEBVTT"))
            self.assertIn("00:00:01.000 --> 00:00:04.500", content)
            self.assertIn("Halo dunia subtitle!", content)

    def test_library_poster_generation(self):
        """Tests that /api/library/poster/<id> generates a rich 2:3 cinematic SVG poster card."""
        item = create_library_item("Dokter Ajaib", 2024, "dokter ajaib", "movies/Dokter Ajaib (2024)", db_path=config.LIBRARY_DB_PATH)
        item_id = item["id"]

        url = f"http://127.0.0.1:{self.api_port}/api/library/poster/{item_id}?library_auth={config.LIBRARY_PASSWORD}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/svg+xml")
            svg_data = resp.read().decode("utf-8")
            self.assertIn("<svg", svg_data)
            self.assertIn("Dokter Ajaib", svg_data)
            self.assertIn("WEB-DL", svg_data)
            self.assertIn("SUB INDO", svg_data)

    def test_library_retry_actions_graceful_offline_handling(self):
        """Tests that retry-index and scan gracefully inform the caller when Jellyfin is offline without crashing."""
        from backend.core.jellyfin import jellyfin_client
        item = create_library_item("Offline Jellyfin Film", 2024, "offline", "movies/Offline", db_path=config.LIBRARY_DB_PATH)
        item_id = item["id"]

        # Call retry-index with non-existent port (simulate connection refused)
        orig_url = jellyfin_client.internal_url
        jellyfin_client.internal_url = "http://127.0.0.1:59999"  # Port without server
        try:
            res = self._api_request("/api/library/retry-index", {"item_id": item_id})
            self.assertIn("jellyfin_offline", res)
            self.assertTrue(res["jellyfin_offline"])

            res_scan = self._api_request("/api/library/scan", {})
            self.assertIn("jellyfin_offline", res_scan)
            self.assertTrue(res_scan["jellyfin_offline"])
        finally:
            jellyfin_client.internal_url = orig_url


if __name__ == "__main__":
    unittest.main()

