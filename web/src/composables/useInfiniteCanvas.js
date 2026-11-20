import { computed, onBeforeUnmount, reactive, ref } from "vue";

export function useInfiniteCanvas({ items, positionOf, updatePosition, nodeWidth, nodeHeight }) {
  const canvas = ref();
  const pan = reactive({ x: 36, y: 46 });
  const interaction = reactive({
    mode: null,
    pointerId: null,
    itemId: null,
    startX: 0,
    startY: 0,
    originX: 0,
    originY: 0,
    moved: false,
  });
  const panTransform = computed(() => `translate(${pan.x} ${pan.y})`);
  const gridX = computed(() => ((pan.x % 24) + 24) % 24);
  const gridY = computed(() => ((pan.y % 24) + 24) % 24);
  let tracking = false;

  function worldPoint(event) {
    const rect = canvas.value?.getBoundingClientRect();
    return {
      x: event.clientX - (rect?.left || 0) - pan.x,
      y: event.clientY - (rect?.top || 0) - pan.y,
    };
  }
  function capture(event) {
    interaction.pointerId = event.pointerId;
    canvas.value?.setPointerCapture?.(event.pointerId);
  }
  function startTracking() {
    if (tracking) return;
    tracking = true;
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  }
  function stopTracking() {
    if (!tracking) return;
    tracking = false;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
  }
  function beginPan(event) {
    if (![0, 1].includes(event.button)) return;
    interaction.mode = "pan";
    interaction.startX = event.clientX;
    interaction.startY = event.clientY;
    interaction.originX = pan.x;
    interaction.originY = pan.y;
    interaction.moved = false;
    capture(event);
    startTracking();
  }
  function beginNodeDrag(event, item) {
    if (event.button !== 0) return;
    event.stopPropagation();
    const position = positionOf(item);
    interaction.mode = "node";
    interaction.itemId = item.id;
    interaction.startX = event.clientX;
    interaction.startY = event.clientY;
    interaction.originX = position.x;
    interaction.originY = position.y;
    interaction.moved = false;
    capture(event);
    startTracking();
  }
  function move(event) {
    const point = worldPoint(event);
    if (interaction.mode === "pan") {
      const dx = event.clientX - interaction.startX;
      const dy = event.clientY - interaction.startY;
      interaction.moved ||= Math.hypot(dx, dy) > 3;
      pan.x = interaction.originX + dx;
      pan.y = interaction.originY + dy;
    }
    if (interaction.mode === "node") {
      const dx = event.clientX - interaction.startX;
      const dy = event.clientY - interaction.startY;
      interaction.moved ||= Math.hypot(dx, dy) > 3;
      const item = items().find((entry) => entry.id === interaction.itemId);
      if (item)
        updatePosition(item, {
          x: interaction.originX + dx,
          y: interaction.originY + dy,
        });
    }
    return point;
  }
  function end(event) {
    if (interaction.pointerId != null) canvas.value?.releasePointerCapture?.(interaction.pointerId);
    interaction.mode = null;
    interaction.pointerId = null;
    interaction.itemId = null;
    stopTracking();
    return worldPoint(event);
  }
  function consumeDrag() {
    const value = interaction.moved;
    interaction.moved = false;
    return value;
  }
  function centerOn(item) {
    const rect = canvas.value?.getBoundingClientRect();
    if (!rect || !item) return;
    const position = positionOf(item);
    pan.x = rect.width / 2 - position.x - nodeWidth / 2;
    pan.y = rect.height / 2 - position.y - nodeHeight / 2;
  }
  function centerWorld() {
    const rect = canvas.value?.getBoundingClientRect();
    return {
      x: (rect?.width || 800) / 2 - pan.x - nodeWidth / 2,
      y: (rect?.height || 500) / 2 - pan.y - nodeHeight / 2,
    };
  }

  onBeforeUnmount(stopTracking);

  return {
    canvas,
    pan,
    interaction,
    panTransform,
    gridX,
    gridY,
    worldPoint,
    beginPan,
    beginNodeDrag,
    move,
    end,
    consumeDrag,
    centerOn,
    centerWorld,
  };
}
