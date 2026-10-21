"""Desktop mode (desktop.py): geometry port, composition, filter order, settings, switching.

Run with: python -m unittest discover -s tests -v
"""
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

import app
import desktop
import poses

ROOT = Path(__file__).resolve().parent.parent
SPRITE = ROOT / 'web' / 'desktop'


def pose(file='a.png', box=(100, 100, 300, 900), mode='feet', stature=None, adjust=1.0, w=400, h=1000):
    x1, y1, x2, y2 = box
    profile = [[x, y2 - (0 if abs(x - (x1 + x2) / 2) < 40 else 30)] for x in range(x1 + 5, x2, 10)]
    return {'file': file, 'box': list(box), 'w': w, 'h': h, 'stature': stature or (y2 - y1),
            'scale_adjust': adjust, 'support': {'mode': mode, 'cut': poses.CUTS[mode], 'profile': profile}}


class GeometryTests(unittest.TestCase):
    meta = desktop.load_meta(SPRITE)

    def test_projection_round_trip_on_the_podium_top(self):
        for i in range(16):
            a = i / 16 * 2 * math.pi
            x, z = math.cos(a) * 1.7, math.sin(a) * 1.7
            gx, gz = desktop.to_ground(self.meta, *desktop.project(self.meta, x, self.meta['top'], z))
            self.assertAlmostEqual(gx, x, places=9)
            self.assertAlmostEqual(gz, z, places=9)

    def test_one_body_size_and_limits(self):  # "body" sizing
        body = desktop.BODY_TO_PODIUM * self.meta['diameter']
        for stature in (600, 800, 1000):
            p = desktop.fit_placement(pose(stature=stature), self.meta, 'body')
            self.assertFalse(p['limited'])
            self.assertAlmostEqual(p['scale'] * stature, body, places=6)
        bigger = desktop.fit_placement(pose(stature=800, adjust=1.1), self.meta, 'body')
        self.assertAlmostEqual(bigger['scale'] * 800, body * 1.1, places=6)
        # A very wide lying pose cannot keep the body size: shrunk, contacts still on the podium.
        lying = pose(box=(0, 700, 2000, 900), mode='ground', stature=600, w=2000)
        p = desktop.fit_placement(lying, self.meta, 'body')
        self.assertTrue(p['limited'])
        contacts, cx, cy = desktop.support_data(lying)
        for x, y in contacts:
            gx, gz = desktop.to_ground(self.meta, self.meta['centerX'] + (x - cx) * p['scale'],
                                       self.meta['centerY'] + (cy - y) * p['scale'])
            self.assertLessEqual(math.hypot(gx, gz), self.meta['supportRadius'] + 1e-9)

    def test_file_sizing_keeps_every_picture_at_one_scale(self):  # the default
        self.assertEqual(desktop.fit_placement.__defaults__, ('file',))
        scales = {desktop.fit_placement(pose(stature=s, adjust=a, box=b), self.meta, 'file')['scale']
                  for s, a, b in ((600, 1, (100, 100, 300, 900)), (900, 1.3, (0, 700, 2000, 900)))}
        self.assertEqual(len(scales), 1)  # no body estimate, no correction, no limits
        expected = desktop.BODY_TO_PODIUM * self.meta['diameter'] / (desktop.FILE_FIGURE_SHARE * 1000)
        self.assertAlmostEqual(scales.pop(), expected, places=9)
        p = desktop.fit_placement(pose(), self.meta, 'file')
        _, cx, cy = desktop.support_data(pose())
        self.assertAlmostEqual(p['x'], self.meta['centerX'] + (200 - cx) * p['scale'], places=9)  # support centred
        self.assertFalse(p['limited'])

    def test_shadow_falls_back_and_right(self):
        dx, dy = desktop.shadow_step(self.meta)
        self.assertGreater(dx, 0)  # key light from the left
        self.assertLess(dy, 0)  # away from the camera = up on screen


class OrderTests(unittest.TestCase):
    items = [pose('a.png', mode='feet'), pose('sub/b.png', mode='kneel'), pose('c.png', mode='ground')]

    def test_no_filter_cycles_everything(self):
        self.assertEqual(desktop.order(self.items, {}, {}), [0, 1, 2])

    def test_saved_gallery_filter_is_applied(self):
        library = {'favorites': ['c.png', 'sub/b.png'], 'tags': {'sub/b.png': ['Стоя']}}
        self.assertEqual(desktop.order(self.items, {'filter': {'mode': 'kneel'}}, library), [1])
        self.assertEqual(desktop.order(self.items, {'filter': {'favorites': True}}, library), [1, 2])
        self.assertEqual(desktop.order(self.items, {'filter': {'tag': 'стоя'}}, library), [1])
        self.assertEqual(desktop.order(self.items, {'filter': {'search': 'SUB/'}}, library), [1])
        # A filter matching nothing falls back to all poses instead of an empty podium.
        self.assertEqual(desktop.order(self.items, {'filter': {'tag': 'нет'}}, library), [0, 1, 2])


class StageTests(unittest.TestCase):
    def test_frame_layers_and_premultiplied_pixels(self):
        stage = desktop.Stage(SPRITE, 350)
        image = Image.new('RGBA', (400, 1000), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle((100, 100, 300, 900), fill=(180, 40, 40, 255))
        figure, shadow, placement = stage.layers(pose(), image)
        self.assertEqual(figure.size, stage.size)
        # The cast shadow only lands on the podium top.
        outside = Image.eval(stage.top_mask, lambda v: 255 - v)
        self.assertIsNone(Image.composite(shadow.getchannel('A'), Image.new('L', stage.size, 0), outside).getbbox())
        self.assertIsNotNone(shadow.getchannel('A').getbbox())
        frame = stage.frame([(figure, shadow, 1)])
        self.assertEqual(frame.getpixel((0, 0))[3], 0)  # transparent corner: click-through
        half = stage.frame([(figure, shadow, .5)])
        box = figure.getchannel('A').getbbox()
        centre = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        self.assertLess(half.getpixel(centre)[0], frame.getpixel(centre)[0] + 1)
        data = desktop.premultiplied_bgra(Image.new('RGBA', (1, 1), (200, 100, 50, 128)))
        self.assertEqual(tuple(data), (25, 50, 100, 128))  # B, G, R premultiplied by alpha


class SettingsAndSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = poses.DATA_DIR
        poses.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        poses.DATA_DIR = self.old
        self.tmp.cleanup()

    def test_settings_defaults_and_validation(self):
        self.assertEqual(desktop.read_settings(), {'size': 'medium', 'on_top': True})
        poses.write_json(poses.DATA_DIR / 'desktop.json', {'size': 'huge', 'on_top': 'yes', 'x': 10, 'y': 1.5})
        self.assertEqual(desktop.read_settings(), {'size': 'medium', 'on_top': True})
        desktop.save_settings({'size': 'large', 'on_top': False, 'x': -20, 'y': 40})
        self.assertEqual(desktop.read_settings(), {'size': 'large', 'on_top': False, 'x': -20, 'y': 40})

    def test_launch_command(self):
        with patch('desktop.subprocess.Popen') as popen, patch.object(sys, 'frozen', True, create=True):
            desktop.launch(['--desktop'])
        command = popen.call_args.args[0]
        self.assertEqual(command, [sys.executable, '--desktop'])
        self.assertEqual(popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        with patch('desktop.subprocess.Popen') as popen:
            desktop.launch([])
        self.assertTrue(popen.call_args.args[0][1].endswith('app.py'))

    def test_open_desktop_from_the_window(self):
        catalog = poses.PoseCatalog(None, '/t')
        catalog.scan()
        api = app.Api(catalog)
        self.assertFalse(api.open_desktop())  # no window: nothing to switch from
        api._window = MagicMock()
        with patch('desktop.launch') as launch:
            self.assertTrue(api.open_desktop())
        launch.assert_called_once_with(['--desktop'])
        api._window.destroy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
