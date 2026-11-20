<script setup>
import StructuredField from "./StructuredField.vue";

defineProps({
  modelValue: { default: () => ({}) },
  schema: { type: Object, default: () => ({}) },
  optionSources: { type: Object, default: () => ({}) },
  label: String,
  description: String,
  emptyText: { type: String, default: "没有需要配置的字段。" },
  layout: { type: String, default: "standard" },
});
defineEmits(["update:modelValue"]);
</script>

<template>
  <div class="structured-data-editor">
    <StructuredField
      :model-value="modelValue"
      :schema="schema"
      :root-schema="schema"
      :option-sources="optionSources"
      :label="label"
      :description="description"
      :layout="layout"
      path="$"
      root
      @update:model-value="$emit('update:modelValue', $event)"
    />
    <p
      v-if="
        schema.type === 'object' &&
        schema.additionalProperties === false &&
        !Object.keys(schema.properties || {}).length
      "
      class="structured-empty"
    >
      {{ emptyText }}
    </p>
  </div>
</template>
