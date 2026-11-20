<script setup>
import { ref, useId, nextTick } from "vue";
import MotionPresence from "./MotionPresence.vue";
const props = defineProps({ open: Boolean });
const expanded = ref(props.open),
  id = useId();
function revealInvalid(event) {
  event.preventDefault();
  expanded.value = true;
  nextTick(() => event.target.focus());
}
</script>
<template>
  <section class="disclosure" :class="{ expanded }" @invalid.capture="revealInvalid">
    <button
      type="button"
      class="disclosure-trigger"
      :aria-expanded="expanded"
      :aria-controls="id"
      @click="expanded = !expanded"
    >
      <svg class="disclosure-chevron" viewBox="0 0 16 16" aria-hidden="true">
        <path d="m6 4 4 4-4 4" />
      </svg>
      <slot name="title" />
    </button>
    <MotionPresence resize persistent
      ><div v-show="expanded" :id="id" class="disclosure-body motion-shell">
        <div class="disclosure-content"><slot /></div></div
    ></MotionPresence>
  </section>
</template>
