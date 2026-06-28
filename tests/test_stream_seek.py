from __future__ import annotations

import unittest
from unittest.mock import patch

from iamped import server as server_module


class FakeLibrary:
    def get_tracks(self, keys):
        return {
            keys[0]: {
                "part_key": "/library/parts/1/file.flac",
                "container": "flac",
            }
        }


class FakeStdout:
    def __init__(self):
        self.chunks = [b"audio", b""]

    def read(self, _size):
        return self.chunks.pop(0)


class FakeProcess:
    def __init__(self):
        self.stdout = FakeStdout()
        self.killed = False

    def kill(self):
        self.killed = True


class StreamSeekTest(unittest.TestCase):
    def setUp(self):
        server_module.app.config.update(TESTING=True)
        self.client = server_module.app.test_client()

    def test_lossless_seek_restarts_ffmpeg_at_requested_offset(self):
        process = FakeProcess()
        with (
            patch.object(server_module, "_lib", return_value=FakeLibrary()),
            patch.object(server_module, "get_server", return_value=object()),
            patch.object(server_module.plex_client, "stream_url",
                         return_value="https://plex.example/file.flac"),
            patch.object(server_module, "have_ffmpeg", return_value=True),
            patch.object(server_module.subprocess, "Popen",
                         return_value=process) as popen,
        ):
            response = self.client.get("/api/stream/123?start=61.250")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"audio")

        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-ss") + 1], "61.250")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-reconnect") + 1], "1")
        self.assertLess(command.index("-reconnect"), command.index("-i"))
        self.assertTrue(process.killed)

    def test_visualizer_window_limits_transcode_duration(self):
        process = FakeProcess()
        with (
            patch.object(server_module, "_lib", return_value=FakeLibrary()),
            patch.object(server_module, "get_server", return_value=object()),
            patch.object(server_module.plex_client, "stream_url",
                         return_value="https://plex.example/file.flac"),
            patch.object(server_module, "have_ffmpeg", return_value=True),
            patch.object(server_module.subprocess, "Popen",
                         return_value=process) as popen,
        ):
            response = self.client.get("/api/stream/123?start=12.500&duration=45")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"audio")
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
            self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "cross-origin")

        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("-ss") + 1], "12.500")
        self.assertEqual(command[command.index("-t") + 1], "45.000")


if __name__ == "__main__":
    unittest.main()
