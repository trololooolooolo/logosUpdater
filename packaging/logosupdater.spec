# PyInstaller spec: one-file LogosUpdater executable.
#   pip install pyinstaller && pyinstaller --noconfirm --clean packaging/logosupdater.spec
# Result: dist/LogosUpdater(.exe). User data (config.yaml, work/, inputs/, ...) is created
# next to the executable (config.app_dir()); web/ and lang/ are read from the bundle.
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(ROOT, 'view_stations.py')],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, 'web'), 'web'), (os.path.join(ROOT, 'lang'), 'lang'),
           (os.path.join(ROOT, 'LICENSE'), '.')]
          # written by packaging/third_party_notices.py (release.yml) before the build
          + ([(os.path.join(ROOT, 'THIRD_PARTY_NOTICES.txt'), '.')]
             if os.path.isfile(os.path.join(ROOT, 'THIRD_PARTY_NOTICES.txt')) else []),
    # data-source plugins are discovered at runtime (pkgutil), so list them explicitly
    # (collect_submodules would import from an isolated process without ROOT on sys.path)
    hiddenimports=['sources.' + f[:-3] for f in sorted(os.listdir(os.path.join(ROOT, 'sources')))
                   if f.endswith('.py') and f != '__init__.py'],
    excludes=['tkinter', 'easyocr', 'torch', 'numpy'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='LogosUpdater',
    console=True,          # the console window shows the URL and the log; closing it stops the app
    upx=False,
)
