import unittest
from app.main import ServiceIn, clean_service, validate_url

class ValidatorTests(unittest.TestCase):
    def test_http(self):
        self.assertEqual(validate_url('https://example.org/health'), 'https://example.org/health')
    def test_credentials_in_url_blocked(self):
        with self.assertRaises(ValueError): validate_url('https://user:password@example.org')
    def test_javascript_blocked(self):
        with self.assertRaises(ValueError): validate_url('javascript:alert(1)')
    def test_tcp(self):
        record = clean_service(ServiceIn(name=' DB ',url='https://example.org',check_url='tcp://192.168.1.3:5432',check_type='tcp'))
        self.assertEqual(record[0], 'DB')
    def test_tcp_missing_port(self):
        with self.assertRaises(ValueError): validate_url('tcp://localhost',check=True)

if __name__ == '__main__': unittest.main()
