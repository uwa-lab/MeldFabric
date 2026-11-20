<script setup>
import { ref } from "vue";
import { notify } from "../store.js";

const props = defineProps({
  label: { type: String, default: "ID" },
  value: { type: String, required: true },
  detail: String,
});

const copied = ref(false);

async function copy() {
  try {
    await navigator.clipboard.writeText(props.value);
    copied.value = true;
    notify(`${props.label} 已复制`);
    window.setTimeout(() => (copied.value = false), 1600);
  } catch {
    notify("无法访问剪贴板，请手动复制", true);
  }
}
</script>

<template>
  <button
    type="button"
    class="technical-id"
    :title="`复制 ${label}`"
    @click="copy"
  >
    <span>{{ label }}</span>
    <code>{{ value }}</code>
    <small v-if="detail">{{ detail }}</small>
    <em>{{ copied ? "已复制" : "复制" }}</em>
  </button>
</template>
