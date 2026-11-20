<script setup>
import { useMotion } from "../motion.js";
const props = defineProps({
  resize: Boolean,
  enterOnly: Boolean,
  persistent: Boolean,
});
const { play, cancel } = useMotion();
const interrupted = new WeakMap(),
  disabledControls = new WeakMap();
function frame(el) {
  const style = getComputedStyle(el);
  return {
    height: `${el.getBoundingClientRect().height}px`,
    opacity: style.opacity,
  };
}
function collapsed() {
  return {
    height: "0px",
    opacity: 0,
  };
}
function restoreControls(el) {
  for (const [control, disabled] of disabledControls.get(el) || []) control.disabled = disabled;
  disabledControls.delete(el);
}
function stop(el) {
  interrupted.set(el, props.resize ? frame(el) : { opacity: getComputedStyle(el).opacity });
  cancel(el);
}
async function enter(el, done) {
  el.inert = false;
  restoreControls(el);
  const previous = interrupted.get(el);
  interrupted.delete(el);
  try {
    await play(
      el,
      props.resize
        ? [previous || collapsed(), frame(el)]
        : [previous || { opacity: props.enterOnly ? 0.75 : 0 }, { opacity: 1 }],
      props.resize ? "resize" : "enter",
    );
  } finally {
    done();
  }
}
async function leave(el, done) {
  if (props.enterOnly) {
    cancel(el);
    done();
    return;
  }
  const start =
    interrupted.get(el) || (props.resize ? frame(el) : { opacity: getComputedStyle(el).opacity });
  interrupted.delete(el);
  if (el.contains(document.activeElement) && el.id) {
    document
      .querySelector(`[aria-controls="${CSS.escape(el.id)}"]`)
      ?.focus({ preventScroll: true });
  }
  el.inert = true;
  if (!props.persistent && !disabledControls.has(el)) {
    const controls = [...el.querySelectorAll("input, select, textarea, button")].map((control) => [
      control,
      control.disabled,
    ]);
    disabledControls.set(el, controls);
    for (const [control] of controls) control.disabled = true;
  }
  try {
    await play(el, [start, props.resize ? collapsed() : { opacity: 0 }], "exit");
  } finally {
    done();
  }
}
</script>
<template>
  <Transition
    :css="false"
    @enter="enter"
    @leave="leave"
    @enter-cancelled="stop"
    @leave-cancelled="stop"
    ><slot
  /></Transition>
</template>
