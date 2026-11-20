<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId } from "vue";
import { notify, state } from "../store.js";
import { useInfiniteCanvas } from "../composables/useInfiniteCanvas.js";
import ContextMenu from "./ContextMenu.vue";
import ConfigSchemaForm from "./ConfigSchemaForm.vue";
import ModalFrame from "./ModalFrame.vue";
import SelectControl from "./SelectControl.vue";
import TechnicalId from "./TechnicalId.vue";

const props = defineProps({
  agents: { type: Array, default: () => [] },
  remotes: { type: Array, default: () => [] },
  conditionOptions: { type: Array, default: () => [] },
  conditionDefinitions: { type: Array, default: () => [] },
});
const draft = defineModel("draft", { type: Object, required: true });

const cardWidth = 214;
const cardHeight = 98;
const gapX = 92;
const gapY = 54;
const markerId = `graph-arrow-${useId().replaceAll(":", "")}`;
const editingNode = ref(null);
const connectionSource = ref(null);
const cursor = reactive({ x: 0, y: 0 });
const menu = reactive({ open: false, x: 0, y: 0, node: null });
const configuringEdge = ref(null);

const graphOptionSources = computed(() => ({
  graph_nodes: draft.value.nodes.map((node) => ({
    value: node.id,
    label: nodeName(node),
    description: node.id,
  })),
  graph_agent_nodes: draft.value.nodes
    .filter((node) => node.target_type === "agent")
    .map((node) => ({
      value: node.id,
      label: nodeName(node),
      description: node.id,
    })),
  graph_remote_nodes: draft.value.nodes
    .filter((node) => node.target_type === "remote_agent")
    .map((node) => ({
      value: node.id,
      label: nodeName(node),
      description: node.id,
    })),
}));

function conditionDefinition(edge) {
  return props.conditionDefinitions.find((item) => item.id === edge.condition_registration_id);
}

function setCondition(edge, value) {
  edge.condition_registration_id = value;
  edge.condition_registration_digest = null;
  edge.condition_config = {};
}

const automaticPositions = computed(() => {
  const nodeMap = new Map(draft.value.nodes.map((node) => [node.id, node]));
  const levels = new Map();
  function level(nodeId, trail = new Set()) {
    if (levels.has(nodeId)) return levels.get(nodeId);
    if (trail.has(nodeId)) return 0;
    const incoming = draft.value.edges
      .filter((edge) => edge.target === nodeId && nodeMap.has(edge.source))
      .map((edge) => edge.source);
    const nextTrail = new Set(trail).add(nodeId);
    const value = incoming.length
      ? Math.max(...incoming.map((source) => level(source, nextTrail))) + 1
      : 0;
    levels.set(nodeId, value);
    return value;
  }
  draft.value.nodes.forEach((node) => level(node.id));
  const rows = new Map();
  const result = new Map();
  draft.value.nodes.forEach((node) => {
    const column = levels.get(node.id) || 0;
    const row = rows.get(column) || 0;
    rows.set(column, row + 1);
    result.set(node.id, {
      x: column * (cardWidth + gapX),
      y: row * (cardHeight + gapY),
    });
  });
  return result;
});

function positionOf(node) {
  return node.position || automaticPositions.value.get(node.id) || { x: 0, y: 0 };
}
function updatePosition(node, position) {
  node.position = { x: Math.round(position.x), y: Math.round(position.y) };
}

const canvasTools = useInfiniteCanvas({
  items: () => draft.value.nodes,
  positionOf,
  updatePosition,
  nodeWidth: cardWidth,
  nodeHeight: cardHeight,
});

function bezier(source, target) {
  const direction = target.x >= source.x ? 1 : -1;
  const bend = Math.max(54, Math.abs(target.x - source.x) * 0.48);
  return `M ${source.x} ${source.y} C ${source.x + bend * direction} ${source.y}, ${target.x - bend * direction} ${target.y}, ${target.x} ${target.y}`;
}

const edgeLines = computed(() =>
  draft.value.edges
    .map((edge) => {
      const sourceNode = draft.value.nodes.find((node) => node.id === edge.source);
      const targetNode = draft.value.nodes.find((node) => node.id === edge.target);
      if (!sourceNode || !targetNode) return null;
      const source = positionOf(sourceNode);
      const target = positionOf(targetNode);
      return {
        ...edge,
        path: bezier(
          { x: source.x + cardWidth, y: source.y + cardHeight / 2 },
          { x: target.x, y: target.y + cardHeight / 2 },
        ),
      };
    })
    .filter(Boolean),
);

const temporaryLine = computed(() => {
  const node = draft.value.nodes.find((item) => item.id === connectionSource.value);
  if (!node) return null;
  const position = positionOf(node);
  return bezier({ x: position.x + cardWidth, y: position.y + cardHeight / 2 }, cursor);
});

const contextItems = computed(() => {
  const node = menu.node;
  if (!node) return [];
  const isEntry = draft.value.entry_points.includes(node.id);
  const isPrimary = draft.value.primary_entry_point === node.id;
  const localNodesAfterDelete = draft.value.nodes.filter(
    (item) => item.id !== node.id && item.target_type === "agent",
  );
  return [
    {
      id: "entry",
      label: isEntry ? "取消启动节点" : "设为启动节点",
      disabled: isEntry && (draft.value.entry_points.length === 1 || isPrimary),
      hint: isPrimary ? "请先指定其他主入口" : null,
    },
    {
      id: "primary",
      label: isPrimary ? "当前主入口" : "设为主入口",
      disabled: isPrimary || !isEntry || node.target_type !== "agent",
    },
    { id: "connect", label: "连接节点" },
    { id: "copy_id", label: "复制节点 ID" },
    { separator: true },
    {
      id: "delete",
      label: "删除节点",
      danger: true,
      disabled: draft.value.nodes.length === 1 || !localNodesAfterDelete.length,
    },
  ];
});

function generated(prefix) {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}
function nodePool(node) {
  return node.target_type === "remote_agent" ? props.remotes : props.agents;
}
function nodeOptions(node) {
  return nodePool(node).map((item) => ({
    value: item.id,
    label: item.display_name || item.name,
  }));
}
function nodeName(node) {
  const target = nodePool(node).find((item) => item.id === node.target_ref);
  return target?.display_name || target?.name || "选择执行目标";
}
function edgeNodeName(nodeId) {
  const node = draft.value.nodes.find((item) => item.id === nodeId);
  return node ? nodeName(node) : "节点不可用";
}
function normalizeEntries() {
  draft.value.entry_points = [
    ...new Set(
      draft.value.entry_points.filter((id) => draft.value.nodes.some((node) => node.id === id)),
    ),
  ];
  if (!draft.value.entry_points.length) {
    const fallback = draft.value.nodes.find((node) => node.target_type === "agent");
    if (fallback) draft.value.entry_points.push(fallback.id);
  }
  if (
    !draft.value.entry_points.some(
      (id) => draft.value.nodes.find((node) => node.id === id)?.target_type === "agent",
    )
  ) {
    const fallback = draft.value.nodes.find((node) => node.target_type === "agent");
    if (fallback) draft.value.entry_points.unshift(fallback.id);
  }
  const primary = draft.value.nodes.find(
    (node) =>
      node.id === draft.value.primary_entry_point &&
      node.target_type === "agent" &&
      draft.value.entry_points.includes(node.id),
  );
  if (!primary) {
    const fallback = draft.value.nodes.find(
      (node) => node.target_type === "agent" && draft.value.entry_points.includes(node.id),
    );
    draft.value.primary_entry_point = fallback?.id || null;
  }
}
function addNode(targetType) {
  const pool = targetType === "remote_agent" ? props.remotes : props.agents;
  const node = {
    id: generated("node"),
    target_type: targetType,
    target_ref: pool[0]?.id || null,
    position: canvasTools.centerWorld(),
  };
  draft.value.nodes.push(node);
  if (!draft.value.entry_points.length && targetType === "agent") {
    draft.value.entry_points.push(node.id);
    draft.value.primary_entry_point = node.id;
  }
}
function removeNode(node) {
  draft.value.nodes = draft.value.nodes.filter((item) => item.id !== node.id);
  draft.value.edges = draft.value.edges.filter(
    (edge) => edge.source !== node.id && edge.target !== node.id,
  );
  draft.value.entry_points = draft.value.entry_points.filter((id) => id !== node.id);
  normalizeEntries();
  if (editingNode.value === node.id) editingNode.value = null;
  if (connectionSource.value === node.id) connectionSource.value = null;
}
function toggleEntry(node) {
  if (draft.value.entry_points.includes(node.id)) {
    draft.value.entry_points = draft.value.entry_points.filter((id) => id !== node.id);
  } else {
    draft.value.entry_points.push(node.id);
  }
  normalizeEntries();
}
function setPrimary(node) {
  if (node.target_type === "agent" && draft.value.entry_points.includes(node.id))
    draft.value.primary_entry_point = node.id;
}
function connectTo(node) {
  const source = connectionSource.value;
  if (!source) return;
  if (source === node.id) {
    notify("节点不能连接自身", true);
    return;
  }
  if (draft.value.edges.some((edge) => edge.source === source && edge.target === node.id)) {
    notify("这条连接已经存在", true);
    connectionSource.value = null;
    return;
  }
  draft.value.edges.push({
    id: generated("edge"),
    source,
    target: node.id,
    condition_registration_id: null,
    condition_registration_digest: null,
    condition_config: {},
  });
  connectionSource.value = null;
}
function nodeClick(node) {
  if (canvasTools.consumeDrag()) return;
  if (connectionSource.value) connectTo(node);
}
function nodePointerDown(event, node) {
  if (connectionSource.value) {
    event.stopPropagation();
    return;
  }
  canvasTools.beginNodeDrag(event, node);
}
function startConnection(node) {
  connectionSource.value = node.id;
  const position = positionOf(node);
  cursor.x = position.x + cardWidth + 36;
  cursor.y = position.y + cardHeight / 2;
}
function openMenu(event, node) {
  Object.assign(menu, { open: true, x: event.x, y: event.y, node });
}
function chooseMenu(action) {
  const node = menu.node;
  menu.open = false;
  if (!node) return;
  if (action === "entry") toggleEntry(node);
  if (action === "primary") setPrimary(node);
  if (action === "connect") startConnection(node);
  if (action === "copy_id") copyNodeId(node);
  if (action === "delete") removeNode(node);
}
async function copyNodeId(node) {
  try {
    await navigator.clipboard.writeText(node.id);
    notify("节点 ID 已复制");
  } catch {
    notify("无法访问剪贴板，请手动复制", true);
  }
}
function resetLayout() {
  draft.value.nodes.forEach((node) => {
    const automatic = automaticPositions.value.get(node.id);
    if (automatic) updatePosition(node, automatic);
  });
  nextTick(() =>
    canvasTools.centerOn(
      draft.value.nodes.find((node) => node.id === draft.value.primary_entry_point) ||
        draft.value.nodes[0],
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
    editingNode.value = null;
  }
}

onMounted(() => {
  window.addEventListener("keydown", escape);
  nextTick(() =>
    canvasTools.centerOn(
      draft.value.nodes.find((node) => node.id === draft.value.primary_entry_point) ||
        draft.value.nodes[0],
    ),
  );
});
onBeforeUnmount(() => window.removeEventListener("keydown", escape));
</script>

<template>
  <div class="blueprint-builder">
    <div class="blueprint-toolbar">
      <div class="button-row">
        <UiButton variant="secondary" @click="addNode('agent')">添加 Agent</UiButton>
        <UiButton v-if="remotes.length" variant="secondary" @click="addNode('remote_agent')"
          >添加 A2A</UiButton
        >
        <UiButton variant="ghost" @click="resetLayout">复位</UiButton>
      </div>
      <div class="canvas-toolbar-meta">
        <label class="canvas-limit"
          ><span>执行上限</span
          ><input
            v-model.number="draft.max_node_executions"
            type="number"
            min="1"
            aria-label="Graph 最大节点执行次数"
        /></label>
        <span v-if="connectionSource" class="connection-hint">选择目标节点 · Esc 取消</span>
        <span v-else class="canvas-hint">拖动画布 · 拖拽节点 · 右击打开操作</span>
      </div>
    </div>

    <svg
      :ref="canvasTools.canvas"
      class="blueprint-canvas"
      :class="{ connecting: connectionSource }"
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
        <marker :id="markerId" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,0 L8,4 L0,8 Z" />
        </marker>
      </defs>
      <rect
        class="blueprint-hit-surface"
        width="100%"
        height="100%"
        :fill="`url(#${markerId}-grid)`"
        @pointerdown="canvasTools.beginPan"
        @click="connectionSource = null"
      />
      <g :transform="canvasTools.panTransform.value">
        <g class="blueprint-edges" aria-hidden="true">
          <path
            v-for="edge in edgeLines"
            :key="edge.id"
            :d="edge.path"
            :marker-end="`url(#${markerId})`"
          />
          <path v-if="temporaryLine" class="temporary" :d="temporaryLine" />
        </g>
        <foreignObject
          v-for="node in draft.nodes"
          :key="node.id"
          :x="positionOf(node).x"
          :y="positionOf(node).y"
          :width="cardWidth"
          :height="cardHeight"
        >
          <article
            v-context-menu="(event) => openMenu(event, node)"
            class="blueprint-node"
            :class="{
              entry: draft.entry_points.includes(node.id),
              source: connectionSource === node.id,
            }"
            @pointerdown="nodePointerDown($event, node)"
            @click.stop="nodeClick(node)"
          >
            <div class="blueprint-node-meta">
              <span>{{ node.target_type === "agent" ? "AGENT" : "A2A" }}</span>
              <span v-if="draft.primary_entry_point === node.id" class="entry-mark">主入口</span
              ><span
                v-else-if="draft.entry_points.includes(node.id)"
                class="entry-mark secondary-entry"
                >启动</span
              >
            </div>
            <div class="blueprint-node-title">
              <SelectControl
                v-if="editingNode === node.id"
                v-model="node.target_ref"
                :options="nodeOptions(node)"
                aria-label="选择节点 Agent"
                @pointerdown.stop
                @click.stop
                @change="editingNode = null"
              />
              <strong v-else>{{ nodeName(node) }}</strong>
              <button
                v-if="editingNode !== node.id"
                type="button"
                title="更换执行目标"
                aria-label="更换执行目标"
                @pointerdown.stop
                @click.stop="editingNode = node.id"
              >
                ↻
              </button>
            </div>
            <small class="canvas-technical-id" :title="node.id">{{ node.id }}</small>
          </article>
        </foreignObject>
        <foreignObject v-if="!draft.nodes.length" x="-210" y="-70" width="420" height="140">
          <div class="blueprint-empty">
            <strong>添加第一个 Agent 节点</strong>
            <span>画布没有边界，复位会回到入口节点。</span>
          </div>
        </foreignObject>
      </g>
    </svg>

    <div v-if="draft.edges.length" class="connection-ledger">
      <span>路径</span>
      <article v-for="(edge, index) in draft.edges" :key="edge.id">
        <strong>{{ edgeNodeName(edge.source) }}</strong
        ><span>→</span><strong>{{ edgeNodeName(edge.target) }}</strong>
        <SelectControl
          :model-value="edge.condition_registration_id"
          :options="conditionOptions"
          aria-label="连接条件"
          @update:model-value="setCondition(edge, $event)"
        />
        <UiButton
          v-if="edge.condition_registration_id"
          variant="ghost"
          @click="configuringEdge = edge"
          >{{
            Object.keys(conditionDefinition(edge)?.config_schema?.properties || {}).length
              ? "配置"
              : "查看上下文"
          }}</UiButton
        >
        <UiButton variant="danger" aria-label="删除连接" @click="draft.edges.splice(index, 1)"
          >×</UiButton
        >
      </article>
    </div>

    <ContextMenu
      :open="menu.open"
      :x="menu.x"
      :y="menu.y"
      :items="contextItems"
      @select="chooseMenu"
      @dismiss="menu.open = false"
    />
    <ModalFrame
      v-if="configuringEdge"
      title="路径条件配置"
      :description="conditionDefinition(configuringEdge)?.description"
      @dismiss="configuringEdge = null"
    >
      <div class="graph-condition-context">
        <header>
          <strong>当前路径上下文</strong>
          <small>这些值会通过 GraphConditionContext 直接传给条件函数。</small>
        </header>
        <div>
          <TechnicalId label="Edge ID" :value="configuringEdge.id" />
          <TechnicalId
            label="Source Node ID"
            :value="configuringEdge.source"
            :detail="edgeNodeName(configuringEdge.source)"
          />
          <TechnicalId
            label="Target Node ID"
            :value="configuringEdge.target"
            :detail="edgeNodeName(configuringEdge.target)"
          />
          <TechnicalId v-if="draft.id" label="Orchestration ID" :value="draft.id" />
          <TechnicalId
            v-if="state.config?.workspace?.id"
            label="Workspace ID"
            :value="state.config.workspace.id"
          />
        </div>
      </div>
      <ConfigSchemaForm
        v-model="configuringEdge.condition_config"
        :schema="conditionDefinition(configuringEdge)?.config_schema || {}"
        :option-sources="graphOptionSources"
        layout="dialog"
      />
    </ModalFrame>
  </div>
</template>
