<script setup>
defineProps({ items: { type: Array, default: () => [] } });
defineEmits(["dismiss"]);
</script>

<template>
  <div class="toast-stack" aria-live="polite" aria-relevant="additions">
    <TransitionGroup name="toast-list">
      <article
        v-for="item in items"
        :key="item.id"
        class="toast"
        :class="{ bad: item.bad }"
        role="status"
      >
        <span class="toast-indicator" aria-hidden="true" />
        <p>{{ item.message }}</p>
        <button
          type="button"
          aria-label="关闭通知"
          @click="$emit('dismiss', item.id)"
        >
          ×
        </button>
      </article>
    </TransitionGroup>
  </div>
</template>
