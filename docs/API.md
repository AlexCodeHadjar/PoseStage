# PoseStage — внутренние контракты

Документ фиксирует границы между Python-бэкендом, ядром фронтенда (`web/main.js`)
и UI-модулями (`web/ui/*.js`). Меняя контракт, обновите этот файл.

## 1. Python API (`window.pywebview.api`, класс `app.Api`)

Все методы возвращают JSON-совместимые значения. Публичные атрибуты `Api` — только методы.

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
}
```

Поддерживаемые файлы: `.png .jpg .jpeg .webp` (регистр не важен), рекурсивно во вложенных
папках, кроме скрытых и служебных (`.`/`_` в начале имени папки). Сортировка — естественная
(`pose-2` < `pose-10`) по относительному пути.

### Состояние

- `get_state() -> {index: int, file: string|null}` — индекс восстанавливается **по имени файла**;
  если файла больше нет — по сохранённому индексу, ограниченному диапазоном, иначе 0.
- `save_state(index: int)` — сохраняет `file` (и индекс как запасной вариант).

### Опора позы

- `set_support_mode(file: string, mode: "feet"|"kneel"|"ground"|"auto") -> Pose`
  Пользовательское переопределение хранится в `%APPDATA%\PoseStage\support_overrides.json`
  (по коллекции). `"auto"` удаляет переопределение. Возвращает пересчитанную позу.

### Коллекции (несколько папок)

```ts
type Collection = {id: string; name: string; folder: string; active: boolean};
```

- `get_collections() -> Collection[]`
- `add_collection() -> {collections: Collection[], poses: Pose[]}` — диалог выбора папки;
  при успехе новая коллекция становится активной. Отмена — вернуть текущие данные.
- `switch_collection(id: string) -> {collections, poses}`
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
  filter: {mode: "all"|"feet"|"kneel"|"ground", favorites: bool, tag: string|null}`.

### Окно

- `toggle_fullscreen()`, `quit()`, `report_error(message)` — как раньше.
- `set_on_top(on: bool) -> bool` — окно поверх всех.
- `set_compact(on: bool) -> bool` — компактный режим: выход из полного экрана, окно ~ 480×720
  у правого края рабочей области, поверх всех. `false` — возврат в полный экран.
- Перетаскивание frameless-окна: элементы с CSS-классом `pywebview-drag-region`.

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
  goTo(index: number): void;         // переход с анимацией (или мгновенно при далёком прыжке)
  step(delta: number): void;         // ±N; быстрые нажатия накапливаются
  reload(poses: Pose[], index?: number): Promise<void>; // новый список (смена коллекции)
  updatePose(index: number, pose: Pose): void;  // заменить данные позы и переразместить
  setOrder(indices: number[] | null): void;     // порядок обхода ←/→ (фильтр); null — все
  on(event: "pose"|"poses"|"transition", cb: (detail) => void): () => void;
  shortcuts: {
    register(def: {keys: string[]; title: string; group: string; handler: (e) => void}): () => void;
    list(): {keys, title, group}[];
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
| G | галерея миниатюр | навигация |
| S | избранное | навигация |
| 1 / 2 / 3 / 0 | опора: стопы / колени / сидя-лёжа / авто | навигация |
| O | добавить папку-коллекцию | навигация |
| счётчик «12 / 220» и имя файла | — | навигация |
