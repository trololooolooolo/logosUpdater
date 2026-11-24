#!/usr/bin/env python3
"""Screenshots for docs/USER_GUIDE*.md, taken on the demo set (no real station logos).

Starts the app in a throw-away data directory with the demo set from tools/demo_set.py,
drives headless Firefox through geckodriver (plain WebDriver over HTTP, no Selenium) and
writes docs/img/<shot>-<lang>.png. Needs firefox + geckodriver on PATH.

  python3 tools/docs_screenshots.py                 # en + pl -> docs/img/
  python3 tools/docs_screenshots.py --langs pl --only overview,load
"""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import demo_set  # noqa: E402

NAMES = {'E2/3D01': 'Station A', 'E2/3D02': 'Station B', 'E2/3D03': 'Radio C', 'E2/3D04': 'Station G',
         'E2/3DA1': 'DAB One', 'E0/DD01': 'Sender E', 'E0/DD02': 'Welle F', 'E0/DDA1': 'Welle F DAB'}
SHOTS = ('empty', 'load', 'overview', 'station', 'style', 'changes', 'usb', 'settings', 'advanced')
W, H = 1400, 900


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f'no answer from {url}')


class Browser:
    """Minimal W3C WebDriver client."""

    def __init__(self, port):
        self.base = f'http://127.0.0.1:{port}'
        caps = {'capabilities': {'alwaysMatch': {'moz:firefoxOptions': {'args': ['-headless']}}}}
        self.sid = self._call('POST', '/session', caps)['sessionId']
        self.call('POST', '/window/rect', {'width': W, 'height': H})

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={'Content-Type': 'application/json'})
        return json.loads(urllib.request.urlopen(r, timeout=120).read())['value']

    def call(self, method, path, body=None):
        return self._call(method, f'/session/{self.sid}{path}', body)

    def go(self, url):
        self.call('POST', '/url', {'url': url})

    def js(self, script, *args):
        return self.call('POST', '/execute/sync', {'script': script, 'args': list(args)})

    def wait(self, script, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(script):
                return
            time.sleep(0.2)
        raise RuntimeError(f'timeout waiting for: {script}')

    def shot(self, path):
        with open(path, 'wb') as f:
            f.write(base64.b64decode(self.call('GET', '/screenshot')))

    def close(self):
        self._call('DELETE', f'/session/{self.sid}')


def api(base, path, body):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                               headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=120).read())


MODAL_OPEN = "const m=document.getElementById(arguments[0]); return !!m && getComputedStyle(m).display!='none'"
IDLE = "return !document.body.classList.contains('busy')"


def run_lang(lang, out, only, gecko_port):
    with tempfile.TemporaryDirectory() as data:
        demo = demo_set.build(os.path.join(data, 'inputs', '00_demo'))
        with open(os.path.join(data, 'station_names.json'), 'w', encoding='utf-8') as f:
            json.dump(NAMES, f)
        port = free_port()
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, LOGOSUPDATER_DATA=data)
        log = open(os.path.join(data, 'server.out'), 'w')
        cmd = [sys.executable, os.path.join(ROOT, 'view_stations.py'), '--no-browser', '--port', str(port),
               '--lang', lang]
        srv = None
        b = None
        try:
            # 1) nothing loaded yet
            srv = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
            wait_http(base + '/api/session')
            b = Browser(gecko_port)

            def take(name, url='/', modal=None, before=None, settle=1.0):
                if only and name not in only:
                    return
                b.go('about:blank')         # a hash-only change would not reload the page
                b.go(base + url)
                b.wait("return document.readyState=='complete'")
                if before:
                    b.js(before)
                if modal:
                    b.wait(MODAL_OPEN.replace('arguments[0]', repr(modal)))
                time.sleep(settle)
                p = os.path.join(out, f'{name}-{lang}.png')
                b.shot(p)
                print('  ', os.path.relpath(p, ROOT))

            b.go(base + '/')
            b.js("try{localStorage.clear()}catch(e){}")
            take('empty')
            take('load', '/#load', modal='loadModal', settle=2)
            srv.terminate()
            srv.wait()
            # 2) demo set loaded (same as `--load`, the CLI way of "① Load from car")
            srv = subprocess.Popen(cmd + ['--load', demo], env=env, stdout=log, stderr=subprocess.STDOUT)
            wait_http(base + '/api/session')
            take('overview', '/?country=E2-3')
            take('station', '/?country=E2-3', modal='stationModal',
                 before="document.querySelector('button[onclick^=editStation]').click()")
            take('style', '/?country=E2-3', modal='styleModal', before='openStyle()', settle=4)
            # a few edits so that "③ Review changes" has something to show
            api(base, '/api/style_apply', {'country': 'E2-3', 'preset': 'white'})
            api(base, '/api/station', {'action': 'add', 'country': 'E2-3', 'type': 'FM', 'pi': '3D05',
                                       'freq': '99.9', 'name': 'My Station'})
            take('changes', '/?country=E2-3#changes', modal='changesModal', settle=3)
            take('usb', '/?country=E2-3#usb', modal='usbModal', settle=3)
            take('settings', '/?country=E2-3#settings', modal='settingsModal', settle=1.5)
            take('advanced', '/?country=E2-3', before="setAdvanced(true); document.getElementById('logoBg').value='sync_black'; applyLogoBg()",
                 settle=1.5)
        finally:
            if b:
                b.close()
            if srv:
                srv.terminate()
                srv.wait()
            log.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'img'))
    ap.add_argument('--langs', default='en,pl')
    ap.add_argument('--only', default='', help='comma-separated subset of: ' + ', '.join(SHOTS))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    only = {s for s in args.only.split(',') if s}
    gport = free_port()
    # geckodriver from snap can only write under $HOME: run it from there
    gd = subprocess.Popen(['geckodriver', '--port', str(gport)], cwd=os.path.expanduser('~'),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_http(f'http://127.0.0.1:{gport}/status')
        for lang in args.langs.split(','):
            print(lang)
            run_lang(lang, args.out, only, gport)
    finally:
        gd.terminate()


if __name__ == '__main__':
    main()
