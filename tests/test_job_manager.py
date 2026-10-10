import unittest
from pathlib import Path
import time
from backend.core.job_manager import JobManager
from backend.core.config import RETENTION_SECONDS

class TestJobManager(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()

    def test_add_job(self):
        job = self.manager.add_job("session123", {"url": "http://example.com"})
        self.assertEqual(job.owner_session, "session123")
        self.assertEqual(job.status, "downloading") # Thread starts immediately, but might be queued depending on thread scheduler.
        self.assertIn(job.id, self.manager.jobs)
        
        job2 = self.manager.get_job(job.id, "session123")
        self.assertIsNotNone(job2)
        
        # Another session shouldn't get it
        job3 = self.manager.get_job(job.id, "wrong_session")
        self.assertIsNone(job3)

if __name__ == '__main__':
    unittest.main()
