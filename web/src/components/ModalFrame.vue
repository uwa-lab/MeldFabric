<script setup>
import { useId } from "vue";
import MotionDialog from "./MotionDialog.vue";
import UiButton from "./UiButton.vue";

const props = defineProps({
    title: String,
    description: String,
    titleId: String,
    dismissible: { type: Boolean, default: true },
    showClose: { type: Boolean, default: true },
  }),
  emit = defineEmits(["dismiss"]),
  generatedId = useId(),
  headingId = props.titleId || `modal-title-${generatedId}`;
</script>

<template>
  <MotionDialog
    :labelledby="headingId"
    :dismissible="dismissible"
    @dismiss="emit('dismiss', $event)"
    v-slot="{ dismiss, closing }"
  >
    <header class="dialog-head">
      <div>
        <h2 :id="headingId">{{ title }}</h2>
        <UiDescription v-if="description">{{ description }}</UiDescription>
      </div>
      <UiButton
        v-if="showClose"
        variant="ghost"
        :disabled="closing"
        :aria-label="`关闭${title}`"
        @click="dismiss(false)"
      >
        关闭
      </UiButton>
    </header>
    <div class="dialog-body">
      <slot :dismiss="dismiss" :closing="closing" />
    </div>
    <footer v-if="$slots.footer" class="dialog-foot">
      <slot name="footer" :dismiss="dismiss" :closing="closing" />
    </footer>
  </MotionDialog>
</template>
