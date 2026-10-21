"""Run with: python -m unittest discover -s tests -v"""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
import app
import poses


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = self.root / 'Позы с пробелами'
        self.images.mkdir()
        self.old_data, self.old_app_data = poses.DATA_DIR, app.DATA_DIR
        poses.DATA_DIR = app.DATA_DIR = self.root / 'appdata'
        self.name = 'photo-000001__pose-01.png'
        im = Image.new('RGBA', (20, 20))
        im.putpixel((3, 4), (255, 255, 255, 8))
        im.putpixel((6, 9), (128, 128, 128, 9))
        im.save(self.images / self.name)

    def tearDown(self):
        poses.DATA_DIR, app.DATA_DIR = self.old_data, self.old_app_data
        self.tmp.cleanup()

    def catalog(self):
        catalog = poses.PoseCatalog(self.images, '/test')
        catalog.scan()
        return catalog

    def test_alpha_threshold_and_filter(self):
        (self.images / 'index.html').write_text('ignore', encoding='utf-8')
        (self.images / 'photo-000002__pose-01.png').write_bytes(b'broken')
        with self.assertLogs(level='ERROR'):
            catalog = self.catalog()
        self.assertEqual(len(catalog.items), 1)
        self.assertEqual(catalog.items[0]['box'], [6, 9, 7, 10])
        self.assertEqual(catalog.items[0]['support']['profile'], [[6.5, 10]])
        self.assertTrue(poses.valid_entry(catalog.items[0]))

    def test_cache_only_reads_changed_images(self):
        catalog = self.catalog()
        original_open = Image.open
        with patch('poses.Image.open', wraps=original_open) as opened:
            catalog.scan()
            self.assertEqual(opened.call_count, 0)
            Image.new('RGBA', (20, 20), 'white').save(self.images / 'photo-000002__pose-01.PNG')
            catalog.scan()
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(catalog.items), 2)

    def test_invalid_cache_is_rebuilt(self):
        catalog = self.catalog()
        cache = poses.read_json(poses.DATA_DIR / 'poses_cache.json', {})
        entry = next(iter(cache.values()))
        entry['box'] = [-1, 'invalid', 100, 100]
        poses.write_json(poses.DATA_DIR / 'poses_cache.json', cache)
        catalog.scan()
        self.assertEqual(catalog.items[0]['box'], [6, 9, 7, 10])
        cache = poses.read_json(poses.DATA_DIR / 'poses_cache.json', {})
        next(iter(cache.values()))['support']['profile'] = [['invalid', 10]]
        poses.write_json(poses.DATA_DIR / 'poses_cache.json', cache)
        catalog.scan()
        self.assertEqual(catalog.items[0]['support']['profile'], [[6.5, 10]])

    def test_state_clamping_and_private_api(self):
        api = app.Api(self.catalog())
        self.assertTrue(all(name.startswith('_') or callable(getattr(api, name)) for name in vars(api)))
        expected = {'index': 0, 'file': self.name}
        api.save_state(0)
        self.assertEqual(api.get_state(), expected)
        poses.write_json(poses.DATA_DIR / 'state.json', {'index': 200})
        self.assertEqual(api.get_state(), expected)
        (poses.DATA_DIR / 'state.json').write_text('broken', encoding='utf-8')
        self.assertEqual(api.get_state(), expected)

    def test_server_allowlist_and_traversal(self):
        server = app.LocalServer(self.catalog())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.assertEqual(server.server_address[0], '127.0.0.1')
        try:
            def get(path, host=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                connection.request('GET', path, headers={'Host': host} if host else {})
                response = connection.getresponse()
                data = response.read()
                connection.close()
                return response.status, data

            self.assertEqual(get('/test/web/index.html')[0], 200)
            self.assertEqual(get('/test/poses/' + self.name)[0], 200)
            for url in ('/', '/test/web/', '/test/poses/index.html', '/test/web/../app.py',
                        '/test/web/%2e%2e%5capp.py', '/test/poses/%2e%2e%2fapp.py',
                        '/test/web/C:%5cWindows%5cwin.ini', '/test/web/%5c%5coutside.invalid%5cshare',
                        '/test/web/%00'):
                self.assertEqual(get(url)[0], 404, url)
            self.assertEqual(get('/test/web/index.html', 'outside.invalid')[0], 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_folder_search_order(self):
        saved = self.root / 'saved'
        saved.mkdir()
        (saved / self.name).write_bytes((self.images / self.name).read_bytes())
        poses.write_json(poses.DATA_DIR / 'config.json', {'poses_folder': str(saved)})
        self.assertEqual(poses.find_folder(self.images), saved.resolve())


if __name__ == '__main__':
    unittest.main()
