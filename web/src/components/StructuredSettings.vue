<script setup>
import { computed } from "vue";
import Disclosure from "./Disclosure.vue";
import StructuredDataEditor from "./StructuredDataEditor.vue";

const props = defineProps({
  modelValue: { default: () => ({}) },
  schema: { type: Object, default: () => ({}) },
  optionSources: { type: Object, default: () => ({}) },
  title: { type: String, default: "" },
  description: String,
  emptyText: String,
  collapsible: Boolean,
  open: Boolean,
  layout: { type: String, default: "standard" },
  headerless: Boolean,
});
const emit = defineEmits(["update:modelValue"]);

const configuredCount = computed(() => {
  if (Array.isArray(props.modelValue)) return props.modelValue.length;
  if (props.modelValue && typeof props.modelValue === "object")
    return Object.keys(props.modelValue).length;
  return props.modelValue === undefined || props.modelValue === null ? 0 : 1;
});
const schemaCount = computed(
  () => Object.keys(props.schema?.properties || {}).length,
);
const status = computed(() =>
  configuredCount.value ? `已设置 ${configuredCount.value} 项` : "使用默认值",
);
</script>

<template>
  <Disclosure v-if="collapsible" :open="open" class="structured-settings">
    <template #title>
      <span class="structured-settings-copy">
        <strong>{{ title }}</strong>
        <small v-if="description">{{ description }}</small>
      </span>
      <span class="structured-settings-status">{{ status }}</span>
    </template>
    <StructuredDataEditor
      :model-value="modelValue"
      :schema="schema"
      :option-sources="optionSources"
      :empty-text="emptyText"
      :layout="layout"
      @update:model-value="emit('update:modelValue', $event)"
    />
  </Disclosure>

  <section v-else class="structured-settings expanded">
    <header v-if="!headerless" class="structured-settings-header">
      <div class="structured-settings-copy">
        <strong>{{ title }}</strong>
        <small v-if="description">{{ description }}</small>
      </div>
      <span v-if="schemaCount" class="structured-settings-status"
        >{{ schemaCount }} 个字段</span
      >
    </header>
    <StructuredDataEditor
      :model-value="modelValue"
      :schema="schema"
      :option-sources="optionSources"
      :empty-text="emptyText"
      :layout="layout"
      @update:model-value="emit('update:modelValue', $event)"
    />
  </section>
</template>
