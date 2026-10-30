import io
import json
import unittest
from unittest.mock import patch

from support import ROOT
import launch
import desktop_app


class LaunchTests(unittest.TestCase):
    def test_default_and_gui_launch_desktop_without_http(self):
        for args in ([], ["gui"], ["desktop"]):
            with self.subTest(args=args), \
                    patch.object(desktop_app, "main", return_value=0) as desktop, \
                    patch.object(launch, "ensure_server") as server, \
                    patch.object(launch, "open_workspace") as browser:
                self.assertEqual(launch.main(args), 0)
                desktop.assert_called_once_with([])
                server.assert_not_called()
                browser.assert_not_called()

    def test_desktop_failure_does_not_silently_open_browser(self):
        with patch.object(desktop_app, "main", side_effect=RuntimeError("desktop unavailable")), \
                patch.object(launch, "ensure_server") as server, \
                patch.object(launch, "open_workspace") as browser, \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(launch.main([]), 1)
            self.assertIn("desktop unavailable", err.getvalue())
            server.assert_not_called()
            browser.assert_not_called()

    def test_explicit_ui_mode_keeps_browser_available(self):
        with patch.object(launch, "ensure_server", return_value=8788) as server, \
                patch.object(launch, "open_workspace") as browser, \
                patch.object(desktop_app, "main") as desktop, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(launch.main(["ui", "--port", "8788"]), 0)
            server.assert_called_once_with(8788)
            browser.assert_called_once_with(8788, browser=True)
            desktop.assert_not_called()

    def test_ready_requires_application_and_version(self):
        for data, expected in (({"app": "agent-hub", "uiVersion": 6}, True),
                               ({"app": "agent-hub", "uiVersion": 5}, False),
                               ({"app": "agent-hub", "uiVersion": 4}, False),
                               ({"app": "agent-hub", "uiVersion": 3}, False),
                               ({"app": "agent-hub", "uiVersion": 2}, False),
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
