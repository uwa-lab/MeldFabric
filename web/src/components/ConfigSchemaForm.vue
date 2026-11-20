<script setup>
import StructuredSettings from "./StructuredSettings.vue";

defineOptions({ name: "ConfigSchemaForm" });
defineProps({
  schema: { type: Object, default: () => ({}) },
  modelValue: { type: Object, default: () => ({}) },
  optionSources: { type: Object, default: () => ({}) },
  layout: { type: String, default: "standard" },
  title: { type: String, default: "实现参数" },
  description: {
    type: String,
    default: "字段和约束来自代码注册时声明的配置模型。",
  },
  headerless: Boolean,
});
defineEmits(["update:modelValue"]);
</script>

<template>
  <div class="schema-config-workspace">
    <StructuredSettings
      :title="title"
      :description="description"
      :model-value="modelValue"
      :schema="schema"
      :option-sources="optionSources"
      :layout="layout"
      :headerless="headerless"
      empty-text="这个代码能力不需要额外配置。"
      @update:model-value="$emit('update:modelValue', $event)"
    />
  </div>
</template>
