"""Exports the dance phrases of a pose collection to folders of copies (docs/DANCE.md, stage 2).

Usage: python tools/export_phrases.py [--folder POSES] [--out FOLDER] [--limit 40] [--gif]
         [--style soft|groove] [--fps 50]

Each phrase becomes "NN Name/" with numbered copies (01__file.png …; mirrored poses are saved
flipped), plus a review sheet — and with --gif an animation at 120 BPM — in "_просмотр/".
The copies get their skeletons and support modes as PoseStage corrections, so the output (or
one phrase folder) opened as a collection animates like the original.

Nothing is overwritten and the pose folder is never written to. "_экспорт.json" in the output
remembers every exported phrase: numbers continue, and a phrase exported before is skipped even
after its folder was deleted (deleting is how unwanted phrases are rejected).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import sys

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dance  # noqa: E402
import dance_motion  # noqa: E402
import desktop  # noqa: E402
import poses  # noqa: E402

HEIGHT = 640
MIRROR_TAG = "__зеркально"
REVIEW = "_просмотр"
MANIFEST = "_экспорт.json"
COLORS = {"head": (244, 244, 244), "pink": (255, 143, 200), "red_l": (255, 138, 138), "red_r": (232, 58, 58),
          "green_l": (125, 235, 168), "green_r": (47, 191, 107)}  # docs/SKELETON.md
PARENT = {child: parent for child, parent in dance_motion.TREE}


def color(key):
    if key == "head":
        return COLORS["head"]
    if key in ("chest", "navel", "pelvis"):
        return COLORS["pink"]
    arm = key.split("_")[0] in ("shoulder", "elbow", "wrist")
    return COLORS[("red_" if arm else "green_") + key[-1]]


def display_joints(prepared):
    """Skeleton points of a prepared pose in stage pixels."""
    pose, k = prepared["pose"], prepared["per_source"]
    x1, y1 = pose["box"][:2]
    return {key: (prepared["left"] + (point["xy"][0] - x1) * k, prepared["top"] + (point["xy"][1] - y1) * k)
            for key, point in pose["joints"]["points"].items() if point.get("xy") and point.get("state") != "absent"}


def existing(out: Path):
    """(highest number in use, pose sets exported before): folders, review files and the manifest."""
    highest, known = 0, set()
    for entry in out.iterdir() if out.is_dir() else ():
        match = re.match(r"(\d+)\s", entry.name)
        if entry.is_dir() and match:
            highest = max(highest, int(match.group(1)))
            known.add(frozenset(re.sub(r"^\d+__", "", f.stem) for f in entry.iterdir()
                                if f.is_file() and f.suffix.lower() in poses.EXTENSIONS))
    for entry in (out / REVIEW).iterdir() if (out / REVIEW).is_dir() else ():
        match = re.match(r"(\d+)\s", entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    for phrase in poses.read_dict(out / MANIFEST).get("phrases") or []:
        if isinstance(phrase, dict) and isinstance(phrase.get("poses"), list):
            known.add(frozenset(str(stem) for stem in phrase["poses"]))
            if type(phrase.get("number")) is int:
                highest = max(highest, phrase["number"])
    return highest, known


def upright(path: Path) -> Image.Image:
    """The drawing as the catalog measured it: EXIF orientation applied, in sRGB."""
    with Image.open(path) as opened:
        opened.load()
        return poses._to_srgb(ImageOps.exif_transpose(opened), "RGBA")


def sheet(title, cells, mirrors):
    cw, ch = cells[0].size
    w, h = cw // 2, ch // 2
    out = Image.new("RGB", (w * len(cells), h + 40), (28, 30, 34))
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("segoeui.ttf", 20)
    except OSError:
        font = None
    draw.text((10, 8), title, fill=(255, 255, 255), font=font)
    for i, cell in enumerate(cells):
        out.paste(cell.convert("RGB").resize((w, h)), (i * w, 40))
        draw.text((i * w + 8, 46), f"{i + 1}{'  (зеркально)' if mirrors[i] else ''}", fill=(230, 230, 230), font=font)
    return out


def store_corrections(corrections: dict):
    """{collection folder: {relative file: pose}} → skeleton and support-mode corrections."""
    for folder, files in corrections.items():
        cid = poses.collection_id(str(folder))

        def joints(section):
            section.update({rel: poses.clean_joints(p["joints"]) for rel, p in files.items()
                            if poses.valid_joints(p.get("joints"))})

        def modes(section):
            section.update({rel: p["support"]["mode"] for rel, p in files.items()
                            if (p.get("support") or {}).get("mode") in poses.MODES})
        poses.update_section("joint_overrides.json", cid, joints)
        poses.update_section("support_overrides.json", cid, modes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--folder", type=Path, help="pose collection (default: the one open in PoseStage)")
    parser.add_argument("--out", type=Path, help='output (default: "Позы — связки" next to the collection)')
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--gif", action="store_true", help="also render an animation of each phrase (slow)")
    parser.add_argument("--style", choices=("soft", "groove"), default="soft")
    parser.add_argument("--fps", type=int, default=50)
    args = parser.parse_args(argv)
    folder = args.folder
    if folder is None:
        config = poses.load_config()
        active = next((c for c in config["collections"] if c["id"] == config["active"]), None)
        folder = Path(active["folder"]) if active else None
    if folder is None or not folder.is_dir():
        parser.error("no pose folder: pass --folder")
    folder = folder.resolve()
    out = (args.out or folder.parent / "Позы — связки").resolve()
    if out == folder or folder in out.parents or out in folder.parents:
        parser.error("the output must be outside the pose folder (and not contain it)")
    catalog = poses.PoseCatalog(folder, "/poses")
    catalog.scan()
    items = {pose["file"]: pose for pose in catalog.get()}
    print(f"{len(items)} poses, reading outfits…", flush=True)
    styles = dance.load_styles(list(items.values()), catalog.resolve)
    phrases = dance.find_phrases(list(items.values()), limit=args.limit, styles=styles)
    number, known = existing(out)
    manifest = poses.read_dict(out / MANIFEST)
    manifest.setdefault("phrases", [])
    stage = desktop.Stage(ROOT / "web" / "desktop", HEIGHT, "body")
    top, bottom = round(stage.size[1] * .08), round(stage.size[1] * .86)
    corrections = {}
    made = 0
    for phrase in phrases:
        stems = [Path(e["file"]).stem + (MIRROR_TAG if e["mirror"] else "") for e in phrase["poses"]]
        if frozenset(stems) in known:
            print("skip (exported before):", phrase["name"])
            continue
        sources = [catalog.resolve(e["file"]) for e in phrase["poses"]]
        if not all(source and source.is_file() for source in sources):
            print("skip (a file is missing):", phrase["name"])
            continue
        name = re.sub(r'[<>:"/\\|?*]', "", phrase["name"].replace(" · ", ", "))
        number += 1
        while (out / f"{number:02d} {name}").exists() or (out / REVIEW / f"{number:02d} {name}.png").exists():
            number += 1
        target = out / f"{number:02d} {name}"
        target.mkdir(parents=True)
        (out / REVIEW).mkdir(exist_ok=True)
        canvases, joints, cells = [], [], []
        for n, (entry, stem, source) in enumerate(zip(phrase["poses"], stems, sources), 1):
            pose, image = items[entry["file"]], upright(source)
            if entry["mirror"]:
                pose, image = dance.mirror_pose(pose), ImageOps.mirror(image)
                copy = target / f"{n:02d}__{stem}.png"
                image.save(copy)
            else:
                copy = target / f"{n:02d}__{stem}{source.suffix}"
                shutil.copy2(source, copy)
            corrections.setdefault(out, {})[f"{target.name}/{copy.name}"] = pose
            corrections.setdefault(target, {})[copy.name] = pose
            prepared = stage.prepare(pose, image)
            canvas = Image.new("RGBA", stage.size, (0, 0, 0, 0))
            canvas.alpha_composite(prepared["figure"], (prepared["left"], prepared["top"]))
            points = display_joints(prepared)
            canvases.append(canvas)
            joints.append(points)
            cell = Image.new("RGBA", stage.size, (40, 44, 50, 255))
            cell.alpha_composite(canvas)
            draw = ImageDraw.Draw(cell)
            for key, parent in PARENT.items():
                if key in points and parent in points:
                    draw.line([points[parent], points[key]], fill=color(key), width=4)
            for key, (x, y) in points.items():
                draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color(key), outline=(22, 24, 27))
            cells.append(cell.crop((0, top, stage.size[0], bottom)))
        title = f"{phrase['name']} — {len(cells)} поз{', по кругу' if phrase['loop'] else ''}"
        sheet(title, cells, [e["mirror"] for e in phrase["poses"]]).save(out / REVIEW / f"{target.name}.png")
        if args.gif:
            background = Image.new("RGBA", stage.size, (36, 39, 44, 255))

            def render(layer):
                sprite = desktop.Sprite(layer, 0, 0)
                frame = background.copy()
                frame.alpha_composite(stage.frame([(sprite, stage.cast_shadow(sprite), 1)]))
                frame = frame.convert("RGB").crop((0, top, stage.size[0], stage.size[1]))
                return frame.resize((frame.width * 3 // 4, frame.height * 3 // 4), Image.LANCZOS)
            frames = dance_motion.dance_frames(canvases, joints, phrase["loop"], render, fps=args.fps, style=args.style)
            palette = frames[0].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
            gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
            # GIF delays are whole hundredths; browsers slow anything under 20 ms down.
            gif[0].save(out / REVIEW / f"{target.name} — 120 BPM.gif", save_all=True, append_images=gif[1:],
                        duration=max(20, round(1000 / args.fps)), loop=0)
        manifest["phrases"].append({"number": number, "folder": target.name, "poses": stems, "loop": phrase["loop"]})
        poses.write_json(out / MANIFEST, manifest)
        known.add(frozenset(stems))
        made += 1
        print(f"{target.name}: {len(cells)} poses", flush=True)
    store_corrections(corrections)
    print(f"done: {made} new phrase folders in {out}")


if __name__ == "__main__":
    main()
