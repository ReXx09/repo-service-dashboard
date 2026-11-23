import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import main


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = main.DB_PATH
        self.old_github_token = main.GH_TOKEN
        self.old_password_iterations = main.PASSWORD_ITERATIONS
        main.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.sqlite3")
        main.GH_TOKEN = ""
        main.PASSWORD_ITERATIONS = 100
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        main.DB_PATH = self.old_db_path
        main.GH_TOKEN = self.old_github_token
        main.PASSWORD_ITERATIONS = self.old_password_iterations
        self.temp_dir.cleanup()

    def request_headers(self):
        return {"X-Dashboard-Request": "1"}

    def setup_payload(self):
        return {"username": "Admin-01", "password": "a-very-long-password"}

    def test_first_run_setup_and_session_lifecycle(self):
        status = self.client.get("/api/auth/status")
        self.assertEqual(status.json(), {"setup_required": True, "authenticated": False})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/auth.css").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)

        setup = self.client.post(
            "/api/auth/setup",
            json=self.setup_payload(),
            headers=self.request_headers(),
        )
        self.assertEqual(setup.status_code, 200)
        self.assertTrue(setup.cookies.get(main.SESSION_COOKIE))
        self.assertIn("httponly", setup.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", setup.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)

        with main.connection() as con:
            admin = con.execute(
                "SELECT username,password_hash FROM admin_user WHERE id=1"
            ).fetchone()
        self.assertEqual(admin["username"], "admin-01")
        self.assertNotEqual(admin["password_hash"], self.setup_payload()["password"].encode())

        second_setup = self.client.post(
            "/api/auth/setup",
            json=self.setup_payload(),
            headers=self.request_headers(),
        )
        self.assertEqual(second_setup.status_code, 409)

        logout = self.client.post("/api/auth/logout", headers=self.request_headers())
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)

        login = self.client.post(
            "/api/auth/login",
            json=self.setup_payload(),
            headers=self.request_headers(),
        )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(self.client.get("/api/auth/status").json()["authenticated"])

    def test_login_rejects_bad_password_and_limits_retries(self):
        setup = self.client.post(
            "/api/auth/setup",
            json=self.setup_payload(),
            headers=self.request_headers(),
        )
        self.assertEqual(setup.status_code, 200)

        wrong_credentials = {"username": "admin-01", "password": "incorrect-password"}
        for _ in range(main.LOGIN_MAX_ATTEMPTS):
            response = self.client.post(
                "/api/auth/login",
                json=wrong_credentials,
                headers=self.request_headers(),
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/api/auth/login",
            json=self.setup_payload(),
            headers=self.request_headers(),
        )
        self.assertEqual(limited.status_code, 429)

    def test_setup_requires_long_password(self):
        response = self.client.post(
            "/api/auth/setup",
            json={"username": "admin-01", "password": "short"},
            headers=self.request_headers(),
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
