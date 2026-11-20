<script setup>
defineProps({
  modelValue: { type: String, default: "basic" },
  basicLabel: { type: String, default: "基本配置" },
  advancedLabel: { type: String, default: "更多设置" },
});
defineEmits(["update:modelValue"]);
</script>

<template>
  <div class="configuration-modes">
    <div class="target-switch configuration-mode-switch" role="tablist">
      <button
        type="button"
        role="tab"
        :aria-selected="modelValue === 'basic'"
        :class="{ active: modelValue === 'basic' }"
        @click="$emit('update:modelValue', 'basic')"
      >
        {{ basicLabel }}
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="modelValue === 'advanced'"
        :class="{ active: modelValue === 'advanced' }"
        @click="$emit('update:modelValue', 'advanced')"
      >
        {{ advancedLabel }}
      </button>
    </div>
    <div class="configuration-mode-content">
      <slot v-if="modelValue === 'basic'" name="basic" />
      <slot v-else name="advanced" />
    </div>
  </div>
</template>
