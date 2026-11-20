<script setup>
import { ref } from "vue";
import { notify } from "../store.js";

const props = defineProps({
  title: { type: String, required: true },
  description: String,
  code: { type: String, required: true },
  language: { type: String, default: "python" },
});
const copied = ref(false);

async function copyCode() {
  try {
    await navigator.clipboard.writeText(props.code);
    copied.value = true;
    notify("代码已复制");
    window.setTimeout(() => (copied.value = false), 1800);
  } catch {
    notify("无法访问剪贴板，请手动复制", true);
  }
}
</script>

<template>
  <article class="code-example">
    <header>
      <div>
        <h3>{{ title }}</h3>
        <UiDescription v-if="description">{{ description }}</UiDescription>
      </div>
      <UiButton variant="ghost" @click="copyCode">
        {{ copied ? "已复制" : "复制" }}
      </UiButton>
    </header>
    <div class="code-window">
      <span>{{ language }}</span>
      <pre><code>{{ code }}</code></pre>
    </div>
  </article>
</template>
