<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api.js";
import {
  collection,
  deleteEntity,
  notify,
  references,
  saveEntity,
  saveSecret,
  saveTool,
  setEditorDirty,
  state,
} from "../store.js";
import { confirmAction } from "../dialogs.js";
import { editorPath, listPath } from "../router.js";
import { labels } from "../domain/metadata.js";
import AgentConfigEditor from "../components/AgentConfigEditor.vue";
import OrchestrationConfigEditor from "../components/OrchestrationConfigEditor.vue";
import EndpointConfigEditor from "../components/EndpointConfigEditor.vue";
import ResourceConfigEditor from "../components/ResourceConfigEditor.vue";
import ModalFrame from "../components/ModalFrame.vue";
import TechnicalId from "../components/TechnicalId.vue";

const props = defineProps({ type: String, id: String });
const route = useRoute();
const router = useRouter();
const effectiveType = computed(() =>
  props.type === "models" && route.query.entity === "model_providers"
    ? "model_providers"
    : props.type,
);
const creating = computed(() => props.id === "new");
const secret = ref("");
const referenceData = ref(null);
const topologyData = ref(null);
const form = ref();

const clone = (value) => JSON.parse(JSON.stringify(value));

function defaultEntity(type) {
  const firstAgent =
    collection("agents").find((item) => item.enabled !== false)?.id || null;
  const firstProvider =
    route.query.provider || collection("model_providers")[0]?.id || null;
  const defaults = {
    agents: {
      name: "new_agent",
      description: "",
      enabled: true,
      model_ref: collection("models")[0]?.id || null,
      prompt: {
        selected_version: "v1",
        versions: [{ version: "v1", content: "" }],
      },
      initial_state: {},
      tool_bindings: [],
      mcp_bindings: [],
      remote_tool_bindings: [],
      plugin_bindings: [],
      hook_bindings: [],
      intervention_bindings: [],
      conversation_manager: {
        strategy: "sliding_window",
        window_size: 40,
        truncate_tool_results: true,
        per_turn: false,
        summary_ratio: 0.3,
        preserve_recent_messages: 10,
        summarization_system_prompt: null,
        pin_first: null,
        proactive_compression: false,
      },
      context_manager: { strategy: "auto" },
      session_manager: null,
      memory_manager: null,
      retry_strategy: {
        enabled: true,
        max_attempts: 6,
        initial_delay_seconds: 4,
        max_delay_seconds: 128,
      },
      structured_output: {
        enabled: false,
        name: "AgentOutput",
        description: null,
        json_schema: {},
        prompt: null,
      },
      background_tasks: { enabled: false },
      sandbox_ref: null,
      observability_ref: null,
    },
    orchestrations: {
      kind: "agent_as_tool",
      name: "new_orchestration",
      description: "",
      enabled: true,
      coordinator_ref: firstAgent,
      members: [],
    },
    endpoints: {
      name: "new_endpoint",
      description: "",
      enabled: true,
      target_type: "agent",
      target_ref: firstAgent,
      protocols: ["rest", "ag_ui"],
    },
    model_providers: {
      name: "New Provider",
      enabled: true,
      adapter: "openai_compatible",
      base_url: "",
      api_key_secret_ref: null,
      timeout_seconds: 30,
      headers: {},
    },
    models: {
      name: "model",
      enabled: true,
      provider_ref: firstProvider,
      model_name: route.query.model_name || "",
      protocol: "chat_completions",
      capabilities: {
        tool_calling: null,
        image_inputs: null,
        audio_inputs: null,
        video_inputs: null,
        pdf_inputs: null,
      },
      timeout_seconds: null,
      temperature: null,
      top_p: null,
      max_retries: null,
      parameters: {},
    },
    databases: {
      name: "agent_data",
      enabled: true,
      engine: "sqlite",
      url: "sqlite+aiosqlite:///agent-data.db",
      options: {},
    },
    mcp_servers: {
      name: "mcp_server",
      enabled: true,
      transport: "streamable_http",
      url: "",
      credential_secret_ref: null,
      headers: {},
      credential_header: "Authorization",
      credential_scheme: "Bearer",
      timeout_seconds: 30,
    },
    skill_sources: {
      name: "skills",
      enabled: true,
      path: "./skills",
      order: 0,
    },
    plugins: {
      kind: "agent_skills",
      name: "agent_skills",
      enabled: true,
      skill_source_refs: [],
      strict: false,
    },
    hooks: { name: "hook", enabled: true, registration_id: "", config: {} },
    interventions: {
      kind: "human_in_the_loop",
      name: "human_approval",
      enabled: true,
      tool_refs: [],
      mcp_tool_names: [],
      enable_trust: false,
    },
    sandbox_profiles: {
      name: "docker_sandbox",
      enabled: true,
      provider: "docker",
      network_enabled: false,
      cpu_limit: null,
      memory_limit_mb: null,
      timeout_seconds: 300,
      ttl_seconds: 900,
      config: {},
    },
    observability_providers: {
      name: "langfuse",
      enabled: true,
      adapter: "langfuse",
      host: "",
      public_key: "",
      secret_key_ref: null,
      sample_rate: 1,
      fail_closed: false,
      environment: null,
    },
    remote_agents: {
      name: "remote_agent",
      description: "",
      enabled: true,
      base_url: "",
      credential_secret_ref: null,
      remote_service_id: null,
      remote_agent_id: null,
      protocols: [],
      timeout_seconds: 60,
    },
  };
  if (type === "plugins" && route.query.kind) {
    const kind = route.query.kind;
    defaults.plugins = { name: kind, enabled: true, kind };
    if (kind === "agent_skills")
      Object.assign(defaults.plugins, { skill_source_refs: [], strict: false });
    if (kind === "context_offloader")
      Object.assign(defaults.plugins, {
        database_ref: collection("databases")[0]?.id || null,
        max_result_tokens: 2500,
        preview_tokens: 1000,
        include_retrieval_tool: true,
      });
    if (kind === "goal_loop")
      Object.assign(defaults.plugins, {
        goal: "",
        max_attempts: 3,
        timeout_seconds: 300,
        preserve_context: true,
      });
    if (kind === "registered")
      Object.assign(defaults.plugins, {
        name: route.query.registration_name || "code_plugin",
        registration_id: route.query.registration_id || "",
        config: {},
      });
  }
  if (type === "hooks" && route.query.registration_id) {
    defaults.hooks = {
      name: route.query.registration_name || "code_hook",
      enabled: true,
      registration_id: route.query.registration_id,
      config: {},
    };
  }
  if (
    type === "interventions" &&
    (route.query.kind === "registered" || route.query.registration_id)
  ) {
    defaults.interventions = {
      kind: "registered",
      name: route.query.registration_name || "code_intervention",
      enabled: true,
      registration_id: route.query.registration_id || "",
      config: {},
      on_error: "throw",
    };
  }
  return clone(defaults[type]);
}

const existing = creating.value
  ? null
  : collection(effectiveType.value).find((item) => item.id === props.id);
const draft = ref(
  existing ? clone(existing) : defaultEntity(effectiveType.value),
);
const initial = ref(JSON.stringify(draft.value));
watch(
  draft,
  (value) => setEditorDirty(JSON.stringify(value) !== initial.value),
  { deep: true },
);

const title = computed(() =>
  creating.value
    ? `创建${labels[effectiveType.value] || "配置"}`
    : draft.value.display_name || draft.value.name,
);

async function save() {
  if (!form.value?.checkValidity()) {
    form.value?.reportValidity();
    return;
  }
  if (
    effectiveType.value === "plugins" &&
    draft.value.kind === "agent_skills" &&
    !draft.value.skill_source_refs?.length
  ) {
    notify("AgentSkills 至少需要选择一个已启用的 Skill 目录", true);
    return;
  }
  try {
    let result;
    if (effectiveType.value === "tools") {
      result = await saveTool(props.id, {
        enabled: draft.value.enabled,
        display_name: draft.value.display_name,
        description: draft.value.description_overridden
          ? draft.value.description
          : "",
        requires_sandbox: draft.value.requires_sandbox,
        parameter_descriptions: draft.value.parameter_descriptions,
      });
    } else {
      result = await saveEntity(
        effectiveType.value,
        draft.value,
        creating.value ? null : props.id,
      );
      if (
        secret.value &&
        [
          "model_providers",
          "mcp_servers",
          "remote_agents",
          "observability_providers",
        ].includes(effectiveType.value)
      )
        await saveSecret(effectiveType.value, result.id, secret.value);
    }
    setEditorDirty(false);
    if (creating.value) {
      const query =
        effectiveType.value === "model_providers"
          ? { entity: "model_providers" }
          : {};
      await router.replace({ path: editorPath(props.type, result.id), query });
    } else {
      const fresh = collection(effectiveType.value).find(
        (item) => item.id === props.id,
      );
      if (fresh) {
        draft.value = clone(fresh);
        initial.value = JSON.stringify(draft.value);
      }
    }
  } catch {
    // Store operations already provide the user-facing error notification.
  }
}

async function remove() {
  const ok = await confirmAction(
    `删除“${draft.value.display_name || draft.value.name}”并解除所有相关挂载。持久化会话和记忆不会自动清理。`,
    { title: "永久删除配置？", confirmText: "删除", danger: true },
  );
  if (!ok) return;
  try {
    await deleteEntity(effectiveType.value, props.id);
    setEditorDirty(false);
    await router.push(listPath(props.type));
  } catch {
    // Store operations already provide the user-facing error notification.
  }
}

async function showReferences() {
  referenceData.value = await references(props.id);
}

async function showTopology() {
  try {
    topologyData.value = await api(
      `/admin/agents/${encodeURIComponent(props.id)}/remote-topology`,
    );
  } catch (error) {
    notify(error.message, true);
  }
}

onBeforeUnmount(() => setEditorDirty(false));
</script>

<template>
  <div class="route-view">
    <div v-if="draft" class="entity-editor-page">
      <header class="editor-heading">
        <button
          type="button"
          class="back-link"
          @click="router.push(listPath(type))"
        >
          ← 返回列表
        </button>
        <div class="editor-title-row">
          <div>
            <p class="eyebrow">
              {{ (labels[effectiveType] || effectiveType).toUpperCase() }}
            </p>
            <h1 :title="title">{{ title }}</h1>
            <TechnicalId
              v-if="!creating"
              :label="`${labels[effectiveType] || effectiveType} ID`"
              :value="id"
            />
          </div>
          <div class="heading-actions">
            <UiButton
              v-if="!creating && effectiveType === 'agents'"
              variant="secondary"
              @click="showTopology"
              >查看拓扑</UiButton
            >
            <UiButton
              v-if="!creating"
              variant="secondary"
              @click="showReferences"
              >查看引用</UiButton
            >
            <UiButton
              v-if="!creating && effectiveType !== 'tools'"
              variant="danger"
              :disabled="state.saving"
              @click="remove"
              >删除</UiButton
            >
            <UiButton variant="primary" :busy="state.saving" @click="save"
              >保存</UiButton
            >
          </div>
        </div>
      </header>
      <form ref="form" @submit.prevent="save">
        <AgentConfigEditor v-if="effectiveType === 'agents'" :draft="draft" />
        <OrchestrationConfigEditor
          v-else-if="effectiveType === 'orchestrations'"
          :draft="draft"
          :creating="creating"
        />
        <EndpointConfigEditor
          v-else-if="effectiveType === 'endpoints'"
          :draft="draft"
          :creating="creating"
          :entity-id="id"
        />
        <ResourceConfigEditor
          v-else
          v-model:secret="secret"
          :type="effectiveType"
          :draft="draft"
          :creating="creating"
          :entity-id="id"
        />
      </form>
    </div>
    <div v-else class="empty-state large">
      <strong>配置不存在</strong
      ><UiButton variant="secondary" @click="router.push(listPath(type))"
        >返回列表</UiButton
      >
    </div>

    <ModalFrame
      v-if="referenceData"
      title="资源引用"
      @dismiss="referenceData = null"
    >
      <div v-if="referenceData.total" class="reference-groups">
        <section v-for="group in referenceData.groups" :key="group.module">
          <h3>{{ group.label }}</h3>
          <button
            v-for="item in group.items"
            :key="item.owner_id + item.usage"
            type="button"
            @click="
              referenceData = null;
              router.push(item.href);
            "
          >
            <span
              ><strong>{{ item.owner_name }}</strong
              ><small>{{ item.usage }}</small></span
            ><span>前往 →</span>
          </button>
        </section>
      </div>
      <p v-else class="empty-state">当前没有其他配置引用此项。</p>
    </ModalFrame>

    <ModalFrame
      v-if="topologyData"
      title="远程 Agent 调用拓扑"
      description="从当前 Agent 出发，递归检查已挂载的 MeldFabric Remote 调用链。"
      @dismiss="topologyData = null"
    >
      <div class="topology-summary">
        <span
          ><strong>{{ topologyData.nodes.length }}</strong
          >节点</span
        >
        <span
          ><strong>{{ topologyData.edges.length }}</strong
          >连接</span
        >
        <span
          ><strong>{{ topologyData.cycles.length }}</strong
          >回环</span
        >
        <span
          ><strong>{{ topologyData.unreachable.length }}</strong
          >不可达</span
        >
      </div>
      <div v-if="topologyData.edges.length" class="topology-list">
        <article
          v-for="edge in topologyData.edges"
          :key="`${edge.source}:${edge.target}`"
        >
          <code>{{ edge.source }}</code
          ><span>→</span><code>{{ edge.target }}</code>
        </article>
      </div>
      <p v-else class="empty-copy">当前 Agent 没有远程挂载。</p>
      <div v-if="topologyData.cycles.length" class="topology-warning">
        <strong>发现调用回环</strong>
        <p v-for="cycle in topologyData.cycles" :key="cycle.join(':')">
          {{ cycle.join(" → ") }}
        </p>
      </div>
      <div v-if="topologyData.unreachable.length" class="topology-warning">
        <strong>无法完成检查</strong>
        <p
          v-for="item in topologyData.unreachable"
          :key="item.remote_agent_ref"
        >
          {{ item.remote_agent_ref }}：{{ item.message }}
        </p>
      </div>
    </ModalFrame>
  </div>
</template>
