import unittest
from backend.core.downloader import build_ytdlp_command

class TestDownloader(unittest.TestCase):
    def test_build_ytdlp_command(self):
        data = {
            "url": "https://example.com/video",
            "resolution": "720",
            "name": "Test Video",
            "write_subs": True
        }
        cmd = build_ytdlp_command(data, "test_folder")
        self.assertIn("https://example.com/video", cmd)
        self.assertIn("-f", cmd)
        self.assertTrue(any("720" in arg for arg in cmd))

if __name__ == '__main__':
    unittest.main()
