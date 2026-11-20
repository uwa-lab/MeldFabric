<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({
  open: Boolean,
  x: Number,
  y: Number,
  items: { type: Array, default: () => [] },
});
const emit = defineEmits(["select", "dismiss"]);
const menu = ref();

async function place() {
  if (!props.open) return;
  await nextTick();
  const element = menu.value;
  if (!element) return;
  const margin = 8;
  const rect = element.getBoundingClientRect();
  element.style.left = `${Math.max(margin, Math.min(props.x, innerWidth - rect.width - margin))}px`;
  element.style.top = `${Math.max(margin, Math.min(props.y, innerHeight - rect.height - margin))}px`;
}
function outside(event) {
  if (props.open && !menu.value?.contains(event.target)) emit("dismiss");
}
function keydown(event) {
  if (props.open && event.key === "Escape") emit("dismiss");
}
function choose(item) {
  if (item.disabled) return;
  emit("select", item.id);
}
watch(() => [props.open, props.x, props.y], place);
onMounted(() => {
  document.addEventListener("pointerdown", outside);
  window.addEventListener("keydown", keydown);
  window.addEventListener("resize", place);
});
onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", outside);
  window.removeEventListener("keydown", keydown);
  window.removeEventListener("resize", place);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="context-menu">
      <div
        v-if="open"
        ref="menu"
        class="context-menu"
        role="menu"
        @contextmenu.prevent
        @pointerdown.stop
      >
        <template v-for="item in items" :key="item.id">
          <div v-if="item.separator" class="context-menu-separator" />
          <UiButton
            v-else
            :variant="item.danger ? 'danger' : 'ghost'"
            role="menuitem"
            :class="{ danger: item.danger }"
            :disabled="item.disabled"
            @click="choose(item)"
          >
            <span>{{ item.label }}</span
            ><small v-if="item.hint">{{ item.hint }}</small>
          </UiButton>
        </template>
      </div>
    </Transition>
  </Teleport>
</template>
