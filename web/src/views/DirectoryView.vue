<script setup>
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import { collection, load, setEntityEnabled, state } from "../store.js";
import { editorPath } from "../router.js";
import {
  labels,
  orchestrationKinds,
  resourceSections,
  extensionSections,
  valueLabel,
} from "../domain/metadata.js";
import SearchField from "../components/SearchField.vue";
import ToggleSwitch from "../components/ToggleSwitch.vue";
import CodeRegistrationCatalog from "../components/CodeRegistrationCatalog.vue";

const props = defineProps({ type: String });
const router = useRouter();
const search = ref("");
const status = ref("all");
const toggling = ref("");

const isModels = computed(() => props.type === "models");
const isGraphConditions = computed(() => props.type === "graph_conditions");
const registrationKind = computed(
  () =>
    ({
      plugins: "plugin",
      hooks: "hook",
      interventions: "intervention",
      graph_conditions: "graph_condition",
    })[props.type],
);
const registeredDefinitions = computed(
  () => state.features?.code_registrations?.[registrationKind.value] || [],
);
const title = computed(() =>
  isModels.value
    ? "模型"
    : labels[props.type] ||
      resourceSections[props.type]?.label ||
      extensionSections[props.type]?.label,
);
const descriptions = {
  agents: "Agent 可以被发布端点直接调用，也可以作为编排成员复用。",
  orchestrations: "组合多个 Agent；协作方式决定成员关系、入口与执行顺序。",
  endpoints: "每个端点发布一个 Agent 或编排，并独立控制协议与访问 Key。",
  models: "服务商负责连接与凭据，模型负责请求协议、能力和推理参数。",
  databases: "统一维护会话、记忆和插件存储使用的数据库连接。",
  tools: "只显示当前进程已导入的 @register.tool；模型看到的说明可在这里覆盖。",
  mcp_servers: "连接 Streamable HTTP MCP 服务，并把远程工具挂载给 Agent。",
  remote_agents: "发现其他 Agent 服务，并将其作为远程能力挂载给本地 Agent。",
  skill_sources:
    "相对路径以 state_dir 为根；目录下每个有效子目录对应一个 Skill。",
  plugins: "内置插件可直接配置；代码插件只有导入注册模块后才会出现。",
  hooks: "Hook 在指定生命周期执行；先由代码注册，再创建可挂载配置。",
  interventions:
    "Intervention 在工具或模型调用前后介入；人工确认需要可恢复会话。",
  graph_conditions: "条件由代码注册，参数保存在具体的 Graph 路径上。",
  sandbox_profiles: "为需要命令、代码或文件访问的工具提供隔离执行环境。",
  observability_providers: "将调用链路发送到 Langfuse；凭据会加密保存。",
};
const description = computed(
  () =>
    descriptions[props.type] || "创建可复用配置，并在 Agent 或编排中按需引用。",
);
const source = computed(() => collection(props.type));
const items = computed(() =>
  source.value.filter((item) => {
    const matches = (item.display_name || item.name || "")
      .toLowerCase()
      .includes(search.value.toLowerCase());
    return (
      matches &&
      (status.value === "all" ||
        (item.enabled !== false) === (status.value === "enabled"))
    );
  }),
);
const providers = computed(() => state.config.resources.model_providers);
const pluginTemplates = [
  {
    key: "agent_skills",
    kind: "agent_skills",
    name: "AgentSkills",
    description: "按需向 Agent 暴露 Skill 目录中的渐进式指令。",
  },
  {
    key: "context_offloader",
    kind: "context_offloader",
    name: "ContextOffloader",
    description: "把较大的工具结果移出上下文，并提供按需取回能力。",
  },
  {
    key: "goal_loop",
    kind: "goal_loop",
    name: "GoalLoop",
    description: "围绕固定目标执行可控次数的自我检查与改进循环。",
  },
];

function open(type, id) {
  if (props.type === "models" && type === "model_providers") {
    router.push({ path: editorPath("models", id), query: { entity: type } });
  } else router.push(editorPath(type, id));
}

function create(type = props.type, query = {}) {
  const path =
    props.type === "models" && type === "model_providers"
      ? { path: editorPath("models", "new"), query: { entity: type, ...query } }
      : { path: editorPath(type, "new"), query };
  router.push(path);
}

function createFromRegistration(item) {
  if (props.type === "graph_conditions") return;
  const query =
    props.type === "plugins"
      ? {
          kind: "registered",
          registration_id: item.id,
          registration_name: item.name,
        }
      : props.type === "interventions"
        ? {
            kind: "registered",
            registration_id: item.id,
            registration_name: item.name,
          }
        : { registration_id: item.id, registration_name: item.name };
  create(props.type, query);
}

async function toggle(type, item, enabled) {
  if (toggling.value) return;
  toggling.value = item.id;
  try {
    await setEntityEnabled(type, item, enabled);
  } catch {
    await load().catch(() => {});
  } finally {
    toggling.value = "";
  }
}

function summary(item, type = props.type) {
  if (type === "agents")
    return (
      state.config.resources.models.find((model) => model.id === item.model_ref)
        ?.name || "尚未选择模型"
    );
  if (type === "orchestrations") return orchestrationKinds[item.kind];
  if (type === "endpoints") {
    const pool =
      item.target_type === "agent"
        ? state.config.agents
        : state.config.orchestrations;
    const target = pool.find((entry) => entry.id === item.target_ref);
    return `${item.target_type === "agent" ? "Agent" : "编排"} · ${target?.name || "目标不可用"}`;
  }
  if (type === "model_providers") return item.base_url;
  if (type === "models")
    return `${valueLabel(item.protocol)} · ${item.model_name}`;
  if (type === "databases") return valueLabel(item.engine);
  if (type === "tools")
    return item.description || item.python_path || "未填写模型可见描述";
  if (type === "skill_sources") return item.path;
  if (
    type === "hooks" ||
    ((type === "plugins" || type === "interventions") &&
      item.kind === "registered")
  ) {
    const kind =
      type === "plugins"
        ? "plugin"
        : type === "hooks"
          ? "hook"
          : "intervention";
    return (
      (state.features?.code_registrations?.[kind] || []).find(
        (definition) => definition.id === item.registration_id,
      )?.name || item.registration_id
    );
  }
  if (type === "plugins" || type === "interventions")
    return valueLabel(item.kind);
  if (type === "sandbox_profiles") return item.provider;
  return item.url || item.base_url || item.host || item.description || "已配置";
}
</script>

<template>
  <div class="directory-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">{{ type.toUpperCase() }}</p>
        <h1>{{ title }}</h1>
        <UiDescription>{{ description }}</UiDescription>
      </div>
      <div class="heading-actions">
        <template v-if="isModels">
          <UiButton variant="secondary" @click="create('model_providers')"
            >添加服务商</UiButton
          >
          <UiButton
            variant="primary"
            :disabled="!providers.length"
            @click="create('models')"
            >注册模型</UiButton
          >
        </template>
        <UiButton
          v-else-if="!['tools', 'plugins', 'graph_conditions'].includes(type)"
          variant="primary"
          @click="create()"
          >创建{{ title }}</UiButton
        >
      </div>
    </header>

    <section v-if="type === 'plugins'" class="template-gallery">
      <header>
        <div>
          <h2>内置插件</h2>
          <UiDescription
            >无需代码注册，创建配置后即可挂载到 Agent。</UiDescription
          >
        </div>
      </header>
      <div class="template-grid">
        <button
          v-for="template in pluginTemplates"
          :key="template.key"
          type="button"
          @click="
            create('plugins', {
              kind: template.kind,
            })
          "
        >
          <span class="template-mark">{{ template.name.slice(0, 1) }}</span>
          <span
            ><strong>{{ template.name }}</strong
            ><small>{{ template.description }}</small></span
          >
          <span class="row-chevron">→</span>
        </button>
      </div>
    </section>

    <CodeRegistrationCatalog
      v-if="registrationKind"
      :items="registeredDefinitions"
      :title="
        type === 'plugins'
          ? '已注册的插件'
          : type === 'hooks'
            ? '已注册的 Hook'
            : type === 'interventions'
              ? '已注册的 Intervention'
              : '已注册的 Graph 条件'
      "
      :description="
        type === 'graph_conditions'
          ? '这些定义只能在 Graph 的具体路径上选择和配置。'
          : '只显示当前进程已经导入的注册项；创建配置后才能挂载到 Agent。'
      "
      :actionable="!isGraphConditions"
      action-label="创建配置"
      usage-label="在 Graph 边中配置"
      @create="createFromRegistration"
    />

    <template v-if="isModels">
      <section class="directory-section">
        <header>
          <div>
            <h2>模型服务商</h2>
            <UiDescription>连接地址和认证凭据只在服务商层维护。</UiDescription>
          </div>
          <span>{{ providers.length }}</span>
        </header>
        <div v-if="providers.length" class="entity-list">
          <article v-for="item in providers" :key="item.id" class="entity-row">
            <button
              type="button"
              class="entity-open"
              @click="open('model_providers', item.id)"
            >
              <span class="entity-symbol">P</span
              ><span class="entity-main"
                ><strong>{{ item.display_name || item.name }}</strong
                ><small>{{ summary(item, "model_providers") }}</small></span
              >
              <span class="entity-meta"
                >{{
                  state.config.resources.models.filter(
                    (m) => m.provider_ref === item.id,
                  ).length
                }}
                个模型</span
              >
            </button>
            <span class="entity-status">{{
              item.enabled === false ? "已禁用" : "已启用"
            }}</span>
            <ToggleSwitch
              :model-value="item.enabled !== false"
              :disabled="Boolean(toggling) || state.saving"
              :aria-label="`${item.name} 启用状态`"
              @update:model-value="toggle('model_providers', item, $event)"
            />
          </article>
        </div>
        <p v-else class="empty-state">
          还没有模型服务商。先添加连接，再从服务商发现模型。
        </p>
      </section>
      <section class="directory-section">
        <header>
          <div>
            <h2>已注册模型</h2>
            <UiDescription>Agent 只能选择这里已注册的模型。</UiDescription>
          </div>
          <span>{{ source.length }}</span>
        </header>
        <div v-if="source.length" class="entity-list">
          <article v-for="item in source" :key="item.id" class="entity-row">
            <button
              type="button"
              class="entity-open"
              @click="open('models', item.id)"
            >
              <span class="entity-symbol">M</span
              ><span class="entity-main"
                ><strong>{{ item.display_name || item.name }}</strong
                ><small>{{ summary(item) }}</small></span
              >
            </button>
            <span class="entity-status">{{
              item.enabled === false ? "已禁用" : "已启用"
            }}</span>
            <ToggleSwitch
              :model-value="item.enabled !== false"
              :disabled="Boolean(toggling) || state.saving"
              :aria-label="`${item.name} 启用状态`"
              @update:model-value="toggle('models', item, $event)"
            />
          </article>
        </div>
        <p v-else class="empty-state">还没有注册模型。</p>
      </section>
    </template>

    <template v-else-if="!isGraphConditions">
      <header
        v-if="['plugins', 'hooks', 'interventions'].includes(type)"
        class="configured-directory-head"
      >
        <div>
          <h2>配置实例</h2>
          <UiDescription
            >这里的实例可以独立启停，并复用到不同 Agent。</UiDescription
          >
        </div>
        <span>{{ source.length }}</span>
      </header>
      <div class="directory-toolbar">
        <SearchField v-model="search" placeholder="搜索名称…" />
        <div class="segmented-filter" aria-label="状态筛选">
          <button
            v-for="[value, text] in [
              ['all', '全部'],
              ['enabled', '启用'],
              ['disabled', '停用'],
            ]"
            :key="value"
            type="button"
            :class="{ active: status === value }"
            @click="status = value"
          >
            {{ text }}
          </button>
        </div>
        <span>{{ items.length }} 项</span>
      </div>
      <div v-if="items.length" class="entity-list">
        <article v-for="item in items" :key="item.id" class="entity-row">
          <button
            type="button"
            class="entity-open"
            @click="open(type, item.id)"
          >
            <span class="entity-symbol">{{
              (item.display_name || item.name || "?").slice(0, 1).toUpperCase()
            }}</span>
            <span class="entity-main"
              ><strong>{{ item.display_name || item.name }}</strong
              ><small>{{ summary(item) }}</small></span
            >
          </button>
          <span class="entity-status">{{
            item.enabled === false ? "已禁用" : "已启用"
          }}</span>
          <ToggleSwitch
            :model-value="item.enabled !== false"
            :disabled="Boolean(toggling) || state.saving"
            :aria-label="`${item.name} 启用状态`"
            @update:model-value="toggle(type, item, $event)"
          />
        </article>
      </div>
      <div v-else class="empty-state large">
        <strong>暂无{{ title }}</strong>
        <p>
          {{
            search
              ? "没有匹配当前筛选条件的项目。"
              : "创建第一个配置后会显示在这里。"
          }}
        </p>
      </div>
    </template>
  </div>
</template>
