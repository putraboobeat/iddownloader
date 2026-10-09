import tempfile
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
import urllib.request

import app


class DownloadEndpointTests(unittest.TestCase):
    def test_get_job_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'Video.mp4').write_bytes(b'video content 123')
            (folder / 'Video.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello')
            (folder / '.expiry.json').write_text('{"delete_at": 123}')

            files = app.get_job_files(folder, job_id='job-test123')
            self.assertEqual(len(files), 2)
            names = [f['name'] for f in files]
            self.assertIn('Video.mp4', names)
            self.assertIn('Video.srt', names)
            self.assertNotIn('.expiry.json', names)

            mp4 = next(f for f in files if f['name'] == 'Video.mp4')
            self.assertEqual(mp4['url'], '/download/job-test123/Video.mp4')
            self.assertEqual(mp4['size'], 17)

    def test_download_endpoint_vps_mode(self):
        with tempfile.TemporaryDirectory() as vps_root:
            app.VPS_ROOT = Path(vps_root)
            job_folder = app.VPS_ROOT / 'job-abc'
            job_folder.mkdir()
            (job_folder / 'Film.mp4').write_bytes(b'mp4 binary data')

            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            base = f'http://127.0.0.1:{port}'

            try:
                # Valid download
                url = f'{base}/download/job-abc/Film.mp4'
                req = urllib.request.Request(url, headers={'Host': f'127.0.0.1:{port}'})
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get('Content-Type'), 'video/mp4')
                    self.assertEqual(resp.read(), b'mp4 binary data')

                # Invalid path traversal
                bad_url = f'{base}/download/job-abc/..%2FFilm.mp4'
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(urllib.request.Request(bad_url, headers={'Host': f'127.0.0.1:{port}'}))
                self.assertIn(ctx.exception.code, [400, 403, 404])
            finally:
                app.VPS_ROOT = None
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
