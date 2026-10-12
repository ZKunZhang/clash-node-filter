"""Isolated, bounded Mihomo reachability checks (Python 3.9 standard library)."""

import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TEST_URL = "https://www.gstatic.com/generate_204"
MAX_WORKERS = 8
STARTUP_SECONDS = 10


def _request(base, secret, path, timeout):
    # Do not inherit HTTP_PROXY or a user's system proxy for the controller.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        base + path, headers={"Authorization": "Bearer " + secret}
    )
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Unexpected controller response")
        return json.loads(response.read(4 * 1024 * 1024))


def _check_node(base, secret, name, timeout_ms, attempts, cancel_event=None):
    query = urllib.parse.urlencode(
        {"timeout": timeout_ms, "url": TEST_URL, "expected": 204}
    )
    path = "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?" + query
    result = {"name": name, "status": "failed", "delay": None,
              "reason": "节点连接失败", "attempts": 0}
    for attempt in range(1, attempts + 1):
        _check_cancelled(cancel_event)
        result["attempts"] = attempt
        try:
            payload = _request(base, secret, path, timeout_ms / 1000 + 2)
            delay = payload.get("delay") if isinstance(payload, dict) else None
            if type(delay) is int and delay > 0:
                result.update(status="ok", delay=delay, reason="连接成功")
                break
            result.update(status="failed", reason="核心返回无效延迟")
        except urllib.error.HTTPError as error:
            if error.code == 504:
                result.update(status="timeout", reason="节点连接超时")
            else:
                result.update(status="failed", reason="节点连接失败")
            error.close()
        except (TimeoutError, socket.timeout):
            result.update(status="timeout", reason="节点连接超时")
        except urllib.error.URLError as error:
            timed_out = isinstance(error.reason, (TimeoutError, socket.timeout))
            result.update(status="timeout" if timed_out else "failed",
                          reason="节点连接超时" if timed_out else "节点连接失败")
        except (OSError, ValueError, TypeError):
            # Never return controller error bodies or proxy credentials.
            result.update(status="failed", reason="节点连接失败")
    return result


def _stop(process):
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    except ProcessLookupError:
        pass


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("检测已取消。")


def run_probe(proxies, core, progress=None, timeout_ms=5000, attempts=2, cancel_event=None):
    """Check all supplied nodes; global failures raise safe Chinese RuntimeError."""
    _check_cancelled(cancel_event)
    if not isinstance(proxies, list) or not 1 <= len(proxies) <= 1000:
        raise RuntimeError("检测需要 1 至 1000 个节点")
    names = []
    for proxy in proxies:
        name = proxy.get("name") if isinstance(proxy, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("节点名称不能为空")
        names.append(name)
    if len(set(names)) != len(names):
        raise RuntimeError("节点名称必须唯一")
    if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000:
        raise RuntimeError("检测超时参数无效")
    if type(attempts) is not int or attempts not in (1, 2):
        raise RuntimeError("检测次数只能为 1 或 2")
    try:
        executable = Path(core).expanduser().resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("未找到可执行的 Mihomo 核心")
    except (OSError, TypeError, ValueError):
        raise RuntimeError("未找到可执行的 Mihomo 核心") from None

    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="mihomo-probe-") as directory:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            secret = secrets.token_urlsafe(32)
            base = "http://127.0.0.1:" + str(port)
            config = {
                "external-controller": "127.0.0.1:" + str(port),
                "secret": secret,
                "mixed-port": 0, "port": 0, "socks-port": 0,
                "allow-lan": False, "mode": "rule", "log-level": "silent",
                "proxies": proxies, "proxy-groups": [], "rules": ["MATCH,DIRECT"],
                "dns": {"enable": False},
                "profile": {"store-selected": False, "store-fake-ip": False},
            }
            config_path = Path(directory) / "config.json"
            try:
                config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                config_path.chmod(0o600)
                process = subprocess.Popen(
                    [str(executable), "-d", directory, "-f", str(config_path)],
                    cwd=directory, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, ValueError, TypeError):
                raise RuntimeError("无法启动独立 Mihomo 检测核心") from None
            try:
                deadline = time.monotonic() + STARTUP_SECONDS
                ready = False
                while time.monotonic() < deadline:
                    _check_cancelled(cancel_event)
                    if process.poll() is not None:
                        raise RuntimeError("独立 Mihomo 检测核心启动失败，请检查节点配置")
                    try:
                        version = _request(base, secret, "/version", min(0.5, max(0.01, deadline - time.monotonic())))
                        ready = isinstance(version, dict) and bool(version.get("version"))
                        if ready:
                            break
                    except (OSError, ValueError, TypeError, urllib.error.URLError):
                        pass
                    time.sleep(0.05)
                if not ready:
                    raise RuntimeError("独立 Mihomo 检测核心启动超时")
                _check_cancelled(cancel_event)
                try:
                    loaded = _request(base, secret, "/proxies", 2)
                    registered = loaded.get("proxies") if isinstance(loaded, dict) else None
                except (OSError, ValueError, TypeError, urllib.error.URLError):
                    raise RuntimeError("无法读取独立核心的节点列表") from None
                if not isinstance(registered, dict) or any(name not in registered for name in names):
                    raise RuntimeError("独立核心未加载全部节点，检测已停止")
                results = [None] * len(names)
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {
                        executor.submit(_check_node, base, secret, name, timeout_ms, attempts, cancel_event): index
                        for index, name in enumerate(names)
                    }
                    done = 0
                    for future in concurrent.futures.as_completed(futures):
                        _check_cancelled(cancel_event)
                        results[futures[future]] = future.result()
                        if process.poll() is not None:
                            raise RuntimeError("独立 Mihomo 检测核心意外退出，检测已停止")
                        done += 1
                        if progress is not None:
                            progress(done, len(names))
                _check_cancelled(cancel_event)
                return {
                    "checkedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "testUrl": TEST_URL, "timeoutMs": timeout_ms, "results": results,
                }
            finally:
                # Stop before deleting its private working directory, even on errors.
                _stop(process)
                process = None
    finally:
        _stop(process)
