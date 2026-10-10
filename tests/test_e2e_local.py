import unittest
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
import time
from pathlib import Path
import shutil
import os
from backend.core.job_manager import job_manager, Job

class SyntheticMediaHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass # suppress logs

class TestE2ELocal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create synthetic media
        cls.test_dir = Path("test_fixtures")
        cls.test_dir.mkdir(exist_ok=True)
        
        # Create a dummy mp4
        dummy_mp4 = cls.test_dir / "dummy.mp4"
        with open(dummy_mp4, "wb") as f:
            f.write(b"0" * 1024 * 1024) # 1MB dummy file
            
        cls.server = HTTPServer(('127.0.0.1', 8999), lambda *args, **kwargs: SyntheticMediaHandler(*args, directory=str(cls.test_dir), **kwargs))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(1) # wait for server to start

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_job_download_deterministic(self):
        # Test downloading the dummy mp4
        job = job_manager.add_job("session_test", {
            "url": "http://127.0.0.1:8999/dummy.mp4",
            "name": "test_dummy"
        })
        
        # Wait for job to finish
        max_wait = 30
        for _ in range(max_wait):
            if job.status in ("completed", "failed"):
                break
            time.sleep(1)
            
        # Since it's a dummy file, ffprobe will fail validation, so status should be 'failed'
        # Let's check that validation logic caught the fake mp4!
        self.assertEqual(job.status, "failed")
        log_text = "\n".join(job.logs)
        self.assertIn("Gagal validasi", log_text)

    def test_job_recovery_simulate_error(self):
        # Trigger an error (404) to see if recovery kicks in and eventually fails
        job = job_manager.add_job("session_test", {
            "url": "http://127.0.0.1:8999/does_not_exist.mp4",
            "name": "test_404"
        })
        
        max_wait = 30
        for _ in range(max_wait):
            if job.status in ("completed", "failed"):
                break
            time.sleep(1)
            
        self.assertEqual(job.status, "failed")
        log_text = "\n".join(job.logs)
        # Should NOT have retried because 404 is considered fatal/unavailable
        self.assertNotIn("🔄 Memulai ulang percobaan ke-1", log_text)
        self.assertIn("URL sudah kedaluwarsa", log_text)

if __name__ == '__main__':
    unittest.main()
