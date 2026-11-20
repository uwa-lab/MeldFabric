<script setup>
import { computed, nextTick, ref } from "vue";

defineOptions({ inheritAttrs: false });
const props = defineProps({
  modelValue: { default: null },
  options: { type: Array, default: () => [] },
  ariaLabel: String,
  disabled: Boolean,
});
const emit = defineEmits(["update:modelValue"]);
const nodes = ref([]);
const selectedIndex = computed(() =>
  props.options.findIndex((option) =>
    Object.is(option.value, props.modelValue),
  ),
);

function move(from, direction) {
  if (props.disabled || !props.options.length) return;
  let index = from;
  for (let checked = 0; checked < props.options.length; checked += 1) {
    index = (index + direction + props.options.length) % props.options.length;
    if (!props.options[index].disabled) {
      emit("update:modelValue", props.options[index].value);
      nextTick(() => nodes.value[index]?.focus());
      return;
    }
  }
}
</script>

<template>
  <div
    v-bind="$attrs"
    class="choice-control"
    role="radiogroup"
    :aria-label="ariaLabel"
    :aria-disabled="disabled || undefined"
  >
    <button
      v-for="(option, index) in options"
      :key="option.key ?? `${index}-${String(option.value)}`"
      :ref="(element) => (nodes[index] = element)"
      type="button"
      role="radio"
      :aria-checked="Object.is(option.value, modelValue)"
      :tabindex="
        index === selectedIndex || (selectedIndex < 0 && index === 0) ? 0 : -1
      "
      :disabled="disabled || option.disabled"
      :class="{ selected: Object.is(option.value, modelValue) }"
      @click="emit('update:modelValue', option.value)"
      @keydown.left.prevent="move(index, -1)"
      @keydown.up.prevent="move(index, -1)"
      @keydown.right.prevent="move(index, 1)"
      @keydown.down.prevent="move(index, 1)"
    >
      {{ option.label }}
    </button>
  </div>
</template>
