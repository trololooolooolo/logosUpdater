#!/usr/bin/env python3
"""LogosUpdater — entry point.

Thin launcher: ensure deps, load config (CLI > config.yaml > defaults), set the
UI language, pick the active data-source plugin, then hand off to the HTTP server.

  python3 view_stations.py                 # serves http://localhost:<port>
  python3 view_stations.py --lang pl --debug
  python3 view_stations.py --config my.yaml --port 9000
  python3 view_stations.py --no-browser    # do not open the browser tab at start

Architecture and device/QNX6 internals: docs/ARCHITECTURE.md
"""

import os
import sys
import argparse
import logging


# requirements.txt package name -> module it provides (for the "is it installed?" check)
_MODULE_OF = {'pillow': 'PIL', 'pyyaml': 'yaml', 'beautifulsoup4': 'bs4', 'requests': 'requests'}


def _requirements():
    """Package specs from requirements.txt (comments/blank lines skipped)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'requirements.txt')
    with open(path, encoding='utf-8') as f:
        return [ln.split('#', 1)[0].strip() for ln in f if ln.split('#', 1)[0].strip()]


def _ensure_deps():
    """Install the packages from requirements.txt that are missing, saying which.

    Exits with code 1 (and the manual command) if an install fails. Skipped in a
    frozen (bundled) build, where everything is already included.
    """
    import importlib
    import re
    import subprocess
    if getattr(sys, 'frozen', False):
        return
    missing = []
    for spec in _requirements():
        name = re.split(r'[<>=!~\[; ]', spec, maxsplit=1)[0]
        try:
            importlib.import_module(_MODULE_OF.get(name.lower(), name))
        except ImportError:
            missing.append(spec)
    if not missing:
        return
    print("Installing missing dependencies: " + ", ".join(missing), flush=True)
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', *missing])
    except Exception as e:  # noqa: BLE001
        print(f"Could not install dependencies ({e}).\n"
              f"Install them manually and start again:\n"
              f"    {sys.executable} -m pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)
    importlib.invalidate_caches()


def _fix_windows_argv(argv):
    """Windows: in `--load "C:\\dir\\" --port 8080` the closing \\" is read as a literal quote, so the
    path argument becomes `C:\\dir" --port 8080`. Split it back into the path and the options."""
    out = []
    for a in argv:
        if '"' in a and not a.startswith('-'):
            path, rest = a.split('"', 1)
            out.append(path)
            # shlex in Windows mode keeps a following quoted value with spaces together
            import shlex
            try:
                parts = shlex.split(rest, posix=False)
            except ValueError:               # an unclosed quote: plain split is better than a crash
                parts = rest.split()
            out.extend(x[1:-1] if len(x) > 1 and x[0] == x[-1] == '"' else x for x in parts)
        else:
            out.append(a)
    return out


def _utf8_output():
    """Console/log output redirected to a file used the Windows code page ("—" became "\\x97")."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def main():
    _ensure_deps()                       # before importing config (it needs PyYAML)
    import config as _cfg_paths
    # Run in the data directory so config.yaml / DBs / images resolve next to the
    # script (from source) or next to the executable (PyInstaller bundle, where
    # __file__ points into the temporary unpack dir). LOGOSUPDATER_DATA overrides it
    # (Docker: code in /app, data in the /data volume).
    data = _cfg_paths.app_dir()
    os.makedirs(data, exist_ok=True)
    os.chdir(data)

    # config.py registers --config/--lang/--port/--debug; we add --log-file.
    parser = argparse.ArgumentParser(description='LogosUpdater Station Viewer')
    parser.add_argument('--log-file', default='sync_log.txt', help='Log file path')
    import config as _config
    _config.add_config_cli_args(parser)
    # a typo such as --prots 8080 must not be ignored silently: argparse stops with a message
    args = parser.parse_args(_fix_windows_argv(sys.argv[1:]))

    cfg = _config.load_config(getattr(args, 'config', 'config.yaml') or 'config.yaml',
                              _config.cli_overrides_from_args(args))

    # Logging (console/log messages stay English).
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, 'debug', False) else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.FileHandler(args.log_file, encoding='utf-8'),
                  logging.StreamHandler(sys.stdout)])
    logger = logging.getLogger(__name__)

    # Optional: load a fresh image + FM/DAB DBs from a car package dir (--load DIR)
    if getattr(args, 'load', None):
        load_dir = args.load
        if len(load_dir) > 3:                  # keep "C:\\" / "/" as they are
            load_dir = load_dir.rstrip('\\/')
        try:
            _config.load_inputs(cfg, load_dir, origin=('cli', None))
        except _config.InputError as e:
            sys.exit(f"--load {load_dir}: {e}. Nothing was changed.")

    import i18n
    import sources
    import server

    i18n.load(cfg.get('language', 'en'))
    src = sources.get_active(cfg)
    logger.info("Data source: %s (%s); language: %s",
                src.id, src.name, cfg.get('language', 'en'))

    server.run(cfg, src, args.log_file,
               config_path=getattr(args, 'config', 'config.yaml') or 'config.yaml',
               open_browser=not getattr(args, 'no_browser', False))


def _pause_if_double_clicked(failed):
    """The ready-made .exe on Windows runs in its own console window, which closes the moment the
    program ends - so a start-up error would flash by unread. Keep it open until Enter."""
    if failed and getattr(sys, 'frozen', False) and os.name == 'nt':
        try:
            if sys.stdin and sys.stdin.isatty():
                input("\nLogosUpdater stopped because of the error above. Press Enter to close this window...")
        except (EOFError, OSError):
            pass


if __name__ == "__main__":
    _utf8_output()
    failed = False
    try:
        main()
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        failed = e.code not in (None, 0)
        if failed and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)     # sys.exit("message"): show it before pausing
            e.code = 1
        _pause_if_double_clicked(failed)
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        failed = True
    _pause_if_double_clicked(failed)
    if failed:
        sys.exit(1)
