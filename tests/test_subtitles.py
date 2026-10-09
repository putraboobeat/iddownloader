import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

import app
from subtitles import save_subtitle, subtitle_format

VTT = b'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSubtitle uji.\n'


class SubtitleTests(unittest.TestCase):
    def test_formats_and_invalid_response(self):
        self.assertEqual(subtitle_format(VTT), 'vtt')
        self.assertEqual(subtitle_format(b'1\n00:00:00,000 --> 00:00:01,000\nHello\n'), 'srt')
        with self.assertRaises(ValueError):
            subtitle_format(b'<html>Access denied</html>')

    def test_save_repeat_and_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            track = dict(url='https://example.test/i18n/id/sub.vtt?t=abc', body=VTT)
            first = save_subtitle(track, folder, 'Film', lambda: False)
            second = save_subtitle(track, folder, 'Film', lambda: False)
            self.assertEqual(first.name, 'Film.srt')
            self.assertEqual(second.name, 'Film.srt')
            self.assertIn(b'00:00:00,000 --> 00:00:01,000', first.read_bytes())
            self.assertIn(b'Subtitle uji.', first.read_bytes())
            with self.assertRaises(InterruptedError):
                save_subtitle(track, folder, 'Cancelled', lambda: True)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_direct_job_no_video_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            response = MagicMock()
            response.read.side_effect = [VTT, b'']
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            app.STATE.update(cancelled=False, running=True, logs=[])
            with patch('subtitles.build_opener', return_value=opener), patch('app.build_command', side_effect=AssertionError('Must not use yt-dlp')):
                app.run_job(dict(url='https://example.test/i18n/id/sub.vtt?t=c1_%2F&pm=browser', name='Film', folder=folder))
            self.assertTrue((Path(folder) / 'Film' / 'Film.srt').exists())
            self.assertFalse(app.STATE['running'])
            self.assertEqual(app.STATE['percent'], 100)
            self.assertIn('subtitle tersimpan', app.STATE['status'])

    def test_url_escapes_preserved(self):
        self.assertEqual(app.clean_url(r'https://example.test/sub.vtt?t=c1\_%2F\&pm=browser'), 'https://example.test/sub.vtt?t=c1_%2F&pm=browser')

    def test_subtitle_failure_keeps_video_download(self):
        with tempfile.TemporaryDirectory() as folder:
            app.STATE.update(cancelled=False, running=True, logs=[])
            def finish_video(command):
                app.STATE['status'] = 'Selesai — file tersimpan di folder tujuan.'
            with patch('app.save_subtitle', side_effect=ValueError('HTTP 403')), patch('app.run_download', side_effect=finish_video) as video:
                app.run_job(dict(url='https://example.test/movie.mp4', subtitle_url='https://example.test/sub.vtt', folder=folder))
            video.assert_called_once()
            self.assertIn('Video selesai', app.STATE['status'])
            self.assertIn('subtitle gagal', app.STATE['status'])


if __name__ == '__main__':
    unittest.main()
