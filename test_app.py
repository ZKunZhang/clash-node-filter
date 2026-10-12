import http.client
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        (self.root / 'index.html').write_text('__SESSION_TOKEN__')
        self.patch = patch.object(app, 'ROOT', self.root)
        self.patch.start()
        self.server = app.http.server.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.server.host = f'127.0.0.1:{self.server.server_port}'
        self.server.origin = f'http://{self.server.host}'
        self.server.token = 'test-session'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.patch.stop()
        self.directory.cleanup()

    def request(self, method, path, data=None, override=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'X-Session-Token': self.server.token, 'Origin': self.server.origin}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        headers.update(override or {})
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        status, body = response.status, response.read().decode()
        connection.close()
        return status, body

    def test_private_paths_and_cross_origin_blocked(self):
        self.assertEqual(self.request('GET', '/subscriptions.local.json')[0], 404)
        self.assertEqual(self.request('GET', '/inputs/rome.yaml')[0], 404)
        self.assertEqual(self.request('GET', '/api/settings', override={'X-Session-Token': ''})[0], 403)
        self.assertEqual(self.request('GET', '/', override={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/export', {'yaml': 'x'}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/'), (200, self.server.token))

    def test_save_reload_export_and_permissions(self):
        settings = {'urls': ['https://example.com/sub?token=private'], 'preferences': {'interval': 180}}
        self.assertEqual(self.request('POST', '/api/save', settings)[0], 200)
        data = json.loads(self.request('GET', '/api/settings')[1])
        self.assertEqual(data['urls'], settings['urls'])
        self.assertEqual(data['preferences'], settings['preferences'])
        self.assertEqual((self.root / 'subscriptions.local.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request('POST', '/api/export', {'yaml': 'proxies: []\n'})[0], 200)
        self.assertEqual((self.root / 'output/clash-filtered.yaml').read_text(), 'proxies: []\n')
        self.assertEqual(self.request('POST', '/api/save', {'urls': ['http://example.com']})[0], 400)
        (self.root / 'preferences.local.json').write_text('broken')
        self.assertEqual(self.request('GET', '/api/settings')[0], 500)

    def test_fetch_keeps_order_and_reports_failure(self):
        def fixture(pair):
            i, _ = pair
            return {'label': f'订阅 {i + 1}', 'text': 'proxies: []'} if i == 0 else {'label': '订阅 2', 'error': 'HTTP 403'}
        with patch.object(app, 'fetch_source', fixture):
            status, body = self.request('POST', '/api/fetch', {'urls': ['https://a.example', 'https://b.example']})
        self.assertEqual(status, 200)
        sources = json.loads(body)['sources']
        self.assertEqual([s['label'] for s in sources], ['订阅 1', '订阅 2'])
        self.assertIn('error', sources[1])
        self.assertEqual(self.request('POST', '/api/fetch', {'urls': []})[0], 400)
        with self.assertRaises(ValueError):
            app.SafeRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')

    def test_probe_job_progress_result_and_report_export(self):
        jobs = app.ProbeJobs()
        def fixture(proxies, core, progress=None, cancel_event=None):
            progress(1, 1)
            return {'checkedAt': 'test', 'testUrl': 'https://www.gstatic.com/generate_204', 'results': [{'name': 'Japan', 'status': 'ok', 'delay': 100}]}
        with patch.object(app, 'PROBES', jobs), patch.object(app, 'core_path', return_value='fake-core'), patch.object(app, 'run_probe', fixture):
            status, body = self.request('POST', '/api/probe', {'proxies': [{'name': 'Japan'}]})
            self.assertEqual(status, 202)
            job_id = json.loads(body)['job']
            for _ in range(30):
                job = json.loads(self.request('GET', '/api/probe?job=' + job_id)[1])
                if job['state'] != 'running':
                    break
                time.sleep(0.01)
            self.assertEqual(job['state'], 'complete')
            self.assertEqual(job['done'], 1)
            self.assertEqual(self.request('GET', '/api/probe?job=missing')[0], 404)
            self.assertEqual(self.request('GET', '/api/probe?job=' + job_id, override={'X-Session-Token': ''})[0], 403)
            self.assertEqual(self.request('POST', '/api/export', {'yaml': 'proxies: []', 'report': job['report']})[0], 200)
            report_file = self.root / 'output/health-report.local.json'
            self.assertEqual(json.loads(report_file.read_text()), job['report'])
            self.assertEqual(report_file.stat().st_mode & 0o777, 0o600)
            jobs.stop()
        with patch.object(app, 'core_path', return_value=None):
            self.assertEqual(self.request('POST', '/api/probe', {'proxies': [{'name': 'Japan'}]})[0], 503)

    def test_probe_rejects_overlap_and_global_failure_is_not_dead_nodes(self):
        jobs = app.ProbeJobs()
        started, release = threading.Event(), threading.Event()
        def fixture(*args, **kwargs):
            started.set()
            release.wait(2)
            raise RuntimeError('检测核心启动失败')
        with patch.object(app, 'PROBES', jobs), patch.object(app, 'core_path', return_value='fake-core'), patch.object(app, 'run_probe', fixture):
            try:
                status, body = self.request('POST', '/api/probe', {'proxies': [{'name': 'Japan'}]})
                self.assertEqual(status, 202)
                self.assertTrue(started.wait(1))
                self.assertEqual(self.request('POST', '/api/probe', {'proxies': [{'name': 'Japan'}]})[0], 409)
                release.set()
                jobs.stop()
                job = jobs.snapshot(json.loads(body)['job'])
                self.assertEqual(job['state'], 'error')
                self.assertNotIn('report', job)
            finally:
                release.set()
                jobs.stop()


if __name__ == '__main__':
    unittest.main()
