"""Runs the body / hand analysis over the fixed benchmark (tools/parts_bench.json) and writes what
docs/PARTS.md reports: per image time, peak memory, what was found, hand checks, and a contact
sheet (picture + parts + skeleton) to look at by eye. There is no hand-made ground truth yet, so
nothing here is an accuracy number.

    <analysis python> tools/parts_bench.py <collection folder> <out folder> [--split tune|validate]
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import figure_doc as fd  # noqa: E402


def derived(entry, collection: Path, folder: Path) -> Path:
    path = collection / entry["file"]
    if not entry.get("derive"):
        return path
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    box = image.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox() or (0, 0, *image.size)
    if entry["derive"] == "crop":
        image = image.crop((0, 0, image.width, box[1] + int(.55 * (box[3] - box[1]))))
    elif entry["derive"] == "small":
        k = 300 / (box[3] - box[1])
        image = image.resize((max(1, round(image.width * k)), max(1, round(image.height * k))), Image.LANCZOS)
    out = folder / f"{entry['id']}_{entry['derive']}.png"
    image.save(out)
    return out


def sheet_cell(project, size=(300, 420)):
    image, _ = fd.render(project, ("original", "parts", "skeleton"), max_side=900)
    image = image.convert("RGB")
    image.thumbnail(size)
    cell = Image.new("RGB", size, "white")
    cell.paste(image, ((size[0] - image.width) // 2, 0))
    return cell


def main():
    collection, out = Path(sys.argv[1]), Path(sys.argv[2])
    split = sys.argv[sys.argv.index("--split") + 1] if "--split" in sys.argv else None
    out.mkdir(parents=True, exist_ok=True)
    bench = json.loads((ROOT / "tools" / "parts_bench.json").read_text(encoding="utf-8"))["images"]
    rows, cells = [], []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for entry in bench:
            if split and entry["split"] != split:
                continue
            image = derived(entry, collection, tmp)
            project = fd.Project.create(image, tmp / f"p_{entry['id']}")
            inst = project.instance()
            request = tmp / f"{entry['id']}.json"
            request.write_text(json.dumps({"image": str(image), "roi": inst["roi"], "mode": "full"}), encoding="utf-8")
            result_dir = tmp / f"r_{entry['id']}"
            t0 = time.perf_counter()
            run = subprocess.run([sys.executable, str(ROOT / "parts_analyzer.py"), str(request), str(result_dir)],
                                 capture_output=True, text=True)
            wall = time.perf_counter() - t0
            if run.returncode:
                rows.append({"id": entry["id"], "category": entry["category"], "split": entry["split"], "error": run.stderr[-800:]})
                print(entry["id"], "FAILED", run.stderr[-800:], flush=True)
                continue
            result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
            project.execute("store_analysis", {"result": {"labels": Image.open(result_dir / "labels.png").convert("L"),
                                                          "uncertain": Image.open(result_dir / "uncertain.png").convert("L"),
                                                          **result}}, source="model")
            info = result["analysis"]
            parts = project.execute("get_parts")["parts"]
            figure_px = sum(p["px"] for p in parts.values()) + project.execute("get_parts")["unknown_px"]
            hands = {}
            for side in ("left", "right"):
                fingers = [f for f in ("thumb", "index", "middle", "ring", "pinky") if parts[f"{side}_hand.{f}"]["px"]]
                hands[side] = {"landmarks": side in (info.get("hands") or {}),
                               "issues": (info.get("hands") or {}).get(side, {}).get("issues"),
                               "split_fingers": len(fingers), "unsplit_px": parts[f"{side}_hand.unsplit"]["px"]}
            row = {"id": entry["id"], "category": entry["category"], "split": entry["split"], "size": project.doc["source"]["width"],
                   "seconds": info.get("seconds"), "wall_seconds": round(wall, 2), "peak_mb": info.get("peak_mb"),
                   "body": info.get("error") is None, "view": info.get("view"),
                   "variants_with_pose": info.get("variants_with_pose"), "hands": hands,
                   "unknown_share": round(project.execute("get_parts")["unknown_px"] / max(1, figure_px), 3),
                   "issues": [i["code"] for i in project.execute("validate")["issues"] if i["severity"] == "warning"]}
            rows.append(row)
            full, _ = fd.render(project, ("original", "parts", "outline", "skeleton"), max_side=1100)
            full.convert("RGB").save(out / f"{entry['id']}.png")
            for side in ("left", "right"):  # hands close up, where landmarks were found
                pts = [j["xy"] for jid, j in project.instance()["joints"].items() if jid.startswith(f"{side}_hand.")]
                if pts:
                    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                    pad = max(20, .5 * max(max(xs) - min(xs), max(ys) - min(ys)))
                    crop, _ = fd.render(project, ("original", "parts", "outline", "skeleton"),
                                        region=(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad), max_side=420)
                    crop.convert("RGB").save(out / f"{entry['id']}_{side}_hand.png")
            cell = sheet_cell(project)
            from PIL import ImageDraw
            ImageDraw.Draw(cell).text((4, 404), f"{entry['id']} {entry['category']}", fill=(0, 0, 0))
            cells.append(cell)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    cols = 6
    sheet = Image.new("RGB", (300 * cols, 420 * ((len(cells) + cols - 1) // cols)), "white")
    for i, cell in enumerate(cells):
        sheet.paste(cell, ((i % cols) * 300, (i // cols) * 420))
    sheet.save(out / f"parts_sheet{'_' + split if split else ''}.png")
    (out / f"parts_bench{'_' + split if split else ''}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
