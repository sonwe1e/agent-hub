import io
import json
import unittest
from unittest.mock import patch

from support import ROOT
import launch


class LaunchTests(unittest.TestCase):
    def test_ready_requires_application_and_version(self):
        for data, expected in (({"app": "agent-hub", "uiVersion": 2}, True),
                               ({"app": "agent-hub", "uiVersion": 1}, False),
                               ({"providerCount": 4}, False),
                               ({"app": "other", "uiVersion": 2}, False)):
            with self.subTest(data=data), patch.object(launch.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
                self.assertEqual(launch.ready(8787), expected)

    def test_reuses_existing_new_server_without_starting_process(self):
        with patch.object(launch, "ready", return_value=True), patch.object(launch.subprocess, "Popen") as start:
            self.assertEqual(launch.ensure_server(8787), 8787)
            start.assert_not_called()

    def test_old_busy_port_falls_back_without_killing_existing_process(self):
        with patch.object(launch, "ready", side_effect=[False, True]), patch.object(launch, "free", return_value=False), patch.object(launch.subprocess, "Popen") as start:
            self.assertEqual(launch.ensure_server(8787), 8788)
            start.assert_not_called()

    def test_no_free_port_gives_clear_error(self):
        with patch.object(launch, "ready", return_value=False), patch.object(launch, "free", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "端口"):
                launch.ensure_server(8787)

    def test_browser_mode_uses_browser_fallback(self):
        with patch.object(launch.webbrowser, "open") as opened:
            launch.open_workspace(8788, browser=True)
            opened.assert_called_once_with("http://127.0.0.1:8788/")


if __name__ == "__main__":
    unittest.main()
