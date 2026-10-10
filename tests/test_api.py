import unittest
import threading
import time
import urllib.request
import json
from http.server import ThreadingHTTPServer
from backend.api.router import APIHandler

class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), APIHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.daemon = True
        cls.thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_status_endpoint(self):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/status')
        req.add_header('X-App-Session', 'testsession123')
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode())
            self.assertEqual(data.get('jobs'), [])

    def test_not_found(self):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/not-found')
        req.add_header('X-App-Session', 'testsession123')
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

if __name__ == '__main__':
    unittest.main()
