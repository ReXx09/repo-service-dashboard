import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import main


class TileTests(unittest.TestCase):
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
        self.headers = {"X-Dashboard-Request": "1"}
        self.client.post(
            "/api/auth/setup",
            json={"username": "tile-admin", "password": "a-very-long-password"},
            headers=self.headers,
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        main.DB_PATH = self.old_db_path
        main.GH_TOKEN = self.old_github_token
        main.PASSWORD_ITERATIONS = self.old_password_iterations
        self.temp_dir.cleanup()

    def add_service(self, name):
        response = self.client.post(
            "/api/services",
            json={
                "name": name,
                "url": f"http://192.168.1.20/{name}",
                "check_url": f"http://192.168.1.20/{name}/health",
                "check_type": "http",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_tile_settings_order_styles_and_service_deletion(self):
        first_id = self.add_service("app-one")
        second_id = self.add_service("app-two")
        initial = self.client.get("/api/dashboard").json()["services"]
        self.assertEqual([service["id"] for service in initial], [first_id, second_id])
        self.assertEqual(initial[0]["tile_width"], 1)

        updated = self.client.put(
            f"/api/services/{first_id}/tile",
            json={"width": 2, "height": 3, "color": "#12aBcF", "shape": "square"},
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200)
        settings = self.client.get("/api/dashboard").json()["services"][0]
        self.assertEqual(settings["tile_width"], 2)
        self.assertEqual(settings["tile_height"], 3)
        self.assertEqual(settings["tile_color"], "#12aBcF")
        self.assertEqual(settings["tile_shape"], "square")
        self.assertIn(f"#service-tile-{first_id}{{--tile-color:#12aBcF;}}", self.client.get("/api/tiles.css").text)

        reordered = self.client.put(
            "/api/tiles/order",
            json={"service_ids": [second_id, first_id]},
            headers=self.headers,
        )
        self.assertEqual(reordered.status_code, 200)
        services = self.client.get("/api/dashboard").json()["services"]
        self.assertEqual([service["id"] for service in services], [second_id, first_id])

        invalid_color = self.client.put(
            f"/api/services/{first_id}/tile",
            json={"width": 2, "height": 1, "color": "red", "shape": "pill"},
            headers=self.headers,
        )
        self.assertEqual(invalid_color.status_code, 422)

        invalid_order = self.client.put(
            "/api/tiles/order",
            json={"service_ids": [first_id, first_id]},
            headers=self.headers,
        )
        self.assertEqual(invalid_order.status_code, 422)

        deleted = self.client.delete(f"/api/services/{first_id}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        with main.connection() as con:
            tile = con.execute("SELECT 1 FROM service_tiles WHERE service_id=?", (first_id,)).fetchone()
        self.assertIsNone(tile)


if __name__ == "__main__":
    unittest.main()
