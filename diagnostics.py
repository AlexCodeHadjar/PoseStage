"""WebView2 runtime selection and the offline packaged-resource check (no desktop window)."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

FORCE_ENV = 'POSESTAGE_WEBVIEW2'  # "portable": set for the one automatic relaunch
FAILED_MARKER = 'webview2_system_failed.json'  # {"version": system runtime version that failed to start}


def check_runtime(portable: Path | None = None):
    """Version of the installed (portable=None) or portable WebView2 Runtime; raises if unavailable."""
    import webview
    if portable is not None and not (portable / 'msedgewebview2.exe').is_file():
        raise FileNotFoundError(f'No portable WebView2 Runtime in {portable}')
    loader = Path(webview.__file__).parent / 'lib/runtimes/win-x64/native/WebView2Loader.dll'
    dll = ctypes.WinDLL(str(loader))
    function = dll.GetAvailableCoreWebView2BrowserVersionString
    function.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    function.restype = ctypes.c_long
    value = ctypes.c_void_p()
    status = function(str(portable) if portable else None, ctypes.byref(value))
    if status < 0 or not value.value:
        raise RuntimeError(f'WebView2 Runtime unavailable: HRESULT 0x{status & 0xffffffff:08X}')
    try:
        return {'mode': 'portable' if portable else 'system', 'version': ctypes.wstring_at(value)}
    finally:
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoTaskMemFree(value)


def probe(portable: Path | None = None) -> dict:
    try:
        return {'available': True, 'version': check_runtime(portable)['version']}
    except Exception as error:
        return {'available': False, 'error': str(error)}


def decide_runtime(system_version, portable_available, forced=False, failed_version=None):
    """Prefer the system runtime; portable if forced, if the system one is missing, or if this very
    system version already failed to start (registered but broken install)."""
    if portable_available and (forced or not system_version or system_version == failed_version):
        return 'portable'
    return 'system' if system_version else None


def failed_system_version(data_dir: Path | None):
    try:
        value = json.loads((data_dir / FAILED_MARKER).read_text(encoding='utf-8')).get('version')
        return value if isinstance(value, str) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def select_runtime(portable: Path, data_dir: Path | None = None) -> dict:
    """Probe both runtimes and choose one: {'mode', 'version', 'reason', 'system', 'portable', ...}."""
    system, local = probe(None), probe(portable)
    forced = os.environ.get(FORCE_ENV) == 'portable'
    failed = failed_system_version(data_dir)
    mode = decide_runtime(system.get('version'), local['available'], forced, failed)
    reason = ('forced' if forced else 'system failed before' if failed and failed == system.get('version')
              else 'system unavailable') if mode == 'portable' else 'preferred' if mode else 'none available'
    return {'mode': mode, 'version': {'system': system, 'portable': local}.get(mode, {}).get('version'),
            'reason': reason, 'system': system, 'portable': local, 'forced': forced,
            'system_failed_version': failed}


def can_fall_back(runtime: dict | None) -> bool:
    """A failed system runtime may be retried once with the portable one (never in a forced run)."""
    return bool(runtime and runtime['mode'] == 'system' and runtime['portable']['available'] and not runtime['forced'])


def self_check(resources: Path, executable_dir: Path):
    from poses import DATA_DIR, PoseCatalog, find_source, write_json
    report = {'passed': False}
    try:
        vendor = resources / 'web/vendor/three'
        manifest = json.loads((vendor / 'manifest.json').read_text(encoding='utf-8'))
        for name, expected in manifest['sha256'].items():
            if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f'Bundled file checksum mismatch: {name}')
        for name in ('index.html', 'style.css', 'main.js', 'scene/figure.js', 'scene/placement.js',
                     'scene/pose_support_modes.json', 'shaders/dissolve.js',
                     'desktop/podium.png', 'desktop/podium.json'):
            if not (resources / 'web' / name).is_file():
                raise FileNotFoundError(name)
        report['vendor_files_verified'] = len(manifest['sha256'])
        report['webview2'] = select_runtime(executable_dir / 'WebView2', DATA_DIR)
        if report['webview2']['mode'] is None:
            raise RuntimeError('WebView2 Runtime unavailable (system and portable)')
        folder, recursive = find_source(executable_dir if getattr(sys, 'frozen', False) else None)
        catalog = PoseCatalog(folder, '/check', recursive=recursive)
        catalog.scan()
        report['poses'] = len(catalog.get())
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
    write_json(DATA_DIR / 'diagnostics.json', report)
    return report
