<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useAttrs, useId } from "vue";

defineOptions({ inheritAttrs: false });
const props = defineProps({
    modelValue: { default: null },
    options: { type: Array, default: () => [] },
    inputId: String,
    ariaLabel: String,
    placeholder: { type: String, default: "请选择" },
    disabled: Boolean,
    required: Boolean,
  }),
  emit = defineEmits(["update:modelValue", "change"]),
  root = ref(),
  trigger = ref(),
  menu = ref(),
  optionNodes = ref([]),
  open = ref(false),
  active = ref(-1),
  teleportTarget = ref("body"),
  generatedId = useId(),
  attrs = useAttrs(),
  rootClass = computed(() => attrs.class),
  triggerAttrs = computed(() =>
    Object.fromEntries(Object.entries(attrs).filter(([key]) => !["class", "style"].includes(key))),
  ),
  controlId = computed(() => props.inputId || `select-${generatedId}`),
  listId = computed(() => `${controlId.value}-listbox`),
  selectedIndex = computed(() =>
    props.options.findIndex((item) => Object.is(item.value, props.modelValue)),
  ),
  selected = computed(() => props.options[selectedIndex.value]),
  label = computed(() => selected.value?.label || props.placeholder);

function enabled(index) {
  return index >= 0 && index < props.options.length && !props.options[index].disabled;
}
function nextEnabled(start, direction) {
  if (!props.options.length) return -1;
  let index = start;
  for (let checked = 0; checked < props.options.length; checked += 1) {
    index = (index + direction + props.options.length) % props.options.length;
    if (enabled(index)) return index;
  }
  return -1;
}
async function reveal(index) {
  active.value = index;
  await nextTick();
  optionNodes.value[index]?.scrollIntoView({ block: "nearest" });
}
function place() {
  const rect = trigger.value?.getBoundingClientRect();
  if (!rect || !menu.value) return;
  const margin = 8,
    gap = 4,
    viewportWidth = window.innerWidth,
    viewportHeight = window.innerHeight,
    below = viewportHeight - rect.bottom - margin - gap,
    above = rect.top - margin - gap,
    useAbove = below < 180 && above > below,
    width = Math.min(Math.max(rect.width, 180), viewportWidth - margin * 2),
    left = Math.max(margin, Math.min(rect.left, viewportWidth - width - margin));
  if (rect.bottom < 0 || rect.top > viewportHeight) {
    hide();
    return;
  }
  Object.assign(menu.value.style, {
    left: `${left}px`,
    width: `${width}px`,
    maxHeight: `${Math.min(280, Math.max(96, useAbove ? above : below))}px`,
    top: useAbove ? "auto" : `${rect.bottom + gap}px`,
    bottom: useAbove ? `${viewportHeight - rect.top + gap}px` : "auto",
  });
}
async function show(preferred = selectedIndex.value) {
  if (props.disabled || open.value) return;
  teleportTarget.value = root.value?.closest("dialog") || document.body;
  open.value = true;
  const fallback = nextEnabled(-1, 1);
  await nextTick();
  place();
  await reveal(enabled(preferred) ? preferred : fallback);
}
function hide({ restore = false } = {}) {
  if (!open.value) return;
  open.value = false;
  active.value = -1;
  if (restore) nextTick(() => trigger.value?.focus({ preventScroll: true }));
}
function choose(index) {
  if (!enabled(index)) return;
  const value = props.options[index].value;
  emit("update:modelValue", value);
  emit("change", value);
  hide({ restore: true });
}
function invalid(event) {
  event.preventDefault();
  trigger.value?.focus({ preventScroll: true });
}
function keydown(event) {
  if (["ArrowDown", "ArrowUp"].includes(event.key)) {
    event.preventDefault();
    if (!open.value) {
      const edge = event.key === "ArrowDown" ? -1 : 0;
      show(
        enabled(selectedIndex.value)
          ? selectedIndex.value
          : nextEnabled(edge, event.key === "ArrowDown" ? 1 : -1),
      );
      return;
    }
    reveal(nextEnabled(active.value, event.key === "ArrowDown" ? 1 : -1));
    return;
  }
  if (event.key === "Home" && open.value) {
    event.preventDefault();
    reveal(nextEnabled(-1, 1));
    return;
  }
  if (event.key === "End" && open.value) {
    event.preventDefault();
    reveal(nextEnabled(0, -1));
    return;
  }
  if (["Enter", " "].includes(event.key)) {
    event.preventDefault();
    if (open.value) choose(active.value);
    else show();
    return;
  }
  if (event.key === "Escape" && open.value) {
    event.preventDefault();
    hide({ restore: true });
  }
  if (event.key === "Tab") hide();
}
function outside(event) {
  if (!root.value?.contains(event.target) && !menu.value?.contains(event.target)) hide();
}
function escape(event) {
  if (event.key === "Escape" && open.value) {
    event.preventDefault();
    hide({ restore: true });
  }
}
onMounted(() => {
  document.addEventListener("pointerdown", outside);
  document.addEventListener("scroll", place, true);
  window.addEventListener("keydown", escape);
  window.addEventListener("resize", hide);
});
onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", outside);
  document.removeEventListener("scroll", place, true);
  window.removeEventListener("keydown", escape);
  window.removeEventListener("resize", hide);
});
</script>
<template>
  <div ref="root" class="select-control" :class="[rootClass, { open, disabled }]">
    <button
      v-bind="triggerAttrs"
      :id="controlId"
      ref="trigger"
      type="button"
      role="combobox"
      class="field select-trigger"
      :class="{ placeholder: !selected }"
      aria-haspopup="listbox"
      :aria-controls="listId"
      :aria-expanded="open"
      :aria-label="ariaLabel"
      :aria-required="required || undefined"
      :data-value="modelValue == null ? '' : String(modelValue)"
      :disabled="disabled"
      @click="open ? hide() : show()"
      @keydown="keydown"
    >
      <span>{{ label }}</span>
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <path d="m4 6 4 4 4-4" />
      </svg>
    </button>
    <input
      v-if="required"
      class="select-validation"
      type="text"
      tabindex="-1"
      aria-hidden="true"
      :disabled="disabled"
      :value="selected ? String(modelValue ?? '') : ''"
      required
      @invalid="invalid"
    />
    <Teleport :to="teleportTarget">
      <div
        v-if="open"
        :id="listId"
        ref="menu"
        class="select-menu"
        role="listbox"
        :aria-label="ariaLabel"
        :aria-activedescendant="active >= 0 ? `${controlId}-option-${active}` : undefined"
      >
        <button
          v-for="(item, index) in options"
          :id="`${controlId}-option-${index}`"
          :key="item.key ?? `${index}-${String(item.value)}`"
          :ref="(element) => (optionNodes[index] = element)"
          type="button"
          role="option"
          class="select-option"
          :class="{
            active: active === index,
            selected: index === selectedIndex,
          }"
          :aria-selected="index === selectedIndex"
          :data-value="item.value == null ? '' : String(item.value)"
          :disabled="item.disabled"
          @pointermove="!item.disabled && (active = index)"
          @click="choose(index)"
        >
          <span v-if="item.description" class="select-option-copy">
            <strong>{{ item.label }}</strong>
            <small>{{ item.description }}</small>
          </span>
          <span v-else>{{ item.label }}</span>
          <svg v-if="index === selectedIndex" viewBox="0 0 16 16" aria-hidden="true">
            <path d="m3 8 3 3 7-7" />
          </svg>
        </button>
        <p v-if="!options.length" class="select-empty">没有可选项</p>
      </div>
    </Teleport>
  </div>
</template>
