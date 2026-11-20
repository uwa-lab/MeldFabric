<script setup>
import { computed } from "vue";

const props = defineProps({
  modelValue: { type: Number, default: null },
  integer: Boolean,
  minimum: Number,
  maximum: Number,
  step: Number,
  required: Boolean,
  ariaLabel: String,
});
const emit = defineEmits(["update:modelValue"]);
const increment = computed(() => props.step ?? (props.integer ? 1 : 0.1));

function commit(value) {
  if (value === "" || value == null) {
    emit("update:modelValue", null);
    return;
  }
  let number = props.integer ? Number.parseInt(value, 10) : Number(value);
  if (!Number.isFinite(number)) return;
  if (props.minimum != null) number = Math.max(props.minimum, number);
  if (props.maximum != null) number = Math.min(props.maximum, number);
  emit("update:modelValue", number);
}

function adjust(direction) {
  const current = Number.isFinite(props.modelValue)
    ? props.modelValue
    : (props.minimum ?? 0);
  commit(current + increment.value * direction);
}
</script>

<template>
  <div class="number-stepper">
    <button type="button" aria-label="减少" @click="adjust(-1)">−</button>
    <input
      class="field"
      type="number"
      :value="modelValue ?? ''"
      :min="minimum"
      :max="maximum"
      :step="integer ? step || 1 : step || 'any'"
      :required="required"
      :aria-label="ariaLabel"
      @input="commit($event.target.value)"
    />
    <button type="button" aria-label="增加" @click="adjust(1)">+</button>
  </div>
</template>
