import tempfile
import unittest
import json
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
            (folder / 'Video.f1242.mp4').write_bytes(b'fragment')
            (folder / 'Video.part').write_bytes(b'part')
            (folder / '.expiry.json').write_text('{"delete_at": 123}')

            files = app.get_job_files(folder, job_id='job-test123')
            self.assertEqual(len(files), 2)
            names = [f['name'] for f in files]
            self.assertIn('Video.mp4', names)
            self.assertIn('Video.srt', names)
            self.assertNotIn('.expiry.json', names)
            self.assertNotIn('Video.f1242.mp4', names)
            self.assertNotIn('Video.part', names)

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

    def test_build_ytdlp_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Video mode
            data_video = {
                'urls': ['https://www.youtube.com/watch?v=dQw4w9WgXcQ'],
                'folder': tmp,
                'media_type': 'video',
                'resolution': '1080',
                'video_format': 'mp4',
                'embed_metadata': True,
                'embed_thumb': True,
                'embed_subs': True,
                'sub_lang': 'id,en'
            }
            cmd, folder, batch_file = app.build_ytdlp_command(data_video)
            self.assertIn('-f', cmd)
            self.assertIn('--merge-output-format', cmd)
            self.assertIn('mp4', cmd)
            self.assertIn('--embed-metadata', cmd)
            self.assertIn('--embed-thumbnail', cmd)
            self.assertIn('--embed-subs', cmd)
            self.assertIn('id,en', cmd)
            self.assertIsNone(batch_file)

            # Audio extraction mode
            data_audio = {
                'urls': ['https://soundcloud.com/artist/track'],
                'folder': tmp,
                'media_type': 'audio',
                'audio_format': 'mp3',
                'audio_quality': '0',
                'embed_metadata': True
            }
            cmd, folder, batch_file = app.build_ytdlp_command(data_audio)
            self.assertIn('-x', cmd)
            self.assertIn('--audio-format', cmd)
            self.assertIn('mp3', cmd)
            self.assertIn('--audio-quality', cmd)
            self.assertIn('0', cmd)

            # Batch mode with multi URLs
            data_batch = {
                'urls': ['https://instagram.com/p/123', 'https://tiktok.com/@u/video/456'],
                'folder': tmp,
                'ignore_errors': True,
                'playlist_start': 1,
                'playlist_end': 10
            }
            cmd, folder, batch_file = app.build_ytdlp_command(data_batch)
            self.assertIn('--batch-file', cmd)
            self.assertIsNotNone(batch_file)
            self.assertTrue(Path(batch_file).exists())
            self.assertIn('-i', cmd)
            self.assertIn('--playlist-start', cmd)
            self.assertIn('--playlist-end', cmd)
            if batch_file and Path(batch_file).exists():
                Path(batch_file).unlink()

    def test_start_ytdlp_endpoint_validation(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            # Request without urls should fail with 400
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/start',
                data=b'{"mode": "ytdlp", "urls": ""}',
                headers={'X-App-Token': app.TOKEN, 'Content-Type': 'application/json'}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()


    def test_download_http_range_and_stream(self):
        with tempfile.TemporaryDirectory() as vps_root:
            app.VPS_ROOT = Path(vps_root)
            job_folder = app.VPS_ROOT / 'job-range'
            job_folder.mkdir()
            (job_folder / 'Sample.mp4').write_bytes(b'0123456789ABCDEF')

            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            base = f'http://127.0.0.1:{port}'

            try:
                # Test Range: bytes=4-9 (expect 6 bytes '456789')
                url = f'{base}/download/job-range/Sample.mp4'
                req = urllib.request.Request(url, headers={'Host': f'127.0.0.1:{port}', 'Range': 'bytes=4-9'})
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(resp.headers.get('Content-Range'), 'bytes 4-9/16')
                    self.assertEqual(resp.headers.get('Content-Length'), '6')
                    self.assertEqual(resp.read(), b'456789')

                # Test stream endpoint (inline disposition)
                url_stream = f'{base}/stream/job-range/Sample.mp4'
                req_stream = urllib.request.Request(url_stream, headers={'Host': f'127.0.0.1:{port}'})
                with urllib.request.urlopen(req_stream) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers.get('Content-Disposition'), 'inline')
                    self.assertEqual(resp.read(), b'0123456789ABCDEF')
            finally:
                app.VPS_ROOT = None
                server.shutdown()
                server.server_close()

    def test_cookies_and_disk_endpoints(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        base = f'http://127.0.0.1:{port}'

        try:
            # Test disk-info
            req_disk = urllib.request.Request(
                f'{base}/disk-info',
                headers={'X-App-Token': app.TOKEN}
            )
            with urllib.request.urlopen(req_disk) as resp:
                self.assertEqual(resp.status, 200)
                info = json.loads(resp.read())
                self.assertIn('free_gb', info)
                self.assertIn('total_gb', info)

            # Test upload-cookies
            cookie_content = ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t1234567890"
            req_upload = urllib.request.Request(
                f'{base}/upload-cookies',
                data=cookie_content.encode('utf-8'),
                headers={'X-App-Token': app.TOKEN, 'Content-Type': 'text/plain'}
            )
            with urllib.request.urlopen(req_upload) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read())
                self.assertTrue(res.get('ok'))
                self.assertTrue(res.get('has_instagram'))

            # Test cookies-status
            req_status = urllib.request.Request(
                f'{base}/cookies-status',
                headers={'X-App-Token': app.TOKEN}
            )
            with urllib.request.urlopen(req_status) as resp:
                self.assertEqual(resp.status, 200)
                c_stat = json.loads(resp.read())
                self.assertTrue(c_stat.get('loaded'))
                self.assertTrue(c_stat.get('has_instagram'))
        finally:
            server.shutdown()
            server.server_close()

    def test_ytdlp_sections_and_instagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {
                'urls': ['https://www.instagram.com/reel/123/'],
                'folder': tmp,
                'start_time': '00:00:10',
                'end_time': '00:01:30'
            }
            cmd, folder, batch_file = app.build_ytdlp_command(data)
            self.assertIn('--download-sections', cmd)
            self.assertIn('*00:00:10-00:01:30', cmd)
            self.assertIn('--add-header', cmd)
            self.assertIn('Referer:https://www.instagram.com/', cmd)
            self.assertIn('--yes-playlist', cmd)
            self.assertIn('--write-thumbnail', cmd)

    def test_ensure_job_zip_and_get_job_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            # 1 single file should not trigger zip
            (folder / 'Slide_1.jpg').write_bytes(b'image1')
            zip_res = app.ensure_job_zip(folder, 'TestJob')
            self.assertIsNone(zip_res)
            files = app.get_job_files(folder, 'job-test')
            self.assertEqual(len(files), 1)
            self.assertFalse(files[0]['is_zip'])

            # 2 files should trigger zip creation
            (folder / 'Slide_2.mp4').write_bytes(b'video2')
            zip_res = app.ensure_job_zip(folder, 'TestJob')
            self.assertIsNotNone(zip_res)
            self.assertTrue(zip_res.exists())
            self.assertEqual(zip_res.name, 'TestJob.zip')

            # Verify get_job_files places the .zip at index 0
            all_files = app.get_job_files(folder, 'job-test')
            self.assertEqual(len(all_files), 3) # TestJob.zip, Slide_1.jpg, Slide_2.mp4
            self.assertEqual(all_files[0]['name'], 'TestJob.zip')
            self.assertTrue(all_files[0]['is_zip'])


if __name__ == '__main__':
    unittest.main()


