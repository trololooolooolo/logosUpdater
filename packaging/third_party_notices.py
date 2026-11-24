"""Write THIRD_PARTY_NOTICES.txt: licences of everything the release executable bundles.

    python packaging/third_party_notices.py [OUT]      (default: THIRD_PARTY_NOTICES.txt)

Covers Python itself, the runtime requirements (requirements.txt) with their dependencies, and
PyInstaller (its bootloader is part of the executable). Run it in the build environment, after
`pip install -r requirements.txt pyinstaller`.
"""
import os
import re
import sys
import sysconfig
from importlib import metadata

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LICENSE_RE = re.compile(r'(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$', re.I)


def _norm(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _req_names(lines):
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name, _, marker = line.partition(';')
        if 'extra' in marker:           # optional extras are not installed/bundled
            continue
        m = re.match(r'[A-Za-z0-9][A-Za-z0-9._-]*', name.strip())
        if m:
            yield _norm(m.group(0))


def _closure(roots):
    seen, todo = {}, list(roots)
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        try:
            dist = metadata.distribution(n)
        except metadata.PackageNotFoundError:
            continue
        seen[n] = dist
        todo.extend(_req_names(dist.requires or []))
    return seen


def _licence_name(dist):
    md = dist.metadata
    expr = md.get('License-Expression')
    if expr:
        return expr
    lic = (md.get('License') or '').strip()
    if lic and '\n' not in lic and len(lic) < 80:
        return lic
    cls = [c.split('::')[-1].strip() for c in md.get_all('Classifier') or [] if c.startswith('License ::')]
    return ', '.join(cls) or 'see the licence text below'


def _licence_texts(dist):
    out = []
    for f in dist.files or []:
        if LICENSE_RE.search(str(f).replace('\\', '/')):
            try:
                with open(f.locate(), encoding='utf-8', errors='replace') as fh:
                    out.append((str(f), fh.read().strip()))
            except OSError:
                pass
    return out


def _python_licence():
    for d in (sys.base_prefix, sysconfig.get_paths()['stdlib']):
        p = os.path.join(d, 'LICENSE.txt')
        if os.path.isfile(p):
            with open(p, encoding='utf-8', errors='replace') as fh:
                return fh.read().strip()
    return 'Python Software Foundation License: https://docs.python.org/3/license.html'


def build():
    with open(os.path.join(ROOT, 'requirements.txt'), encoding='utf-8') as fh:
        roots = list(_req_names(fh))
    dists = _closure(roots + ['pyinstaller'])
    rule = '=' * 78
    parts = ['LogosUpdater bundles the following third-party software. LogosUpdater itself is MIT-licensed',
             '(see LICENSE). Cropper.js (web/vendor/cropperjs) is MIT-licensed; its licence ships with it.', '',
             rule, f'Python {sys.version.split()[0]}', rule, _python_licence(), '']
    for n in sorted(dists):
        d = dists[n]
        parts += [rule, f"{d.metadata['Name']} {d.version} - {_licence_name(d)}"]
        home = d.metadata.get('Home-page') or ''
        if home:
            parts.append(home)
        parts.append(rule)
        texts = _licence_texts(d)
        for path, text in texts:
            parts += [f'--- {path} ---', text, '']
        if not texts:
            parts += ['(no licence file in the installed package; see the project page)', '']
    return '\n'.join(parts) + '\n'


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else 'THIRD_PARTY_NOTICES.txt'
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(build())
    print(out)
