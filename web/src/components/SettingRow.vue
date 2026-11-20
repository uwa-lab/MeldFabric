<script setup>
import { nextTick, onMounted, onUpdated, ref } from "vue";

const props = defineProps({
  title: String,
  description: String,
  required: Boolean,
});
const root = ref();

function connectAccessibleNames() {
  nextTick(() => {
    const controls = root.value?.querySelectorAll(
      "input, textarea, select, button[role='combobox'], button[role='switch']",
    );
    controls?.forEach((control, index) => {
      const hasTextLabel = [...(control.labels || [])].some((label) =>
        Boolean(label.textContent?.trim()),
      );
      if (
        !control.getAttribute("aria-label") &&
        !control.getAttribute("aria-labelledby") &&
        !hasTextLabel
      )
        control.setAttribute(
          "aria-label",
          controls.length > 1 ? `${props.title} ${index + 1}` : props.title,
        );
    });
  });
}

onMounted(connectAccessibleNames);
onUpdated(connectAccessibleNames);
</script>
<template>
  <section ref="root" class="setting-row">
    <div class="setting-row-copy">
      <h4>{{ title }}<span v-if="required" class="required">*</span></h4>
      <UiDescription v-if="description">{{ description }}</UiDescription>
    </div>
    <div class="setting-row-control"><slot /></div>
  </section>
</template>
