<script setup>
import { computed, ref } from "vue";
import { confirmAction } from "../dialogs.js";

const props = defineProps({ modelValue: Object });
const emit = defineEmits(["update:modelValue"]);
const selected = ref(
  Math.max(
    0,
    (props.modelValue?.versions || []).findIndex(
      (item) => item.version === props.modelValue?.selected_version,
    ),
  ),
);
const prompt = computed(() => props.modelValue);
const version = computed(() => prompt.value.versions[selected.value]);

function changed() {
  emit("update:modelValue", { ...prompt.value });
}

function add() {
  let index = prompt.value.versions.length + 1;
  while (prompt.value.versions.some((item) => item.version === `v${index}`))
    index += 1;
  prompt.value.versions.push({
    version: `v${index}`,
    content: version.value?.content || "",
  });
  selected.value = prompt.value.versions.length - 1;
  changed();
}

async function remove() {
  if (prompt.value.versions.length === 1) return;
  if (
    !(await confirmAction("此 Prompt 版本将从配置中删除。", {
      title: `删除 ${version.value.version}？`,
      confirmText: "删除版本",
      danger: true,
    }))
  )
    return;
  const removed = version.value.version;
  prompt.value.versions.splice(selected.value, 1);
  selected.value = Math.max(0, selected.value - 1);
  if (prompt.value.selected_version === removed)
    prompt.value.selected_version = prompt.value.versions[0].version;
  changed();
}

function rename(value) {
  const previous = version.value.version;
  version.value.version = value;
  if (prompt.value.selected_version === previous)
    prompt.value.selected_version = value;
  changed();
}

function insert(variable) {
  version.value.content += `{{ ${variable} }}`;
  changed();
}
</script>

<template>
  <div class="prompt-editor split-editor">
    <aside class="split-index">
      <UiButton variant="secondary" class="wide" @click="add"
        >新增版本</UiButton
      >
      <button
        v-for="(item, index) in prompt.versions"
        :key="index"
        type="button"
        :class="{ active: selected === index }"
        @click="selected = index"
      >
        <span>{{ item.version }}</span>
        <small v-if="item.version === prompt.selected_version">使用中</small>
      </button>
    </aside>
    <section v-if="version" class="split-editor-body">
      <label
        >版本名称
        <input
          class="field"
          :value="version.version"
          required
          @input="rename($event.target.value)"
        />
      </label>
      <label
        >系统 Prompt
        <textarea
          v-model="version.content"
          class="field prompt-content"
          placeholder="说明 Agent 的职责、工作方式、边界与输出要求…"
          @input="changed"
        />
      </label>
      <footer class="editor-footer compact">
        <div class="variable-list">
          <span>插入变量</span>
          <button type="button" @click="insert('user.id')">用户</button>
          <button type="button" @click="insert('time.date')">日期</button>
          <button type="button" @click="insert('time.now')">时间</button>
          <button type="button" @click="insert('time.timezone')">时区</button>
        </div>
        <div class="button-row">
          <UiButton
            variant="danger"
            :disabled="prompt.versions.length === 1"
            @click="remove"
          >
            删除版本
          </UiButton>
          <UiButton
            variant="primary"
            :disabled="version.version === prompt.selected_version"
            @click="
              prompt.selected_version = version.version;
              changed();
            "
          >
            {{
              version.version === prompt.selected_version
                ? "当前使用"
                : "使用此版本"
            }}
          </UiButton>
        </div>
      </footer>
    </section>
  </div>
</template>
