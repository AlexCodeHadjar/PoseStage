"""Background images: stored copies, downscale, strict serving, view prefs.

Run with: python -m unittest discover -s tests -v
"""
import hashlib
import http.client
import io
import struct
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from PIL import Image
import app
import poses


def png(path, size=(40, 60)):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new('RGBA', size)
    im.paste((200, 50, 50, 255), (5, 10, 30, 50))
    im.save(path)


def display_p3_icc() -> bytes:
    """Minimal ICC v2 matrix/TRC display profile with Display P3 colorants (D50-adapted), gamma 2.2."""
    def s15(v):
        return struct.pack('>i', round(v * 65536))

    def xyz(x, y, z):
        return b'XYZ ' + b'\0' * 4 + s15(x) + s15(y) + s15(z)
    text = b'Display P3 test\0'
    desc = b'desc' + b'\0' * 4 + struct.pack('>I', len(text)) + text + b'\0' * 4 + b'\0' * 4 + b'\0' * 3 + b'\0' * 67
    curve = b'curv' + b'\0' * 4 + struct.pack('>I', 1) + struct.pack('>H', 0x0233) + b'\0\0'
    tags = [(b'desc', desc), (b'wtpt', xyz(0.9642, 1.0, 0.8249)),
            (b'rXYZ', xyz(0.5151, 0.2412, -0.0011)), (b'gXYZ', xyz(0.2920, 0.6922, 0.0419)),
            (b'bXYZ', xyz(0.1571, 0.0666, 0.7841)),
            (b'rTRC', curve), (b'gTRC', curve), (b'bTRC', curve)]
    offset = 128 + 4 + 12 * len(tags)
    table, body = b'', b''
    for signature, data in tags:
        data += b'\0' * (-len(data) % 4)
        table += signature + struct.pack('>II', offset + len(body), len(data))
        body += data
    size = offset + len(body)
    header = (struct.pack('>I', size) + b'\0' * 4 + bytes([2, 0x10, 0, 0]) + b'mntrRGB XYZ ' + b'\0' * 12
              + b'acsp' + b'\0' * 24 + b'\0' * 4 + s15(0.9642) + s15(1.0) + s15(0.8249) + b'\0' * 48)
    assert len(header) == 128
    return header + struct.pack('>I', len(tags)) + table + body


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data, self.old_app_data = poses.DATA_DIR, app.DATA_DIR
        poses.DATA_DIR = app.DATA_DIR = self.root / 'appdata'
        self.images = self.root / 'poses'
        png(self.images / 'pose-1.png')
        catalog = poses.PoseCatalog(self.images, '/test')
        catalog.scan()
        self.api = app.Api(catalog)
        self.pictures = self.root / 'Мои картинки'
        self.pictures.mkdir()

    def tearDown(self):
        poses.DATA_DIR, app.DATA_DIR = self.old_data, self.old_app_data
        self.tmp.cleanup()

    def pick(self, *paths):
        self.api._pick_background = MagicMock(side_effect=list(paths))

    def landscape(self, name='land.jpg', size=(800, 400), exif_rotate=False):
        path = self.pictures / name
        im = Image.linear_gradient('L').resize(size).convert('RGB')
        if exif_rotate:
            exif = Image.Exif()
            exif[0x0112] = 6  # rotate 90° CW on display
            im.save(path, 'JPEG', exif=exif)
        else:
            im.save(path, 'JPEG' if name.endswith('.jpg') else None)
        return path

    @contextmanager
    def server(self):
        server = app.LocalServer(self.api._catalog)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def get(path):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                connection.request('GET', path)
                response = connection.getresponse()
                data = response.read()
                connection.close()
                return response.status, response.headers, data
            yield get
        finally:
            server.shutdown()
            server.server_close()

    def test_copy_not_original_and_selected(self):
        source = self.landscape()
        before = (source.read_bytes(), source.stat().st_mtime_ns)
        self.pick(source)
        entry = self.api.choose_background()
        digest = hashlib.sha256(before[0]).hexdigest()[:16]
        self.assertEqual(entry, {'id': digest, 'url': f'/test/backgrounds/{digest}.jpg', 'w': 800, 'h': 400})
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)
        stored = poses.DATA_DIR / 'backgrounds' / f'{digest}.jpg'
        self.assertTrue(stored.is_file())
        self.assertEqual(sorted(p.name for p in self.pictures.iterdir()), ['land.jpg'])
        self.assertEqual(self.api.get_view_prefs()['background'], {'mode': 'image', 'id': digest})
        self.assertEqual(self.api.list_backgrounds(), [entry])
        # The same file again: no duplicate.
        self.pick(source)
        self.assertEqual(self.api.choose_background(), entry)
        self.assertEqual(len(self.api.list_backgrounds()), 1)

    def test_downscale_exif_and_alpha(self):
        big = self.landscape('big.png', size=(6000, 3000))
        self.pick(big)
        entry = self.api.choose_background()
        self.assertEqual((entry['w'], entry['h']), (4096, 2048))
        self.assertTrue(entry['url'].endswith('.jpg'))  # opaque PNG → JPEG copy
        rotated = self.landscape('rotated.jpg', size=(300, 100), exif_rotate=True)
        self.pick(rotated)
        self.assertEqual((lambda e: (e['w'], e['h']))(self.api.choose_background()), (100, 300))
        alpha = self.pictures / 'alpha.webp'
        im = Image.new('RGBA', (64, 32), (10, 20, 30, 128))
        im.save(alpha, 'WEBP', lossless=True)
        self.pick(alpha)
        entry = self.api.choose_background()
        self.assertTrue(entry['url'].endswith('.png'))
        with Image.open(poses.DATA_DIR / 'backgrounds' / Path(entry['url']).name) as stored:
            self.assertEqual(stored.mode, 'RGBA')
        self.assertEqual(len(self.api.list_backgrounds()), 3)

    def test_icc_profile_converted_to_srgb(self):
        icc = display_p3_icc()
        color = (180, 120, 60)  # a warm P3 color: more saturated (more red, less blue) in sRGB
        tagged = self.pictures / 'p3.jpg'
        Image.new('RGB', (64, 32), color).save(tagged, 'JPEG', quality=100, subsampling=0, icc_profile=icc)
        alpha = self.pictures / 'p3-alpha.png'
        Image.new('RGBA', (64, 32), color + (128,)).save(alpha, icc_profile=icc)
        broken = self.pictures / 'broken-icc.jpg'
        Image.new('RGB', (64, 32), color).save(broken, 'JPEG', quality=100, subsampling=0, icc_profile=b'junk' * 40)
        self.pick(tagged, alpha, broken)
        stored = []
        for i in range(3):
            if i < 2:
                entry = self.api.choose_background()
            else:
                with self.assertLogs(level='WARNING'):  # unusable profile: logged, then ignored
                    entry = self.api.choose_background()
            with Image.open(poses.DATA_DIR / 'backgrounds' / Path(entry['url']).name) as im:
                stored.append((im.mode, im.getpixel((10, 10))))
        mode, (r, g, b) = stored[0]
        self.assertEqual(mode, 'RGB')
        self.assertGreater(r, color[0] + 5)
        self.assertLess(b, color[2] - 5)
        self.assertEqual(stored[1][0], 'RGBA')
        self.assertTrue(all(abs(a - c) <= 2 for a, c in zip(stored[1][1][:3], (r, g, b))))  # PNG vs JPEG
        self.assertEqual(stored[1][1][3], 128)  # alpha kept
        self.assertTrue(all(abs(a - c) <= 2 for a, c in zip(stored[2][1], color)))  # unusable profile: as-is

    def test_invalid_images_and_no_window(self):
        self.assertIsNone(app.Api(self.api._catalog).choose_background())  # no window: harmless
        broken = self.pictures / 'broken.png'
        broken.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 100)
        text = self.pictures / 'notes.txt'
        text.write_text('nope', encoding='utf-8')
        gif = self.pictures / 'fake.jpg'
        Image.new('RGB', (8, 8)).save(gif, 'GIF')
        truncated = self.pictures / 'cut.jpg'
        truncated.write_bytes(self.landscape('full.jpg').read_bytes()[:400])
        for path in (broken, text, gif, truncated):
            self.pick(path)
            with self.assertRaises(ValueError, msg=path.name):
                self.api.choose_background()
        self.pick(None)
        self.assertIsNone(self.api.choose_background())  # cancel
        self.assertEqual(self.api.list_backgrounds(), [])
        self.assertNotIn('background', self.api.get_view_prefs())

    def test_serving_and_traversal(self):
        self.pick(self.landscape())
        entry = self.api.choose_background()
        folder = poses.DATA_DIR / 'backgrounds'
        (folder / 'notes.txt').write_text('secret', encoding='utf-8')
        (poses.DATA_DIR / 'view_prefs.json').write_text('{}', encoding='utf-8')
        with self.server() as get:
            status, headers, data = get(entry['url'])
            self.assertEqual((status, headers['Content-Type']), (200, 'image/jpeg'))
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual(im.size, (800, 400))
            for url in ('/test/backgrounds/', '/test/backgrounds/notes.txt',
                        f'/test/backgrounds/{entry["id"]}.png', f'/test/backgrounds/{entry["id"].upper()}.jpg',
                        '/test/backgrounds/../view_prefs.json', '/test/backgrounds/%2e%2e%2fview_prefs.json',
                        '/test/backgrounds/..%5cview_prefs.json', '/test/backgrounds/0123456789abcdef.jpg',
                        f'/test/backgrounds/{entry["id"]}.jpg/x', f'/test/backgrounds/sub/{entry["id"]}.jpg',
                        '/test/backgrounds/C:%5cWindows%5cwin.ini', f'/test/backgrounds/{entry["id"]}.jpg%00'):
                self.assertEqual(get(url)[0], 404, url)
        self.assertIsNone(poses.background_file('../view_prefs.json'))
        self.assertIsNone(poses.background_file(None))

    def test_remove(self):
        self.pick(self.landscape('a.jpg'), self.landscape('b.jpg', size=(500, 500)))
        first = self.api.choose_background()
        second = self.api.choose_background()
        self.assertEqual(self.api.get_view_prefs()['background']['id'], second['id'])
        self.assertEqual(self.api.remove_background(first['id']), [second])
        self.assertEqual(self.api.get_view_prefs()['background'], {'mode': 'image', 'id': second['id']})
        self.assertEqual(self.api.remove_background(second['id']), [])  # selected → studio
        self.assertEqual(self.api.get_view_prefs()['background'], {'mode': 'studio', 'id': None})
        for bad in ('../x', None, 5, 'ABCDEF0123456789', second['id']):
            self.assertEqual(self.api.remove_background(bad), [])
        self.assertEqual(list((poses.DATA_DIR / 'backgrounds').glob('*.jpg')), [])

    def test_view_prefs_whitelist(self):
        api = self.api
        saved = api.save_view_prefs({'parallax': 'strong', 'background': {
            'mode': 'image', 'id': '0123456789abcdef', 'blur': 6, 'dim': 0.3, 'extra': 1}})
        self.assertEqual(saved, {'parallax': 'strong', 'background': {
            'mode': 'image', 'id': '0123456789abcdef', 'blur': 6.0, 'dim': 0.3}})
        # Partial update merges; invalid values are dropped, numbers are clamped.
        saved = api.save_view_prefs({'parallax': 'wild', 'background': {
            'mode': 'video', 'id': '../../evil', 'blur': 100, 'dim': True}})
        self.assertEqual(saved['parallax'], 'strong')
        self.assertEqual(saved['background'], {'mode': 'image', 'id': '0123456789abcdef', 'blur': 24.0, 'dim': 0.3})
        saved = api.save_view_prefs({'background': {'mode': 'studio', 'id': None, 'dim': -2, 'blur': float('nan')}})
        self.assertEqual(saved['background'], {'mode': 'studio', 'id': None, 'blur': 24.0, 'dim': 0.0})
        self.assertEqual(api.save_view_prefs({'background': 'image'})['background']['mode'], 'studio')
        for key in ('parallax', 'background'):
            self.assertNotIn(key, poses.clean_view_prefs({key: None}))
        self.assertEqual(poses.clean_view_prefs({'background': {'id': 'ABCDEF0123456789'}}), {})
        self.assertEqual(api.get_view_prefs(), saved)


if __name__ == '__main__':
    unittest.main()
