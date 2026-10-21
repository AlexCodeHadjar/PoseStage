// Keyboard shortcuts (App.shortcuts) and the modal overlay stack (App.overlays).

const MODIFIERS = ['Ctrl', 'Alt', 'Shift', 'Meta'];

// "shift+slash" / "Slash+Shift" -> "Shift+Slash" (canonical modifier order, KeyboardEvent.code last).
export function normalizeCombo(combo) {
  const parts = String(combo).split('+').map((part) => part.trim()).filter(Boolean);
  const modifiers = new Set();
  let code = '';
  for (const part of parts) {
    const modifier = MODIFIERS.find((name) => name.toLowerCase() === part.toLowerCase());
    if (modifier) {
      modifiers.add(modifier);
    } else {
      code = part;
    }
  }
  return [...MODIFIERS.filter((name) => modifiers.has(name)), code].join('+');
}

export function eventCombo(event) {
  const parts = [];
  if (event.ctrlKey) parts.push('Ctrl');
  if (event.altKey) parts.push('Alt');
  if (event.shiftKey) parts.push('Shift');
  if (event.metaKey) parts.push('Meta');
  parts.push(event.code);
  return parts.join('+');
}

// True while the user types into a text field: shortcuts are ignored then.
export function isTypingTarget(element) {
  if (!element || element.nodeType !== 1) {
    return false;
  }
  if (element.isContentEditable) {
    return true;
  }
  const tag = element.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') {
    return true;
  }
  if (tag === 'INPUT') {
    const type = (element.getAttribute('type') || 'text').toLowerCase();
    return !['button', 'checkbox', 'radio', 'range', 'color', 'submit', 'reset', 'image', 'file'].includes(type);
  }
  return false;
}

const KEY_LABELS = {
  ArrowLeft: '←',
  ArrowRight: '→',
  ArrowUp: '↑',
  ArrowDown: '↓',
  Escape: 'Esc',
  Space: 'Пробел',
  Delete: 'Del',
  Slash: '/',
  'Shift+Slash': '?',
  Equal: '=',
  'Shift+Equal': '+',
  Minus: '−',
  NumpadAdd: 'Num +',
  NumpadSubtract: 'Num −',
  Comma: ',',
  Period: '.',
  BracketLeft: '[',
  BracketRight: ']',
};

// Human-readable label of a combo, e.g. "KeyH" -> "H", "Shift+Slash" -> "?".
export function keyLabel(combo) {
  const normalized = normalizeCombo(combo);
  if (KEY_LABELS[normalized]) {
    return KEY_LABELS[normalized];
  }
  const parts = normalized.split('+');
  const code = parts.pop();
  let label = KEY_LABELS[code] || code;
  if (/^Key[A-Z]$/.test(code)) {
    label = code.slice(3);
  } else if (/^Digit\d$/.test(code)) {
    label = code.slice(5);
  } else if (/^Numpad\d$/.test(code)) {
    label = `Num ${code.slice(6)}`;
  }
  return [...parts, label].join('+');
}

export function createShortcuts(report) {
  const definitions = []; // registration order; the latest registration wins on conflicts

  // Core-internal options (not part of the module contract):
  //   anyShift: also fire with Shift held (plain keys as at baseline),
  //   eventKeys: also match KeyboardEvent.key values (layout-independent "?"),
  //   enabled: () => boolean, skipped (event left untouched) while false.
  function register(definition) {
    const { keys, title = '', group = '', handler, anyShift = false, eventKeys = [], enabled = null } = definition || {};
    if (!Array.isArray(keys) || typeof handler !== 'function') {
      throw new TypeError('shortcuts.register({keys: string[], title, group, handler})');
    }
    const entry = {
      keys: keys.map(normalizeCombo),
      title: String(title),
      group: String(group),
      handler,
      anyShift: !!anyShift,
      eventKeys: [...eventKeys],
      enabled: typeof enabled === 'function' ? enabled : null,
    };
    definitions.push(entry);
    return () => {
      const i = definitions.indexOf(entry);
      if (i >= 0) {
        definitions.splice(i, 1);
      }
    };
  }

  function list() {
    return definitions.map(({ keys, title, group }) => ({ keys: [...keys], title, group }));
  }

  // Runs the matching handler; returns true when a shortcut consumed the event.
  function dispatch(event) {
    if (isTypingTarget(event.target) || isTypingTarget(document.activeElement)) {
      return false;
    }
    const combo = eventCombo(event);
    const onlyShift = event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey;
    const exact = (entry) => entry.keys.includes(combo) || entry.eventKeys.includes(event.key);
    const shifted = (entry) => entry.anyShift && onlyShift && entry.keys.includes(event.code);
    // Exact matches first (latest registration wins), then the Shift-tolerant fallback.
    for (const matches of [exact, shifted]) {
      for (let i = definitions.length - 1; i >= 0; i--) {
        const entry = definitions[i];
        if (!matches(entry) || (entry.enabled && !entry.enabled())) {
          continue;
        }
        event.preventDefault();
        try {
          entry.handler(event);
        } catch (error) {
          report(error);
        }
        return true;
      }
    }
    return false;
  }

  return { register, list, dispatch };
}

export function createOverlays(report) {
  const stack = []; // [{id, close}], top = last

  function open(id, close) {
    remove(id);
    stack.push({ id: String(id), close: typeof close === 'function' ? close : () => {} });
  }

  function remove(id) {
    const i = stack.findIndex((entry) => entry.id === String(id));
    if (i >= 0) {
      stack.splice(i, 1);
    }
  }

  // Esc: close the top overlay through its own close callback. False if none is open.
  function closeTop() {
    const top = stack.pop();
    if (!top) {
      return false;
    }
    try {
      top.close();
    } catch (error) {
      report(error);
    }
    return true;
  }

  return {
    open,
    close: remove,
    any: () => stack.length > 0,
    closeTop,
    top: () => (stack.length ? stack[stack.length - 1].id : null),
  };
}
