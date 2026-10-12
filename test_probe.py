import io
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

import probe


class ProbeTests(unittest.TestCase):
    def test_delay_classification_and_retry(self):
        cases = [
            ([{"delay": 8}], "ok", 8, 1),
            ([urllib.error.HTTPError("url", 504, "private-password", {}, io.BytesIO(b"secret"))] * 2, "timeout", None, 2),
            ([urllib.error.HTTPError("url", 503, "private-password", {}, io.BytesIO(b"secret"))] * 2, "failed", None, 2),
            ([socket.timeout("secret"), {"delay": 9}], "ok", 9, 2),
            ([urllib.error.URLError(socket.timeout("secret"))] * 2, "timeout", None, 2),
            ([{"delay": 0}, {"delay": True}], "failed", None, 2),
            ([urllib.error.URLError("private-password")] * 2, "failed", None, 2),
        ]
        for responses, status, delay, attempts in cases:
            with self.subTest(status=status, responses=responses):
                with patch.object(probe, "_request", side_effect=responses) as request:
                    result = probe._check_node("http://local", "secret", "香港 /?#", 5000, 2)
                self.assertEqual((result["status"], result["delay"], result["attempts"]), (status, delay, attempts))
                self.assertNotIn("secret", result["reason"])
                self.assertNotIn("private-password", result["reason"])
                url = request.call_args.args[2]
                self.assertTrue(url.startswith("/proxies/%E9%A6%99%E6%B8%AF%20%2F%3F%23/delay?"))
                query = urllib.parse.parse_qs(url.split("?", 1)[1])
                self.assertEqual(query["expected"], ["204"])
                self.assertEqual(query["timeout"], ["5000"])
                self.assertEqual(query["url"], [probe.TEST_URL])
                self.assertEqual(request.call_args.args[3], 7)

    def setup_core(self, names, missing=False, exited=False):
        process = Mock()
        process.poll.return_value = 1 if exited else None
        self.launches = []

        def launch(command, **kwargs):
            directory = command[2]
            config = json.loads(Path(command[4]).read_text())
            self.launches.append((directory, config, command, kwargs))
            return process

        def request(base, secret, path, timeout):
            config = self.launches[-1][1]
            self.assertEqual(secret, config["secret"])
            self.assertTrue(base.startswith("http://127.0.0.1:"))
            if path == "/version":
                return {"version": "fake"}
            if path == "/proxies":
                return {"proxies": {} if missing else {name: {} for name in names}}
            name = urllib.parse.unquote(path.split("/", 2)[2].split("/delay?", 1)[0])
            if name == names[0]:
                time.sleep(0.02)
            return {"delay": 12}

        return process, patch.object(probe.subprocess, "Popen", side_effect=launch), patch.object(probe, "_request", side_effect=request)

    def test_isolation_order_progress_and_cleanup(self):
        names = ["first", "second / name", "third"]
        proxies = [{"name": name, "type": "ss", "password": "private"} for name in names]
        process, launch, request = self.setup_core(names)
        progress = Mock()
        with launch, request:
            # Existing executable supplies the path check; subprocess itself is mocked.
            import sys
            result = probe.run_probe(proxies, sys.executable, progress=progress)
        self.assertEqual([r["name"] for r in result["results"]], names)
        self.assertEqual([call.args for call in progress.call_args_list], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(result["timeoutMs"], 5000)
        self.assertTrue(result["checkedAt"].endswith("+00:00"))
        directory, config, command, options = self.launches[0]
        self.assertFalse(Path(directory).exists())
        self.assertEqual(config["proxies"], proxies)
        self.assertEqual(config["proxy-groups"], [])
        self.assertEqual(config["rules"], ["MATCH,DIRECT"])
        self.assertEqual(config["dns"], {"enable": False})
        self.assertEqual(config["profile"], {"store-selected": False, "store-fake-ip": False})
        self.assertFalse(config["allow-lan"])
        self.assertEqual([config[key] for key in ("mixed-port", "port", "socks-port")], [0, 0, 0])
        self.assertGreaterEqual(len(config["secret"]), 40)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_global_errors_and_cleanup(self):
        import sys
        for missing, exited, expected in [(True, False, "未加载全部"), (False, True, "启动失败")]:
            with self.subTest(missing=missing, exited=exited):
                process, launch, request = self.setup_core(["one"], missing, exited)
                with launch, request, self.assertRaisesRegex(RuntimeError, expected):
                    probe.run_probe([{"name": "one"}], sys.executable)
                process.wait.assert_called_once()
                self.assertFalse(Path(self.launches[0][0]).exists())

    def test_invalid_input_does_not_start_core(self):
        import sys
        with patch.object(probe.subprocess, "Popen") as launch:
            for proxies in [[], [{"name": "same"}] * 2, [{"name": ""}], [{"name": "node"}] * 1001]:
                with self.assertRaises(RuntimeError):
                    probe.run_probe(proxies, sys.executable)
            with self.assertRaisesRegex(RuntimeError, "未找到"):
                probe.run_probe([{"name": "one"}], "/missing/core")
            launch.assert_not_called()

    def test_start_timeout_cleanup(self):
        import sys
        process, launch, unused = self.setup_core(["one"])
        with launch, patch.object(probe, "STARTUP_SECONDS", 0.01), patch.object(probe, "_request", side_effect=urllib.error.URLError("private")):
            with self.assertRaisesRegex(RuntimeError, "启动超时"):
                probe.run_probe([{"name": "one"}], sys.executable)
        process.terminate.assert_called_once()
        self.assertFalse(Path(self.launches[0][0]).exists())

    def test_cancellation_and_kill_fallback(self):
        import sys
        event = threading.Event()
        process, launch, request = self.setup_core(["one", "two"])
        process.wait.side_effect = [subprocess.TimeoutExpired("fake", 3), None]
        def cancel(done, total):
            event.set()
        with launch, request, self.assertRaisesRegex(RuntimeError, "检测已取消"):
            probe.run_probe([{"name": "one"}, {"name": "two"}], sys.executable, progress=cancel, cancel_event=event)
        process.kill.assert_called_once()
        self.assertFalse(Path(self.launches[0][0]).exists())
        with patch.object(probe, "_request") as request:
            with self.assertRaisesRegex(RuntimeError, "检测已取消"):
                probe._check_node("base", "secret", "one", 5000, 2, event)
            request.assert_not_called()

    def test_callback_failure_still_cleans_up(self):
        import sys
        process, launch, request = self.setup_core(["one"])
        with launch, request, self.assertRaisesRegex(ValueError, "callback"):
            probe.run_probe([{"name": "one"}], sys.executable, progress=Mock(side_effect=ValueError("callback")))
        process.terminate.assert_called_once()
        self.assertFalse(Path(self.launches[0][0]).exists())


if __name__ == "__main__":
    unittest.main()
