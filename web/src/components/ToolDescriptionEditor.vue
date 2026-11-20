<script setup>
import { computed } from "vue";

const props = defineProps({ draft: { type: Object, required: true } });

const parameters = computed(() => props.draft.parameters || []);

function setToolDescription(value) {
  props.draft.description = value;
  props.draft.description_overridden =
    value !== (props.draft.description_default || "");
}

function resetToolDescription() {
  props.draft.description = props.draft.description_default || "";
  props.draft.description_overridden = false;
}

function setParameter(parameter, value) {
  parameter.description = value;
  parameter.description_overridden =
    value !== (parameter.description_default || "");
  const overrides = { ...(props.draft.parameter_descriptions || {}) };
  if (parameter.description_overridden) overrides[parameter.name] = value;
  else delete overrides[parameter.name];
  props.draft.parameter_descriptions = overrides;
}

function resetParameter(parameter) {
  setParameter(parameter, parameter.description_default || "");
}

function fieldType(schema = {}) {
  if (Array.isArray(schema.type))
    return schema.type.filter((item) => item !== "null").join(" / ");
  if (schema.enum) return "enum";
  return schema.type || "any";
}
</script>

<template>
  <div class="tool-description-editor">
    <section class="tool-description-primary">
      <header>
        <div>
          <h3>模型可见描述</h3>
          <UiDescription>模型根据这段说明判断何时使用该工具。</UiDescription>
        </div>
        <UiButton
          variant="ghost"
          :disabled="!draft.description_overridden"
          @click="resetToolDescription"
          >重置</UiButton
        >
      </header>
      <textarea
        class="field"
        :value="draft.description"
        placeholder="说明这个工具能完成什么，以及适合在什么情况下调用。"
        @input="setToolDescription($event.target.value)"
      />
    </section>

    <section class="tool-parameter-descriptions">
      <header>
        <div>
          <h3>参数说明</h3>
          <UiDescription
            >参数结构来自函数签名，描述会写入模型看到的工具
            Schema。</UiDescription
          >
        </div>
        <span>{{ parameters.length }} 个参数</span>
      </header>
      <div v-if="parameters.length" class="tool-parameter-list">
        <article v-for="parameter in parameters" :key="parameter.name">
          <div class="tool-parameter-identity">
            <strong>{{ parameter.name }}</strong>
            <small>
              {{ fieldType(parameter.schema) }} ·
              {{ parameter.required ? "必填" : "可选" }}
              <template v-if="Object.hasOwn(parameter.schema || {}, 'default')">
                · 默认 {{ parameter.schema.default }}
              </template>
            </small>
          </div>
          <input
            class="field"
            :value="parameter.description"
            :placeholder="`说明 ${parameter.name} 的含义`"
            @input="setParameter(parameter, $event.target.value)"
          />
          <UiButton
            variant="ghost"
            :disabled="!parameter.description_overridden"
            @click="resetParameter(parameter)"
            >重置</UiButton
          >
        </article>
      </div>
      <p v-else class="structured-empty">这个工具没有输入参数。</p>
    </section>
  </div>
</template>
