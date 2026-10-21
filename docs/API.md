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

- `set_pose_scale(file: string, factor: number) -> Pose` — ручная поправка 0.5…2, `1` удаляет её.
  Хранится в `scale_overrides.json` по коллекции. Иное значение — `ValueError`.

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
  parallax: "off"|"soft"|"strong", sizing: "body"|"file", background: {mode: "studio"|"image", id: string|null, blur: 0…24, dim: 0…0.85}`.
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
(миниатюры, кэш в `%APPDATA%\PoseStage\thumbs`). Папку поз программа не изменяет.

## 2. Ядро фронтенда → UI-модули

`web/main.js` импортирует `web/ui/artist.js` и `web/ui/navigator.js` и вызывает
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
  on(event: "pose"|"poses"|"transition", cb: (detail) => void): () => void;
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
| G | галерея миниатюр | навигация |
| S | избранное | навигация |
| 1 / 2 / 3 / 0 | опора: стопы / колени / сидя-лёжа / авто | навигация |
| [ / ], Shift+[ / ], \ | размер позы −3 % / +3 %, ±10 %, сброс | навигация |
| O | добавить папку-коллекцию | навигация |
| счётчик «12 / 220» и имя файла | — | навигация |
