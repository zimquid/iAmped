"""The unauthenticated local API must refuse cross-site and DNS-rebound requests."""
import unittest

from iamped.server import app


class LocalOnlyGuardTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def get(self, headers=None, host="127.0.0.1:5123"):
        return self.client.get("/api/job/missing", headers=headers or {},
                               base_url=f"http://{host}")

    def test_same_origin_and_plain_clients_reach_the_api(self):
        # 404 = past the guard (the job id simply doesn't exist).
        self.assertEqual(self.get().status_code, 404)
        self.assertEqual(self.get({"Sec-Fetch-Site": "same-origin"}).status_code, 404)
        self.assertEqual(self.get({"Sec-Fetch-Site": "none"}).status_code, 404)
        self.assertEqual(self.get({"Origin": "http://127.0.0.1:5123"}).status_code, 404)
        self.assertEqual(self.get(host="localhost:5123").status_code, 404)
        self.assertEqual(self.get(host="[::1]:5123").status_code, 404)

    def test_cross_site_browser_requests_are_refused(self):
        self.assertEqual(self.get({"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.get({"Sec-Fetch-Site": "same-site"}).status_code, 403)
        self.assertEqual(self.get({"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.get({"Origin": "null"}).status_code, 403)
        self.assertEqual(self.get({"Origin": "http://127.0.0.1:9999"}).status_code, 403)

    def test_dns_rebinding_host_is_refused(self):
        self.assertEqual(self.get(host="evil.example:5123").status_code, 403)
        self.assertEqual(self.get(host="192.168.1.20:5123").status_code, 403)

    def test_post_from_other_site_is_refused(self):
        r = self.client.post("/api/cache/clear", base_url="http://127.0.0.1:5123",
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
