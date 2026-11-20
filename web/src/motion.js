import { onScopeDispose } from "vue";

// Durations and curves come from the same tokens as CSS micro-interactions.
const recipes = {
  enter: ["--motion-enter", "--ease-enter"],
  exit: ["--motion-exit", "--ease-exit"],
  resize: ["--motion-layout", "--ease-standard"],
};
// CSS minifiers may rewrite 120ms as .12s. WAAPI always expects milliseconds.
function milliseconds(value) {
  const text = value.trim();
  const amount = parseFloat(text);
  return Number.isFinite(amount)
    ? Math.max(0, amount * (text.endsWith("ms") ? 1 : text.endsWith("s") ? 1000 : 1))
    : 0;
}
export function useMotion() {
  const running = new Map();
  const preference = matchMedia("(prefers-reduced-motion: reduce)");
  let disposed = false;
  function cancel(element) {
    running.get(element)?.cancel();
    running.delete(element);
  }
  async function play(element, frames, recipe = "enter") {
    cancel(element);
    if (disposed || !element?.isConnected || preference.matches || !element.animate)
      return "skipped";
    const tokens = getComputedStyle(document.documentElement);
    const [duration, easing] = recipes[recipe];
    const animation = element.animate(frames, {
      duration: milliseconds(tokens.getPropertyValue(duration)),
      easing: tokens.getPropertyValue(easing).trim() || "ease",
      fill: "both",
    });
    running.set(element, animation);
    try {
      await animation.finished;
      return "finished";
    } catch (error) {
      if (error.name !== "AbortError") throw error;
      return "cancelled";
    } finally {
      if (running.get(element) === animation) running.delete(element);
      animation.cancel();
    }
  }
  function settle() {
    for (const animation of running.values()) animation.finish();
  }
  function visibility() {
    if (document.hidden) settle();
  }
  preference.addEventListener("change", settle);
  document.addEventListener("visibilitychange", visibility);
  onScopeDispose(() => {
    disposed = true;
    preference.removeEventListener("change", settle);
    document.removeEventListener("visibilitychange", visibility);
    for (const element of running.keys()) cancel(element);
  });
  return { play, cancel };
}
