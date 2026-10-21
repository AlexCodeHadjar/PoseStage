"""Collections, state by file, recursive scan, overrides, library, prefs, thumbnails, server.

Run with: python -m unittest discover -s tests -v
"""
import base64
import contextlib
from contextlib import contextmanager
import hashlib
import http.client
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
import app
import diagnostics
import poses

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import dev_server  # noqa: E402


def png(path, size=(40, 60), box=(5, 10, 30, 50)):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new('RGBA', size)
    im.paste((200, 50, 50, 255), box)
    im.save(path)


def jpg(path, size=(64, 32)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, (90, 90, 90)).save(path, 'JPEG')


def snapshot(folder):
    """Every path below `folder` with size and mtime: proves nothing was written there."""
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in Path(folder).rglob('*'))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data, self.old_app_data = poses.DATA_DIR, app.DATA_DIR
        poses.DATA_DIR = app.DATA_DIR = self.root / 'appdata'
        self.images = self.root / 'Позы с пробелами'
        for name in ('pose-1.png', 'pose-2.png', 'pose-3.png'):
            png(self.images / name)
        self.resources = self.root / 'resources'
        modes = self.resources / 'web/scene/pose_support_modes.json'
        modes.parent.mkdir(parents=True)
        modes.write_text(json.dumps({'POSE-2.png': 'kneel', 'pose-3.png': 'bogus'}), encoding='utf-8')
        self.resource_patch = patch.object(poses, 'RESOURCE_DIR', self.resources)
        self.resource_patch.start()

    def tearDown(self):
        self.resource_patch.stop()
        poses.DATA_DIR, app.DATA_DIR = self.old_data, self.old_app_data
        self.tmp.cleanup()

    def catalog(self, folder=None):
        catalog = poses.PoseCatalog(folder or self.images, '/test')
        catalog.scan()
        return catalog

    def files(self, catalog):
        return [pose['file'] for pose in catalog.get()]

    @contextmanager
    def server(self, catalog, handler=None, api=None):
        server = app.LocalServer(catalog)
        if handler:
            server.RequestHandlerClass = handler
            server.api = api
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def request(path, method='GET', body=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                data = response.read()
                connection.close()
                return response.status, response.headers, data
            yield request
        finally:
            server.shutdown()
            server.server_close()


class ScanTests(Base):
    def test_recursive_formats_hidden_and_natural_sort(self):
        png(self.images / 'pose-10.png')
        jpg(self.images / 'sub' / 'Opaque Photo.JPG')
        png(self.images / 'Sub2' / 'b.webp')
        Image.new('RGBA', (8, 8), (0, 0, 0, 255)).save(self.images / 'Sub2' / 'c.webp', 'WEBP')
        png(self.images / '.hidden' / 'x.png')
        png(self.images / '_service' / 'y.png')
        png(self.images / '.dotfile.png')
        (self.images / 'notes.txt').write_text('no', encoding='utf-8')
        (self.images / 'fake.jpeg').write_bytes(b'GIF89a')  # unreadable: skipped with a log line
        with self.assertLogs(level='ERROR'):
            catalog = self.catalog()
        self.assertEqual(self.files(catalog), ['pose-1.png', 'pose-2.png', 'pose-3.png', 'pose-10.png',
                                               'sub/Opaque Photo.JPG', 'Sub2/b.webp', 'Sub2/c.webp'])
        photo = catalog.get()[4]
        self.assertEqual((photo['w'], photo['h'], photo['box']), (64, 32, [0, 0, 64, 32]))  # opaque → whole image
        self.assertEqual(photo['url'], '/test/poses/sub/Opaque%20Photo.JPG')
        self.assertEqual(photo['thumb'], '/test/thumbs/sub/Opaque%20Photo.JPG')
        self.assertTrue(all(poses.valid_entry(pose) for pose in catalog.get()))

    def test_natural_key(self):
        names = ['b/pose-10.png', 'a/pose-2.png', 'B/pose-1.png', 'pose-10.png', 'pose-9.png', 'a/pose-10.png']
        self.assertEqual(sorted(names, key=poses.natural_key),
                         ['a/pose-2.png', 'a/pose-10.png', 'B/pose-1.png', 'b/pose-10.png', 'pose-9.png', 'pose-10.png'])

    def test_url_encodes_each_segment(self):
        png(self.images / 'под папка' / 'поза #1 %.png')
        catalog = self.catalog()
        pose = next(p for p in catalog.get() if p['file'].startswith('под'))
        self.assertEqual(pose['file'], 'под папка/поза #1 %.png')
        self.assertEqual(pose['url'].count('/'), 4)
        self.assertNotIn('#', pose['url'])
        with self.server(catalog) as get:
            self.assertEqual(get(pose['url'])[0], 200)
            self.assertEqual(get(pose['thumb'])[0], 200)

    def test_cache_keeps_only_configured_collections(self):
        other = self.root / 'other'
        png(other / 'x.png')
        poses.remember_collection(self.images.resolve())
        poses.remember_collection(other.resolve())
        self.catalog()
        self.catalog(other)
        cache = poses.read_dict(poses.DATA_DIR / 'poses_cache.json')
        self.assertEqual(len(cache), 4)
        with patch('poses.Image.open', wraps=Image.open) as opened:
            self.catalog()
            self.assertEqual(opened.call_count, 0)
        poses.forget_collection(poses.collection_id(other.resolve()))
        self.catalog()  # entries of a collection that is no longer configured are dropped
        self.assertEqual(len(poses.read_dict(poses.DATA_DIR / 'poses_cache.json')), 3)

    def test_cache_of_nested_collection_survives_parent_scan(self):
        # Same situation as a collection at a drive root: its folder contains another collection
        # (here inside a skipped `_` folder) whose cache entries must not be swallowed.
        outer, inner = self.root / 'outer', self.root / 'outer' / '_inner'
        png(outer / 'a.png')
        png(inner / 'b.png')
        poses.remember_collection(outer.resolve())
        poses.remember_collection(inner.resolve())
        self.catalog(inner)
        self.catalog(outer)
        parents = {key.split('|')[0] for key in poses.read_dict(poses.DATA_DIR / 'poses_cache.json')}
        self.assertEqual(parents, {str(outer.resolve()), str(inner.resolve())})

    def test_max_files_sorts_before_truncating(self):
        for name in ('pose-10.png', 'pose-1.png', 'pose-2.png', 'pose-3.png'):
            png(self.root / 'many' / name)
        with patch.object(poses, 'MAX_FILES', 2), self.assertLogs(level='WARNING'):
            self.assertEqual([r for r, _ in poses.pose_files(self.root / 'many')], ['pose-1.png', 'pose-2.png'])

    def test_bad_paths_do_not_raise(self):
        for folder in (Path('C:/a\0b'), self.root / 'missing', None):
            self.assertEqual(poses.pose_files(folder), [])
            self.assertFalse(poses.has_poses(folder))

    def test_scan_never_writes_into_pose_folder(self):
        before = snapshot(self.images)
        catalog = self.catalog()
        with self.server(catalog) as get:
            for pose in catalog.get():
                self.assertEqual(get(pose['thumb'])[0], 200)
        catalog.set_mode('pose-1.png', 'ground')
        self.assertEqual(snapshot(self.images), before)


class BodyScaleTests(Base):
    def test_area_and_stature_give_one_body_size(self):
        # The same 25×40 body drawn at half size: half the stature, whatever the canvas.
        png(self.images / 'small.png', size=(80, 120), box=(10, 20, 22, 40))
        catalog = self.catalog()
        by_file = {pose['file']: pose for pose in catalog.get()}
        self.assertEqual(by_file['pose-1.png']['area'], 25 * 40)
        self.assertEqual(by_file['small.png']['area'], 12 * 20)
        # Standing poses define the reference: stature of a standing pose == its box height.
        self.assertAlmostEqual(catalog.reference, 40 / 1000 ** .5, places=9)
        self.assertAlmostEqual(by_file['pose-1.png']['stature'], 40, places=2)
        self.assertAlmostEqual(by_file['small.png']['stature'], 40 * (240 / 1000) ** .5, places=2)
        self.assertEqual(by_file['pose-1.png']['scale_adjust'], 1.0)

    def test_opaque_pictures_use_their_box_as_the_body(self):
        jpg(self.images / 'tall.jpg', size=(1024, 1536))
        jpg(self.images / 'wide.jpg', size=(1536, 1024))
        by_file = {pose['file']: pose for pose in self.catalog().get()}
        self.assertEqual(by_file['tall.jpg']['stature'], 1536)
        self.assertEqual(by_file['wide.jpg']['stature'], 1024)
        self.assertAlmostEqual(by_file['pose-1.png']['stature'], 40, places=2)  # cut-outs unchanged

    def test_reference_uses_standing_transparent_poses_only(self):
        items = [{'box': [0, 0, 10, 40], 'w': 100, 'h': 100, 'area': 100, 'support': {'mode': 'feet'}},
                 {'box': [0, 0, 10, 10], 'w': 100, 'h': 100, 'area': 100, 'support': {'mode': 'ground'}},
                 {'box': [0, 0, 10, 99], 'w': 10, 'h': 99, 'area': 990, 'support': {'mode': 'feet'}}]  # opaque
        self.assertEqual(poses.body_reference(items), 4.0)
        self.assertEqual(poses.body_reference(items[1:]), poses.DEFAULT_REFERENCE)

    def test_old_cache_without_area_is_measured_again(self):
        catalog = self.catalog()
        cache = poses.read_dict(poses.DATA_DIR / 'poses_cache.json')
        for entry in cache.values():
            entry.pop('area')
        poses.write_json(poses.DATA_DIR / 'poses_cache.json', cache)
        with patch('poses.Image.open', wraps=Image.open) as opened:
            catalog.scan()
            self.assertEqual(opened.call_count, 3)
        self.assertTrue(all(pose['area'] == 1000 for pose in catalog.get()))

    def test_scale_override_round_trip_and_validation(self):
        catalog = self.catalog()
        api = app.Api(catalog)
        pose = api.set_pose_scale('pose-2.png', 1.25)
        self.assertEqual(pose['scale_adjust'], 1.25)
        self.assertEqual(pose['support']['mode'], 'kneel')  # other pose data unchanged
        before = {p['file']: p['stature'] for p in catalog.get()}
        catalog.set_mode('pose-1.png', 'ground')  # a mode change keeps every stature
        self.assertEqual({p['file']: p['stature'] for p in catalog.get()}, before)
        catalog.scan()
        self.assertEqual(next(p for p in catalog.get() if p['file'] == 'pose-2.png')['scale_adjust'], 1.25)
        self.assertEqual(api.set_pose_scale('pose-2.png', 1)['scale_adjust'], 1.0)
        self.assertEqual(poses.read_dict(poses.DATA_DIR / 'scale_overrides.json'), {})
        for bad in (0.1, 3, float('nan'), '1.2', None, True):
            with self.assertRaises(ValueError):
                api.set_pose_scale('pose-1.png', bad)
        with self.assertRaises(ValueError):
            api.set_pose_scale('missing.png', 1.1)

    def test_corrupt_scale_store_is_ignored(self):
        cid = poses.collection_id(self.images.resolve())
        poses.write_json(poses.DATA_DIR / 'scale_overrides.json',
                         {cid: {'pose-1.png': 'big', 'pose-2.png': 9, 'pose-3.png': 0.8}})
        by_file = {p['file']: p['scale_adjust'] for p in self.catalog().get()}
        self.assertEqual(by_file, {'pose-1.png': 1.0, 'pose-2.png': 1.0, 'pose-3.png': 0.8})


class StateTests(Base):
    def test_state_follows_file_when_order_changes(self):
        api = app.Api(self.catalog())
        api.save_state(1)
        state = poses.read_json(poses.DATA_DIR / 'state.json', {})
        self.assertEqual((state['file'], state['index']), ('pose-2.png', 1))
        png(self.images / 'pose-0.png')  # sorts first: pose-2 moves to index 2
        api._catalog.scan()
        self.assertEqual(api.get_state(), {'index': 2, 'file': 'pose-2.png'})

    def test_missing_file_falls_back_to_clamped_index(self):
        api = app.Api(self.catalog())
        api.save_state(2)
        (self.images / 'pose-3.png').unlink()
        api._catalog.scan()
        self.assertEqual(api.get_state(), {'index': 1, 'file': 'pose-2.png'})
        poses.write_json(poses.DATA_DIR / 'state.json', {'index': 1})  # old format
        self.assertEqual(api.get_state(), {'index': 1, 'file': 'pose-2.png'})
        poses.write_json(poses.DATA_DIR / 'state.json', {'index': -4, 'file': 7})
        self.assertEqual(api.get_state(), {'index': 0, 'file': 'pose-1.png'})
        for bad in (-1, 99, '1', True, None):
            api.save_state(bad)
        self.assertEqual(poses.read_json(poses.DATA_DIR / 'state.json', {})['index'], -4)

    def test_empty_catalog_state(self):
        api = app.Api(self.catalog(self.root / 'missing'))
        self.assertEqual(api.get_state(), {'index': 0, 'file': None})
        api.save_state(0)
        self.assertFalse((poses.DATA_DIR / 'state.json').exists())

    def test_save_state_prefers_file(self):
        api = app.Api(self.catalog())
        api.save_state(0, 'pose-3.png')
        self.assertEqual(api.get_state(), {'index': 2, 'file': 'pose-3.png'})
        api.save_state(1, 'stale-from-other-collection.png')  # ignored, nothing saved
        api.save_state(1, 7)
        self.assertEqual(poses.read_json(poses.DATA_DIR / 'state.json', {})['file'], 'pose-3.png')
        api.save_state(1)
        self.assertEqual(api.get_state(), {'index': 1, 'file': 'pose-2.png'})

    def test_state_is_per_collection(self):
        other = self.root / 'other'
        for name in ('a.png', 'b.png'):
            png(other / name)
        api = app.Api(self.catalog())
        api.save_state(2)
        api._catalog.scan(other)
        self.assertEqual(api.get_state(), {'index': 0, 'file': 'a.png'})  # not pose-3's index
        api.save_state(1)
        api._catalog.scan(self.images)
        self.assertEqual(api.get_state(), {'index': 2, 'file': 'pose-3.png'})


class ConfigTests(Base):
    def test_no_personal_default_folder(self):
        self.assertFalse(hasattr(poses, 'DEFAULT_FOLDER'))
        self.assertNotIn('Alast', Path(poses.__file__).read_text(encoding='utf-8'))

    def test_migrates_old_config(self):
        poses.write_json(poses.DATA_DIR / 'config.json', {'poses_folder': str(self.images)})
        config = poses.load_config()
        cid = poses.collection_id(self.images.resolve())
        self.assertEqual(config['active'], cid)
        self.assertEqual(config['collections'], [{'id': cid, 'name': self.images.name, 'folder': str(self.images.resolve())}])
        stored = poses.read_json(poses.DATA_DIR / 'config.json', {})
        self.assertNotIn('poses_folder', stored)
        self.assertEqual(poses.load_config(), config)
        self.assertEqual(poses.find_folder(self.root / 'exe'), self.images.resolve())

    def test_exe_fallback_is_top_level_and_session_only(self):
        exe = self.root / 'exe'
        png(exe / 'nested' / 'pose.png')  # images only in subfolders do not count
        self.assertEqual(poses.find_source(exe), (None, True))
        self.assertIsNone(poses.find_folder(exe))
        png(exe / 'pose.png')
        self.assertEqual(poses.find_source(exe), (exe.resolve(), False))
        self.assertIsNone(poses.find_folder(None))  # run from source: no EXE fallback
        self.assertFalse((poses.DATA_DIR / 'config.json').exists())  # not saved as a collection
        catalog = poses.PoseCatalog(exe, '/t', recursive=False)
        catalog.scan()
        self.assertEqual(self.files(catalog), ['pose.png'])
        self.assertEqual(app.Api(catalog).get_collections()[0]['active'], True)

    def test_search_order(self):
        exe = self.root / 'exe'
        png(exe / 'pose.png')
        other = self.root / 'other'
        png(other / 'a.png')
        poses.remember_collection(self.images.resolve())
        poses.remember_collection(other.resolve())
        self.assertEqual(poses.find_folder(exe), other.resolve())  # active collection first
        (other / 'a.png').unlink()  # active folder unusable → next saved collection
        self.assertEqual(poses.find_folder(exe), self.images.resolve())
        self.assertEqual(poses.load_config()['active'], poses.collection_id(self.images.resolve()))
        for file in self.images.iterdir():
            file.unlink()
        self.assertEqual(poses.find_source(exe), (exe.resolve(), False))
        self.assertEqual(len(poses.load_config()['collections']), 2)

    def test_broken_config(self):
        for raw in ('broken', '[]', '{"collections": [1, {"folder": 5}, {"folder": ""}], "active": 3}',
                    '{"collections": [], "active": []}', '{"active": {}}', '{"poses_folder": "C:/a\\u0000b"}',
                    '{"collections": [{"folder": "C:/a\\u0000b"}], "active": {"x": 1}}', '{"poses_folder": 5}'):
            (poses.DATA_DIR).mkdir(parents=True, exist_ok=True)
            (poses.DATA_DIR / 'config.json').write_text(raw, encoding='utf-8')
            with self.assertLogs(level='WARNING') if '"poses_folder": "' in raw else contextlib.nullcontext():
                self.assertEqual(poses.load_config(), {'collections': [], 'active': None}, raw)
            self.assertEqual(poses.find_source(None), (None, True), raw)

    def test_upgrade_from_scan_cache(self):
        """Builds without collections had no config.json: recover the folder from poses_cache.json."""
        catalog = self.catalog()  # writes cache keys for self.images
        cache = poses.read_dict(poses.DATA_DIR / 'poses_cache.json')
        gone = self.root / 'gone'
        cache.update({f'{gone}|x{i}.png|1|1': {} for i in range(5)})  # most frequent, but has no poses
        cache['garbage'] = {}
        poses.write_json(poses.DATA_DIR / 'poses_cache.json', cache)
        poses.write_json(poses.DATA_DIR / 'state.json', {'index': 1})
        self.assertEqual(poses.find_folder(None), self.images.resolve())
        config = poses.load_config()
        self.assertEqual([c['folder'] for c in config['collections']], [str(self.images.resolve())])
        self.assertEqual(config['active'], config['collections'][0]['id'])
        self.assertEqual(app.Api(catalog).get_state(), {'index': 1, 'file': 'pose-2.png'})
        other = self.root / 'other'
        png(other / 'a.png')
        poses.write_json(poses.DATA_DIR / 'config.json', {'collections': [{'folder': str(other.resolve())}]})
        self.assertEqual(poses.find_folder(None), other.resolve())  # saved collections win over the cache


class SupportModeTests(Base):
    def test_priority_user_bundled_auto(self):
        png(self.images / 'wide.png', size=(80, 40), box=(0, 0, 80, 30))
        catalog = self.catalog()
        by_file = {p['file']: p for p in catalog.get()}
        self.assertEqual((by_file['pose-1.png']['support']['mode'], by_file['pose-1.png']['support_source']), ('feet', 'auto'))
        self.assertEqual((by_file['pose-2.png']['support']['mode'], by_file['pose-2.png']['support_source']), ('kneel', 'bundled'))
        self.assertEqual(by_file['pose-3.png']['support_source'], 'auto')  # invalid bundled value ignored
        self.assertEqual(by_file['wide.png']['support']['mode'], 'ground')
        api = app.Api(catalog)
        with patch('poses.Image.open', wraps=Image.open) as opened:
            pose = api.set_support_mode('pose-2.png', 'ground')
            self.assertEqual((pose['support']['mode'], pose['support']['cut'], pose['support_source']), ('ground', .55, 'user'))
            self.assertTrue(poses.valid_entry(pose))
            self.assertEqual(api.get_poses()[1], pose)
            catalog.scan()
            self.assertEqual(opened.call_count, 0)  # mode change needs no image decoding
        self.assertEqual(api.get_poses()[1]['support_source'], 'user')
        stored = poses.read_json(poses.DATA_DIR / 'support_overrides.json', {})
        self.assertEqual(stored, {poses.collection_id(self.images.resolve()): {'pose-2.png': 'ground'}})
        pose = api.set_support_mode('pose-2.png', 'auto')
        self.assertEqual((pose['support']['mode'], pose['support_source']), ('kneel', 'bundled'))
        self.assertEqual(poses.read_json(poses.DATA_DIR / 'support_overrides.json', None), {})
        self.assertEqual(api.set_support_mode('pose-1.png', 'kneel')['support_source'], 'user')

    def test_bundled_modes_only_for_top_level_files(self):
        png(self.images / 'sub' / 'pose-2.png')
        by_file = {p['file']: p for p in self.catalog().get()}
        self.assertEqual(by_file['pose-2.png']['support_source'], 'bundled')
        self.assertEqual(by_file['sub/pose-2.png']['support_source'], 'auto')

    def test_invalid_input(self):
        api = app.Api(self.catalog())
        for file, mode in (('pose-1.png', 'fly'), ('missing.png', 'feet'), (None, 'feet'), ('../x.png', 'auto')):
            with self.assertRaises(ValueError):
                api.set_support_mode(file, mode)

    def test_overrides_are_per_collection(self):
        other = self.root / 'other'
        png(other / 'pose-1.png')
        api = app.Api(self.catalog())
        api.set_support_mode('pose-1.png', 'kneel')
        api._catalog.scan(other)
        self.assertEqual(api.get_poses()[0]['support_source'], 'auto')


class CollectionTests(Base):
    def api(self, picks):
        api = app.Api(self.catalog())
        poses.remember_collection(self.images.resolve())
        api._pick_folder = MagicMock(side_effect=list(picks))
        return api

    def test_add_switch_remove(self):
        other = self.root / 'Другая'
        png(other / 'nested' / 'z.png')
        empty = self.root / 'empty'
        empty.mkdir()
        api = self.api([None, empty, other, other])
        first = api.get_collections()
        self.assertEqual([(c['name'], c['active']) for c in first], [(self.images.name, True)])
        self.assertEqual(api.add_collection(), {'collections': first, 'poses': api.get_poses()})  # cancel
        with self.assertLogs(level='WARNING'):
            self.assertEqual(api.add_collection()['collections'], first)  # no poses → not added
        data = api.add_collection()
        self.assertEqual([c['active'] for c in data['collections']], [False, True])
        self.assertEqual([p['file'] for p in data['poses']], ['nested/z.png'])
        self.assertEqual(data['collections'][1]['id'], poses.collection_id(other.resolve()))
        self.assertEqual(len(api.add_collection()['collections']), 2)  # same folder: same stable id
        with self.server(api._catalog) as get:
            self.assertEqual(get('/test/poses/nested/z.png')[0], 200)
            self.assertEqual(get('/test/poses/pose-1.png')[0], 404)  # allowlist swapped
        data = api.switch_collection(first[0]['id'])
        self.assertEqual(len(data['poses']), 3)
        self.assertEqual(poses.load_config()['active'], first[0]['id'])
        with self.assertRaises(ValueError):
            api.switch_collection('nope')
        data = api.remove_collection(first[0]['id'])  # active removed → switches to the other one
        self.assertEqual([(c['name'], c['active']) for c in data['collections']], [('Другая', True)])
        self.assertEqual([p['file'] for p in data['poses']], ['nested/z.png'])
        self.assertEqual(api.remove_collection(data['collections'][0]['id']), data)  # last one stays
        self.assertEqual(api.remove_collection('nope'), data)

    def test_choose_folder_compat(self):
        other = self.root / 'other'
        png(other / 'a.png')
        api = self.api([other])
        self.assertEqual([p['file'] for p in api.choose_folder()], ['a.png'])

    def test_no_window_is_harmless(self):
        api = app.Api(self.catalog())
        self.assertIsNone(api._pick_folder())
        data = api.add_collection()
        self.assertEqual(len(data['poses']), 3)
        self.assertEqual(data['collections'][0]['active'], True)  # unsaved current folder is still listed

    def test_switch_to_unavailable_folder(self):
        other = self.root / 'other'
        png(other / 'a.png')
        api = self.api([other])
        api.add_collection()
        images_id, other_id = (c['id'] for c in api.get_collections())
        for file in self.images.iterdir():
            file.unlink()
        with self.assertRaisesRegex(ValueError, 'Папка недоступна'):
            api.switch_collection(images_id)
        self.assertEqual((poses.load_config()['active'], api._catalog.id), (other_id, other_id))
        self.assertEqual(api.switch_collection(other_id)['poses'][0]['file'], 'a.png')  # current: fine

    def test_concurrent_switches_keep_catalog_consistent(self):
        other = self.root / 'other'
        png(other / 'a.png')
        api = self.api([other])
        api.add_collection()
        ids = [c['id'] for c in api.get_collections()]
        threads = [threading.Thread(target=api.switch_collection, args=(ids[i % 2],)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        cid, items = api._catalog.snapshot()
        self.assertEqual(set(api._catalog.allowed), {p['file'] for p in items})
        self.assertEqual(cid, poses.load_config()['active'])


class LibraryTests(Base):
    def test_favorites_and_tags(self):
        api = app.Api(self.catalog())
        self.assertEqual(api.get_library(), {'favorites': [], 'tags': {}})
        self.assertTrue(api.toggle_favorite('pose-2.png'))
        self.assertTrue(api.toggle_favorite('pose-1.png'))
        self.assertFalse(api.toggle_favorite('pose-2.png'))
        tags = api.set_tags('pose-1.png', ['  сидя  ', 'Сидя', '', 5, None, 'x' * 40, 'a  b'] + [f't{i}' for i in range(30)])
        self.assertEqual(tags[:4], ['сидя', 'x' * 32, 'a b', 't0'])
        self.assertEqual(len(tags), 16)
        self.assertEqual(api.get_library(), {'favorites': ['pose-1.png'], 'tags': {'pose-1.png': tags}})
        self.assertEqual(api.set_tags('pose-1.png', []), [])
        self.assertEqual(api.get_library()['tags'], {})
        for call in (lambda: api.toggle_favorite('missing.png'), lambda: api.set_tags('pose-1.png', 'tag'),
                     lambda: api.set_tags('missing.png', ['a']), lambda: api.toggle_favorite(['pose-1.png'])):
            with self.assertRaises(ValueError):
                call()

    def test_library_is_per_collection_and_validated(self):
        other = self.root / 'other'
        png(other / 'pose-1.png')
        api = app.Api(self.catalog())
        api.toggle_favorite('pose-1.png')
        api._catalog.scan(other)
        self.assertEqual(api.get_library(), {'favorites': [], 'tags': {}})
        poses.write_json(poses.DATA_DIR / 'library.json', {api._catalog.id: {
            'favorites': ['pose-1.png', 'pose-1.png', 3], 'tags': {'pose-1.png': 'x', 'b': [' y ', 1]}}})
        self.assertEqual(api.get_library(), {'favorites': ['pose-1.png'], 'tags': {'b': ['y']}})
        (poses.DATA_DIR / 'library.json').write_text('[broken', encoding='utf-8')
        self.assertEqual(api.get_library(), {'favorites': [], 'tags': {}})
        self.assertTrue(api.toggle_favorite('pose-1.png'))


class ViewPrefsTests(Base):
    def test_whitelist(self):
        api = app.Api(self.catalog())
        self.assertEqual(api.get_view_prefs(), {})
        saved = api.save_view_prefs({'flip': True, 'gray': 1, 'grid': 'thirds', 'on_top': 'yes', 'compact': False,
                                     'evil': '<script>', 'filter': {'mode': 'kneel', 'favorites': True,
                                                                    'tag': '  сидя ', 'x': 1}})
        self.assertEqual(saved, {'flip': True, 'grid': 'thirds', 'compact': False,
                                 'filter': {'mode': 'kneel', 'favorites': True, 'tag': 'сидя'}})
        self.assertEqual(api.save_view_prefs({'filter': {'search': 'поза ' + 'x' * 200}})['filter']['search'],
                         'поза ' + 'x' * 95)
        self.assertEqual(api.save_view_prefs({'filter': {'search': 5}})['filter']['search'], 'поза ' + 'x' * 95)
        api.save_view_prefs({'filter': {'search': ''}})
        saved = api.save_view_prefs({'grid': 'spiral', 'gray': True, 'filter': {'tag': None, 'mode': 'fly'}})
        self.assertEqual(saved, {'flip': True, 'grid': 'thirds', 'compact': False, 'gray': True,
                                 'filter': {'mode': 'kneel', 'favorites': True, 'tag': None, 'search': ''}})
        self.assertEqual(api.get_view_prefs(), saved)
        self.assertEqual(api.save_view_prefs('nope'), saved)
        poses.write_json(poses.DATA_DIR / 'view_prefs.json', {'flip': 'x', 'grid': 'proportions', 'other': 1})
        self.assertEqual(api.get_view_prefs(), {'grid': 'proportions'})


class ThumbnailTests(Base):
    def test_thumbs_route_and_cache(self):
        png(self.images / 'big' / 'tall.png', size=(400, 1000), box=(100, 100, 300, 900))
        jpg(self.images / 'photo.jpeg', size=(900, 300))
        catalog = self.catalog()
        before = snapshot(self.images)
        with self.server(catalog) as get:
            status, headers, data = get('/test/thumbs/big/tall.png')
            self.assertEqual((status, headers['Content-Type']), (200, 'image/png'))
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual((im.size, im.mode), ((128, 320), 'RGBA'))
                self.assertEqual(im.getpixel((0, 0))[3], 0)  # alpha kept
            with Image.open(io.BytesIO(get('/test/thumbs/photo.jpeg')[2])) as im:
                self.assertEqual((im.size, im.mode), ((320, 107), 'RGB'))
            cached = sorted((poses.DATA_DIR / 'thumbs').iterdir())
            self.assertEqual(len(cached), 2)
            self.assertTrue(all(re.fullmatch(r'[0-9a-f]{40}\.png', p.name) for p in cached))
            with patch('poses.Image.open', wraps=Image.open) as opened:
                self.assertEqual(get('/test/thumbs/big/tall.png')[0], 200)
                self.assertEqual(get('/test/thumbs/big/tall.png', 'HEAD')[0], 200)
                self.assertEqual(opened.call_count, 0)
            for url in ('/test/thumbs/', '/test/thumbs/missing.png', '/test/thumbs/../app.py',
                        '/test/thumbs/%2e%2e%2fapp.py', '/test/thumbs/big%5ctall.png', '/test/thumbs/big/../pose-1.png',
                        '/test/thumbs/C:%5cWindows%5cwin.ini'):
                self.assertEqual(get(url)[0], 404, url)
        self.assertEqual(snapshot(self.images), before)

    def test_changed_file_gets_new_thumbnail(self):
        catalog = self.catalog()
        path = catalog.resolve('pose-1.png')
        first = poses.thumbnail(path)
        png(path, size=(50, 50))
        os.utime(path, ns=(1, 1))
        second = poses.thumbnail(path)
        self.assertNotEqual(first, second)
        self.assertTrue(second.is_relative_to(poses.DATA_DIR / 'thumbs'))

    def test_concurrent_generation(self):
        png(self.images / 'big.png', size=(1500, 1500), box=(0, 0, 1500, 1500))
        catalog = self.catalog()
        results = []
        threads = [threading.Thread(target=lambda: results.append(poses.thumbnail(catalog.resolve('big.png'))))
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(results)), 1)
        self.assertEqual([p.suffix for p in (poses.DATA_DIR / 'thumbs').iterdir()], ['.png'])

    def test_prune_thumbs_lru(self):
        poses.prune_thumbs()  # no folder yet: nothing to do
        folder = poses.DATA_DIR / 'thumbs'
        folder.mkdir(parents=True)
        for i in range(6):
            (folder / f'{i}.png').write_bytes(b'x' * 10)
            os.utime(folder / f'{i}.png', (1000 + i, 1000 + i))
        (folder / 'old.tmp').write_bytes(b'')
        os.utime(folder / 'old.tmp', (0, 0))
        (folder / 'fresh.tmp').write_bytes(b'')
        poses.prune_thumbs()  # far below the byte cap: no file-count limit
        self.assertEqual(len(list(folder.iterdir())), 7)
        self.assertFalse((folder / 'old.tmp').exists())
        (folder / 'recent.png').write_bytes(b'x' * 100)  # used within THUMB_GRACE: never pruned
        poses.prune_thumbs(max_bytes=25)
        self.assertEqual(sorted(p.name for p in folder.glob('*.png')), ['recent.png'])

    def test_pruned_thumbnail_is_regenerated_on_open_failure(self):
        catalog = self.catalog()
        thumb = poses.thumbnail(catalog.resolve('pose-1.png'))
        real_open, pruned = Path.open, []

        def flaky_open(path, *args, **kwargs):  # simulate one prune between lookup and open()
            if path == thumb and not pruned:
                pruned.append(thumb.unlink())
            return real_open(path, *args, **kwargs)
        with self.server(catalog) as get, patch.object(Path, 'open', flaky_open):
            status, headers, data = get('/test/thumbs/pose-1.png')
        self.assertEqual((status, headers['Content-Type']), (200, 'image/png'))
        self.assertTrue(thumb.exists())

    def test_cache_hit_refreshes_lru_time(self):
        path = self.catalog().resolve('pose-1.png')
        thumb = poses.thumbnail(path)
        os.utime(thumb, (1000, 1000))
        self.assertEqual(poses.thumbnail(path), thumb)
        self.assertGreater(thumb.stat().st_mtime, 1000)

    def test_broken_image_thumbnail(self):
        catalog = self.catalog()
        path = catalog.resolve('pose-1.png')
        path.write_bytes(b'broken')
        with self.assertLogs(level='ERROR'), self.server(catalog) as get:
            self.assertEqual(get('/test/thumbs/pose-1.png')[0], 404)


class ContainmentTests(Base):
    def test_allowlisted_path_outside_folder_is_rejected(self):
        outside = self.root / 'outside.png'
        png(outside)
        catalog = self.catalog()
        with catalog.lock:
            catalog.allowed['pose-1.png'] = self.images / '..' / 'outside.png'
            catalog.allowed['other.png'] = outside
        with self.server(catalog) as get:
            for url in ('/test/poses/pose-1.png', '/test/thumbs/pose-1.png', '/test/poses/other.png',
                        '/test/thumbs/other.png'):
                self.assertEqual(get(url)[0], 404, url)
            self.assertEqual(get('/test/poses/pose-2.png')[0], 200)
        self.assertFalse((poses.DATA_DIR / 'thumbs').exists())

    def test_symlinked_file_escape(self):
        outside = self.root / 'outside.png'
        png(outside)
        try:
            os.symlink(outside, self.images / 'link.png')
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks need Developer Mode or admin rights on Windows')
        catalog = self.catalog()
        self.assertNotIn('link.png', self.files(catalog))
        with catalog.lock:
            catalog.allowed['link.png'] = self.images / 'link.png'
        with self.server(catalog) as get:
            self.assertEqual(get('/test/poses/link.png')[0], 404)
            self.assertEqual(get('/test/thumbs/link.png')[0], 404)

    def test_junction_escape(self):
        try:
            import _winapi
            outside = self.root / 'outside'
            png(outside / 'secret.png')
            _winapi.CreateJunction(str(outside), str(self.images / 'junction'))
        except (ImportError, AttributeError, OSError):
            self.skipTest('Junctions are Windows-only')
        try:
            catalog = self.catalog()
            self.assertEqual(self.files(catalog), ['pose-1.png', 'pose-2.png', 'pose-3.png'])
            with catalog.lock:
                catalog.allowed['junction/secret.png'] = self.images / 'junction' / 'secret.png'
            with self.server(catalog) as get:
                self.assertEqual(get('/test/poses/junction/secret.png')[0], 404)
        finally:
            os.rmdir(self.images / 'junction')

    def test_file_swapped_after_scan(self):
        catalog = self.catalog()
        (self.images / 'pose-1.png').unlink()
        (self.images / 'pose-1.png').mkdir()
        with self.server(catalog) as get:
            self.assertEqual(get('/test/poses/pose-1.png')[0], 404)


class ServerTests(Base):
    def test_csp_hashes_inline_importmap(self):
        catalog = self.catalog()
        with self.server(catalog) as get:
            status, headers, html = get('/test/web/index.html')
            csp = headers['Content-Security-Policy']
            self.assertEqual(status, 200)
            self.assertNotIn("'unsafe-inline'", csp)
            match = re.search(rb'<script type="importmap">(.*?)</script>', html, re.DOTALL)
            if match:
                digest = base64.b64encode(hashlib.sha256(match.group(1).replace(b'\r\n', b'\n')).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", csp)
            self.assertNotIn('sha256', get('/test/web/main.js')[1]['Content-Security-Policy'])
        policy = app.content_security_policy(b'<script>a\r\nb</script><script src="x.js"></script><STYLE>p{}</STYLE>')
        expected = base64.b64encode(hashlib.sha256(b'a\nb').digest()).decode()
        self.assertIn(f"script-src 'self' 'unsafe-eval' 'sha256-{expected}';", policy)
        self.assertEqual(policy.count('sha256-'), 2)

    def test_image_mime_types(self):
        jpg(self.images / 'a.jpg')
        Image.new('RGBA', (8, 8), (0, 0, 0, 255)).save(self.images / 'b.webp', 'WEBP')
        with self.server(self.catalog()) as get:
            self.assertEqual(get('/test/poses/a.jpg')[1]['Content-Type'], 'image/jpeg')
            self.assertEqual(get('/test/poses/b.webp')[1]['Content-Type'], 'image/webp')
            self.assertEqual(get('/test/poses/pose-1.png')[1]['Content-Type'], 'image/png')

    def test_dev_server_api(self):
        catalog = self.catalog()
        api = app.Api(catalog, fullscreen=False)
        with self.server(catalog, dev_server.DevHandler, api) as request:
            json_headers = {'Content-Type': 'application/json'}
            status, _, body = request('/test/__api/get_poses', 'POST', b'[]', json_headers)
            self.assertEqual((status, len(json.loads(body))), (200, 3))
            status, _, body = request('/test/__api/save_state', 'POST', b'[1]', json_headers)
            self.assertEqual(status, 200)
            status, _, body = request('/test/__api/get_state', 'POST', b'[]', json_headers)
            self.assertEqual(json.loads(body), {'index': 1, 'file': 'pose-2.png'})
            for name in ('set_on_top', 'set_compact'):
                self.assertEqual(json.loads(request(f'/test/__api/{name}', 'POST', b'[true]', json_headers)[2]), True)
            self.assertEqual(request('/test/__api/toggle_fullscreen', 'POST', b'[]', json_headers)[0], 200)
            self.assertEqual(request('/test/__api/get_poses', 'POST', b'[]', {'Content-Type': 'text/plain'})[0], 403)
            self.assertEqual(request('/test/__api/_pick_folder', 'POST', b'[]', json_headers)[0], 404)
            for body in (b'{"index": 1}', b'5', b'not json'):
                self.assertEqual(request('/test/__api/save_state', 'POST', body, json_headers)[0], 400, body)
            self.assertEqual(request('/test/__api/save_state', 'POST', b'[0, "pose-3.png"]', json_headers)[0], 200)
            self.assertEqual(api.get_state()['file'], 'pose-3.png')
            self.assertEqual(request('/test/__api/set_tags', 'POST', b'["x", ["a"]]', json_headers)[0], 500)
            self.assertEqual(request('/test/thumbs/pose-1.png')[1]['Content-Type'], 'image/png')
            self.assertIn(b'pywebviewready', request('/test/__shim.js')[2])


class WindowTests(Base):
    def test_window_methods_without_window(self):
        api = app.Api(self.catalog())
        self.assertIs(api.set_on_top(True), True)
        self.assertIs(api.set_compact(True), True)
        self.assertIs(api.set_compact(False), False)
        api.toggle_fullscreen()
        api.quit()

    def test_compact_round_trip(self):
        api = app.Api(self.catalog())
        window = api._window = MagicMock()
        window.native = None  # already on the UI thread
        with patch.object(app.Api, '_place_compact') as place:
            api.set_on_top(False)
            self.assertTrue(api.set_compact(True))
            place.assert_called_once_with(window)
            window.toggle_fullscreen.assert_called_once()
            self.assertIs(window.on_top, True)
            self.assertTrue(api.set_compact(True))  # idempotent
            self.assertEqual(window.toggle_fullscreen.call_count, 1)
            api.toggle_fullscreen()  # F in compact mode: back to fullscreen
            self.assertEqual(window.toggle_fullscreen.call_count, 2)
            self.assertIs(window.on_top, False)
            self.assertFalse(api.set_compact(False))
            self.assertEqual(window.toggle_fullscreen.call_count, 2)

    def test_window_changes_run_on_the_ui_thread(self):
        # Setting TopMost from an API thread deadlocked the window ("Не отвечает").
        calls = []

        class Native:
            InvokeRequired = True

            def Invoke(self, action):
                calls.append('ui')
                action()

        fake_system = MagicMock(Action=lambda fn: fn)
        api = app.Api(self.catalog())
        window = api._window = MagicMock()
        window.native = Native()
        with patch.dict(sys.modules, {'System': fake_system}), patch.object(app.Api, '_place_compact') as place:
            api.set_on_top(True)
            self.assertEqual(calls, ['ui'])
            self.assertIs(window.on_top, True)
            api.set_compact(True)
            self.assertEqual(calls, ['ui', 'ui'])
            place.assert_called_once_with(window)

            def broken():
                raise RuntimeError('boom')
            with self.assertRaises(RuntimeError):  # errors on the UI thread reach the caller
                app.Api._on_ui(window, broken)

    def test_place_compact_uses_work_area(self):
        window = MagicMock()
        window.native = None  # no native handle → pywebview resize/move fallback
        with patch.object(app, 'work_area', return_value=(0, 0, 1920, 1040)):
            app.Api._place_compact(window)
        window.resize.assert_called_once_with(480, 720)
        window.move.assert_called_once_with(1920 - 480 - 12, 160)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows API')
    def test_work_area_is_sane(self):
        left, top, right, bottom = app.work_area()
        self.assertGreater(right - left, 0)
        self.assertGreater(bottom - top, 0)


class RuntimeTests(Base):
    def setUp(self):
        super().setUp()
        self.portable = self.root / 'WebView2'
        self.portable.mkdir()
        (self.portable / 'msedgewebview2.exe').write_bytes(b'')
        self.versions = {'system': '144.0', 'portable': '155.0'}
        self.addCleanup(app.LAUNCH.update, {'runtime': None, 'fallback': False})
        self.addCleanup(os.environ.pop, diagnostics.FORCE_ENV, None)

    def check(self, path=None):
        version = self.versions['portable' if path else 'system']
        if version is None:
            raise RuntimeError('unavailable')
        return {'mode': 'portable' if path else 'system', 'version': version}

    def select(self):
        with patch.object(diagnostics, 'check_runtime', side_effect=self.check):
            return diagnostics.select_runtime(self.portable, poses.DATA_DIR)

    def test_decision_table(self):
        decide = diagnostics.decide_runtime
        self.assertEqual(decide('1', True), 'system')
        self.assertEqual(decide(None, True), 'portable')
        self.assertEqual(decide('1', True, forced=True), 'portable')
        self.assertEqual(decide('1', True, failed_version='1'), 'portable')
        self.assertEqual(decide('2', True, failed_version='1'), 'system')  # system updated: try it again
        self.assertEqual(decide('1', False, forced=True, failed_version='1'), 'system')
        self.assertIsNone(decide(None, False))

    def test_select_reports_both_runtimes(self):
        result = self.select()
        self.assertEqual((result['mode'], result['version'], result['reason']), ('system', '144.0', 'preferred'))
        self.assertEqual(result['system'], {'available': True, 'version': '144.0'})
        self.assertEqual(result['portable'], {'available': True, 'version': '155.0'})
        self.assertTrue(diagnostics.can_fall_back(result))
        self.versions['system'] = None
        result = self.select()
        self.assertEqual((result['mode'], result['version'], result['reason']), ('portable', '155.0', 'system unavailable'))
        self.assertFalse(result['system']['available'])
        self.versions['portable'] = None
        self.assertEqual((self.select()['mode'], self.select()['reason']), (None, 'none available'))

    def test_failed_system_goes_portable_until_version_changes(self):
        poses.write_json(poses.DATA_DIR / diagnostics.FAILED_MARKER, {'version': '144.0'})
        result = self.select()
        self.assertEqual((result['mode'], result['reason'], result['system_failed_version']),
                         ('portable', 'system failed before', '144.0'))
        self.assertFalse(diagnostics.can_fall_back(result))
        self.versions['system'] = '145.0'
        self.assertEqual(self.select()['mode'], 'system')
        (poses.DATA_DIR / diagnostics.FAILED_MARKER).write_text('[1]', encoding='utf-8')
        self.assertEqual(self.select()['mode'], 'system')

    def test_forced_run_never_falls_back_again(self):
        os.environ[diagnostics.FORCE_ENV] = 'portable'
        result = self.select()
        self.assertEqual((result['mode'], result['reason'], result['forced']), ('portable', 'forced', True))
        app.LAUNCH['runtime'] = dict(result, mode='system')  # even if portable were missing → system
        self.assertFalse(app.runtime_failed())

    def test_runtime_failure_relaunches_once_with_portable(self):
        app.LAUNCH['runtime'] = self.select()
        window = MagicMock()
        logging_handler = app.RuntimeFailureWatch(window)
        pywebview_log = logging.getLogger('pywebview')
        pywebview_log.addHandler(logging_handler)
        try:
            with self.assertLogs(level='WARNING'):
                pywebview_log.error('WebView2 initialization failed with exception:\n boom')
        finally:
            pywebview_log.removeHandler(logging_handler)
        self.assertTrue(app.LAUNCH['fallback'])
        for _ in range(50):
            if window.destroy.called:
                break
            threading.Event().wait(0.02)
        window.destroy.assert_called_once()
        self.assertTrue(app.runtime_failed(window))  # idempotent: no second destroy
        with patch('subprocess.Popen') as popen, patch.object(sys, 'argv', ['app.py', '--x']), \
                self.assertLogs(level='INFO'):
            app.relaunch_portable()
        command, env = popen.call_args.args[0], popen.call_args.kwargs['env']
        self.assertEqual(command, [sys.executable, 'app.py', '--x'])
        self.assertEqual((env[diagnostics.FORCE_ENV], env['PYINSTALLER_RESET_ENVIRONMENT']), ('portable', '1'))
        self.assertEqual(diagnostics.failed_system_version(poses.DATA_DIR), '144.0')
        self.assertEqual(self.select()['mode'], 'portable')  # next launch goes straight to portable

    def test_no_fallback_without_portable(self):
        self.versions['portable'] = None
        (self.portable / 'msedgewebview2.exe').unlink()
        app.LAUNCH['runtime'] = self.select()
        self.assertEqual(app.LAUNCH['runtime']['mode'], 'system')
        self.assertFalse(app.runtime_failed(MagicMock()))


if __name__ == '__main__':
    unittest.main()
