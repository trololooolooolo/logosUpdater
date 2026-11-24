"""Guided workflow backend: upload of the three car files, session state, review of changes.

Runs a real server on a free port against a tiny QNX6 image built from a synthetic
template (no Ford files needed). Run: python3 -m unittest discover -s tests
"""
import http.server
import json
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import i18n  # noqa: E402
import server  # noqa: E402
from qnx6fs import QNX6Writer, BOOTBLOCK_SIZE  # noqa: E402
from PIL import Image  # noqa: E402

SCHEMA = "CREATE TABLE {t} (ECC TEXT, STATION_ID TEXT, RECEIVER_TYPE TEXT, FREQUENCY TEXT, LOGO_FILENAME TEXT)"


def make_image(tree, out):
    os.makedirs(os.path.join(tree, 'E2'), exist_ok=True)
    Image.new('RGBA', (160, 120), (255, 0, 0, 255)).save(os.path.join(tree, 'E2', 'ABC.PNG'))
    with open(os.path.join(tree, 'Version.inf'), 'w') as f:
        f.write('Version Number = TEST-1\n')
    tmpl = out + '.tmpl'
    sb = bytearray(512)
    struct.pack_into('<I', sb, 0x30, 1024)
    struct.pack_into('<I', sb, 0x44, 1)
    with open(tmpl, 'wb') as f:
        f.write(bytes(BOOTBLOCK_SIZE) + bytes(sb))
    QNX6Writer(tree, template_img=tmpl, image_size=1 << 20).write(out)
    os.remove(tmpl)


def make_db(path, table, rows):
    c = sqlite3.connect(path)
    c.execute(SCHEMA.format(t=table))
    c.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?)", rows)
    c.commit()
    c.close()


def read(p):
    with open(p, 'rb') as f:
        return f.read()


def multipart(files):
    b = uuid.uuid4().hex
    body = b''
    for field, (fname, data) in files.items():
        body += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b'\r\n'
    return body + f'--{b}--\r\n'.encode(), f'multipart/form-data; boundary={b}'


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        i18n.load('en')
        server.T = i18n.catalog()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.src = os.path.join(d, 'car')
        os.makedirs(self.src)
        make_image(os.path.join(d, 'tree'), os.path.join(self.src, 'station_logos.img'))
        make_db(os.path.join(self.src, 'fm_station_logo.db'), 'fm_station_logo',
                [('E2', '3001', 'F', '098500', 'ABC'), ('E2', '3002', 'F', '101300', 'ABC')])
        make_db(os.path.join(self.src, 'dab_station_logo.db'), 'dab_station_logo',
                [('E2', '3A01', 'D', '000000', 'ABC')])
        w = os.path.join(d, 'work')
        os.makedirs(w)
        p = {'image': f'{w}/station_logos.img', 'image_out': f'{w}/new.img', 'db_fm': f'{w}/fm_station_logo.db',
             'db_dab': f'{w}/dab_station_logo.db', 'db_ocr': f'{w}/ocr.db', 'logo_dir': f'{d}/sync5',
             'mount_dir': f'{d}/mnt', 'work_dir': f'{d}/merged', 'inputs_dir': f'{d}/inputs',
             'usb_dir': f'{d}/usb', 'logo_choices': f'{d}/choices.json', 'styled_dir': f'{d}/styled',
             'station_names': f'{d}/names.json'}
        self.cfg = config.deep_merge(config.DEFAULTS, {'paths': p})
        s = server
        s.CFG, s.SOURCE = self.cfg, None
        s.IMG_ORIG, s.IMG_OUT, s.DB_FM, s.DB_DAB = p['image'], p['image_out'], p['db_fm'], p['db_dab']
        s.LOGO_DIR, s.LOGO_DIR_NAME, s.MOUNT_DIR, s.WORK_DIR = p['logo_dir'], p['logo_dir'], p['mount_dir'], p['work_dir']
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), s.StationViewerHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, path, body=None, ctype='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=body, headers={'Content-Type': ctype} if body else {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def files(self, **over):
        f = {k: (n, read(os.path.join(self.src, n))) for k, n in server.UPLOAD_FIELDS.items()}
        f.update(over)
        return multipart(f)

    def test_nothing_loaded(self):
        code, s = self.req('/api/session')
        self.assertEqual((code, s['has_data'], s['step']), (200, False, 1))
        self.assertEqual(len(s['missing']), 3)

    def test_upload_then_review(self):
        code, d = self.req('/api/load_upload', *self.files())
        self.assertEqual((code, d['status']), (200, 'ok'), d)
        _, s = self.req('/api/session')
        self.assertEqual((s['has_data'], s['step'], s['firmware'], s['rows']), (True, 2, 'TEST-1', {'fm': 2, 'dab': 1}))
        self.assertEqual(s['loaded']['kind'], 'upload')
        self.assertFalse([n for n in os.listdir(os.path.dirname(server.IMG_ORIG)) if n.startswith('upload-')],
                         'upload dir is removed after loading')
        _, ch = self.req('/api/changes')
        self.assertTrue(ch['identical'], ch)
        # change one logo, add one row, delete one row
        c = sqlite3.connect(server.DB_FM)
        c.execute("UPDATE fm_station_logo SET LOGO_FILENAME='NEW1' WHERE STATION_ID='3001'")
        c.execute("DELETE FROM fm_station_logo WHERE STATION_ID='3002'")
        c.execute("INSERT INTO fm_station_logo VALUES ('E2','3003','1','099100','')")
        c.commit()
        c.close()
        os.makedirs(os.path.join(server.LOGO_DIR, 'E2'))
        Image.new('RGBA', (160, 120), (0, 255, 0, 255)).save(os.path.join(server.LOGO_DIR, 'E2', 'NEW1.PNG'))
        _, ch = self.req('/api/changes')
        self.assertFalse(ch['identical'])
        r = ch['rows']
        self.assertEqual((r['added']['count'], r['deleted']['count'], r['logo']['count']), (1, 1, 1))
        self.assertEqual(r['logo']['items'][0]['old_logo'], 'ABC')
        self.assertEqual(r['added']['items'][0]['freq'], '99.1')
        self.assertEqual(ch['logos']['added']['count'], 1)
        self.assertEqual(ch['logos']['added']['items'][0]['country'], 'E2-3')

    def test_basic_usb_package_builds_fresh_image(self):
        import hashlib
        import zipfile
        self.assertEqual(self.req('/api/load_upload', *self.files())[0], 200)
        os.makedirs(os.path.join(server.LOGO_DIR, 'E2'))
        Image.new('RGBA', (160, 120), (0, 0, 255, 255)).save(os.path.join(server.LOGO_DIR, 'E2', 'NEW2.PNG'))
        code, d = self.req('/api/usb_package', {'target': ''})          # no "image": basic mode
        self.assertEqual((code, d['status'], d['kind']), (200, 'ok', 'built'), d)
        self.assertEqual(d['build']['added'], 1)
        zp = os.path.join(self.cfg['paths']['usb_dir'], d['zip'])
        with zipfile.ZipFile(zp) as z:
            img = z.read('SyncMyMod/files/other/station_logos.img')
        self.assertEqual(hashlib.md5(img).hexdigest(), hashlib.md5(read(server.IMG_OUT)).hexdigest())

    def test_restore_package_is_the_loaded_files(self):
        import zipfile
        self.assertEqual(self.req('/api/load_upload', *self.files())[0], 200)
        # edits after loading: a DB row and a new logo
        c = sqlite3.connect(server.DB_FM)
        c.execute("UPDATE fm_station_logo SET LOGO_FILENAME='NEW1' WHERE STATION_ID='3001'")
        c.commit()
        c.close()
        os.makedirs(os.path.join(server.LOGO_DIR, 'E2'))
        Image.new('RGBA', (160, 120), (0, 0, 255, 255)).save(os.path.join(server.LOGO_DIR, 'E2', 'NEW1.PNG'))
        code, d = self.req('/api/usb_package', {'image': 'restore', 'target': ''})
        self.assertEqual((code, d['status'], d['kind'], d['build']), (200, 'ok', 'restore', None), d)
        with zipfile.ZipFile(os.path.join(self.cfg['paths']['usb_dir'], d['zip'])) as z:
            for name in server.UPLOAD_FIELDS.values():
                self.assertEqual(z.read('SyncMyMod/files/other/' + name), read(os.path.join(self.src, name)), name)

    def test_restore_needs_the_loaded_snapshot(self):
        self.assertEqual(self.req('/api/load_upload', *self.files())[0], 200)
        import shutil
        shutil.rmtree(os.path.join(os.path.dirname(server.IMG_ORIG), '.orig'))
        code, d = self.req('/api/usb_package', {'image': 'restore', 'target': ''})
        self.assertEqual(code, 400, d)

    def test_second_load_replaces_everything(self):
        """Regression: loading set B after set A used to crash half way (origin shadowed)."""
        import json
        import zipfile
        b = os.path.join(self.tmp.name, 'setB')
        os.makedirs(b)
        make_image(os.path.join(self.tmp.name, 'treeB'), os.path.join(b, 'station_logos.img'))
        make_db(os.path.join(b, 'fm_station_logo.db'), 'fm_station_logo', [('E0', 'D001', 'F', '090000', 'ABC')])
        make_db(os.path.join(b, 'dab_station_logo.db'), 'dab_station_logo', [('E0', 'D0A1', 'D', '000000', 'ABC')])
        config.load_inputs(self.cfg, self.src, origin=('usb', 'A'))
        config.load_inputs(self.cfg, b, origin=('usb', 'B'))
        work = os.path.dirname(server.IMG_ORIG)
        with open(os.path.join(work, '.loaded_from.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['label'], 'B')
        self.assertEqual(read(os.path.join(work, '.orig', 'fm_station_logo.db')), read(os.path.join(b, 'fm_station_logo.db')))
        code, d = self.req('/api/usb_package', {'image': 'restore', 'target': ''})
        self.assertEqual(code, 200, d)
        with zipfile.ZipFile(os.path.join(self.cfg['paths']['usb_dir'], d['zip'])) as z:
            for name in server.UPLOAD_FIELDS.values():
                self.assertEqual(z.read('SyncMyMod/files/other/' + name), read(os.path.join(b, name)), name)

    def test_partial_or_damaged_set_refused(self):
        """A folder with only the image (or a cut image) must not be mixed into the working set."""
        config.load_inputs(self.cfg, self.src, origin=('usb', 'A'))
        before = {p: read(p) for p in (server.IMG_ORIG, server.DB_FM, server.DB_DAB)}
        only_img = os.path.join(self.tmp.name, 'onlyimg')
        os.makedirs(only_img)
        with open(os.path.join(only_img, 'station_logos.img'), 'wb') as f:
            f.write(read(os.path.join(self.src, 'station_logos.img')))
        with self.assertRaises(config.InputError) as cm:
            config.load_inputs(self.cfg, only_img, origin=('usb', 'B'))
        self.assertEqual(cm.exception.code, 'missing')
        cut = os.path.join(self.tmp.name, 'cut')
        os.makedirs(cut)
        for n in server.UPLOAD_FIELDS.values():
            data = read(os.path.join(self.src, n))
            with open(os.path.join(cut, n), 'wb') as f:
                f.write(data[:len(data) // 2] if n.endswith('.img') else data)
        with self.assertRaises(config.InputError) as cm:
            config.load_inputs(self.cfg, cut, origin=('usb', 'C'))
        self.assertEqual(cm.exception.code, 'bad_img')
        self.assertEqual({p: read(p) for p in before}, before, 'working set untouched')
        self.assertFalse([n for n in os.listdir(os.path.dirname(server.IMG_ORIG)) if n.endswith('.new')])

    def test_wrong_db_refused_and_nothing_changes(self):
        bad = os.path.join(self.tmp.name, 'bad.db')
        make_db(bad, 'something_else', [])
        code, d = self.req('/api/load_upload', *self.files(dab=('dab_station_logo.db', read(bad))))
        self.assertEqual(code, 400)
        self.assertIn('dab_station_logo.db', d['error'])
        self.assertFalse(os.path.exists(server.IMG_ORIG), 'working set untouched')

    def test_not_an_image_refused(self):
        code, d = self.req('/api/load_upload', *self.files(image=('station_logos.img', b'x' * 5000)))
        self.assertEqual(code, 400)
        self.assertIn('station_logos.img', d['error'])

    def test_usb_path_must_be_detected(self):
        code, d = self.req('/api/load_inputs', {'usb': self.src})
        self.assertEqual(code, 400)

    def test_settings_switch_source_live(self):
        p = os.path.join(self.tmp.name, 'cfg.yaml')
        with open(p, 'w') as f:
            f.write('port: 9001\n')
        server.CONFIG_PATH = p
        try:
            code, d = self.req('/api/settings', {'data_source': 'csv_file', 'language': 'pl',
                                                 'sources': {'csv_file': {'path': 'x.csv', 'default_ecc': 'e0'}}})
            self.assertEqual((code, d['status']), (200, 'ok'), d)
            self.assertEqual((server.SOURCE.id, server.T['step.1']), ('csv_file', 'Wczytaj z auta'))
            cfg = config.load_config(p)
            self.assertEqual((cfg['port'], cfg['data_source'], cfg['sources']['csv_file']['default_ecc']), (9001, 'csv_file', 'E0'))
            self.assertEqual(self.req('/api/settings', {'data_source': 'nope'})[0], 400)
        finally:
            server.CONFIG_PATH = 'config.yaml'
            i18n.load('en')
            server.T = i18n.catalog()

    def test_save_partial_keeps_other_keys(self):
        p = os.path.join(self.tmp.name, 'c.yaml')
        with open(p, 'w') as f:
            f.write('# comment\nport: 9001\nsources:\n  radiopolska:\n    base_url: x\n')
        config.save_partial(p, {'data_source': 'csv_file', 'sources': {'csv_file': {'path': 'a.csv'}}})
        cfg = config.load_config(p)
        self.assertEqual((cfg['port'], cfg['data_source'], cfg['sources']['csv_file']['path'],
                          cfg['sources']['radiopolska']['base_url']), (9001, 'csv_file', 'a.csv', 'x'))
        self.assertTrue(os.path.exists(p + '.bak'))


if __name__ == '__main__':
    unittest.main()
