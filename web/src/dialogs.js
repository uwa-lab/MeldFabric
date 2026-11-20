import { shallowRef } from "vue";
export const confirmation = shallowRef(null);
let sequence = 0;
export function confirmAction(message, options = {}) {
  if (confirmation.value) confirmation.value.resolve(false);
  return new Promise((resolve) => {
    confirmation.value = {
      id: ++sequence,
      title: "确认操作",
      confirmText: "确认",
      danger: false,
      ...options,
      message,
      resolve,
    };
  });
}
export function settleConfirmation(value) {
  const current = confirmation.value;
  confirmation.value = null;
  current?.resolve(value);
}
