// Minimal event hub for App.on(); a throwing listener never breaks the others.
export function createEmitter(names, report) {
  const listeners = new Map(names.map((name) => [name, new Set()]));

  function on(name, callback) {
    const set = listeners.get(name);
    if (!set) {
      throw new Error(`Unknown event "${name}"`);
    }
    if (typeof callback !== 'function') {
      throw new TypeError('Listener must be a function');
    }
    set.add(callback);
    return () => set.delete(callback);
  }

  function emit(name, detail) {
    for (const callback of [...listeners.get(name)]) {
      try {
        callback(detail);
      } catch (error) {
        report(error);
      }
    }
  }

  return { on, emit };
}
