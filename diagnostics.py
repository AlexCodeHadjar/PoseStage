"""Offline packaged-resource check; does not create a desktop window."""
from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path


def check_runtime(portable: Path):
    import webview
    loader = Path(webview.__file__).parent / 'lib/runtimes/win-x64/native/WebView2Loader.dll'
    dll = ctypes.WinDLL(str(loader))
    function = dll.GetAvailableCoreWebView2BrowserVersionString
    function.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    function.restype = ctypes.c_long
    value = ctypes.c_void_p()
    folder = str(portable) if (portable / 'msedgewebview2.exe').is_file() else None
    status = function(folder, ctypes.byref(value))
    if status < 0:
        raise RuntimeError(f'WebView2 Runtime unavailable: HRESULT 0x{status & 0xffffffff:08X}')
    try:
        return {'mode': 'portable' if folder else 'system', 'version': ctypes.wstring_at(value)}
    finally:
        ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.ole32.CoTaskMemFree(value)


def self_check(resources: Path, executable_dir: Path):
    from poses import DATA_DIR, PoseCatalog, find_folder, write_json
    report = {'passed': False}
    try:
        vendor = resources / 'web/vendor/three'
        manifest = json.loads((vendor / 'manifest.json').read_text(encoding='utf-8'))
        for name, expected in manifest['sha256'].items():
            if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f'Bundled file checksum mismatch: {name}')
        for name in ('index.html', 'style.css', 'main.js', 'scene/figure.js', 'scene/placement.js',
                     'scene/pose_support_modes.json', 'shaders/dissolve.js'):
            if not (resources / 'web' / name).is_file():
                raise FileNotFoundError(name)
        report['vendor_files_verified'] = len(manifest['sha256'])
        report['webview2'] = check_runtime(executable_dir / 'WebView2')
        catalog = PoseCatalog(find_folder(executable_dir), '/check')
        catalog.scan()
        report['poses'] = len(catalog.get())
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
    write_json(DATA_DIR / 'diagnostics.json', report)
    return report
