<script setup>
import { computed, useAttrs } from "vue";

defineOptions({ inheritAttrs: false });
defineProps({
  variant: {
    type: String,
    default: "secondary",
    validator: (value) => ["primary", "secondary", "ghost", "danger", "plain"].includes(value),
  },
  busy: Boolean,
  label: String,
  pendingLabel: String,
  type: { type: String, default: "button" },
});
const attrs = useAttrs();
const buttonAttrs = computed(() =>
  Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== "class")),
);
</script>

<template>
  <button
    v-bind="buttonAttrs"
    :type="type"
    :class="['ui-button', variant, attrs.class]"
    :disabled="busy || attrs.disabled"
    :aria-busy="busy || undefined"
    :data-label="label"
    :data-pending-label="pendingLabel"
  >
    <span class="ui-button-content">
      <slot :busy="busy">{{ busy ? pendingLabel : label }}</slot>
    </span>
  </button>
</template>
