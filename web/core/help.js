import { keyLabel } from './input.js';

// Keyboard help overlay ("?" / F1): every registered shortcut, grouped by group.
export function createHelp(shortcuts, overlays) {
  const root = document.createElement('div');
  root.id = 'help';
  root.hidden = true;
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', 'Горячие клавиши');
  const panel = document.createElement('div');
  panel.className = 'help-panel';
  root.append(panel);
  document.body.append(root);

  // Click on the dimmed backdrop closes the panel.
  root.addEventListener('pointerdown', (event) => {
    if (event.target === root) {
      close();
    }
  });

  function render() {
    panel.replaceChildren();
    const heading = document.createElement('h2');
    heading.textContent = 'Горячие клавиши';
    panel.append(heading);

    const groups = new Map();
    for (const item of shortcuts.list()) {
      const name = item.group || 'Прочее';
      if (!groups.has(name)) {
        groups.set(name, []);
      }
      groups.get(name).push(item);
    }

    const columns = document.createElement('div');
    columns.className = 'help-groups';
    for (const [name, items] of groups) {
      const section = document.createElement('section');
      const title = document.createElement('h3');
      title.textContent = name;
      section.append(title);
      const table = document.createElement('dl');
      for (const item of items) {
        const keys = document.createElement('dt');
        for (const label of new Set(item.keys.map(keyLabel))) {
          const kbd = document.createElement('kbd');
          kbd.textContent = label;
          keys.append(kbd);
        }
        const text = document.createElement('dd');
        text.textContent = item.title;
        table.append(keys, text);
      }
      section.append(table);
      columns.append(section);
    }
    panel.append(columns);

    const hint = document.createElement('p');
    hint.className = 'help-hint';
    hint.textContent = 'Esc — закрыть';
    panel.append(hint);
  }

  function open() {
    render();
    root.hidden = false;
    overlays.open('help', close);
  }

  function close() {
    root.hidden = true;
    overlays.close('help');
  }

  function toggle() {
    if (root.hidden) {
      open();
    } else {
      close();
    }
  }

  return { open, close, toggle, isOpen: () => !root.hidden };
}
