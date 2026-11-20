<script setup>
import Icon from "./Icon.vue";
defineOptions({ inheritAttrs: false });
defineProps({ modelValue: String });
const emit = defineEmits(["update:modelValue"]);
</script>
<template>
  <div class="search-control">
    <Icon name="search" />
    <input
      v-bind="$attrs"
      class="field"
      type="search"
      :value="modelValue"
      @input="emit('update:modelValue', $event.target.value)"
    />
  </div>
</template>
