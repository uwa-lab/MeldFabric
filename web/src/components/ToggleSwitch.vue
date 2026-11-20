<script setup>
defineOptions({ inheritAttrs: false });
defineProps({
  modelValue: Boolean,
  label: String,
  disabled: Boolean,
  inputId: String,
});
defineEmits(["update:modelValue"]);
</script>
<template>
  <label class="toggle-switch" :class="{ disabled }" :for="inputId">
    <input
      v-bind="$attrs"
      :id="inputId"
      type="checkbox"
      role="switch"
      :checked="modelValue"
      :disabled="disabled"
      @change="$emit('update:modelValue', $event.target.checked)"
    />
    <span class="toggle-track" aria-hidden="true"><span /></span>
    <span v-if="label" class="toggle-label">{{ label }}</span>
  </label>
</template>
