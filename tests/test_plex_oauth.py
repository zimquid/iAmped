from __future__ import annotations

import unittest
from unittest.mock import patch

from iamped import server as server_module


class FakeLogin:
    def __init__(self):
        self.authorized = False
        self.token = "account-token"

    def oauthUrl(self):
        return "https://app.plex.tv/auth/#!?code=test"

    def checkLogin(self):
        return self.authorized


class FakeResource:
    def __init__(self, identifier, name):
        self.clientIdentifier = identifier
        self.name = name
        self.owned = True
        self.presence = True
        self.platform = "Linux"


class FakeServer:
    _baseurl = "https://plex.example:32400"
    _token = "server-token"


class PlexOAuthRoutesTest(unittest.TestCase):
    def setUp(self):
        server_module.app.config.update(TESTING=True)
        self.client = server_module.app.test_client()
        server_module.PLEX_OAUTH.clear()
        server_module._state["server"] = None
        self.cfg = {
            "plex_baseurl": "",
            "plex_token": "",
            "plex_client_id": "",
            "music_section": "",
        }

    def config_load(self):
        return dict(self.cfg)

    def config_save(self, updates):
        self.cfg.update(updates)
        return dict(self.cfg)

    def test_manual_connect_requires_url_and_token(self):
        response = self.client.post("/api/connect", json={
            "baseurl": "",
            "token": "",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Sign in with Plex", response.get_json()["error"])

    def test_oauth_discovers_and_connects_selected_server(self):
        login = FakeLogin()
        resource = FakeResource("server-1", "Music Server")

        with (
            patch.object(server_module.config, "load", self.config_load),
            patch.object(server_module.config, "save", self.config_save),
            patch.object(server_module.plex_client, "start_oauth",
                         return_value=login),
            patch.object(server_module.webbrowser, "open"),
            patch.object(server_module.plex_client, "oauth_account",
                         return_value=object()),
            patch.object(server_module.plex_client, "account_servers",
                         return_value=[resource]),
            patch.object(server_module.plex_client, "connect_resource",
                         return_value=FakeServer()),
            patch.object(server_module.plex_client, "server_info",
                         return_value={
                             "name": "Music Server",
                             "version": "1.41",
                             "platform": "Linux",
                         }),
            patch.object(server_module.plex_client, "music_sections",
                         return_value=["Music"]),
        ):
            started = self.client.post("/api/plex/oauth/start")
            self.assertEqual(started.status_code, 200)
            login_id = started.get_json()["login_id"]
            self.assertTrue(self.cfg["plex_client_id"])

            pending = self.client.get(
                f"/api/plex/oauth/status/{login_id}")
            self.assertEqual(pending.get_json()["status"], "pending")

            login.authorized = True
            authorized = self.client.get(
                f"/api/plex/oauth/status/{login_id}")
            body = authorized.get_json()
            self.assertEqual(body["status"], "authorized")
            self.assertEqual(body["servers"][0]["id"], "server-1")

            connected = self.client.post("/api/plex/oauth/connect", json={
                "login_id": login_id,
                "server_id": "server-1",
            })
            self.assertEqual(connected.status_code, 200)
            self.assertEqual(connected.get_json()["server"]["name"],
                             "Music Server")
            self.assertEqual(self.cfg["plex_baseurl"],
                             "https://plex.example:32400")
            self.assertEqual(self.cfg["plex_token"], "server-token")
            self.assertEqual(self.cfg["music_section"], "Music")
            self.assertNotIn(login_id, server_module.PLEX_OAUTH)


if __name__ == "__main__":
    unittest.main()
