<script setup>
import { computed, reactive, ref } from "vue";
import {
  FORBIDDEN_KEYS,
  JSON_TYPES,
  cloneValue,
  defaultValue,
  effectiveSchema,
  finiteChoicesFor,
  isComplex,
  resolveSchema,
  schemaForType,
  schemaType,
  summary,
  typeLabel,
  variantsFor,
} from "../domain/structuredData.js";
import ChoiceControl from "./ChoiceControl.vue";
import ModalFrame from "./ModalFrame.vue";
import NumberStepper from "./NumberStepper.vue";
import SelectControl from "./SelectControl.vue";
import ToggleSwitch from "./ToggleSwitch.vue";

defineOptions({ name: "StructuredField" });
const props = defineProps({
  modelValue: { default: undefined },
  schema: { type: Object, default: () => ({}) },
  rootSchema: { type: Object, default: null },
  optionSources: { type: Object, default: () => ({}) },
  label: String,
  description: String,
  path: { type: String, default: "$" },
  required: Boolean,
  canUnset: Boolean,
  root: Boolean,
  layout: { type: String, default: "standard" },
});
const emit = defineEmits(["update:modelValue", "unset"]);

const rootSchema = computed(() => props.rootSchema || props.schema || {});
const definition = computed(() =>
  effectiveSchema(props.schema, props.modelValue, rootSchema.value),
);
const kind = computed(() =>
  schemaType(definition.value, props.modelValue, rootSchema.value),
);
const variants = computed(() =>
  variantsFor(definition.value, rootSchema.value),
);
const finiteChoices = computed(() =>
  finiteChoicesFor(definition.value, rootSchema.value),
);
const title = computed(
  () => props.label || definition.value.title || props.path.split(".").at(-1),
);
const help = computed(
  () => props.description || definition.value.description || "",
);
const requiredKeys = computed(() => new Set(definition.value.required || []));
const properties = computed(() => definition.value.properties || {});
const objectValue = computed(() =>
  props.modelValue &&
  typeof props.modelValue === "object" &&
  !Array.isArray(props.modelValue)
    ? props.modelValue
    : {},
);
const arrayValue = computed(() =>
  Array.isArray(props.modelValue) ? props.modelValue : [],
);
const additionalSchema = computed(() => {
  const additional = definition.value.additionalProperties;
  return additional && typeof additional === "object" ? additional : {};
});
const allowsAdditional = computed(
  () =>
    definition.value.additionalProperties === true ||
    (definition.value.additionalProperties !== null &&
      typeof definition.value.additionalProperties === "object"),
);
const dynamicEntries = computed(() =>
  Object.entries(objectValue.value).filter(
    ([key]) => !Object.hasOwn(properties.value, key),
  ),
);
const options = computed(() => {
  const source = definition.value["x-meldfabric-option-source"];
  if (source) return props.optionSources[source] || [];
  return (definition.value.enum || []).map((value) => ({
    value,
    label: String(value),
  }));
});

const modal = reactive({
  mode: "",
  index: -1,
  key: "",
  type: "string",
  value: "",
});
const modalError = ref("");
const modalForm = ref();

function update(value) {
  emit("update:modelValue", value);
}

function updateObject(key, value) {
  update({ ...objectValue.value, [key]: value });
}

function objectFieldValue(key, field) {
  if (Object.hasOwn(objectValue.value, key)) return objectValue.value[key];
  const fieldSchema = resolveSchema(field, rootSchema.value);
  return Object.hasOwn(fieldSchema, "default")
    ? cloneValue(fieldSchema.default)
    : undefined;
}

function unsetObject(key) {
  const next = { ...objectValue.value };
  delete next[key];
  update(next);
}

function updateArray(index, value) {
  const next = [...arrayValue.value];
  next[index] = value;
  update(next);
}

function removeArray(index) {
  update(arrayValue.value.filter((_, current) => current !== index));
}

function moveArray(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= arrayValue.value.length) return;
  const next = [...arrayValue.value];
  [next[index], next[target]] = [next[target], next[index]];
  update(next);
}

function selectVariant(index) {
  const variant = variants.value.find((item) => item.index === index);
  if (variant) update(defaultValue(variant.schema, rootSchema.value));
}

function activeVariant() {
  const current = kind.value;
  return (
    variants.value.find(
      (item) =>
        schemaType(item.schema, props.modelValue, rootSchema.value) === current,
    )?.index ?? variants.value[0]?.index
  );
}

function addArray() {
  const itemSchema = resolveSchema(
    definition.value.items || {},
    rootSchema.value,
  );
  const value = defaultValue(itemSchema, rootSchema.value);
  Object.assign(modal, {
    mode: "array-add",
    index: -1,
    key: "",
    type: schemaType(itemSchema, value) || "string",
    value,
  });
}

function editArray(index) {
  Object.assign(modal, {
    mode: "array-edit",
    index,
    key: "",
    type: schemaType(
      definition.value.items || {},
      arrayValue.value[index],
      rootSchema.value,
    ),
    value: cloneValue(arrayValue.value[index]),
  });
}

function addEntry() {
  Object.assign(modal, {
    mode: "object-add",
    index: -1,
    key: "",
    type:
      schemaType(additionalSchema.value, undefined, rootSchema.value) ||
      "string",
    value: defaultValue(additionalSchema.value, rootSchema.value),
  });
  if (!Object.keys(additionalSchema.value).length) {
    modal.type = "string";
    modal.value = "";
  }
}

function editEntry(key, value) {
  Object.assign(modal, {
    mode: "object-edit",
    index: -1,
    key,
    type: schemaType(additionalSchema.value, value, rootSchema.value),
    value: cloneValue(value),
  });
}

function changeModalType(type) {
  modal.type = type;
  modal.value = defaultValue(schemaForType(type));
}

function closeModal() {
  modal.mode = "";
  modalError.value = "";
}

function commitModal() {
  const invalid = [
    ...(modalForm.value?.querySelectorAll("input, textarea") || []),
  ].find((control) => !control.checkValidity());
  if (invalid) {
    invalid.reportValidity();
    return;
  }
  if (modal.mode.startsWith("object")) {
    const key = modal.key.trim();
    if (!key) {
      modalError.value = "请输入字段名称";
      return;
    }
    if (FORBIDDEN_KEYS.has(key)) {
      modalError.value = "该字段名称不能用于 JSON 配置";
      return;
    }
    if (modal.mode === "object-add" && Object.hasOwn(objectValue.value, key)) {
      modalError.value = "字段名称已经存在";
      return;
    }
    updateObject(key, cloneValue(modal.value));
  } else if (modal.mode === "array-add") {
    update([...arrayValue.value, cloneValue(modal.value)]);
  } else if (modal.mode === "array-edit") {
    updateArray(modal.index, cloneValue(modal.value));
  }
  closeModal();
}

function inputString(event) {
  update(event.target.value);
}
</script>

<template>
  <div
    v-if="finiteChoices.length"
    class="structured-choice structured-field-block"
    :class="[layout, { root }]"
  >
    <header
      v-if="!root || layout === 'dialog'"
      class="structured-field-heading"
    >
      <div>
        <strong>{{ title }}</strong>
        <small v-if="help">{{ help }}</small>
      </div>
      <UiButton
        v-if="canUnset && layout !== 'dialog'"
        variant="ghost"
        @click="$emit('unset')"
        >重置</UiButton
      >
      <span v-else-if="required">必填</span>
    </header>
    <ChoiceControl
      :model-value="modelValue"
      :options="finiteChoices"
      :aria-label="title"
      @update:model-value="update"
    />
  </div>

  <div
    v-else-if="variants.length"
    class="structured-union structured-field-block"
    :class="[layout, { root }]"
  >
    <header
      v-if="!root || layout === 'dialog'"
      class="structured-field-heading"
    >
      <div>
        <strong>{{ title }}</strong>
        <small v-if="help">{{ help }}</small>
      </div>
      <span>{{ required ? "必填" : "选择类型" }}</span>
    </header>
    <SelectControl
      :model-value="activeVariant()"
      :options="
        variants.map((item) => ({ value: item.index, label: item.label }))
      "
      @update:model-value="selectVariant"
    />
    <StructuredField
      :model-value="modelValue"
      :schema="
        variants.find((item) => item.index === activeVariant())?.schema || {}
      "
      :root-schema="rootSchema"
      :option-sources="optionSources"
      :path="path"
      :layout="layout"
      root
      @update:model-value="update"
    />
  </div>

  <section
    v-else-if="kind === 'object'"
    class="structured-object"
    :class="[layout, { root }]"
  >
    <header v-if="!root" class="structured-group-heading">
      <div>
        <strong>{{ title }}</strong>
        <small v-if="help">{{ help }}</small>
      </div>
      <UiButton
        v-if="canUnset && layout !== 'dialog'"
        variant="ghost"
        @click="$emit('unset')"
        >重置</UiButton
      >
    </header>
    <div class="structured-object-fields">
      <StructuredField
        v-for="(field, name) in properties"
        :key="`${path}.${name}`"
        :model-value="objectFieldValue(name, field)"
        :schema="field"
        :root-schema="rootSchema"
        :option-sources="optionSources"
        :label="resolveSchema(field, rootSchema).title || name"
        :description="resolveSchema(field, rootSchema).description"
        :path="`${path}.${name}`"
        :required="requiredKeys.has(name)"
        :can-unset="!requiredKeys.has(name) && Object.hasOwn(objectValue, name)"
        :layout="layout"
        @update:model-value="updateObject(name, $event)"
        @unset="unsetObject(name)"
      />
    </div>

    <section
      v-if="allowsAdditional || dynamicEntries.length"
      class="structured-collection"
    >
      <header class="structured-collection-heading">
        <div>
          <strong>{{
            Object.keys(properties).length ? "附加字段" : "字段"
          }}</strong>
          <small>以明确的名称和类型保存自定义配置。</small>
        </div>
        <div>
          <span>{{ dynamicEntries.length }} 项</span>
          <UiButton
            v-if="allowsAdditional"
            variant="secondary"
            @click="addEntry"
            >添加字段</UiButton
          >
        </div>
      </header>
      <div v-if="dynamicEntries.length" class="structured-entry-list">
        <article v-for="([key, value], index) in dynamicEntries" :key="key">
          <span class="structured-entry-index">{{
            String(index + 1).padStart(2, "0")
          }}</span>
          <div class="structured-entry-copy">
            <strong>{{ key }}</strong>
            <small>{{ typeLabel(additionalSchema, value, rootSchema) }}</small>
          </div>
          <UiButton
            v-if="isComplex(additionalSchema, value, rootSchema)"
            class="structured-entry-summary"
            variant="plain"
            @click="editEntry(key, value)"
            >{{ summary(value) }}</UiButton
          >
          <StructuredField
            v-else
            :model-value="value"
            :schema="additionalSchema"
            :root-schema="rootSchema"
            :option-sources="optionSources"
            :path="`${path}.${key}`"
            layout="collection"
            root
            @update:model-value="updateObject(key, $event)"
          />
          <div class="structured-entry-actions">
            <UiButton
              v-if="isComplex(additionalSchema, value, rootSchema)"
              variant="ghost"
              @click="editEntry(key, value)"
              >编辑</UiButton
            >
            <UiButton variant="danger" @click="unsetObject(key)">删除</UiButton>
          </div>
        </article>
      </div>
      <p v-else class="structured-empty">尚未添加自定义字段。</p>
    </section>
  </section>

  <section
    v-else-if="kind === 'array'"
    class="structured-array structured-field-block"
    :class="[layout, { root }]"
  >
    <header class="structured-collection-heading">
      <div>
        <strong>{{ !root || layout === "dialog" ? title : "条目" }}</strong>
        <small v-if="help">{{ help }}</small>
        <small v-else>按顺序管理列表中的内容。</small>
      </div>
      <div>
        <span>{{ arrayValue.length }} 项</span>
        <UiButton
          v-if="canUnset && layout !== 'dialog'"
          variant="ghost"
          @click="$emit('unset')"
          >重置</UiButton
        >
        <UiButton variant="secondary" @click="addArray">添加条目</UiButton>
      </div>
    </header>
    <div v-if="arrayValue.length" class="structured-entry-list">
      <article v-for="(value, index) in arrayValue" :key="`${path}.${index}`">
        <span class="structured-entry-index">{{
          String(index + 1).padStart(2, "0")
        }}</span>
        <div
          v-if="isComplex(definition.items || {}, value, rootSchema)"
          class="structured-entry-copy"
        >
          <strong>{{ summary(value) }}</strong>
          <small>{{
            typeLabel(definition.items || {}, value, rootSchema)
          }}</small>
        </div>
        <StructuredField
          v-else
          :model-value="value"
          :schema="definition.items || {}"
          :root-schema="rootSchema"
          :option-sources="optionSources"
          :path="`${path}[${index}]`"
          layout="collection"
          root
          @update:model-value="updateArray(index, $event)"
        />
        <div class="structured-entry-actions">
          <UiButton
            variant="ghost"
            :disabled="index === 0"
            aria-label="向上移动"
            @click="moveArray(index, -1)"
            >↑</UiButton
          >
          <UiButton
            variant="ghost"
            :disabled="index === arrayValue.length - 1"
            aria-label="向下移动"
            @click="moveArray(index, 1)"
            >↓</UiButton
          >
          <UiButton
            v-if="isComplex(definition.items || {}, value, rootSchema)"
            variant="ghost"
            @click="editArray(index)"
            >编辑</UiButton
          >
          <UiButton variant="danger" @click="removeArray(index)">删除</UiButton>
        </div>
      </article>
    </div>
    <p v-else class="structured-empty">还没有任何条目。</p>
  </section>

  <div v-else class="structured-field-row" :class="[layout, { root }]">
    <header
      v-if="!root || layout === 'dialog'"
      class="structured-field-heading"
    >
      <div>
        <strong>{{ title }}</strong>
        <small v-if="help">{{ help }}</small>
      </div>
      <UiButton
        v-if="canUnset && layout !== 'dialog'"
        variant="ghost"
        @click="$emit('unset')"
        >重置</UiButton
      >
      <span v-else-if="required">必填</span>
    </header>
    <div class="structured-control">
      <SelectControl
        v-if="options.length"
        :model-value="modelValue"
        :options="options"
        :required="required"
        :aria-label="title"
        @update:model-value="update"
      />
      <ToggleSwitch
        v-else-if="kind === 'boolean'"
        :model-value="Boolean(modelValue)"
        :label="Boolean(modelValue) ? '已开启' : '已关闭'"
        :aria-label="title"
        @update:model-value="update"
      />
      <NumberStepper
        v-else-if="['integer', 'number'].includes(kind)"
        :model-value="typeof modelValue === 'number' ? modelValue : null"
        :integer="kind === 'integer'"
        :minimum="definition.minimum ?? definition.exclusiveMinimum"
        :maximum="definition.maximum ?? definition.exclusiveMaximum"
        :step="definition.multipleOf"
        :required="required"
        :aria-label="title"
        @update:model-value="update"
      />
      <span v-else-if="kind === 'null'" class="structured-null">null</span>
      <textarea
        v-else-if="
          definition.format === 'multiline' ||
          definition['x-meldfabric-widget'] === 'textarea'
        "
        class="field"
        :value="modelValue ?? ''"
        :required="required"
        :minlength="definition.minLength"
        :maxlength="definition.maxLength"
        :aria-label="title"
        @input="inputString"
      />
      <input
        v-else
        class="field control-wide"
        :type="
          definition.format === 'password' || definition.writeOnly
            ? 'password'
            : definition.format === 'uri'
              ? 'url'
              : 'text'
        "
        :value="modelValue ?? ''"
        :required="required"
        :minlength="definition.minLength"
        :maxlength="definition.maxLength"
        :pattern="definition.pattern"
        :aria-label="title"
        @input="inputString"
      />
    </div>
  </div>

  <ModalFrame
    v-if="modal.mode"
    :title="modal.mode.includes('add') ? '添加结构化内容' : '编辑结构化内容'"
    description="修改先保存在当前页面草稿中，点击页面保存后才会写入配置。"
    @dismiss="closeModal"
  >
    <div ref="modalForm" class="structured-modal-form">
      <label v-if="modal.mode.startsWith('object')" class="structured-modal-key"
        ><span>字段名称</span
        ><small>名称用于程序读取，并作为 JSON 对象的键。</small
        ><input
          v-model="modal.key"
          class="field"
          :disabled="modal.mode === 'object-edit'"
          required
      /></label>
      <label
        class="structured-modal-key"
        v-if="
          !Object.keys(
            modal.mode.startsWith('object')
              ? additionalSchema
              : definition.items || {},
          ).length
        "
      >
        <span>内容类型</span>
        <small>选择这个字段保存的数据结构。</small>
        <SelectControl
          :model-value="modal.type"
          :options="JSON_TYPES"
          @update:model-value="changeModalType"
        />
      </label>
      <StructuredField
        v-model="modal.value"
        :schema="
          Object.keys(
            modal.mode.startsWith('object')
              ? additionalSchema
              : definition.items || {},
          ).length
            ? modal.mode.startsWith('object')
              ? additionalSchema
              : definition.items || {}
            : schemaForType(modal.type)
        "
        :root-schema="rootSchema"
        :option-sources="optionSources"
        :label="modal.mode.startsWith('object') ? '字段值' : '条目内容'"
        layout="dialog"
        path="$.modal"
        root
      />
      <p v-if="modalError" class="field-error">{{ modalError }}</p>
      <div class="dialog-actions">
        <UiButton variant="secondary" @click="closeModal">取消</UiButton>
        <UiButton variant="primary" @click="commitModal">确认</UiButton>
      </div>
    </div>
  </ModalFrame>
</template>
