import tempfile
import unittest
from pathlib import Path
from retention import Retention


class RetentionTests(unittest.TestCase):
    def test_active_then_finished_deadline_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = Retention(tmp)
            self.assertEqual(manager.seconds, 7200)
            job = manager.create('Film')
            (job / 'Film.mp4').write_text('sample')
            manager.sweep(now=10**12)
            self.assertTrue(job.exists())
            deadline = manager.finish(job)
            manager.sweep(now=deadline - 1)
            self.assertTrue(job.exists())
            Retention(tmp).sweep(now=deadline)
            self.assertFalse(job.exists())

    def test_crash_partial_is_cleaned_but_unmanaged_and_symlinks_survive(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            unmanaged = root / 'personal'
            unmanaged.mkdir()
            target = Path(outside) / 'keep.txt'
            target.write_text('keep')
            (root / 'job-link').symlink_to(outside, target_is_directory=True)
            job = Retention(tmp).create('Film')
            (job / 'video.part').write_text('partial')
            (job / 'external').symlink_to(outside, target_is_directory=True)
            Retention(tmp).sweep(now=10**12)
            self.assertFalse(job.exists())
            self.assertTrue(unmanaged.exists())
            self.assertTrue(target.exists())
            self.assertTrue((root / 'job-link').is_symlink())

    def test_failed_delete_is_retried(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            manager = Retention(tmp)
            job = manager.create('Film')
            deadline = manager.finish(job)
            with patch('retention.shutil.rmtree', side_effect=PermissionError('busy')):
                manager.sweep(now=deadline)
            self.assertTrue((job / '.expiry.json').exists())
            manager.sweep(now=deadline)
            self.assertFalse(job.exists())
