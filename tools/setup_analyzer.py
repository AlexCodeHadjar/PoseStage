"""Installs the body / hand analysis runtime for PoseStage (docs/PARTS.md, «Установка анализа»).

PoseStage.exe does not contain the analysis (MediaPipe, numpy, scipy: ~400 MB). This script makes
a separate Python environment for it and tells PoseStage where it is
(%APPDATA%/PoseStage/analyzer.json). It downloads only when you run it, and says what:

    python tools/setup_analyzer.py
        a new environment in %APPDATA%/PoseStage/analyzer/env (needs Python 3.9–3.12 64-bit and
        the internet: the packages of tools/analyzer-requirements.txt from PyPI and the pose model
        pose_landmark_heavy.tflite, 27.7 MB, from storage.googleapis.com/mediapipe-assets, checked
        by its sha256);
    python tools/setup_analyzer.py --model <pose_landmark_heavy.tflite>
        the same, the model taken from a file you already have (checked by sha256);
    python tools/setup_analyzer.py --use <python.exe>
        an existing environment: only checked (packages, model) and registered;
    python tools/setup_analyzer.py --check
        shows what PoseStage will use.

Without the model file the analyzer refuses to run instead of letting MediaPipe fetch it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "PoseStage"
MODEL = "pose_landmark_heavy.tflite"
MODEL_URL = "https://storage.googleapis.com/mediapipe-assets/pose_landmark_heavy.tflite"
MODEL_SHA256 = "59e42d71bcd44cbdbabc419f0ff76686595fd265419566bd4009ef703ea8e1fe"
PROBE = ("import json, mediapipe, numpy, scipy, PIL, os; from pathlib import Path; "
         "m = Path(mediapipe.__file__).parent / 'modules' / 'pose_landmark' / 'pose_landmark_heavy.tflite'; "
         "print(json.dumps({'mediapipe': mediapipe.__version__, 'numpy': numpy.__version__, 'scipy': scipy.__version__, "
         "'pillow': PIL.__version__, 'model': str(m), 'model_present': m.is_file()}))")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def probe(python):
    run = subprocess.run([str(python), "-I", "-c", PROBE], capture_output=True, text=True)
    if run.returncode:
        raise SystemExit(f"Окружение анализа не работает ({python}):\n{run.stderr[-1500:]}")
    return json.loads(run.stdout.strip().splitlines()[-1])


def install_model(info, source: Path | None):
    target = Path(info["model"])
    if target.is_file() and sha256(target) == MODEL_SHA256:
        return
    tmp = target.with_suffix(".download")
    if source:
        shutil.copyfile(source, tmp)
    else:
        print(f"Скачиваю модель позы {MODEL} (27,7 МБ) с {MODEL_URL}")
        with urllib.request.urlopen(MODEL_URL, timeout=120) as response, open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)
    if sha256(tmp) != MODEL_SHA256:
        tmp.unlink()
        raise SystemExit("Контрольная сумма модели не совпала: файл не установлен")
    os.replace(tmp, target)


def register(python, info):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = {"python": str(Path(python).resolve()), "versions": {k: info[k] for k in ("mediapipe", "numpy", "scipy", "pillow")}}
    (DATA_DIR / "analyzer.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Готово: PoseStage будет запускать анализ через {config['python']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--use", type=Path, help="python.exe существующего окружения")
    parser.add_argument("--model", type=Path, help="уже скачанный pose_landmark_heavy.tflite")
    parser.add_argument("--target", type=Path, default=DATA_DIR / "analyzer" / "env")
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    if options.check:
        sys.path.insert(0, str(ROOT))
        import parts_service
        found = parts_service.find_analyzer()
        print(json.dumps(found, ensure_ascii=False, indent=1))
        if found["available"] and found["command"][0].lower().endswith("python.exe"):
            print(json.dumps(probe(found["command"][0]), ensure_ascii=False, indent=1))
        return
    if options.use:
        info = probe(options.use)
        if not info["model_present"] or sha256(info["model"]) != MODEL_SHA256:
            install_model(info, options.model)
        register(options.use, info)
        return
    if not ((3, 9) <= sys.version_info[:2] <= (3, 12)) or sys.maxsize < 2 ** 32:
        raise SystemExit("Нужен 64-битный Python 3.9–3.12 (MediaPipe 0.10.14 других не поддерживает)")
    python = options.target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print(f"Создаю окружение {options.target}")
        subprocess.run([sys.executable, "-m", "venv", str(options.target)], check=True)
    print("Устанавливаю пакеты из tools/analyzer-requirements.txt (PyPI)")
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r",
                    str(ROOT / "tools" / "analyzer-requirements.txt")], check=True)
    info = probe(python)
    install_model(info, options.model)
    register(python, probe(python))


if __name__ == "__main__":
    main()
