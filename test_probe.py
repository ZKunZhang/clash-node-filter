import concurrent.futures
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

    def setup_core(self, names, missing=False, exited=False, responses=None):
        process = Mock()
        process.poll.return_value = 1 if exited else None
        self.launches = []
        self.processes = []
        self.checked = []

        def launch(command, **kwargs):
            directory = command[2]
            config = json.loads(Path(command[4]).read_text())
            self.launches.append((directory, config, command, kwargs))
            current_process = process if len(self.launches) == 1 else Mock()
            current_process.poll.return_value = 1 if exited else None
            self.processes.append(current_process)
            return current_process

        def request(base, secret, path, timeout):
            config = self.launches[-1][1]
            self.assertEqual(secret, config["secret"])
            self.assertTrue(base.startswith("http://127.0.0.1:"))
            if path == "/version":
                return {"version": "fake"}
            if path == "/proxies":
                return {"proxies": {} if missing else {name: {} for name in names}}
            name = urllib.parse.unquote(path.split("/", 2)[2].split("/delay?", 1)[0])
            round_index = len(self.launches) - 1
            self.checked.append((round_index, name))
            if name == names[0]:
                time.sleep(0.02)
            if responses is not None:
                response = responses[round_index][name]
                if isinstance(response, Exception):
                    raise response
                return response
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
            result = probe.run_probe(proxies, sys.executable, progress=progress, verification_rounds=1)
        self.assertEqual([r["name"] for r in result["results"]], names)
        self.assertEqual([call.args for call in progress.call_args_list], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(result["timeoutMs"], 5000)
        self.assertEqual(result["verificationRounds"], 1)
        self.assertTrue(all(len(row["checks"]) == 1 for row in result["results"]))
        self.assertTrue(result["checkedAt"].endswith("+00:00"))
        directory, config, command, options = self.launches[0]
        self.assertFalse(Path(directory).exists())
        self.assertEqual(config["proxies"], proxies)
        self.assertEqual(config["proxy-groups"], [])
        self.assertEqual(config["rules"], ["MATCH,DIRECT"])
        self.assertEqual(config["dns"], probe.DNS_CONFIG)
        self.assertNotIn("listen", config["dns"])
        self.assertEqual(config["profile"], {"store-selected": False, "store-fake-ip": False})
        self.assertFalse(config["allow-lan"])
        self.assertEqual([config[key] for key in ("mixed-port", "port", "socks-port")], [0, 0, 0])
        self.assertGreaterEqual(len(config["secret"]), 40)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=3)

    def test_two_cold_rounds_require_stability_and_preserve_dependencies(self):
        import sys
        names = ["stable", "flaky", "dependency"]
        proxies = [{"name": "stable", "dialer-proxy": "dependency"},
                   {"name": "flaky"}, {"name": "dependency"}]
        responses = [
            {"stable": {"delay": 12}, "flaky": {"delay": 9},
             "dependency": socket.timeout("private")},
            {"stable": {"delay": 28}, "flaky": socket.timeout("private")},
        ]
        process, launch, request = self.setup_core(names, responses=responses)
        progress = Mock()
        with launch, request:
            result = probe.run_probe(proxies, sys.executable, progress=progress)
        self.assertEqual([row["name"] for row in result["results"]], names)
        stable, flaky, dependency = result["results"]
        self.assertEqual((stable["status"], stable["delay"], stable["attempts"]), ("ok", 28, 2))
        self.assertEqual([check["delay"] for check in stable["checks"]], [12, 28])
        self.assertEqual((flaky["status"], flaky["delay"], flaky["attempts"]), ("timeout", None, 3))
        self.assertEqual(flaky["reason"], "复测失败：节点连接超时")
        self.assertEqual(len(flaky["checks"]), 2)
        self.assertEqual((dependency["status"], len(dependency["checks"])), ("timeout", 1))
        self.assertNotIn((1, "dependency"), self.checked)
        self.assertEqual([call.args for call in progress.call_args_list],
                         [(1, 3), (2, 3), (3, 3), (4, 5), (5, 5)])
        self.assertEqual(result["verificationRounds"], 2)
        self.assertEqual(result["dns"], {"enable": True, "enhanced-mode": "fake-ip",
                                          "nameserver": ["223.5.5.5", "119.29.29.29"]})
        self.assertEqual(len(self.launches), 2)
        self.assertNotEqual(self.launches[0][0], self.launches[1][0])
        self.assertNotEqual(self.launches[0][1]["secret"], self.launches[1][1]["secret"])
        for directory, config, command, options in self.launches:
            self.assertFalse(Path(directory).exists())
            self.assertEqual(config["proxies"], proxies)
            self.assertEqual(config["dns"], result["dns"])
        for current_process in self.processes:
            current_process.terminate.assert_called_once()
            current_process.wait.assert_called_once_with(timeout=3)

    def test_no_success_does_not_start_verification_core(self):
        import sys
        process, launch, request = self.setup_core(
            ["one"], responses=[{"one": {"delay": 0}}])
        with launch, request:
            result = probe.run_probe([{"name": "one"}], sys.executable)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(result["results"][0]["checks"]), 1)
        self.assertEqual(result["verificationRounds"], 2)

    def test_cancellation_between_rounds_does_not_start_second_core(self):
        import sys
        event = threading.Event()
        process, launch, request = self.setup_core(["one"])

        def stop(current_process):
            current_process.terminate()
            current_process.wait(timeout=3)
            event.set()

        # The outer cleanup also calls _stop(None).
        def cancel_stop(current_process):
            if current_process is not None:
                stop(current_process)
        with launch, request, patch.object(probe, "_stop", side_effect=cancel_stop):
            with self.assertRaisesRegex(RuntimeError, "检测已取消"):
                probe.run_probe([{"name": "one"}], sys.executable, cancel_event=event)
        self.assertEqual(len(self.launches), 1)
        self.assertFalse(Path(self.launches[0][0]).exists())

    def test_second_round_still_requires_full_proxy_registry(self):
        import sys
        process, launch, request = self.setup_core(
            ["one", "dependency"], responses=[
                {"one": {"delay": 8}, "dependency": {"delay": 0}},
                {"one": {"delay": 8}},
            ])
        def registry_request(base, secret, path, timeout):
            if len(self.launches) == 2 and path == "/proxies":
                return {"proxies": {"one": {}}}
            return original_request(base, secret, path, timeout)

        with launch, request as original_request:
            with patch.object(probe, "_request", side_effect=registry_request):
                with self.assertRaisesRegex(RuntimeError, "未加载全部"):
                    probe.run_probe([{"name": "one", "dialer-proxy": "dependency"},
                                     {"name": "dependency"}], sys.executable)
        self.assertEqual(len(self.launches), 2)
        for directory, config, command, options in self.launches:
            self.assertFalse(Path(directory).exists())
        for current_process in self.processes:
            current_process.wait.assert_called_once()

    def test_second_round_callback_failure_cleans_both_cores(self):
        import sys
        process, launch, request = self.setup_core(["one"])

        def progress(done, total):
            if done == 2:
                raise ValueError("callback")

        with launch, request, self.assertRaisesRegex(ValueError, "callback"):
            probe.run_probe([{"name": "one"}], sys.executable, progress=progress)
        self.assertEqual(len(self.launches), 2)
        for directory, config, command, options in self.launches:
            self.assertFalse(Path(directory).exists())
        for current_process in self.processes:
            current_process.wait.assert_called_once()

    def test_overlapping_probes_keep_each_core_private(self):
        import sys
        cores = {}
        lock = threading.Lock()
        rendezvous = threading.Barrier(2)

        def launch(command, **kwargs):
            config = json.loads(Path(command[4]).read_text())
            process = Mock()
            process.poll.return_value = None
            base = "http://" + config["external-controller"]
            with lock:
                cores[base] = (command[2], config, process)
            return process

        def request(base, secret, path, timeout):
            with lock:
                directory, config, process = cores[base]
            self.assertEqual(secret, config["secret"])
            if path == "/version":
                return {"version": "fake"}
            name = config["proxies"][0]["name"]
            if path == "/proxies":
                return {"proxies": {name: {}}}
            self.assertIn("/proxies/" + name + "/delay?", path)
            rendezvous.wait(timeout=3)
            return {"delay": 17}

        with patch.object(probe.subprocess, "Popen", side_effect=launch), \
                patch.object(probe, "_request", side_effect=request), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(probe.run_probe, [{"name": name}], sys.executable)
                       for name in ["one", "two"]]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual([[row["name"] for row in report["results"]] for report in results],
                         [["one"], ["two"]])
        self.assertEqual(len(cores), 4)
        self.assertEqual(len({directory for directory, config, process in cores.values()}), 4)
        for directory, config, process in cores.values():
            self.assertFalse(Path(directory).exists())
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
            for invalid in [0, 3, True, "2", None]:
                with self.assertRaisesRegex(RuntimeError, "复测轮数"):
                    probe.run_probe([{"name": "one"}], sys.executable, verification_rounds=invalid)
            for invalid in [0, 60001, True, "5000"]:
                with self.assertRaisesRegex(RuntimeError, "超时参数"):
                    probe.run_probe([{"name": "one"}], sys.executable, timeout_ms=invalid)
            for invalid in [0, 3, True, "2"]:
                with self.assertRaisesRegex(RuntimeError, "检测次数"):
                    probe.run_probe([{"name": "one"}], sys.executable, attempts=invalid)
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
