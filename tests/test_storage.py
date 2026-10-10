import unittest
import os
import shutil
from pathlib import Path
from backend.core.storage import get_job_files, save_metadata, read_metadata

class TestStorage(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path('test_job_dir')
        self.test_dir.mkdir(parents=True, exist_ok=True)
        (self.test_dir / 'test.mp4').write_text('video content')
        (self.test_dir / 'test.srt').write_text('subtitle content')

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    def test_get_job_files(self):
        files = get_job_files(self.test_dir, 'job123', 'session123')
        self.assertEqual(len(files), 2)
        urls = [f['url'] for f in files]
        self.assertTrue(any('test.mp4' in u for u in urls))
        self.assertTrue(any('session=session123' in u for u in urls))

    def test_metadata(self):
        save_metadata(self.test_dir, {'owner_session': 'ses123'})
        meta = read_metadata(self.test_dir)
        self.assertEqual(meta.get('owner_session'), 'ses123')

if __name__ == '__main__':
    unittest.main()
