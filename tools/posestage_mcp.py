"""PoseStage MCP adapter: figure markup tools for an AI agent (docs/PARTS.md § «ИИ-агент»).

A Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0). Each tool is one
figure command or read; they reach

* the running PoseStage through its agent bridge (address and token in
  %APPDATA%/PoseStage/agent_bridge.json) — the agent edits the projects open in the window, and
  the user sees every change and can undo it; or
* headless, when PoseStage is not running: the projects given with --project <folder> (or env
  POSESTAGE_PROJECTS, folders separated by ";"), opened in this process. Needs Pillow.

Example client configuration (Claude Desktop / Claude Code .mcp.json):
    {"mcpServers": {"posestage": {"command": "python",
        "args": ["C:/…/PoseStage/tools/posestage_mcp.py"]}}}

The adapter only forwards the fixed tool list below: no file paths to open, no code, no shell.
"""
from __future__ import annotations

import base64
import codecs
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "PoseStage"
PROTOCOL = "2025-06-18"
VERSION = "1.0.0"

_P = {"type": "string", "description": "Проект (list_projects). Можно не указывать, если открыт один."}
_I = {"type": "string", "description": "Фигура внутри проекта (по умолчанию первая, обычно fig1)."}
_REV = {"type": "integer", "description": "Ревизия документа, на которой основано изменение; при расхождении команда "
                                         "отклоняется с code=conflict (перечитайте документ)."}
_PART = {"type": "string", "description": "Id части: head, neck, chest, abdomen, pelvis, left_upper_arm, left_forearm, "
                                          "left_hand.palm, left_hand.thumb … left_hand.pinky, left_hand.unsplit, left_thigh, "
                                          "left_shin, left_foot и то же для right_. Стороны — собственные стороны фигуры."}
_XY = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_TIME = {"type": ["number", "null"], "description": "Время на таймлайне, с; без него — статическая поза."}
_OVERRIDE = {"type": "boolean", "description": "Изменить зафиксированное пользователем. Только по явной просьбе пользователя."}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS = [
    ("list_projects", "Проекты фигур, открытые в PoseStage.", _obj({})),
    ("get_capabilities", "Таксономия частей и суставов, команды, режимы отображения, система координат.",
     _obj({"project_id": _P})),
    ("get_document_summary", "Сводка проекта: ревизия, размер картинки, части (состояние, пиксели, фиксация), анализ, "
                             "есть ли предложение.", _obj({"project_id": _P})),
    ("get_parts", "Все части фигуры: состояние (visible/uncertain/hidden/absent), источник, фиксация, проверка, "
                  "площадь и рамка в пикселях, диагностика.", _obj({"project_id": _P, "instance_id": _I})),
    ("get_part", "Одна часть подробно.", _obj({"project_id": _P, "part_id": _PART, "instance_id": _I}, ["part_id"])),
    ("get_joints", "Суставы тела и 21 точка каждой кисти: xy в пикселях исходника, состояние, источник; кости и цепочки "
                   "пальцев.", _obj({"project_id": _P, "instance_id": _I})),
    ("get_mask", "Маска части как картинка (белое = часть) и её рамка.",
     _obj({"project_id": _P, "part_id": _PART, "instance_id": _I}, ["part_id"])),
    ("get_crop", "Посмотреть фрагмент: картинка с выбранными слоями (original, parts, outline, skeleton, uncertain). "
                 "transform переводит пиксели превью в пиксели исходника: source = preview / scale + offset.",
     _obj({"project_id": _P, "instance_id": _I,
           "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                      "description": "[x1, y1, x2, y2] в пикселях исходника; без неё — вся картинка"},
           "overlays": {"type": "array", "items": {"enum": ["original", "parts", "outline", "skeleton", "uncertain"]}},
           "max_side": {"type": "integer", "minimum": 64, "maximum": 2048}})),
    ("validate", "Диагностика разметки: пиксели без части, пустые видимые части, суставы вне фигуры, перепутанные "
                 "стороны, сомнительные кисти. Это подсказки, не доказательство точности.",
     _obj({"project_id": _P, "instance_id": _I})),
    ("get_journal", "Последние изменения документа (кто, когда, что).",
     _obj({"project_id": _P, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})),
    ("edit_mask", "Нарисовать или стереть часть круглой кистью: strokes = [{points: [[x, y], …], radius, mode: "
                  "add | erase (в «неизвестно») | background (не фигура)}]. Одна команда = один шаг отмены.",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "expected_revision": _REV, "override_lock": _OVERRIDE,
           "figure_only": {"type": "boolean", "description": "Не выходить за силуэт (по умолчанию да)"},
           "strokes": {"type": "array", "items": _obj({"points": {"type": "array", "items": _XY}, "radius": {"type": "number"},
                                                       "mode": {"enum": ["add", "erase", "background"]}}, ["points"])}},
          ["part_id", "strokes"])),
    ("refine_mask", "Залить области рисунка, ограниченные его контурами: positive_points добавляют область к части, "
                    "negative_points убирают. На фото контуров нет — используйте edit_mask.",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "expected_revision": _REV, "override_lock": _OVERRIDE,
           "positive_points": {"type": "array", "items": _XY}, "negative_points": {"type": "array", "items": _XY}},
          ["part_id", "positive_points"])),
    ("split_part", "Разрезать часть линией: пиксели слева от направленной линии [[x1,y1],[x2,y2]] (y вниз) уходят в target.",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "target": _PART, "expected_revision": _REV,
           "override_lock": _OVERRIDE, "line": {"type": "array", "items": _XY, "minItems": 2, "maxItems": 2}},
          ["part_id", "target", "line"])),
    ("merge_parts", "Слить части в target.", _obj({"project_id": _P, "instance_id": _I, "target": _PART, "expected_revision": _REV,
                                                   "override_lock": _OVERRIDE,
                                                   "part_ids": {"type": "array", "items": _PART}}, ["part_ids", "target"])),
    ("label", "Переименовать часть: все её пиксели становятся target (например, перепутаны левая и правая).",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "target": _PART, "expected_revision": _REV,
           "override_lock": _OVERRIDE}, ["part_id", "target"])),
    ("set_joint", "Поставить сустав или точку кисти (например right_hand.index.tip) в [x, y] пикселей исходника.",
     _obj({"project_id": _P, "instance_id": _I, "joint_id": {"type": "string"}, "xy": _XY,
           "state": {"enum": ["visible", "uncertain", "hidden", "absent"]}, "expected_revision": _REV,
           "override_lock": _OVERRIDE}, ["joint_id", "xy"])),
    ("set_visibility", "Состояние части или сустава: visible, uncertain, hidden (есть, но закрыто), absent (нет на картинке).",
     _obj({"project_id": _P, "instance_id": _I, "entity_id": {"type": "string"}, "state": {"enum": ["visible", "uncertain", "hidden", "absent"]},
           "expected_revision": _REV, "override_lock": _OVERRIDE}, ["entity_id", "state"])),
    ("lock", "Зафиксировать (locked=true) или снять фиксацию. Снять фиксацию пользователя — только с override_lock по просьбе пользователя.",
     _obj({"project_id": _P, "instance_id": _I, "entity_id": {"type": "string"}, "locked": {"type": "boolean"},
           "expected_revision": _REV, "override_lock": _OVERRIDE}, ["entity_id"])),
    ("set_review", "Отметка проверки: unreviewed, accepted, rejected, edited.",
     _obj({"project_id": _P, "instance_id": _I, "entity_id": {"type": "string"},
           "review": {"enum": ["unreviewed", "accepted", "rejected", "edited"]}, "expected_revision": _REV}, ["entity_id", "review"])),
    ("set_roi", "Выбрать фигуру на картинке с несколькими людьми: рамка [x1, y1, x2, y2] для анализа.",
     _obj({"project_id": _P, "instance_id": _I, "roi": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
           "expected_revision": _REV}, ["roi"])),
    ("analyze", "Запустить автоматический анализ в фоне (mode full: суставы и маски; masks: только маски по текущим "
                "суставам). Возвращает job_id; ход — analysis_status. Если фигуру уже правили, результат станет "
                "предложением (apply_proposal / discard_proposal); зафиксированное не меняется.",
     _obj({"project_id": _P, "instance_id": _I, "mode": {"enum": ["full", "masks"]}})),
    ("analysis_status", "Ход и итог анализа.", _obj({"job_id": {"type": "string"}}, ["job_id"])),
    ("cancel_analysis", "Отменить анализ.", _obj({"job_id": {"type": "string"}}, ["job_id"])),
    ("apply_proposal", "Принять предложение анализа целиком или только для перечисленных частей и суставов (only).",
     _obj({"project_id": _P, "instance_id": _I, "only": {"type": "array", "items": {"type": "string"}}, "expected_revision": _REV})),
    ("discard_proposal", "Отклонить предложение анализа.", _obj({"project_id": _P, "instance_id": _I, "expected_revision": _REV})),
    ("transaction", "Несколько команд одним шагом отмены, всё или ничего: commands = [{command, args}] (команды "
                    "figure_doc: edit_mask, refine_mask, relabel, merge_parts, split_part, set_joint, set_visibility, "
                    "set_locked, set_review, set_roi, apply_proposal, discard_proposal).",
     _obj({"project_id": _P, "expected_revision": _REV,
           "commands": {"type": "array", "items": _obj({"command": {"type": "string"}, "args": {"type": "object"}}, ["command"])}},
          ["commands"])),
    ("undo", "Отменить последнее изменение (любого источника).", _obj({"project_id": _P})),
    ("redo", "Повторить отменённое.", _obj({"project_id": _P})),
    # --- Posing (docs/RIG.md) ---
    ("get_rig", "Риг фигуры: кости (покой: начало, конец, длина, родитель), цепочки IK, какую кость вращает "
                "сустав, привязка частей к костям, порядок слоёв, ограничения, опоры, поза, дорисовки, сетка.",
     _obj({"project_id": _P, "instance_id": _I})),
    ("get_pose", "Решённая поза: повороты костей после IK и опор, положения суставов в позе, промахи целей.",
     _obj({"project_id": _P, "instance_id": _I, "time": _TIME})),
    ("render_preview", "Посмотреть позу: картинка (pose — фигура в позе, skeleton, rest — исходник бледно, contacts, "
                       "holes — открывшиеся места без рисунка). transform: source = preview / scale + offset.",
     _obj({"project_id": _P, "instance_id": _I, "time": _TIME,
           "overlays": {"type": "array", "items": {"enum": ["pose", "skeleton", "rest", "contacts", "holes"]}},
           "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
           "max_side": {"type": "integer", "minimum": 64, "maximum": 2048}})),
    ("validate_rig", "Проверка рига и позы: непостроенные кости, пальцы без точек, недостигнутые цели IK, "
                     "соскользнувшие опоры, ограниченные повороты, открывшиеся пустоты.",
     _obj({"project_id": _P, "instance_id": _I, "time": _TIME})),
    ("set_joint_rotation", "Повернуть кость: joint_id — сустав, в котором она вращается (elbow_l поворачивает левое "
                           "предплечье, left_hand.index.pip — среднюю фалангу), или bone_id. angle — градусы от "
                           "направления покоя, по часовой на картинке (y вниз); relative=true — прибавить. "
                           "С time — ключ на таймлайне.",
     _obj({"project_id": _P, "instance_id": _I, "joint_id": {"type": "string"}, "bone_id": {"type": "string"},
           "angle": {"type": "number"}, "relative": {"type": "boolean"}, "time": _TIME, "expected_revision": _REV,
           "override_lock": _OVERRIDE}, ["angle"])),
    ("set_ik_target", "Дотянуться цепочкой (left_arm, right_leg, left_hand.index, head…) до точки [x, y] пикселей "
                      "исходника; position=null снимает цель. Ответ — промах в px.",
     _obj({"project_id": _P, "instance_id": _I, "chain_id": {"type": "string"},
           "position": {"type": ["array", "null"], "items": {"type": "number"}}, "time": _TIME,
           "expected_revision": _REV, "override_lock": _OVERRIDE}, ["chain_id", "position"])),
    ("set_root", "Сдвинуть всю фигуру на [dx, dy] px (корень скелета). Опоры удерживают свои суставы.",
     _obj({"project_id": _P, "instance_id": _I, "offset": _XY, "time": _TIME, "expected_revision": _REV,
           "override_lock": _OVERRIDE}, ["offset"])),
    ("reset_pose", "Вернуть позу покоя (или только перечисленные кости и цепочки).",
     _obj({"project_id": _P, "instance_id": _I, "bones": {"type": "array", "items": {"type": "string"}},
           "expected_revision": _REV, "override_lock": _OVERRIDE})),
    ("set_contact", "Опора: держать сустав (стопа, пятка, носок, запястье, колено, локоть, кончик пальца) в точке, "
                    "пока движется остальное. options {point, enabled, remove}; interval [t0, t1] — на таймлайне.",
     _obj({"project_id": _P, "instance_id": _I, "entity_id": {"type": "string"},
           "interval": {"type": ["array", "null"], "items": {"type": "number"}}, "options": {"type": "object"},
           "expected_revision": _REV}, ["entity_id"])),
    ("set_constraint", "Ограничить поворот кости: min / max градусов от покоя (null — свободно); bend 1 / -1 — "
                       "в какую сторону сгибается двухзвенная цепочка (по её первой кости).",
     _obj({"project_id": _P, "instance_id": _I, "bone_id": {"type": "string"}, "min": {"type": ["number", "null"]},
           "max": {"type": ["number", "null"]}, "bend": {"enum": [1, -1, None]}, "expected_revision": _REV}, ["bone_id"])),
    ("set_layer_order", "Порядок слоёв (сзади → вперёд): перечисленные части в этом порядке на своих местах, "
                        "position front / back — наверх / вниз.",
     _obj({"project_id": _P, "instance_id": _I, "part_ids": {"type": "array", "items": _PART},
           "position": {"enum": ["front", "back", None]}, "expected_revision": _REV}, ["part_ids"])),
    ("set_binding", "Какие кости двигают часть (bone_ids); options {reset: true} — по умолчанию.",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "bone_ids": {"type": "array", "items": {"type": "string"}},
           "options": {"type": "object"}, "expected_revision": _REV}, ["part_id"])),
    ("edit_weights", "Кисть весов кости: strokes [{points, radius}], operation add / subtract (на amount 0…1), "
                     "smooth, reset. Веса нормируются по всем костям.",
     _obj({"project_id": _P, "instance_id": _I, "bone_id": {"type": "string"},
           "strokes": {"type": "array", "items": _obj({"points": {"type": "array", "items": _XY}, "radius": {"type": "number"}}, ["points"])},
           "operation": {"enum": ["add", "subtract", "smooth", "reset"]}, "amount": {"type": "number", "minimum": 0, "maximum": 1},
           "expected_revision": _REV}, ["bone_id", "strokes"])),
    ("set_rig_settings", "Ячейка сетки (px) и ширина смешивания весов у суставов (px).",
     _obj({"project_id": _P, "instance_id": _I, "cell": {"type": "integer", "minimum": 3, "maximum": 40},
           "falloff": {"type": "number"}, "expected_revision": _REV})),
    ("add_surface", "Новый слой-дорисовка, привязанный к части (двигается с её костью); рисуется под частью или над "
                    "ней (above). Это не наблюдаемый рисунок: помечается как дорисованный.",
     _obj({"project_id": _P, "instance_id": _I, "part_id": _PART, "name": {"type": "string"}, "above": {"type": "boolean"},
           "expected_revision": _REV}, ["part_id"])),
    ("paint_surface", "Рисовать (color [r, g, b] или [r, g, b, a]) или стирать (mode erase) круглой кистью на дорисовке.",
     _obj({"project_id": _P, "instance_id": _I, "surface_id": {"type": "string"},
           "strokes": {"type": "array", "items": _obj({"points": {"type": "array", "items": _XY}, "radius": {"type": "number"}}, ["points"])},
           "color": {"type": "array", "items": {"type": "integer"}}, "mode": {"enum": ["paint", "erase"]},
           "expected_revision": _REV}, ["surface_id", "strokes"])),
    ("set_surface", "Свойства дорисовки: enabled, above, part_id, bone_id, name; remove=true — удалить.",
     _obj({"project_id": _P, "instance_id": _I, "surface_id": {"type": "string"}, "enabled": {"type": "boolean"},
           "above": {"type": "boolean"}, "part_id": _PART, "bone_id": {"type": "string"}, "name": {"type": "string"},
           "remove": {"type": "boolean"}, "expected_revision": _REV}, ["surface_id"])),
    # --- Animation (timeline.py) ---
    ("get_animation", "Анимация: длительность, fps, цикл, дорожки ключей (rot:<кость> — градусы, root — [dx, dy], "
                      "ik:<цепочка> — [x, y] или null).", _obj({"project_id": _P, "instance_id": _I})),
    ("set_keyframe", "Ключ на дорожке в момент time (с): rot:<кость> | root | ik:<цепочка>; interpolation ease | linear | step. "
                     "Повороты и цели можно ставить и через set_joint_rotation / set_ik_target с time.",
     _obj({"project_id": _P, "instance_id": _I, "track_id": {"type": "string"}, "time": {"type": "number"},
           "value": {}, "interpolation": {"enum": ["ease", "linear", "step"]}, "expected_revision": _REV,
           "override_lock": _OVERRIDE}, ["track_id", "time", "value"])),
    ("delete_keyframe", "Удалить ключ (time) или всю дорожку (без time).",
     _obj({"project_id": _P, "instance_id": _I, "track_id": {"type": "string"}, "time": {"type": "number"},
           "expected_revision": _REV}, ["track_id"])),
    ("key_pose", "Запомнить текущую позу ключами в момент time (все повёрнутые кости, корень, цели).",
     _obj({"project_id": _P, "instance_id": _I, "time": {"type": "number"},
           "bones": {"type": "array", "items": {"type": "string"}}, "expected_revision": _REV}, ["time"])),
    ("set_animation", "Длительность (с), fps (1…60), loop.",
     _obj({"project_id": _P, "instance_id": _I, "duration": {"type": "number"}, "fps": {"type": "integer"},
           "loop": {"type": "boolean"}, "expected_revision": _REV})),
    ("check_animation", "Проверить кадры: где цели не достигнуты, опоры скользят, повороты упёрлись в ограничения. "
                        "Смотреть такие кадры — render_preview с time.",
     _obj({"project_id": _P, "instance_id": _I, "start": {"type": "number"}, "end": {"type": "number"},
           "fps": {"type": "integer"}})),
    ("export_frames", "Фоновый экспорт кадров PNG в папку проекта exports/ (тот же расчёт, что превью). options "
                      "{start, end, fps, max_side, background [r, g, b, a], overlays}. Ход — job_status.",
     _obj({"project_id": _P, "instance_id": _I, "options": {"type": "object"}})),
    ("job_status", "Ход и итог фоновой задачи (анализ или экспорт кадров).", _obj({"job_id": {"type": "string"}}, ["job_id"])),
    ("cancel_job", "Отменить фоновую задачу.", _obj({"job_id": {"type": "string"}}, ["job_id"])),
    ("set_pose_locked", "Зафиксировать позу от изменений агентом (снять фиксацию может только пользователь).",
     _obj({"project_id": _P, "instance_id": _I, "locked": {"type": "boolean"}}, ["locked"])),
    ("export", "Экспорт в папку проекта exports/: исходник, карта меток, цветное превью, маска каждой части, markup.json.",
     _obj({"project_id": _P, "instance_id": _I})),
]
TOOL_NAMES = {name for name, _, _ in TOOLS}
INSTRUCTIONS = ("PoseStage: разметка фигуры на части тела, кисти и пальцы. Координаты — пиксели исходной картинки, "
                "начало слева сверху, y вниз. Сначала get_document_summary и get_crop (посмотреть), потом правки. "
                "Передавайте expected_revision. Позирование: get_rig → set_joint_rotation / set_ik_target / set_contact → "
                "render_preview → validate_rig. Не снимайте фиксацию пользователя без его просьбы. Состояние uncertain "
                "и диагностику validate сообщайте пользователю честно.")


class Bridge:
    """Calls the running PoseStage, or the headless service when it is not running."""

    def __init__(self, projects):
        self.headless_projects = projects
        self.local = None

    def _address(self):
        try:
            info = json.loads((DATA_DIR / "agent_bridge.json").read_text(encoding="utf-8"))
            return info["url"], info["token"]
        except (OSError, ValueError, KeyError):
            return None

    def call(self, tool, args):
        address = self._address()
        if address and self.local is None:
            url, token = address
            request = urllib.request.Request(url, json.dumps({"tool": tool, "args": args}).encode("utf-8"),
                                             {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return json.loads(error.read().decode("utf-8") or "{}") or {"ok": False, "error": str(error)}
            except (urllib.error.URLError, OSError):
                pass  # a stale address file: PoseStage has exited
        return self._headless(tool, args)

    def _headless(self, tool, args):
        if not self.headless_projects:
            return {"ok": False, "code": "unavailable",
                    "error": "PoseStage не запущен. Откройте проект в PoseStage (раздел «Разметка») или запустите адаптер "
                             "с --project <папка проекта>."}
        if self.local is None:
            sys.path.insert(0, str(ROOT))
            import parts_service
            self.local = parts_service.PartsService()
            for folder in self.headless_projects:
                self.local.open_folder(folder)
        import agent_bridge
        import figure_doc
        try:
            return {"ok": True, "result": agent_bridge.call_tool(self.local, tool, args)}
        except figure_doc.CommandError as error:
            return {"ok": False, "error": str(error), "code": error.code}
        except (ValueError, TypeError) as error:
            return {"ok": False, "error": str(error), "code": "invalid"}


def _image_content(path):
    """A preview PNG written by get_crop / get_mask — only from a project's previews/ folder."""
    path = Path(path)
    if path.suffix.lower() != ".png" or path.parent.name != "previews" or not (path.parent.parent / "figure.json").is_file():
        return None
    data = path.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        return None
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}


def tool_result(bridge, name, args):
    if name not in TOOL_NAMES:
        text = json.dumps({"error": f"Неизвестный инструмент {name}", "code": "unknown_command"}, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": True}
    reply = bridge.call(name, args or {})
    if not reply.get("ok"):
        text = json.dumps({"error": reply.get("error"), "code": reply.get("code")}, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": True}
    result = reply["result"]
    content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
    if name in ("get_crop", "get_mask", "render_preview") and isinstance(result, dict) and result.get("file"):
        image = _image_content(result["file"])
        if image:
            content.insert(0, image)
    return {"content": content, "structuredContent": result if isinstance(result, dict) else {"result": result}, "isError": False}


def handle(bridge, message):
    method, mid = message.get("method"), message.get("id")
    if mid is None:  # a notification (initialized, cancelled): nothing to answer
        return None
    try:
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            result = {"protocolVersion": requested if isinstance(requested, str) else PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "posestage", "version": VERSION}, "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = tool_result(bridge, params.get("name"), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as error:  # noqa: BLE001 — a tool failure must not end the session
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(error)}}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    projects = [argv[i + 1] for i, a in enumerate(argv) if a == "--project" and i + 1 < len(argv)]
    projects += [p for p in os.environ.get("POSESTAGE_PROJECTS", "").split(";") if p.strip()]
    bridge = Bridge(projects)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip().removeprefix(codecs.BOM_UTF8)  # a BOM some shells put before the first line
        if not raw:
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(bridge, message) if isinstance(message, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
