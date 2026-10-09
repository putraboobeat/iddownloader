import functools
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import app
from detect_video import discover, media_kind
from subtitles import save_subtitle


class DetectionTests(unittest.TestCase):
    def test_content_not_filename(self):
        self.assertEqual(media_kind('\ufeff#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=123\nvideo.json')[0], 'master')
        self.assertEqual(media_kind('#EXTM3U\n#EXTINF:2,\nsegment.ts')[0], 'hls')
        self.assertIsNone(media_kind('{"config": "not a video"}', 'application/json'))
        self.assertIsNone(media_kind('<html>blocked</html>', 'text/html'))

    def test_command_context(self):
        with tempfile.TemporaryDirectory() as folder:
            command, _ = app.build_command(dict(url='https://example.com/config.json?t=abc', folder=folder,
                referer='https://example.com/player', _cookies='/tmp/test-cookies.txt',
                _headers={'origin': 'https://example.com', 'user-agent': 'test', 'cookie': 'ignored'}))
            self.assertIn('--cookies', command)
            self.assertIn('origin:https://example.com', command)
            self.assertNotIn('cookie:ignored', command)
            self.assertEqual(command[-2], '--')

    @unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS'), 'Requires Chrome and local server')
    def test_browser_to_download_and_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                'testsrc=size=160x90:rate=10', '-t', '1', '-pix_fmt', 'yuv420p', '-c:v', 'libx264',
                '-f', 'hls', '-hls_time', '1', str(root / 'stream.m3u8')], check=True)
            (root / 'config.json').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=160x90\nstream.m3u8\n')
            (root / 'index.html').write_text('<iframe src="/player.html"></iframe>')
            (root / 'player.html').write_text('<script>document.cookie="fixture=yes; path=/"; setTimeout(()=>fetch("/config.json"),300)</script>')
            (root / 'subtitle.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHalo\n')
            (root / 'player.html').write_text('<video><track kind="subtitles" srclang="id" src="/subtitle.vtt"></video><script>document.cookie="fixture=yes; path=/"; setTimeout(()=>fetch("/config.json"),300); setTimeout(()=>fetch("/subtitle.vtt"),800)</script>')
            (root / 'empty.html').write_text('<h1>No player</h1>')
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=folder))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            try:
                with discover(base, root / 'cookies.txt', lambda: False, lambda _: None, timeout=10, headless=True, subtitles=True) as media:
                    self.assertEqual(media['kind'], 'master')
                    self.assertEqual(len(media['subtitles']), 1)
                    saved = save_subtitle(media['subtitles'][0], root, 'result', lambda: False, str(root / 'cookies.txt'))
                    self.assertIn('Halo', saved.read_text())
                    self.assertTrue(media['referer'].endswith('/player.html'))
                    self.assertIn('fixture', (root / 'cookies.txt').read_text())
                    app.STATE.update(cancelled=False, running=True, logs=[])
                    command, _ = app.build_command(dict(url=media['url'], folder=folder, name='result',
                        referer=media['referer'], _headers=media['headers'], _cookies=str(root / 'cookies.txt')))
                    app.run_download(command)
                    self.assertTrue((root / 'result.mp4').exists(), app.STATE['logs'])
                    self.assertIn('Selesai', app.STATE['status'])
                start = time.monotonic()
                with self.assertRaises(InterruptedError):
                    with discover(base + '/empty.html', root / 'cookies.txt', lambda: True, lambda _: None, timeout=10, headless=True):
                        self.fail('Cancelled search must not yield a media URL')
                self.assertLess(time.monotonic() - start, 10)
                with self.assertRaises(ValueError):
                    with discover(base + '/empty.html', root / 'cookies.txt', lambda: False, lambda _: None, timeout=1, headless=True):
                        self.fail('Empty page must time out')
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
