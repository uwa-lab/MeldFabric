<script setup>
import { confirmation, settleConfirmation } from "../dialogs.js";
import ModalFrame from "./ModalFrame.vue";
import UiButton from "./UiButton.vue";
const current = confirmation.value;
function settle(value) {
  if (confirmation.value === current) settleConfirmation(value);
}
</script>
<template>
  <ModalFrame
    class="confirm-dialog"
    title-id="confirmTitle"
    :title="current.title"
    :show-close="false"
    @dismiss="settle"
  >
    <div class="confirm-copy">{{ current.message }}</div>
    <template #footer="{ dismiss, closing }">
      <UiButton
        variant="secondary"
        autofocus
        :disabled="closing"
        @click="dismiss(false)"
      >
        取消
      </UiButton>
      <UiButton
        :variant="current.danger ? 'danger' : 'primary'"
        :disabled="closing"
        @click="dismiss(true)"
      >
        {{ current.confirmText }}
      </UiButton>
    </template>
  </ModalFrame>
</template>
