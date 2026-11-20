<script setup>
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  useId,
} from "vue";
import { notify } from "../store.js";
import { useInfiniteCanvas } from "../composables/useInfiniteCanvas.js";
import ContextMenu from "./ContextMenu.vue";
import ModalFrame from "./ModalFrame.vue";
import SelectControl from "./SelectControl.vue";
import TechnicalId from "./TechnicalId.vue";

const props = defineProps({
  draft: { type: Object, required: true },
  agents: { type: Array, default: () => [] },
});

const cardWidth = 226;
const cardHeight = 108;
const gapX = 96;
const gapY = 54;
const markerId = `workflow-arrow-${useId().replaceAll(":", "")}`;
const editingAgent = ref(null);
const editingTask = ref(null);
const connectionSource = ref(null);
const dependencyTarget = ref(null);
const cursor = reactive({ x: 0, y: 0 });
const menu = reactive({ open: false, x: 0, y: 0, task: null });

const agentOptions = computed(() =>
  props.agents.map((item) => ({
    value: item.id,
    label: item.display_name || item.name,
  })),
);
const automaticPositions = computed(() => {
  const levels = new Map();
  function level(task, trail = new Set()) {
    if (levels.has(task.id)) return levels.get(task.id);
    if (trail.has(task.id)) return 0;
    const nextTrail = new Set(trail).add(task.id);
    const dependencies = task.dependency_refs
      .map((id) => props.draft.tasks.find((item) => item.id === id))
      .filter(Boolean);
    const value = dependencies.length
      ? Math.max(...dependencies.map((item) => level(item, nextTrail))) + 1
      : 0;
    levels.set(task.id, value);
    return value;
  }
  props.draft.tasks.forEach((task) => level(task));
  const rows = new Map();
  const result = new Map();
  props.draft.tasks.forEach((task) => {
    const column = levels.get(task.id) || 0;
    const row = rows.get(column) || 0;
    rows.set(column, row + 1);
    result.set(task.id, {
      x: column * (cardWidth + gapX),
      y: row * (cardHeight + gapY),
    });
  });
  return result;
});

function positionOf(task) {
  return (
    task.position || automaticPositions.value.get(task.id) || { x: 0, y: 0 }
  );
}
function updatePosition(task, position) {
  task.position = { x: Math.round(position.x), y: Math.round(position.y) };
}
const canvasTools = useInfiniteCanvas({
  items: () => props.draft.tasks,
  positionOf,
  updatePosition,
  nodeWidth: cardWidth,
  nodeHeight: cardHeight,
});

function bezier(source, target) {
  const direction = target.x >= source.x ? 1 : -1;
  const bend = Math.max(58, Math.abs(target.x - source.x) * 0.48);
  return `M ${source.x} ${source.y} C ${source.x + bend * direction} ${source.y}, ${target.x - bend * direction} ${target.y}, ${target.x} ${target.y}`;
}
const dependencyLines = computed(() =>
  props.draft.tasks.flatMap((task) =>
    task.dependency_refs
      .map((dependency) => {
        const sourceTask = props.draft.tasks.find(
          (item) => item.id === dependency,
        );
        if (!sourceTask) return null;
        const source = positionOf(sourceTask);
        const target = positionOf(task);
        return {
          id: `${dependency}:${task.id}`,
          path: bezier(
            { x: source.x + cardWidth, y: source.y + cardHeight / 2 },
            { x: target.x, y: target.y + cardHeight / 2 },
          ),
        };
      })
      .filter(Boolean),
  ),
);
const temporaryLine = computed(() => {
  const targetTask = props.draft.tasks.find(
    (item) => item.id === dependencyTarget.value,
  );
  if (targetTask) {
    const position = positionOf(targetTask);
    return bezier(cursor, {
      x: position.x,
      y: position.y + cardHeight / 2,
    });
  }
  const task = props.draft.tasks.find(
    (item) => item.id === connectionSource.value,
  );
  if (!task) return null;
  const position = positionOf(task);
  return bezier(
    { x: position.x + cardWidth, y: position.y + cardHeight / 2 },
    cursor,
  );
});
const contextItems = computed(() => {
  const task = menu.task;
  const isStart = task && !task.dependency_refs.length;
  const isPrimary = task?.id === props.draft.primary_task_ref;
  const startCount = props.draft.tasks.filter(
    (item) => !item.dependency_refs.length,
  ).length;
  return [
    { id: "edit", label: "编辑任务" },
    {
      id: isStart ? "unset_start" : "set_start",
      label: isStart ? "添加前置任务" : "设为起始任务",
      disabled: isStart && (startCount <= 1 || isPrimary),
      hint: isPrimary
        ? "请先指定其他主入口"
        : isStart && startCount <= 1
          ? "至少保留一个起始任务"
          : null,
    },
    {
      id: "primary",
      label: isPrimary ? "当前主入口" : "设为主入口",
      disabled: isPrimary || !isStart,
    },
    { id: "connect", label: "连接后续任务" },
    { id: "copy_id", label: "复制任务 ID" },
    { separator: true },
    {
      id: "delete",
      label: "删除任务",
      danger: true,
      disabled: props.draft.tasks.length === 1,
    },
  ];
});

function generated() {
  return `task_${crypto.randomUUID().replaceAll("-", "")}`;
}
function agentName(task) {
  const agent = props.agents.find((item) => item.id === task.agent_ref);
  return agent?.display_name || agent?.name || "选择执行 Agent";
}
function addTask() {
  props.draft.tasks.push({
    id: generated(),
    agent_ref: props.agents[0]?.id || null,
    instruction: "处理输入并完成当前任务",
    dependency_refs: [],
    position: canvasTools.centerWorld(),
  });
}
function removeTask(task) {
  props.draft.tasks = props.draft.tasks.filter((item) => item.id !== task.id);
  props.draft.tasks.forEach((item) => {
    item.dependency_refs = item.dependency_refs.filter((id) => id !== task.id);
  });
  normalizePrimary();
  if (editingTask.value?.id === task.id) editingTask.value = null;
  if (connectionSource.value === task.id) connectionSource.value = null;
  if (dependencyTarget.value === task.id) dependencyTarget.value = null;
}
function wouldCycle(from, to) {
  const dependents = new Map(props.draft.tasks.map((task) => [task.id, []]));
  props.draft.tasks.forEach((task) =>
    task.dependency_refs.forEach((id) => dependents.get(id)?.push(task.id)),
  );
  const stack = [to];
  const seen = new Set();
  while (stack.length) {
    const current = stack.pop();
    if (current === from) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    stack.push(...(dependents.get(current) || []));
  }
  return false;
}
function connectTo(task) {
  const source = connectionSource.value;
  if (!source) return;
  if (source === task.id) {
    notify("任务不能依赖自身", true);
    return;
  }
  if (task.id === props.draft.primary_task_ref) {
    notify("主入口必须保持为起始任务，请先指定其他主入口", true);
    connectionSource.value = null;
    return;
  }
  if (task.dependency_refs.includes(source)) {
    notify("这条任务依赖已经存在", true);
    connectionSource.value = null;
    return;
  }
  if (wouldCycle(source, task.id)) {
    notify("Workflow 必须保持无环，这条连接会形成循环", true);
    return;
  }
  task.dependency_refs.push(source);
  connectionSource.value = null;
}
function taskClick(task) {
  if (canvasTools.consumeDrag()) return;
  if (dependencyTarget.value) {
    connectAsPredecessor(task);
    return;
  }
  if (connectionSource.value) connectTo(task);
}
function taskPointerDown(event, task) {
  if (connectionSource.value || dependencyTarget.value) {
    event.stopPropagation();
    return;
  }
  canvasTools.beginNodeDrag(event, task);
}
function startConnection(task) {
  dependencyTarget.value = null;
  connectionSource.value = task.id;
  const position = positionOf(task);
  cursor.x = position.x + cardWidth + 36;
  cursor.y = position.y + cardHeight / 2;
}
function setStart(task) {
  task.dependency_refs = [];
}
function setPrimary(task) {
  if (!task.dependency_refs.length) props.draft.primary_task_ref = task.id;
}
function normalizePrimary() {
  const primary = props.draft.tasks.find(
    (task) =>
      task.id === props.draft.primary_task_ref && !task.dependency_refs.length,
  );
  if (!primary)
    props.draft.primary_task_ref =
      props.draft.tasks.find((task) => !task.dependency_refs.length)?.id ||
      null;
}
function unsetStart(task) {
  connectionSource.value = null;
  dependencyTarget.value = task.id;
  const position = positionOf(task);
  cursor.x = position.x - 36;
  cursor.y = position.y + cardHeight / 2;
}
function connectAsPredecessor(task) {
  const target = props.draft.tasks.find(
    (item) => item.id === dependencyTarget.value,
  );
  if (!target) return;
  if (task.id === target.id) {
    notify("任务不能依赖自身", true);
    return;
  }
  if (target.dependency_refs.includes(task.id)) {
    notify("这个前置关系已经存在", true);
    dependencyTarget.value = null;
    return;
  }
  if (wouldCycle(task.id, target.id)) {
    notify("这个前置关系会形成循环，请选择其他任务", true);
    return;
  }
  target.dependency_refs.push(task.id);
  dependencyTarget.value = null;
}
function openMenu(event, task) {
  Object.assign(menu, { open: true, x: event.x, y: event.y, task });
}
function chooseMenu(action) {
  const task = menu.task;
  menu.open = false;
  if (!task) return;
  if (action === "edit") editingTask.value = task;
  if (action === "set_start") setStart(task);
  if (action === "unset_start") unsetStart(task);
  if (action === "primary") setPrimary(task);
  if (action === "connect") startConnection(task);
  if (action === "copy_id") copyTaskId(task);
  if (action === "delete") removeTask(task);
}
async function copyTaskId(task) {
  try {
    await navigator.clipboard.writeText(task.id);
    notify("任务 ID 已复制");
  } catch {
    notify("无法访问剪贴板，请手动复制", true);
  }
}
function removeDependency(task, dependency) {
  task.dependency_refs = task.dependency_refs.filter((id) => id !== dependency);
}
function resetLayout() {
  props.draft.tasks.forEach((task) => {
    const automatic = automaticPositions.value.get(task.id);
    if (automatic) updatePosition(task, automatic);
  });
  nextTick(() =>
    canvasTools.centerOn(
      props.draft.tasks.find(
        (task) => task.id === props.draft.primary_task_ref,
      ) || props.draft.tasks[0],
    ),
  );
}
function move(event) {
  const point = canvasTools.move(event);
  cursor.x = point.x;
  cursor.y = point.y;
}
function escape(event) {
  if (event.key === "Escape") {
    connectionSource.value = null;
    dependencyTarget.value = null;
    editingAgent.value = null;
  }
}

onMounted(() => {
  window.addEventListener("keydown", escape);
  nextTick(() =>
    canvasTools.centerOn(
      props.draft.tasks.find(
        (task) => task.id === props.draft.primary_task_ref,
      ) || props.draft.tasks[0],
    ),
  );
});
onBeforeUnmount(() => window.removeEventListener("keydown", escape));
</script>

<template>
  <div class="blueprint-builder workflow-builder">
    <div class="blueprint-toolbar">
      <div class="button-row">
        <UiButton variant="secondary" @click="addTask">添加任务</UiButton>
        <UiButton variant="ghost" @click="resetLayout">复位</UiButton>
      </div>
      <span v-if="connectionSource" class="connection-hint"
        >选择后续任务 · Esc 取消</span
      ><span v-else-if="dependencyTarget" class="connection-hint"
        >选择一个前置任务 · Esc 取消</span
      >
      <span v-else class="canvas-hint">拖动画布 · 拖拽任务 · 右击打开操作</span>
    </div>

    <svg
      :ref="canvasTools.canvas"
      class="blueprint-canvas"
      :class="{ connecting: connectionSource || dependencyTarget }"
      @pointermove="move"
      @pointerup="canvasTools.end"
      @pointercancel="canvasTools.end"
    >
      <defs>
        <pattern
          :id="`${markerId}-grid`"
          width="24"
          height="24"
          patternUnits="userSpaceOnUse"
          :x="canvasTools.gridX.value"
          :y="canvasTools.gridY.value"
        >
          <circle cx="1" cy="1" r="0.8" />
        </pattern>
        <marker
          :id="markerId"
          markerWidth="8"
          markerHeight="8"
          refX="7"
          refY="4"
          orient="auto"
        >
          <path d="M0,0 L8,4 L0,8 Z" />
        </marker>
      </defs>
      <rect
        class="blueprint-hit-surface"
        width="100%"
        height="100%"
        :fill="`url(#${markerId}-grid)`"
        @pointerdown="canvasTools.beginPan"
        @click="
          connectionSource = null;
          dependencyTarget = null;
        "
      />
      <g :transform="canvasTools.panTransform.value">
        <g class="blueprint-edges" aria-hidden="true">
          <path
            v-for="line in dependencyLines"
            :key="line.id"
            :d="line.path"
            :marker-end="`url(#${markerId})`"
          />
          <path v-if="temporaryLine" class="temporary" :d="temporaryLine" />
        </g>
        <foreignObject
          v-for="(task, index) in draft.tasks"
          :key="task.id"
          :x="positionOf(task).x"
          :y="positionOf(task).y"
          :width="cardWidth"
          :height="cardHeight"
        >
          <article
            v-context-menu="(event) => openMenu(event, task)"
            class="blueprint-node workflow-node"
            :class="{
              root: !task.dependency_refs.length,
              source: connectionSource === task.id,
              target: dependencyTarget === task.id,
            }"
            @pointerdown="taskPointerDown($event, task)"
            @click.stop="taskClick(task)"
          >
            <div class="blueprint-node-meta">
              <span>TASK {{ index + 1 }}</span>
              <span v-if="task.id === draft.primary_task_ref" class="entry-mark"
                >主入口</span
              ><span
                v-else-if="!task.dependency_refs.length"
                class="entry-mark secondary-entry"
                >起始</span
              >
            </div>
            <div class="blueprint-node-title">
              <SelectControl
                v-if="editingAgent === task.id"
                v-model="task.agent_ref"
                :options="agentOptions"
                aria-label="选择任务 Agent"
                @pointerdown.stop
                @click.stop
                @change="editingAgent = null"
              />
              <strong v-else>{{ agentName(task) }}</strong>
              <button
                v-if="editingAgent !== task.id"
                type="button"
                title="更换执行 Agent"
                aria-label="更换执行 Agent"
                @pointerdown.stop
                @click.stop="editingAgent = task.id"
              >
                ↻
              </button>
            </div>
            <small>{{ task.instruction || "右击编辑任务指令" }}</small>
          </article>
        </foreignObject>
      </g>
    </svg>

    <ContextMenu
      :open="menu.open"
      :x="menu.x"
      :y="menu.y"
      :items="contextItems"
      @select="chooseMenu"
      @dismiss="menu.open = false"
    />

    <ModalFrame
      v-if="editingTask"
      title="编辑工作流任务"
      description="设置当前节点的指令与依赖关系。"
      @dismiss="editingTask = null"
    >
      <div class="workflow-task-editor">
        <TechnicalId label="Task ID" :value="editingTask.id" />
        <label
          >执行 Agent<SelectControl
            v-model="editingTask.agent_ref"
            :options="agentOptions"
        /></label>
        <label
          >任务指令<textarea
            v-model="editingTask.instruction"
            class="field"
            rows="8"
          />
        </label>
        <div v-if="editingTask.dependency_refs.length" class="dependency-chips">
          <span>前置任务</span>
          <button
            v-for="id in editingTask.dependency_refs"
            :key="id"
            type="button"
            @click="removeDependency(editingTask, id)"
          >
            TASK {{ draft.tasks.findIndex((item) => item.id === id) + 1 }} ×
          </button>
        </div>
      </div>
    </ModalFrame>
  </div>
</template>
