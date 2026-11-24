"""Who may drive the unauthenticated local API, and a few rendering/locking rules.

- Host must name this machine (DNS rebinding), a browser POST must come from the app's own page (CSRF)
- request size limit, no set swap while a background update runs, delete keeps a backup
- links from source data: http(s) only; placeholders substituted once
Run: python3 -m unittest tests.test_server_gate
"""
import http.client
import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ford_db  # noqa: E402
import server  # noqa: E402
import test_load_upload as tlu  # noqa: E402  (fake car set + running server)


class Gate(tlu.Workflow):
    def raw(self, method, path, body=b'', headers=None):
        port = self.httpd.server_address[1]
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        h = {'Content-Type': 'text/plain'}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data


    def test_foreign_host_refused(self):           # DNS rebinding: evil.example -> 127.0.0.1
        self.assertEqual(self.raw('GET', '/api/session', headers={'Host': 'evil.example:8000'})[0], 403)
        self.assertEqual(self.raw('POST', '/api/settings', b'{}', {'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.raw('GET', '/api/session', headers={'Host': 'localhost:1'})[0], 200)

    def test_cross_site_post_refused(self):        # CSRF: text/plain JSON needs no preflight
        port = self.httpd.server_address[1]
        body = json.dumps({'fm': [1, 2, 3]}).encode()
        code, _ = self.raw('POST', '/api/delete_records', body, {'Origin': 'https://evil.example'})
        self.assertEqual(code, 403)
        code, _ = self.raw('POST', '/api/delete_records', body, {'Origin': 'null'})
        self.assertEqual(code, 403)
        code, _ = self.raw('POST', '/api/session_does_not_exist', b'{}', {'Origin': f'http://127.0.0.1:{port}'})
        self.assertEqual(code, 404, 'own origin passes the gate')

    def test_body_limit(self):
        # no body bytes on the wire: an unread byte could make Windows reset the connection (flaky 10053)
        code, _ = self.raw('POST', '/api/station', b'', {'Content-Length': str(server.BODY_MAX + 1)})
        self.assertEqual(code, 413)

    def test_no_set_swap_during_update(self):
        ford_db.PROGRESS.start('test')
        try:
            # a small body: the gate answers before parsing, and a 2 MB upload the server doesn't
            # read would race the client's send (EPIPE / WinError 10053 -> flaky test)
            self.assertEqual(self.raw('POST', '/api/load_upload', b'x')[0], 409)
            self.assertEqual(self.raw('POST', '/api/settings', b'{"language": "en"}')[0], 409,
                             'no settings switch under a running update')
        finally:
            ford_db.PROGRESS.finish()
        self.assertFalse(os.path.exists(server.IMG_ORIG), 'nothing was loaded')

    def test_delete_keeps_a_backup(self):
        self.assertEqual(self.req('/api/load_upload', *self.files())[0], 200)
        code, d = self.req('/api/delete_records', {'fm': [1]})
        self.assertEqual((code, d['deleted']), (200, 1), d)
        baks = [n for n in os.listdir(os.path.dirname(server.DB_FM)) if '.bak-del-' in n]
        self.assertEqual(len(baks), 1)
        c = sqlite3.connect(os.path.join(os.path.dirname(server.DB_FM), baks[0]))
        self.assertEqual(c.execute('select count(*) from fm_station_logo').fetchone()[0], 2)
        c.close()


# the inherited Workflow tests already run in test_load_upload: not callable = not collected here
for _n in [n for n in dir(tlu.Workflow) if n.startswith('test_')]:
    setattr(Gate, _n, None)


class Rendering(unittest.TestCase):
    def test_only_http_links(self):
        for url, ok in (('javascript:alert(1)', False), (' JavaScript:x', False), ('data:text/html,x', False),
                        ('', False), (None, False), ('https://ok.example/x', True), ('HTTP://a.b', True)):
            self.assertEqual(server._safe_href(url), ok, url)


if __name__ == '__main__':
    unittest.main()
