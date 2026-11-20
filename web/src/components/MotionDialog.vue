<script setup>
import { onMounted, onBeforeUnmount, ref } from "vue";
import { useMotion } from "../motion.js";
defineProps({
  labelledby: String,
  dismissible: { type: Boolean, default: true },
});
const emit = defineEmits(["dismiss"]);
const dialog = ref(),
  closing = ref(false);
const previous = document.activeElement;
const { play } = useMotion();
onMounted(() => {
  dialog.value.showModal();
  document.documentElement.classList.add("modal-open");
  play(dialog.value, [
    { opacity: 0, transform: "translateY(6px) scale(.99)" },
    { opacity: 1, transform: "none" },
  ]);
});
async function dismiss(value = false) {
  if (closing.value) return;
  closing.value = true;
  const current = getComputedStyle(dialog.value);
  await play(
    dialog.value,
    [
      { opacity: current.opacity, transform: current.transform },
      { opacity: 0, transform: "translateY(4px)" },
    ],
    "exit",
  );
  emit("dismiss", value);
}
onBeforeUnmount(() => {
  dialog.value?.close();
  const remaining = [...document.querySelectorAll("dialog[open]")].filter(
    (el) => el !== dialog.value,
  );
  if (!remaining.length) document.documentElement.classList.remove("modal-open");
  if (previous?.isConnected && !previous.closest("[inert]"))
    previous.focus({ preventScroll: true });
  else
    document
      .querySelector(".editor-head h2, .section-head h2, #pageTitle")
      ?.focus({ preventScroll: true });
});
</script>
<template>
  <dialog
    ref="dialog"
    :aria-labelledby="labelledby"
    :aria-busy="closing"
    :class="{ 'is-closing': closing }"
    @cancel.prevent="dismissible && dismiss()"
    @click="
      (event) => {
        if (dismissible && event.target === dialog) dismiss();
      }
    "
  >
    <div class="dialog-surface" :class="{ 'is-closing': closing }" :inert="closing">
      <slot :dismiss="dismiss" :closing="closing" />
    </div>
  </dialog>
</template>
