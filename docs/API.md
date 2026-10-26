# PoseStage — внутренние контракты

Документ фиксирует границы между Python-бэкендом, ядром фронтенда (`web/main.js`)
и UI-модулями (`web/ui/*.js`). Меняя контракт, обновите этот файл.

## 1. Python API (`window.pywebview.api`, класс `app.Api`)

Все методы возвращают JSON-совместимые значения. Публичные атрибуты `Api` — только методы.
Некорректный ввод (`set_support_mode`, `toggle_favorite`, `set_tags`, `switch_collection`)
вызывает `ValueError` — промис в JS отклоняется; UI-модули ловят его и показывают тост.

### Позы

`get_poses() -> Pose[]` — каталог активной коллекции (ждёт окончания сканирования).

```ts
type Pose = {
  file: string;     // путь относительно папки коллекции, разделитель "/", например "sitting/a.png"
  url: string;      // `${prefix}/poses/${encodeURIComponent-по-сегментам(file)}`
  thumb: string;    // `${prefix}/thumbs/...` — миниатюра (длинная сторона ≤ 320 px)
  w: number; h: number;
  box: [x1, y1, x2, y2];               // bbox альфы > 8 (для JPG — всё изображение)
  support: {version, mode: "feet"|"kneel"|"ground", cut, profile: [x, y][]};
  support_source: "auto"|"bundled"|"user";  // откуда взят mode
  area: number;         // площадь силуэта (альфа > 8), px исходника
  stature: number;      // оценка роста стоя, px исходника: reference·√area коллекции
  scale_adjust: number; // ручная поправка размера 0.5…2 (1 — без поправки)
  // Скелет версии 2 (docs/SKELETON.md): 16 точек head, chest, navel, pelvis и
  // {shoulder, elbow, wrist, hip, knee, foot}_{l, r} — стороны самой фигуры.
  joints: {version: 2, view: "front"|"three_quarter"|"side"|"back",
           points: {[id]: {xy?: [x, y], state: "visible"|"hidden"|"absent", support?: bool}}} | null;
  joints_source: "user"|"bundled"|null;  // скелет для оживления (px исходника)
}
```

Поддерживаемые файлы: `.png .jpg .jpeg .webp` (регистр не важен), рекурсивно во вложенных
папках, кроме скрытых и служебных (`.`/`_` в начале имени папки). Сортировка — естественная
(`pose-2` < `pose-10`) по относительному пути.

### Состояние

- `get_state() -> {index: int, file: string|null}` — индекс восстанавливается **по имени файла**;
  если файла больше нет — по сохранённому индексу, ограниченному диапазоном, иначе 0.
- `save_state(index: int, file?: string)` — сохраняет `file` (и индекс как запасной вариант).
  Переданный `file` важнее индекса; файл не из текущего каталога игнорируется.

### Опора позы

- `set_support_mode(file: string, mode: "feet"|"kneel"|"ground"|"auto") -> Pose`
  Пользовательское переопределение хранится в `%APPDATA%\PoseStage\support_overrides.json`
  (по коллекции). `"auto"` удаляет переопределение. Возвращает пересчитанную позу.
  Рост (`stature`) от режима не зависит: `reference` коллекции фиксируется при сканировании.

### Размер позы

Площадь силуэта одного тела почти не зависит от позы, поэтому `stature = reference·√area`,
где `reference` — медиана «высота рамки / √площади» по стоячим прозрачным позам коллекции.
Если стоячих поз нет, берётся 2,6 (значение для манекенов этого набора). У непрозрачных картинок
(фото без прозрачности) силуэта нет — их рост равен высоте рамки.
Фронтенд даёт всем позам один масштаб тела: `stature·scale = 0.61·высота окна·scale_adjust`
(`placement.js: bodyScale`), и уменьшает позу только если иначе опоры не помещаются на крышку
или фигура выходит за края экрана (`placement.limited`).

- `set_joints(file: string, joints | null) -> Pose` — исправленный скелет позы (оживление); `null` —
  вернуть найденный автоматически (`web/scene/pose_joints.json`). Принимает версии 1 и 2, хранит
  версию 2 в `joint_overrides.json` по коллекции. Ось head → chest → navel → pelvis обязательна;
  неверный формат — `ValueError`.
- `set_pose_scale(file: string, factor: number) -> Pose` — ручная поправка 0.5…2, `1` удаляет её.
  Хранится в `scale_overrides.json` по коллекции. Иное значение — `ValueError`.

### Танцы (docs/DANCE.md)

- `get_dances() -> {status: "ready"|"working"|"error", phrases: Dance[], storyboards: Dance[], error: string|null}`
  — танцы активной коллекции.
  - `storyboards` — готовые танцы по раскадровке (`dance.storyboards`): кадры `dance_<NN>_<ИМЯ>__<кадр>__<секунды>s__pose-<k>.png`
    по порядку, `kind: "storyboard"`, `times`, `bpm` (свой темп видео), названия из `*/manifest.json`
    коллекции. Кадру без найденного скелета даётся средний между соседями (`joints_source: "estimated"`).
    Отдаются сразу.
  - `phrases` — связки, найденные среди остальных поз (кадры танцев в поиск не входят). Поиск идёт в фоне
    (`dance.load_styles` + `dance.find_phrases`; первый раз читает все рисунки, около минуты на 300 поз);
    результат хранится в `dances_cache.json`, пока не изменятся позы, их скелеты или опоры. Пока
    `"working"` — вызвать ещё раз позже.

```ts
type Dance = {
  id: string; name: string;      // «Руки волной · Шаг шире»
  loop: boolean;                 // последняя поза перетекает в первую
  score: number;                 // средняя стоимость шага (меньше — лучше)
  poses: {file: string; mirror: boolean; pose: Pose}[];  // pose отражённой — уже отражённая (box, скелет, опора)
};
```

Свои связки (по коллекции, `%APPDATA%\PoseStage\sequences.json`):

- `get_sequences() -> Sequence[]`, где `Sequence = {id, name, loop, poses: {file, mirror}[]}`.
- `save_sequence(sequence) -> Sequence` — добавляет или заменяет (тот же `id`). Название 1–60 символов,
  от 2 до 16 поз, только файлы текущей коллекции; иначе `ValueError`. Новый `id` выдаёт бэкенд.
- `delete_sequence(id) -> bool`.

### Разметка частей тела (docs/PARTS.md)

Проекты фигур открывает `parts_service.PartsService`; документ и команды — `figure_doc.py`.
Ошибки команд приходят значением `{error, code}` (коды `invalid`, `conflict`, `locked`, `unbounded`,
`no_proposal`, `empty`, `not_found`, `source`, `busy`, `unavailable` …), а не исключением.

- `parts_capabilities()` — таксономия: части `{id: {value, parent, name, color}}`, суставы, команды,
  режимы показа, `not_yet`.
- `parts_status() -> {analyzer: {available, source, reason}, open: Project[], recent: [{id, name, folder}]}`.
- `parts_open_pose(file)` — проект позы библиотеки (создаётся при первом открытии в
  `%APPDATA%\PoseStage\figures\<sha256[:16]>`; её скелет — начальные суставы, `source.kind = "estimated"`).
  `parts_open_image()` / `parts_open_folder()` — диалог; `parts_open_recent(folder)`. Все возвращают
  сводку `get_document_summary` + `job` + `analyzer`.
- `parts_call(project_id, command, args, expected_revision?)` — команда или чтение `figure_doc`
  от пользователя (`store_analysis` и `export` запрещены).
- `parts_analyze(project_id, mode: "full"|"masks") -> Job`, `parts_job(job_id)`, `parts_cancel(job_id)`;
  `Job = {job_id, status: "queued"|"running"|"done"|"error"|"cancelled", progress, stage, error, result}`.
- `parts_export(project_id)` — диалог папки → экспорт; `parts_summary(project_id)`; `parts_close(project_id)`.
- Бэкенд сообщает об изменениях (ход анализа, правки агента) вызовом
  `window.__partsChanged({project_id, revision, command, source} | {project_id, job})`.

### Коллекции (несколько папок)

```ts
type Collection = {id: string; name: string; folder: string; active: boolean};
```

- `get_collections() -> Collection[]`
- `add_collection() -> {collections: Collection[], poses: Pose[]}` — диалог выбора папки;
  при успехе новая коллекция становится активной. Отмена — вернуть текущие данные.
- `switch_collection(id: string) -> {collections, poses}` — недоступная или пустая папка:
  `ValueError("Папка недоступна: …")`, активная коллекция не меняется.
- `remove_collection(id: string) -> {collections, poses}` — последнюю удалить нельзя.
- `choose_folder() -> Pose[]` — совместимость: то же, что `add_collection().poses`.

### Библиотека (избранное и теги, по активной коллекции)

- `get_library() -> {favorites: string[], tags: {[file]: string[]}}`
- `toggle_favorite(file) -> boolean` — новое состояние.
- `set_tags(file, tags: string[]) -> string[]` — нормализованные теги (trim, без дублей,
  до 16 тегов по 32 символа).

### Настройки вида

- `get_view_prefs() -> object`, `save_view_prefs(prefs: object) -> object`
  Хранятся в `view_prefs.json`; бэкенд принимает только известные ключи с проверкой типов:
  `flip: bool, gray: bool, grid: "off"|"thirds"|"proportions", on_top: bool, compact: bool,
  filter: {mode: "all"|"feet"|"kneel"|"ground", favorites: bool, tag: string|null, search: string},
  parallax: "off"|"soft"|"strong", sizing: "body"|"file", idle: bool, background: {mode: "studio"|"image", id: string|null, blur: 0…24, dim: 0…0.85},
  dance: {style: "soft"|"groove", speed: 1|0.5|0.25, bpm: 60…200 (целое)}`.
  `save_view_prefs` сливает частичный объект с сохранённым; `get_view_prefs` при первом
  запуске возвращает `{}` — значения по умолчанию задаёт фронтенд.

### Фон

- `choose_background() -> {id, url, w, h} | null` — диалог (png/jpg/jpeg/webp). Копия (EXIF-поворот,
  длинная сторона ≤ 4096) сохраняется в `%APPDATA%\PoseStage\backgrounds\<sha256[:16]>.png|jpg`
  и выбирается. Оригинал не меняется. Отмена или нет окна — `null`; битый файл — `ValueError`.
- `list_backgrounds() -> [{id, url, w, h}]`, `remove_background(id) -> list` (удалённый выбранный
  фон переключает настройки на студию).
- HTTP: `${prefix}/backgrounds/<id>.<png|jpg>`.

### Окно

- `toggle_fullscreen()`, `quit()`, `report_error(message)` — как раньше.
- `set_on_top(on: bool) -> bool` — окно поверх всех.
- `open_desktop() -> bool` — запускает `PoseStage --desktop` (подиум без окна, `desktop.py`) и закрывает
  окно; без окна — `false`. Настройки режима: `desktop.json` `{size: "small"|"medium"|"large", on_top, x, y}`.
- `set_compact(on: bool) -> bool` — компактный режим: выход из полного экрана, окно ~ 480×720
  у правого края рабочей области, поверх всех. `false` — возврат в полный экран.
- Перетаскивание frameless-окна: элементы с CSS-классом `pywebview-drag-region`.

### WebView2

Сначала системный Runtime; при ошибке инициализации программа один раз перезапускается
с `EXE_DIR\WebView2` (`POSESTAGE_WEBVIEW2=portable`) и записывает
`webview2_system_failed.json`, пока версия системного Runtime не изменится.
CSP: `script-src 'self' 'unsafe-eval' 'sha256-<importmap>'` — `unsafe-eval` нужен мосту
pywebview (`new Function`), хэш importmap считается из `index.html` при каждом ответе.

### HTTP

Сервер обслуживает только `127.0.0.1`, префикс случайный:
`/web/...`, `/poses/<file>` (только из allowlist каталога), `/thumbs/<file>`
(миниатюры, кэш в `%APPDATA%\PoseStage\thumbs`),
`/figures/<project id>/<instance>/(source|parts|uncertain|outline).png` — слои открытого проекта
разметки, создаются в памяти. Папку поз программа не изменяет.

Мост ИИ-агента (`agent_bridge.py`) — отдельный сервер `127.0.0.1:<порт>`: `POST /v1/call {tool, args}`
с `Authorization: Bearer <token>`; порт и токен — в `%APPDATA%\PoseStage\agent_bridge.json`, пока
программа запущена. Запросы с заголовком `Origin` (браузерные страницы) отвергаются.

## 2. Ядро фронтенда → UI-модули

`web/main.js` импортирует `web/ui/artist.js`, `web/ui/navigator.js`, `web/ui/skeleton.js` и `web/ui/dance.js` и вызывает
`install(app)` у каждого после создания сцены. Стили модулей: `web/ui/artist.css`,
`web/ui/navigator.css` (подключены в `index.html`). Модуль создаёт свой DOM сам.

```ts
type App = {
  api: PywebviewApi;                 // доступно после ready
  ready: Promise<void>;              // первая поза показана (или показана ошибка)
  poses(): Pose[];                   // текущий список (копия)
  index(): number;
  current(): Pose | null;
  goTo(index: number): void;         // переход всегда с одной анимацией, даже при далёком прыжке
  step(delta: number): void;         // ±N; быстрые нажатия накапливаются
  reload(poses: Pose[], index?: number): Promise<void>; // новый список (смена коллекции)
  updatePose(index: number, pose: Pose): void;  // заменить данные позы и переразместить
  setOrder(indices: number[] | null): void;     // порядок обхода ←/→ (фильтр); null — все
  on(event: "pose"|"poses"|"transition"|"dance", cb: (detail) => void): () => void;
  // Танец на подиуме: по позе на счёт, переходы по костям (scene/dance.js), обычная поза скрыта.
  // Навигация (←/→, goTo, reload) останавливает танец. "dance": {phase: "start"|"step"|"state"|"end", ...DanceState}.
  dance: {
    play(phrase: Dance, options?: {bpm?: number; style?: "soft"|"groove"; speed?: 1|0.5|0.25}): Promise<DanceState|null>;
    stop(): void;
    state(): DanceState | null;   // {name, count, pose (индекс в связке), paused, speed, style, bpm}
    setPaused(on: boolean): DanceState | null;
    setSpeed(speed: 1|0.5|0.25): DanceState | null;
    setStyle(style: "soft"|"groove"): DanceState | null;
    setBpm(bpm: number): DanceState | null;  // 60…200; место в счёте сохраняется
    speeds(): number[];
  };
  shortcuts: {
    // handler обязателен, кроме строк-описаний жестов мыши: keys: [] и непустой hint.
    register(def: {keys: string[]; title: string; group: string; hint?: string; handler?: (e) => void}): () => void;
    list(): {keys, title, group, hint?}[];
  };
  overlays: {             // модальные панели: Esc сначала закрывает их, потом выходит
    open(id: string, close: () => void): void;
    close(id: string): void;
    any(): boolean;
  };
  view: {                 // отрисовка
    canvas: HTMLCanvasElement;
    setZoom(zoom: number, centerX?: number, centerY?: number): void; // 1…8, центр в CSS px
    zoom(): number;
    pan(dx: number, dy: number): void;  // CSS px
    resetView(): void;
    figureRect(): {left, top, width, height} | null;  // экранный bbox фигуры, CSS px
    onFrame(cb: () => void): () => void;
    // Камера облетает подиум за курсором: off — без параллакса, soft — ±4°, strong — ±9,5° / ±3,5°.
    setParallax(mode: "off"|"soft"|"strong"): string;
    parallax(): string;
    // Размер поз: "file" (по умолчанию) — один масштаб для всех картинок (рисунок как в файле),
    // опора только ставится в центр подиума; "body" — единый рост тела с ограничениями подиума.
    setSizing(mode: "body"|"file"): string;
    sizing(): string;
    // Оживление позы (web/scene/idle.js): дыхание, перенос веса, голова; опоры неподвижны.
    setIdle(on: boolean): boolean;
    idle(): boolean;
    // Фон-изображение на плоскости за подиумом (настоящий параллакс); последний вызов побеждает.
    setBackground(state: {mode: "studio"|"image", url?: string, blur?: number, dim?: number}): Promise<object>;
    background(): object;
  };
};
```

`keys` — значения `KeyboardEvent.code` (`"KeyH"`, `"Digit1"`, `"Slash"` …), с модификаторами
через `+` (`"Shift+Slash"`). Ядро игнорирует горячие клавиши, когда фокус в `input/textarea`.

### Раскладка клавиш

| Клавиши | Действие | Владелец |
|---|---|---|
| ← → / A D | предыдущая / следующая | ядро |
| Home / End | первая / последняя | ядро |
| F / F11 | полный экран | ядро |
| Esc | закрыть панель, иначе выход | ядро |
| ? / F1 | справка по клавишам | ядро |
| H | зеркально | художник |
| V | оттенки серого | художник |
| L | сетка: выкл → трети → пропорции | художник |
| колесо, + / −, Z | масштаб, Z — сброс вида | художник |
| перетаскивание при масштабе > 1 | панорама | художник |
| T | поверх всех окон | художник |
| C | компактное окно | художник |
| B | фон: студия / изображение, размытие, затемнение | художник |
| P | параллакс: выкл → мягкий → сильный | художник |
| W | подиум на рабочем столе (`open_desktop`) | художник |
| M | масштаб поз: как в файле (по умолчанию) ↔ единый размер тела (`view.setSizing`) | художник |
| Y | оживление позы (`view.setIdle`) | художник |
| J | правка скелета (`set_joints`) | скелет |
| U | разметка частей тела; внутри: 1–5 режимы, V B E N G J X R инструменты, [ ] кисть, Ctrl+Z / Ctrl+Y | разметка |
| K, пробел, «,» «.» | танцы: список связок; во время танца — пауза, темп; Esc — стоп | танцы |
| G | галерея миниатюр | навигация |
| S | избранное | навигация |
| 1 / 2 / 3 / 0 | опора: стопы / колени / сидя-лёжа / авто | навигация |
| [ / ], Shift+[ / ], \ | размер позы −3 % / +3 %, ±10 %, сброс | навигация |
| O | добавить папку-коллекцию | навигация |
| счётчик «12 / 220» и имя файла | — | навигация |
