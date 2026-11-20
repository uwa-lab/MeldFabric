<script setup>
import { computed } from "vue";

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  items: { type: Array, default: () => [] },
  refField: { type: String, required: true },
  emptyAvailable: { type: String, default: "没有可挂载项" },
  emptyAttached: { type: String, default: "尚未挂载" },
  defaults: { type: Object, default: () => ({}) },
  adaptiveHeight: Boolean,
});
const emit = defineEmits(["update:modelValue"]);

const byId = computed(
  () => new Map(props.items.map((item) => [item.id, item])),
);
const attached = computed(() =>
  props.modelValue.map((binding, index) => ({
    binding,
    index,
    item: byId.value.get(binding[props.refField]),
  })),
);
const available = computed(() => {
  const used = new Set(props.modelValue.map((item) => item[props.refField]));
  return props.items.filter(
    (item) => item.enabled !== false && !used.has(item.id),
  );
});

function add(item) {
  emit("update:modelValue", [
    ...props.modelValue,
    { ...props.defaults, enabled: true, [props.refField]: item.id },
  ]);
}

function remove(index) {
  emit(
    "update:modelValue",
    props.modelValue.filter((_, position) => position !== index),
  );
}
</script>

<template>
  <div class="binding-grid" :class="{ 'adaptive-height': adaptiveHeight }">
    <section class="binding-column attached">
      <header>
        <strong>已挂载</strong><span>{{ attached.length }}</span>
      </header>
      <div class="binding-list">
        <article
          v-for="entry in attached"
          :key="entry.binding.id || entry.index"
        >
          <div>
            <strong>{{
              entry.item?.display_name || entry.item?.name || "资源不可用"
            }}</strong>
          </div>
          <UiButton variant="danger" @click="remove(entry.index)">
            移除
          </UiButton>
        </article>
        <p v-if="!attached.length" class="empty-copy">{{ emptyAttached }}</p>
      </div>
    </section>
    <section class="binding-column available">
      <header>
        <strong>可挂载</strong><span>{{ available.length }}</span>
      </header>
      <div class="binding-list">
        <article v-for="item in available" :key="item.id">
          <div>
            <strong>{{ item.display_name || item.name }}</strong>
            <small v-if="item.description">{{ item.description }}</small>
          </div>
          <button type="button" class="text-button" @click="add(item)">
            添加
          </button>
        </article>
        <p v-if="!available.length" class="empty-copy">{{ emptyAvailable }}</p>
      </div>
    </section>
  </div>
</template>
