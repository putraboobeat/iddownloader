import unittest
from backend.core.security import is_safe_url, clean_url

class TestSecurity(unittest.TestCase):
    def test_clean_url(self):
        self.assertEqual(clean_url("  http://example.com  "), "http://example.com")
        self.assertEqual(clean_url("https://example.com/file?arg=1&arg=2"), "https://example.com/file?arg=1&arg=2")
        with self.assertRaises(ValueError):
            clean_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            clean_url("ftp://server")

    def test_is_safe_url(self):
        self.assertTrue(is_safe_url("https://example.com"))
        self.assertTrue(is_safe_url("http://google.com"))
        
        # Test loopback and private IPs
        self.assertFalse(is_safe_url("http://127.0.0.1"))
        self.assertFalse(is_safe_url("http://localhost"))
        self.assertFalse(is_safe_url("http://192.168.1.1"))
        self.assertFalse(is_safe_url("http://10.0.0.1"))
        
        # AWS metadata
        self.assertFalse(is_safe_url("http://169.254.169.254"))

if __name__ == '__main__':
    unittest.main()
