const stateKey = Symbol("meldfabric-context-menu");

function install(element, binding) {
  const state = {
    callback: binding.value,
    timer: null,
    startX: 0,
    startY: 0,
    fired: false,
  };
  const cancel = () => {
    clearTimeout(state.timer);
    state.timer = null;
  };
  const open = (event, origin) => {
    event.preventDefault();
    state.callback?.({
      x: event.clientX,
      y: event.clientY,
      origin,
      sourceEvent: event,
    });
  };
  state.contextmenu = (event) => open(event, "pointer");
  state.pointerdown = (event) => {
    if (!["touch", "pen"].includes(event.pointerType) || event.button !== 0)
      return;
    cancel();
    state.fired = false;
    state.startX = event.clientX;
    state.startY = event.clientY;
    state.timer = setTimeout(() => {
      state.fired = true;
      open(event, "long-press");
    }, 520);
  };
  state.pointermove = (event) => {
    if (
      Math.hypot(event.clientX - state.startX, event.clientY - state.startY) > 8
    )
      cancel();
  };
  state.pointerend = cancel;
  state.click = (event) => {
    if (!state.fired) return;
    state.fired = false;
    event.preventDefault();
    event.stopPropagation();
  };
  element.addEventListener("contextmenu", state.contextmenu);
  element.addEventListener("pointerdown", state.pointerdown);
  element.addEventListener("pointermove", state.pointermove);
  element.addEventListener("pointerup", state.pointerend);
  element.addEventListener("pointercancel", state.pointerend);
  element.addEventListener("click", state.click, true);
  element[stateKey] = state;
}

function uninstall(element) {
  const state = element[stateKey];
  if (!state) return;
  clearTimeout(state.timer);
  element.removeEventListener("contextmenu", state.contextmenu);
  element.removeEventListener("pointerdown", state.pointerdown);
  element.removeEventListener("pointermove", state.pointermove);
  element.removeEventListener("pointerup", state.pointerend);
  element.removeEventListener("pointercancel", state.pointerend);
  element.removeEventListener("click", state.click, true);
  delete element[stateKey];
}

export const contextMenuDirective = {
  mounted: install,
  updated(element, binding) {
    if (element[stateKey]) element[stateKey].callback = binding.value;
  },
  unmounted: uninstall,
};
