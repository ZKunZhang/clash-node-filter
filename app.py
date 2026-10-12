#!/usr/bin/env python3
"""Local subscription fetcher; Python standard library only."""
import argparse
import base64
import concurrent.futures
import email.message
import http.server
import json
import pathlib
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from probe import DNS_CONFIG, run_probe

ROOT = pathlib.Path(__file__).resolve().parent
LIMIT = 8 * 1024 * 1024
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/ui.js': ('ui.js', 'text/javascript; charset=utf-8'),
    '/filter.js': ('filter.js', 'text/javascript; charset=utf-8'),
    '/vendor/js-yaml.min.js': ('vendor/js-yaml.min.js', 'text/javascript; charset=utf-8'),
}


class ProbeJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.thread = None
        self.cancel = threading.Event()

    def start(self, proxies, core):
        with self.lock:
            if self.job and self.job['state'] == 'running':
                raise RuntimeError('节点检测正在进行，请等待完成。')
            job_id = secrets.token_urlsafe(16)
            self.cancel = threading.Event()
            self.job = {'job': job_id, 'state': 'running', 'done': 0, 'total': len(proxies)}
        def progress(done, total):
            with self.lock:
                self.job.update(done=done, total=total)
        def worker():
            try:
                report = run_probe(proxies, core, progress=progress, cancel_event=self.cancel)
                with self.lock:
                    self.job.update(state='complete', report=report)
            except RuntimeError as error:
                with self.lock:
                    self.job.update(state='error', error=str(error))
            except Exception:
                with self.lock:
                    self.job.update(state='error', error='节点检测失败，请检查本地内核。')
        self.thread = threading.Thread(target=worker)
        self.thread.start()
        return job_id

    def snapshot(self, job_id):
        with self.lock:
            if not self.job or self.job['job'] != job_id:
                return None
            return dict(self.job)

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=15)


PROBES = ProbeJobs()


def core_path():
    for filename in ['mihomo', 'mihomo.exe']:
        path = ROOT / 'vendor' / filename
        if path.is_file():
            return str(path)
    return shutil.which('mihomo')


def private_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
        temporary = pathlib.Path(file.name)
        file.write(content)
    try:
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('订阅必须是 HTTPS 地址。')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_source(pair):
    index, url = pair
    label = f'订阅 {index + 1}'
    try:
        validate_url(url)
        opener = urllib.request.build_opener(SafeRedirect())
        req = urllib.request.Request(url, headers={'User-Agent': 'Clash.Meta', 'Accept': 'application/yaml,text/yaml,*/*'})
        started = time.monotonic()
        with opener.open(req, timeout=15) as response:
            label = subscription_label(response.headers, url)
            chunks, size = [], 0
            while True:
                if time.monotonic() - started > 25:
                    raise TimeoutError()
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > LIMIT:
                    raise ValueError('订阅超过 8 MB 限制。')
                chunks.append(chunk)
        return {'label': label, 'text': b''.join(chunks).decode('utf-8-sig')}
    except urllib.error.HTTPError as error:
        return {'label': label, 'error': f'HTTP {error.code}，请检查订阅是否有效。'}
    except ValueError as error:
        return {'label': label, 'error': str(error) if '订阅' in str(error) else '订阅地址无效。'}
    except (TimeoutError, socket.timeout):
        return {'label': label, 'error': '请求超时，可导入客户端导出的 YAML。'}
    except Exception:
        return {'label': label, 'error': '下载失败，请检查网络或导入本地 YAML。'}


def subscription_label(headers, url):
    title = headers.get('profile-title', '').strip()
    if title.startswith('base64:'):
        try:
            encoded = title[7:]
            title = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True).decode('utf-8').strip()
        except (ValueError, UnicodeError):
            title = ''
    if title:
        return title
    disposition = email.message.Message()
    disposition['Content-Disposition'] = headers.get('Content-Disposition', '')
    filename = disposition.get_filename()
    if filename:
        filename = urllib.parse.unquote(filename).replace('\\', '/').rsplit('/', 1)[-1]
        name = pathlib.Path(filename).stem.strip()
        if name:
            return name
    return urllib.parse.urlsplit(url).hostname or '未命名机场'


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self, api=False):
        if self.headers.get('Host') != self.server.host:
            self.send(403, {'error': '访问来源不允许。'})
            return False
        if api and not secrets.compare_digest(self.headers.get('X-Session-Token', ''), self.server.token):
            self.send(403, {'error': '会话无效，请刷新本地工具。'})
            return False
        if self.command == 'POST' and self.headers.get('Origin') != self.server.origin:
            self.send(403, {'error': '访问来源不允许。'})
            return False
        return True

    def do_GET(self):
        if not self.authorized(api=self.path.startswith('/api/')):
            return
        if urllib.parse.urlsplit(self.path).path == '/api/probe':
            job_id = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('job', [''])[0]
            job = PROBES.snapshot(job_id)
            self.send(200 if job else 404, job or {'error': '检测任务不存在。'})
        elif self.path == '/api/settings':
            try:
                urls_file, prefs_file = ROOT / 'subscriptions.local.json', ROOT / 'preferences.local.json'
                urls = json.loads(urls_file.read_text()) if urls_file.exists() else []
                prefs = json.loads(prefs_file.read_text()) if prefs_file.exists() else {}
                local = [{'label': p.stem, 'text': p.read_text(encoding='utf-8-sig')} for p in sorted((ROOT / 'inputs').glob('*.yaml')) if p.stat().st_size <= LIMIT]
                self.send(200, {'urls': urls, 'preferences': prefs, 'sources': local})
            except Exception:
                self.send(500, {'error': '本地设置或文件无法读取，请检查格式和权限。'})
        elif self.path in STATIC:
            filename, mime = STATIC[self.path]
            data = (ROOT / filename).read_bytes()
            if self.path == '/':
                data = data.replace(b'__SESSION_TOKEN__', self.server.token.encode())
            self.send(200, data, mime)
        else:
            self.send(404, {'error': '文件不存在。'})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= LIMIT or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('请求格式或大小无效。')
            body = json.loads(self.rfile.read(length))
            if self.path == '/api/fetch':
                urls = body.get('urls')
                if not isinstance(urls, list) or not 1 <= len(urls) <= 20 or any(not isinstance(u, str) for u in urls):
                    raise ValueError('请填写 1–20 个订阅地址。')
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    sources = list(pool.map(fetch_source, enumerate(urls)))
                self.send(200, {'sources': sources})
            elif self.path == '/api/probe':
                proxies = body.get('proxies')
                if not isinstance(proxies, list) or not 1 <= len(proxies) <= 1000:
                    raise ValueError('节点数量无效。')
                if body.get('dns', DNS_CONFIG) != DNS_CONFIG:
                    raise ValueError('检测 DNS 与导出配置不一致。')
                core = core_path()
                if not core:
                    self.send(503, {'error': '未找到 Mihomo 检测内核，请安装或放入 vendor 目录。'})
                    return
                self.send(202, {'job': PROBES.start(proxies, core)})
            elif self.path == '/api/save':
                urls, prefs = body.get('urls', []), body.get('preferences', {})
                if not isinstance(urls, list) or len(urls) > 20 or not isinstance(prefs, dict):
                    raise ValueError('设置格式无效。')
                for url in urls:
                    validate_url(url)
                private_write(ROOT / 'subscriptions.local.json', json.dumps(urls, ensure_ascii=False, indent=2))
                private_write(ROOT / 'preferences.local.json', json.dumps(prefs, ensure_ascii=False, indent=2))
                self.send(200, {'ok': True})
            elif self.path == '/api/export':
                content = body.get('yaml')
                if not isinstance(content, str) or not content.strip():
                    raise ValueError('配置为空。')
                private_write(ROOT / 'output' / 'clash-filtered.yaml', content)
                if isinstance(body.get('report'), dict):
                    private_write(ROOT / 'output' / 'health-report.local.json', json.dumps(body['report'], ensure_ascii=False, indent=2))
                self.send(200, {'ok': True, 'path': 'output/clash-filtered.yaml'})
            else:
                self.send(404, {'error': '接口不存在。'})
        except RuntimeError as error:
            self.send(409, {'error': str(error)})
        except (ValueError, TypeError):
            self.send(400, {'error': '输入格式无效，请检查订阅和配置。'})
        except Exception:
            self.send(500, {'error': '本地处理失败，请检查文件权限。'})


def main():
    args = argparse.ArgumentParser(description='Clash / Mihomo 本地节点过滤工具')
    args.add_argument('--no-browser', action='store_true')
    args.add_argument('--port', type=int, default=0)
    options = args.parse_args()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', options.port), Handler)
    server.host = f'127.0.0.1:{server.server_port}'
    server.origin = f'http://{server.host}'
    server.token = secrets.token_urlsafe(32)
    print(f'本地工具已启动：{server.origin}\n关闭此终端或按 Ctrl+C 停止。', flush=True)
    def stop_signal(_signum, _frame):
        raise KeyboardInterrupt
    for signal_name in ['SIGTERM', 'SIGHUP']:
        exit_signal = getattr(signal, signal_name, None)
        if exit_signal is not None:
            signal.signal(exit_signal, stop_signal)
    try:
        if not options.no_browser:
            webbrowser.open(server.origin)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        PROBES.cancel.set()
        server.server_close()
        PROBES.stop()


if __name__ == '__main__':
    main()
