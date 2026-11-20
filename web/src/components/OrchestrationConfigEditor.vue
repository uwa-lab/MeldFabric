<script setup>
import { computed } from "vue";
import { collection, state } from "../store.js";
import { orchestrationKinds } from "../domain/metadata.js";
import SelectControl from "./SelectControl.vue";
import ToggleSwitch from "./ToggleSwitch.vue";
import GraphCanvas from "./GraphCanvas.vue";
import WorkflowCanvas from "./WorkflowCanvas.vue";

const props = defineProps({ draft: Object, creating: Boolean });
const agents = computed(() =>
  collection("agents").filter((item) => item.enabled !== false),
);
const databases = computed(() =>
  collection("databases").filter((item) => item.enabled !== false),
);
const remotes = computed(() =>
  collection("remote_agents").filter((item) => item.enabled !== false),
);
const agentOptions = computed(() =>
  agents.value.map((item) => ({
    value: item.id,
    label: item.display_name || item.name,
  })),
);
const databaseOptions = computed(() =>
  databases.value.map((item) => ({
    value: item.id,
    label: item.display_name || item.name,
  })),
);
const graphConditions = computed(
  () => state.features?.code_registrations?.graph_condition || [],
);
const graphConditionOptions = computed(() => [
  { value: null, label: "无条件" },
  ...graphConditions.value.map((item) => ({
    value: item.id,
    label: item.name,
  })),
]);

const generated = (prefix) =>
  `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;

function selectKind(kind) {
  const common = {
    name: props.draft.name,
    description: props.draft.description,
    enabled: props.draft.enabled,
    kind,
  };
  let next = common;
  if (kind === "agent_as_tool")
    next = {
      ...common,
      coordinator_ref: agents.value[0]?.id || null,
      members: [],
    };
  if (kind === "swarm")
    next = {
      ...common,
      agent_refs: agents.value.slice(0, 2).map((item) => item.id),
      entry_agent_ref: agents.value[0]?.id || null,
      max_handoffs: 20,
      max_iterations: 20,
      execution_timeout_seconds: 900,
      node_timeout_seconds: 300,
      session_manager: null,
    };
  if (kind === "graph") {
    const node = {
      id: generated("node"),
      target_type: "agent",
      target_ref: agents.value[0]?.id || null,
    };
    next = {
      ...common,
      nodes: [node],
      edges: [],
      entry_points: [node.id],
      primary_entry_point: node.id,
      max_node_executions: 100,
      session_manager: null,
    };
  }
  if (kind === "workflow") {
    const task = {
      id: generated("task"),
      agent_ref: agents.value[0]?.id || null,
      instruction: "处理输入并完成当前任务",
      dependency_refs: [],
    };
    next = {
      ...common,
      tasks: [task],
      primary_task_ref: task.id,
    };
  }
  for (const key of Object.keys(props.draft)) delete props.draft[key];
  Object.assign(props.draft, next);
}

function toggleSwarmAgent(id) {
  props.draft.agent_refs = props.draft.agent_refs.includes(id)
    ? props.draft.agent_refs.filter((item) => item !== id)
    : [...props.draft.agent_refs, id];
  if (!props.draft.agent_refs.includes(props.draft.entry_agent_ref))
    props.draft.entry_agent_ref = props.draft.agent_refs[0] || null;
}

function addMember(agent) {
  if (props.draft.members.some((item) => item.agent_ref === agent.id)) return;
  props.draft.members.push({
    id: generated("member"),
    agent_ref: agent.id,
    name: null,
    description: null,
    preserve_context: true,
  });
}

function removeMember(agentId) {
  props.draft.members = props.draft.members.filter(
    (item) => item.agent_ref !== agentId,
  );
}

function enableSession(value) {
  props.draft.session_manager = value
    ? {
        database_ref: databases.value[0]?.id || null,
        save_latest_on: "invocation",
      }
    : null;
}
</script>

<template>
  <div class="orchestration-editor">
    <section class="editor-panel identity-panel">
      <div class="stacked-fields">
        <label
          >名称<input
            v-model="draft.name"
            class="field"
            required
            placeholder="research_team"
        /></label>
        <label
          >用途描述<textarea
            v-model="draft.description"
            class="field"
            placeholder="说明这个编排适合解决什么问题。"
          />
        </label>
      </div>
    </section>

    <section class="editor-panel orchestration-mode">
      <header>
        <div>
          <h3>选择协作方式</h3>
          <UiDescription
            >创建后不能更换模式；不同模式具有不同的入口与执行边界。</UiDescription
          >
        </div>
      </header>
      <div class="mode-grid">
        <button
          v-for="(label, kind) in orchestrationKinds"
          :key="kind"
          type="button"
          :class="{ active: draft.kind === kind }"
          :disabled="!creating && draft.kind !== kind"
          @click="selectKind(kind)"
        >
          <span class="mode-icon">{{
            { agent_as_tool: "T", swarm: "S", graph: "G", workflow: "W" }[kind]
          }}</span>
          <span
            ><strong>{{ label }}</strong
            ><small>{{
              {
                agent_as_tool: "中心 Agent 按需调用专长成员",
                swarm: "成员自主交接并共同收敛",
                graph: "在画布中定义节点与可达路径",
                workflow: "按任务依赖形成确定性流程",
              }[kind]
            }}</small></span
          >
        </button>
      </div>
    </section>

    <section
      v-if="draft.kind === 'agent_as_tool'"
      class="editor-panel orchestration-stage"
    >
      <div class="coordinator-card">
        <span class="stage-label">COORDINATOR</span>
        <div>
          <strong>协调 Agent</strong><small>接收请求并决定调用哪个成员。</small>
        </div>
        <SelectControl
          v-model="draft.coordinator_ref"
          :options="agentOptions"
        />
      </div>
      <div class="member-bench">
        <header>
          <div>
            <h3>成员工作台</h3>
            <UiDescription
              >点选候选 Agent 加入；加入后会作为原生 Agent Tool。</UiDescription
            >
          </div>
          <span>{{ draft.members.length }} 名成员</span>
        </header>
        <div class="member-columns">
          <div class="member-pool">
            <span class="column-label">候选成员</span>
            <button
              v-for="agent in agents.filter(
                (item) =>
                  item.id !== draft.coordinator_ref &&
                  !draft.members.some((member) => member.agent_ref === item.id),
              )"
              :key="agent.id"
              type="button"
              @click="addMember(agent)"
            >
              <span class="agent-avatar">{{
                agent.name.slice(0, 1).toUpperCase()
              }}</span
              ><span
                ><strong>{{ agent.display_name || agent.name }}</strong
                ><small>{{
                  agent.description || "未填写用途描述"
                }}</small></span
              ><span>＋</span>
            </button>
            <p
              v-if="
                !agents.some(
                  (item) =>
                    item.id !== draft.coordinator_ref &&
                    !draft.members.some(
                      (member) => member.agent_ref === item.id,
                    ),
                )
              "
              class="empty-copy"
            >
              没有更多候选 Agent。
            </p>
          </div>
          <div class="member-pool attached">
            <span class="column-label">已加入</span>
            <article
              v-for="member in draft.members"
              :key="member.id || member.agent_ref"
            >
              <span class="agent-avatar">{{
                agents
                  .find((item) => item.id === member.agent_ref)
                  ?.name?.slice(0, 1)
                  .toUpperCase() || "?"
              }}</span>
              <div>
                <strong>{{
                  agents.find((item) => item.id === member.agent_ref)?.name ||
                  "资源不可用"
                }}</strong>
                <ToggleSwitch
                  v-model="member.preserve_context"
                  label="共享上下文"
                />
              </div>
              <UiButton
                variant="danger"
                aria-label="移除成员"
                @click="removeMember(member.agent_ref)"
                >×</UiButton
              >
            </article>
            <p v-if="!draft.members.length" class="empty-copy">
              尚未加入成员。
            </p>
          </div>
        </div>
      </div>
    </section>

    <section
      v-else-if="draft.kind === 'swarm'"
      class="editor-panel orchestration-stage"
    >
      <div class="swarm-stage">
        <header>
          <div>
            <h3>Swarm 成员</h3>
            <UiDescription
              >点选 Agent 进入协作网络，再指定入口成员。</UiDescription
            >
          </div>
          <strong>{{ draft.agent_refs.length }}</strong>
        </header>
        <div class="swarm-grid">
          <button
            v-for="agent in agents"
            :key="agent.id"
            type="button"
            :class="{
              active: draft.agent_refs.includes(agent.id),
              entry: draft.entry_agent_ref === agent.id,
            }"
            @click="toggleSwarmAgent(agent.id)"
          >
            <span class="agent-avatar">{{
              agent.name.slice(0, 1).toUpperCase()
            }}</span
            ><strong>{{ agent.display_name || agent.name }}</strong
            ><small>{{
              draft.entry_agent_ref === agent.id
                ? "入口 Agent"
                : draft.agent_refs.includes(agent.id)
                  ? "已加入网络"
                  : "点击加入"
            }}</small>
          </button>
        </div>
      </div>
      <div class="swarm-controls">
        <label
          >入口 Agent<SelectControl
            v-model="draft.entry_agent_ref"
            :options="
              agentOptions.filter((option) =>
                draft.agent_refs.includes(option.value),
              )
            "
        /></label>
        <label
          >最大交接次数<input
            v-model.number="draft.max_handoffs"
            class="field"
            type="number"
            min="1"
        /></label>
        <label
          >最大迭代次数<input
            v-model.number="draft.max_iterations"
            class="field"
            type="number"
            min="1"
        /></label>
      </div>
    </section>

    <section
      v-else-if="draft.kind === 'graph'"
      class="editor-panel orchestration-stage canvas-stage"
    >
      <header class="stage-heading">
        <div>
          <h3>Graph 画布</h3>
          <UiDescription
            >拖拽整理节点；右击或长按节点可设置入口、发起连接或删除。</UiDescription
          >
        </div>
      </header>
      <GraphCanvas
        :draft="draft"
        :agents="agents"
        :remotes="remotes"
        :condition-options="graphConditionOptions"
        :condition-definitions="graphConditions"
      />
    </section>

    <section v-else class="editor-panel orchestration-stage canvas-stage">
      <header class="stage-heading">
        <div>
          <h3>Workflow 画布</h3>
          <UiDescription
            >拖拽整理任务；右击或长按任务可编辑并连接后续任务。</UiDescription
          >
        </div>
      </header>
      <WorkflowCanvas :draft="draft" :agents="agents" />
    </section>

    <section
      v-if="['swarm', 'graph'].includes(draft.kind)"
      class="editor-panel session-dock"
    >
      <div>
        <span class="stage-label">PERSISTENCE</span><strong>编排会话快照</strong
        ><small>保存协作过程、节点状态与恢复信息。</small>
      </div>
      <div class="session-dock-controls">
        <SelectControl
          v-if="draft.session_manager"
          v-model="draft.session_manager.database_ref"
          :options="databaseOptions"
        />
        <ToggleSwitch
          :model-value="!!draft.session_manager"
          aria-label="启用编排会话快照"
          @update:model-value="enableSession"
        />
      </div>
    </section>
  </div>
</template>
