<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { api, post } from "../api.js";
import { collection, notify, saveEntity, state } from "../store.js";
import SettingRow from "./SettingRow.vue";
import ToggleSwitch from "./ToggleSwitch.vue";
import SelectControl from "./SelectControl.vue";
import ModalFrame from "./ModalFrame.vue";
import SearchField from "./SearchField.vue";
import ConfigSchemaForm from "./ConfigSchemaForm.vue";
import StructuredSettings from "./StructuredSettings.vue";
import ToolDescriptionEditor from "./ToolDescriptionEditor.vue";
import ConfigurationModes from "./ConfigurationModes.vue";

const props = defineProps({
  type: String,
  draft: Object,
  creating: Boolean,
  entityId: String,
  secret: String,
});
const emit = defineEmits(["update:secret"]);
const router = useRouter();
const busy = ref("");
const result = ref(null);
const discoveryOpen = ref(false);
const discoverySearch = ref("");
const registering = ref("");
const configurationMode = ref("basic");

const options = (items) =>
  items
    .filter((item) => item.enabled !== false)
    .map((item) => ({ value: item.id, label: item.display_name || item.name }));
const providerOptions = computed(() => options(collection("model_providers")));
const databaseOptions = computed(() => options(collection("databases")));
const availableSkillSources = computed(() =>
  collection("skill_sources").filter((item) => item.enabled !== false),
);
const structuredSchemas = computed(
  () => state.features?.structured_schemas || {},
);
const databaseOptionSchema = computed(
  () =>
    structuredSchemas.value.database_options?.[props.draft.engine] ||
    structuredSchemas.value.json_object ||
    {},
);
const sandboxConfigSchema = computed(
  () =>
    structuredSchemas.value.sandbox_config?.[props.draft.provider] ||
    structuredSchemas.value.json_object ||
    {},
);
const selectedProvider = computed(() =>
  collection("model_providers").find(
    (item) => item.id === props.draft.provider_ref,
  ),
);
const modelParametersSchema = computed(
  () =>
    structuredSchemas.value.model_parameters?.[
      selectedProvider.value?.adapter
    ] ||
    structuredSchemas.value.json_object ||
    {},
);
const stringMapSchema = {
  type: "object",
  additionalProperties: { type: "string" },
};
const discoveredModels = computed(() =>
  (result.value?.items || []).filter((item) =>
    item.id.toLowerCase().includes(discoverySearch.value.toLowerCase()),
  ),
);
const registeredModelIds = computed(
  () =>
    new Set(
      collection("models")
        .filter((item) => item.provider_ref === props.entityId)
        .map((item) => item.model_name),
    ),
);
const registeredDefinitions = (kind) =>
  state.features?.code_registrations?.[kind] || [];
const registrationOptions = (kind) =>
  registeredDefinitions(kind).map((item) => ({
    value: item.id,
    label: item.name,
    description: item.description,
  }));
const configOptionSources = computed(() => {
  const result = {};
  for (const type of [
    "agents",
    "orchestrations",
    "endpoints",
    "model_providers",
    "models",
    "databases",
    "tools",
    "mcp_servers",
    "skill_sources",
    "plugins",
    "hooks",
    "interventions",
    "sandbox_profiles",
    "observability_providers",
    "remote_agents",
  ]) {
    result[type] = collection(type)
      .filter((item) => item.enabled !== false)
      .map((item) => ({
        value: item.id,
        label: item.display_name || item.name,
        description: item.id,
      }));
  }
  return result;
});
const registrationKind = computed(() => {
  if (props.type === "hooks") return "hook";
  if (props.type === "plugins" && props.draft.kind === "registered")
    return "plugin";
  if (props.type === "interventions" && props.draft.kind === "registered")
    return "intervention";
  return null;
});
const selectedRegistration = computed(() =>
  registrationKind.value
    ? registeredDefinitions(registrationKind.value).find(
        (item) => item.id === props.draft.registration_id,
      )
    : null,
);

watch(
  () => props.draft.registration_id,
  (next, previous) => {
    if (!next || next === previous || !selectedRegistration.value) return;
    props.draft.config = {};
  },
);
watch(
  () => [props.type, props.entityId, props.draft.kind],
  () => {
    configurationMode.value = "basic";
  },
);
const mcpToolNamesText = computed({
  get: () => props.draft.mcp_tool_names?.join(", ") || "",
  set: (value) => {
    props.draft.mcp_tool_names = [
      ...new Set(
        value
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      ),
    ];
  },
});
const resultFacts = computed(() => {
  if (!result.value || typeof result.value !== "object") return [];
  return Object.entries(result.value)
    .filter(
      ([, value]) =>
        ["string", "number", "boolean"].includes(typeof value) ||
        (Array.isArray(value) &&
          value.every((item) =>
            ["string", "number", "boolean"].includes(typeof item),
          )),
    )
    .map(([key, value]) => [
      key.replaceAll("_", " "),
      Array.isArray(value) ? value.join("、") : String(value),
    ]);
});

async function discoverModels() {
  discoveryOpen.value = true;
  busy.value = "discover";
  try {
    result.value = await post(
      "/admin/resources/model-providers/discover-preview",
      {
        resource: props.draft,
        api_key: props.secret || null,
      },
    );
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy.value = "";
  }
}

async function registerModel(item) {
  if (registeredModelIds.value.has(item.id) || registering.value) return;
  registering.value = item.id;
  try {
    await saveEntity("models", {
      name: item.id,
      enabled: true,
      provider_ref: props.entityId,
      model_name: item.id,
      protocol: "chat_completions",
      capabilities: {
        tool_calling: item.capabilities?.tool_calling ?? null,
        image_inputs: item.capabilities?.image_inputs ?? null,
        audio_inputs: item.capabilities?.audio_inputs ?? null,
        video_inputs: item.capabilities?.video_inputs ?? null,
        pdf_inputs: item.capabilities?.pdf_inputs ?? null,
      },
      timeout_seconds: null,
      temperature: null,
      top_p: null,
      max_retries: null,
      parameters: {},
    });
  } catch {
    // The store displays the validation or persistence error.
  } finally {
    registering.value = "";
  }
}

async function testCurrent(silent = false) {
  busy.value = "test";
  try {
    if (props.type === "databases")
      result.value = await post(
        `/admin/resources/databases/${props.entityId}/test`,
      );
    if (props.type === "mcp_servers")
      result.value = await post(
        `/admin/resources/mcp-servers/${props.entityId}/test`,
      );
    if (props.type === "skill_sources")
      result.value = await post(
        `/admin/resources/skill-sources/${props.entityId}/scan`,
      );
    if (props.type === "models") {
      const provider = collection("model_providers").find(
        (item) => item.id === props.draft.provider_ref,
      );
      result.value = await post(
        "/admin/resources/model-providers/test-preview",
        { resource: provider, model: props.draft },
      );
    }
    if (!silent) {
      if (props.type === "databases")
        notify(
          `数据库连接正常${result.value?.capabilities?.length ? ` · ${result.value.capabilities.join("、")}` : ""}`,
        );
      if (props.type === "mcp_servers")
        notify(
          result.value?.reachable
            ? `MCP 连接正常 · 发现 ${result.value.tools?.length || 0} 个工具`
            : result.value?.error || "MCP 连接失败",
          !result.value?.reachable,
        );
      if (props.type === "models")
        notify(
          result.value?.ok
            ? "模型请求测试通过"
            : result.value?.error?.message || "模型请求测试失败",
          !result.value?.ok,
        );
      if (props.type === "skill_sources")
        notify(
          result.value?.status === "ok"
            ? `Skill 扫描完成 · 找到 ${result.value.skills?.length || 0} 个`
            : `Skill 扫描完成 · ${result.value.errors?.length || 0} 个问题`,
          result.value?.status === "error",
        );
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy.value = "";
  }
}

async function discoverRemote() {
  busy.value = "discover";
  try {
    const discovered = props.secret
      ? await post("/admin/resources/remote-agents/discover", {
          base_url: props.draft.base_url,
          access_key: props.secret,
          protocol: "auto",
          timeout_seconds: props.draft.timeout_seconds,
        })
      : await post(`/admin/resources/remote-agents/${props.entityId}/discover`);
    props.draft.remote_service_id = discovered.service_id;
    props.draft.remote_agent_id = discovered.agent_id;
    props.draft.protocols = discovered.protocols;
    if (!props.draft.description)
      props.draft.description = discovered.description;
    result.value = discovered;
    notify(
      `远程 Agent 发现成功 · ${discovered.agent_id || discovered.service_id}`,
    );
  } catch (error) {
    notify(error.message, true);
  } finally {
    busy.value = "";
  }
}

function resetVariant(kind) {
  const common = { name: props.draft.name, enabled: props.draft.enabled, kind };
  let specific = {};
  if (props.type === "plugins" && kind === "agent_skills")
    specific = { skill_source_refs: [], strict: false };
  if (props.type === "plugins" && kind === "context_offloader")
    specific = {
      database_ref: collection("databases")[0]?.id || null,
      max_result_tokens: 2500,
      preview_tokens: 1000,
      include_retrieval_tool: true,
    };
  if (props.type === "plugins" && kind === "goal_loop")
    specific = {
      goal: "",
      max_attempts: 3,
      timeout_seconds: 300,
      preserve_context: true,
    };
  if (props.type === "plugins" && kind === "registered")
    specific = { registration_id: "", config: {} };
  if (props.type === "interventions" && kind === "human_in_the_loop")
    specific = { tool_refs: [], mcp_tool_names: [], enable_trust: false };
  if (props.type === "interventions" && kind === "registered")
    specific = { registration_id: "", config: {}, on_error: "throw" };
  for (const key of Object.keys(props.draft))
    if (!["id", "display_name", "managed_by", "metadata"].includes(key))
      delete props.draft[key];
  Object.assign(props.draft, common, specific);
}

onMounted(() => {
  if (props.type === "skill_sources" && !props.creating) testCurrent(true);
});
</script>

<template>
  <div class="resource-editor-form">
    <section class="editor-panel">
      <div class="stacked-fields">
        <label
          >名称<input
            v-model="draft.name"
            class="field"
            :readonly="type === 'tools'"
            required
        /></label>
        <label v-if="type === 'remote_agents'"
          >模型可见描述<textarea
            v-model="draft.description"
            class="field"
            placeholder="说明该能力何时应该被使用。"
          />
        </label>
      </div>
    </section>

    <section v-if="type === 'model_providers'" class="editor-panel">
      <ConfigurationModes
        v-model="configurationMode"
        advanced-label="自定义 Header"
      >
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="服务地址"
              description="兼容 OpenAI API 的基础 URL，例如 https://example.com/v1。"
              required
              ><input
                v-model="draft.base_url"
                class="field control-wide"
                type="url"
            /></SettingRow>
            <SettingRow
              title="协议适配器"
              description="决定模型发现和请求的协议实现。"
              ><SelectControl
                v-model="draft.adapter"
                :options="[
                  { value: 'openai_compatible', label: 'OpenAI Compatible' },
                ]"
            /></SettingRow>
            <SettingRow
              title="请求超时"
              description="服务商发现与默认模型请求的等待时间。"
              ><input
                v-model.number="draft.timeout_seconds"
                class="field compact-control"
                type="number"
                min="1"
            /></SettingRow>
            <SettingRow
              title="API Key"
              description="留空不会清除已经保存的凭据。"
              ><input
                :value="secret"
                class="field control-wide"
                type="password"
                placeholder="输入新凭据"
                @input="emit('update:secret', $event.target.value)"
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <StructuredSettings
            v-model="draft.headers"
            :schema="stringMapSchema"
            headerless
          />
        </template>
      </ConfigurationModes>
      <div class="provider-actions">
        <UiButton
          variant="secondary"
          :busy="busy === 'discover'"
          :disabled="creating"
          @click="discoverModels"
          >发现模型</UiButton
        >
      </div>
    </section>

    <section v-else-if="type === 'models'" class="editor-panel">
      <ConfigurationModes v-model="configurationMode" advanced-label="推理设置">
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="模型服务商"
              description="凭据和基础 URL 由服务商统一管理。"
              required
              ><SelectControl
                v-model="draft.provider_ref"
                :options="providerOptions"
            /></SettingRow>
            <SettingRow
              title="服务商模型 ID"
              description="请求中实际发送的模型名。"
              required
              ><input v-model="draft.model_name" class="field control-wide"
            /></SettingRow>
            <SettingRow
              title="请求协议"
              description="某些推理模型只支持 Responses API。"
              ><SelectControl
                v-model="draft.protocol"
                :options="[
                  { value: 'chat_completions', label: 'Chat Completions' },
                  { value: 'responses', label: 'Responses API' },
                ]"
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <div class="settings-group">
            <SettingRow title="温度" description="留空使用服务商默认值。"
              ><input
                v-model.number="draft.temperature"
                class="field compact-control"
                type="number"
                min="0"
                max="2"
                step="0.1"
            /></SettingRow>
            <SettingRow title="Top P" description="留空使用服务商默认值。"
              ><input
                v-model.number="draft.top_p"
                class="field compact-control"
                type="number"
                min="0"
                max="1"
                step="0.05"
            /></SettingRow>
            <SettingRow title="请求超时" description="留空继承模型服务商设置。"
              ><input
                v-model.number="draft.timeout_seconds"
                class="field compact-control"
                type="number"
                min="0.1"
                step="0.1"
            /></SettingRow>
            <SettingRow
              title="最大重试"
              description="留空使用模型适配器默认值。"
              ><input
                v-model.number="draft.max_retries"
                class="field compact-control"
                type="number"
                min="0"
            /></SettingRow>
          </div>
          <div class="section-divider" />
          <div class="subsection-heading">
            <div>
              <h3>显式能力</h3>
              <UiDescription
                >不按模型名称猜测。未确认保持“未知”，确认不支持时关闭。</UiDescription
              >
            </div>
          </div>
          <div class="model-capability-grid">
            <SettingRow
              v-for="[field, label] in [
                ['tool_calling', '工具调用'],
                ['image_inputs', '图片输入'],
                ['audio_inputs', '语音输入'],
                ['video_inputs', '视频输入'],
                ['pdf_inputs', 'PDF 输入'],
              ]"
              :key="field"
              :title="label"
              description="三态：未知 / 支持 / 不支持。"
              ><SelectControl
                v-model="draft.capabilities[field]"
                :options="[
                  { value: null, label: '未知' },
                  { value: true, label: '支持' },
                  { value: false, label: '不支持' },
                ]"
            /></SettingRow>
          </div>
          <div class="section-divider" />
          <StructuredSettings
            v-model="draft.parameters"
            title="额外推理参数"
            description="字段由当前模型适配器声明并校验；留空使用适配器默认值。"
            :schema="modelParametersSchema"
          />
        </template>
      </ConfigurationModes>
      <div v-if="!creating" class="action-strip">
        <UiButton
          variant="secondary"
          :busy="busy === 'test'"
          @click="testCurrent()"
          >测试模型</UiButton
        ><span>发送最小文本请求验证协议、参数和凭据。</span>
      </div>
    </section>

    <section v-else-if="type === 'databases'" class="editor-panel">
      <ConfigurationModes v-model="configurationMode" advanced-label="连接选项">
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="数据库类型"
              description="决定连接驱动和可用能力。"
              ><SelectControl
                v-model="draft.engine"
                :options="
                  state.features.database_engines.map((value) => ({
                    value,
                    label: value.toUpperCase(),
                  }))
                " /></SettingRow
            ><SettingRow
              title="连接 URL"
              description="SQLite 可使用相对于 state_dir 的路径。"
              required
              ><input v-model="draft.url" class="field control-wide"
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <StructuredSettings
            v-model="draft.options"
            :schema="databaseOptionSchema"
            headerless
          />
        </template>
      </ConfigurationModes>
      <div v-if="!creating" class="action-strip">
        <UiButton
          variant="secondary"
          :busy="busy === 'test'"
          @click="testCurrent()"
          >测试连接</UiButton
        ><span v-if="result?.capabilities"
          >能力：{{ result.capabilities.join("、") }}</span
        >
      </div>
    </section>

    <section v-else-if="type === 'tools'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="Python 位置"
          description="由 @register.tool 注册，只读。"
          ><code>{{ draft.python_path }}</code></SettingRow
        ><SettingRow
          title="需要沙箱"
          description="启用后，挂载此工具的 Agent 必须选择沙箱。"
          ><ToggleSwitch v-model="draft.requires_sandbox"
        /></SettingRow>
      </div>
      <ToolDescriptionEditor :draft="draft" />
    </section>

    <section v-else-if="type === 'mcp_servers'" class="editor-panel">
      <ConfigurationModes
        v-model="configurationMode"
        advanced-label="自定义 Header"
      >
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="Streamable HTTP URL"
              description="当前版本只开放 streamable_http。"
              ><input
                v-model="draft.url"
                class="field control-wide"
                type="url"
                required /></SettingRow
            ><SettingRow title="认证凭据" description="留空不会清除已有凭据。"
              ><input
                :value="secret"
                class="field control-wide"
                type="password"
                @input="
                  emit('update:secret', $event.target.value)
                " /></SettingRow
            ><SettingRow
              title="请求超时"
              description="连接与工具调用的最长等待时间。"
              ><input
                v-model.number="draft.timeout_seconds"
                class="field compact-control"
                type="number"
                min="1"
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <StructuredSettings
            v-model="draft.headers"
            :schema="stringMapSchema"
            headerless
          />
        </template>
      </ConfigurationModes>
      <div v-if="!creating" class="action-strip">
        <UiButton
          variant="secondary"
          :busy="busy === 'test'"
          @click="testCurrent()"
          >测试连接</UiButton
        >
      </div>
    </section>

    <section v-else-if="type === 'remote_agents'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="服务地址"
          description="MeldFabric Remote 或 A2A 服务基础 URL。"
          ><input
            v-model="draft.base_url"
            class="field control-wide"
            type="url"
            required /></SettingRow
        ><SettingRow title="访问 Key" description="用于发现与调用远程 Agent。"
          ><input
            :value="secret"
            class="field control-wide"
            type="password"
            @input="emit('update:secret', $event.target.value)" /></SettingRow
        ><SettingRow title="调用超时" description="远程调用最长等待时间。"
          ><input
            v-model.number="draft.timeout_seconds"
            class="field compact-control"
            type="number"
            min="1"
        /></SettingRow>
      </div>
      <div class="action-strip">
        <UiButton
          variant="secondary"
          :busy="busy === 'discover'"
          :disabled="creating && !secret"
          @click="discoverRemote"
          >发现远程 Agent</UiButton
        ><span v-if="draft.protocols?.length"
          >协议：{{ draft.protocols.join("、") }}</span
        >
      </div>
    </section>

    <section v-else-if="type === 'skill_sources'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="Skill 目录"
          description="相对路径以 state_dir 为根；每个 Skill 子目录应包含 SKILL.md。"
          ><input
            v-model="draft.path"
            class="field control-wide"
            required /></SettingRow
        ><SettingRow title="加载顺序" description="数字较大的目录后加载。"
          ><input
            v-model.number="draft.order"
            class="field compact-control"
            type="number"
        /></SettingRow>
      </div>
      <div v-if="!creating" class="action-strip">
        <UiButton
          variant="secondary"
          :busy="busy === 'test'"
          @click="testCurrent()"
          >扫描 Skill</UiButton
        ><span v-if="result"
          >找到 {{ result.skills?.length || result.items?.length || 0 }} 个
          Skill</span
        >
      </div>
      <div v-if="result?.skills?.length" class="skill-catalog">
        <article v-for="skill in result.skills" :key="skill.path">
          <span class="skill-mark">S</span>
          <div>
            <strong>{{ skill.name }}</strong
            ><small>{{ skill.description || skill.path }}</small>
          </div>
          <code v-if="skill.allowed_tools?.length">{{
            skill.allowed_tools.join(", ")
          }}</code>
        </article>
      </div>
      <div
        v-else-if="result && !result.skills?.length"
        class="empty-state compact"
      >
        该目录中没有发现有效的 Skill。
      </div>
      <div v-if="result?.errors?.length" class="inline-errors">
        <p v-for="error in result.errors" :key="error">{{ error }}</p>
      </div>
    </section>

    <section v-else-if="type === 'plugins'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="插件类型"
          description="类型在创建后不可修改；同一配置可以挂载到多个 Agent。"
          ><SelectControl
            v-model="draft.kind"
            :disabled="!creating"
            :options="[
              { value: 'agent_skills', label: 'AgentSkills' },
              { value: 'context_offloader', label: 'ContextOffloader' },
              { value: 'goal_loop', label: 'GoalLoop' },
              { value: 'registered', label: '代码注册插件' },
            ]"
            @change="resetVariant"
        /></SettingRow>
      </div>
      <template v-if="draft.kind === 'agent_skills'"
        ><div class="plugin-skill-config">
          <div class="subsection-heading">
            <div>
              <h3>Skill 目录</h3>
              <UiDescription
                >至少选择一个已启用的目录。Agent 只会读取这里明确选中的
                Skill。</UiDescription
              >
            </div>
            <span v-if="availableSkillSources.length" class="selection-count"
              >已选择 {{ draft.skill_source_refs?.length || 0 }} 项</span
            >
          </div>
          <div v-if="availableSkillSources.length" class="choice-grid">
            <label
              v-for="source in availableSkillSources"
              :key="source.id"
              :class="{
                selected: draft.skill_source_refs?.includes(source.id),
              }"
              ><input
                v-model="draft.skill_source_refs"
                type="checkbox"
                :value="source.id"
              /><span
                ><strong>{{ source.name }}</strong
                ><small>{{ source.path }}</small></span
              ></label
            >
          </div>
          <div v-else class="resource-prerequisite">
            <div>
              <strong>还没有可用的 Skill 目录</strong>
              <UiDescription
                >先创建并启用一个 Skill 目录，再回来创建 AgentSkills
                插件。</UiDescription
              >
            </div>
            <UiButton
              variant="secondary"
              @click="router.push('/resources/skill_sources/new')"
              >创建 Skill 目录</UiButton
            >
          </div>
          <p
            v-if="
              availableSkillSources.length && !draft.skill_source_refs?.length
            "
            class="field-error"
          >
            请选择至少一个 Skill 目录后再保存。
          </p>
        </div>
        <div class="settings-group plugin-option-grid">
          <SettingRow
            title="严格模式"
            description="目录或 Skill 无效时阻止 Agent 启动。"
            ><ToggleSwitch v-model="draft.strict"
          /></SettingRow></div
      ></template>
      <template v-else-if="draft.kind === 'context_offloader'"
        ><div class="settings-group">
          <SettingRow title="存储数据库" description="保存被卸载的上下文内容。"
            ><SelectControl
              v-model="draft.database_ref"
              :options="databaseOptions" /></SettingRow
          ><SettingRow
            title="最大结果 Token"
            description="超过阈值的工具结果会被卸载。"
            ><input
              v-model.number="draft.max_result_tokens"
              class="field compact-control"
              type="number"
              min="100" /></SettingRow
          ><SettingRow
            title="预览 Token"
            description="卸载后留在上下文中的结果预览长度。"
            ><input
              v-model.number="draft.preview_tokens"
              class="field compact-control"
              type="number"
              min="50" /></SettingRow
          ><SettingRow title="提供检索工具" description="允许模型取回卸载内容。"
            ><ToggleSwitch v-model="draft.include_retrieval_tool"
          /></SettingRow></div
      ></template>
      <template v-else-if="draft.kind === 'goal_loop'"
        ><div class="stacked-fields">
          <label
            >固定目标<textarea v-model="draft.goal" class="field" required />
          </label>
        </div>
        <div class="settings-group">
          <SettingRow title="最大尝试次数" description="GoalLoop 的改进轮次。"
            ><input
              v-model.number="draft.max_attempts"
              class="field compact-control"
              type="number"
              min="1"
              max="20"
          /></SettingRow>
          <SettingRow
            title="运行超时"
            description="一次目标循环允许执行的秒数。"
            ><input
              v-model.number="draft.timeout_seconds"
              class="field compact-control"
              type="number"
              min="0.1"
              step="0.1"
          /></SettingRow>
          <SettingRow
            title="保留上下文"
            description="在改进轮次之间继续使用已有会话上下文。"
            ><ToggleSwitch v-model="draft.preserve_context"
          /></SettingRow></div
      ></template>
      <template v-else>
        <ConfigurationModes
          v-model="configurationMode"
          advanced-label="参数配置"
        >
          <template #basic>
            <div class="settings-group">
              <SettingRow
                title="代码注册项"
                description="由 @register.plugin 登记的插件实现。"
                ><SelectControl
                  v-model="draft.registration_id"
                  :disabled="!creating"
                  :options="registrationOptions('plugin')"
              /></SettingRow>
            </div>
          </template>
          <template #advanced>
            <ConfigSchemaForm
              v-model="draft.config"
              :schema="selectedRegistration?.config_schema || {}"
              :option-sources="configOptionSources"
              headerless
            />
          </template>
        </ConfigurationModes>
      </template>
    </section>

    <section v-else-if="type === 'hooks'" class="editor-panel">
      <ConfigurationModes v-model="configurationMode" advanced-label="参数配置">
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="代码注册项"
              description="选择 @register.hook 或 @register.hook_group 登记的实现。"
              ><SelectControl
                v-model="draft.registration_id"
                :disabled="!creating"
                :options="registrationOptions('hook')"
                required
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <ConfigSchemaForm
            v-model="draft.config"
            :schema="selectedRegistration?.config_schema || {}"
            :option-sources="configOptionSources"
            headerless
          />
        </template>
      </ConfigurationModes>
    </section>

    <section v-else-if="type === 'interventions'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="Intervention 类型"
          description="人工确认使用 Strands 原生 HumanInTheLoop。"
          ><SelectControl
            v-model="draft.kind"
            :disabled="!creating"
            :options="[
              { value: 'human_in_the_loop', label: 'Human in the Loop' },
              { value: 'registered', label: '代码注册 Intervention' },
            ]"
            @change="resetVariant"
        /></SettingRow>
      </div>
      <template v-if="draft.kind === 'human_in_the_loop'"
        ><div class="choice-grid">
          <label
            v-for="tool in collection('tools')"
            :key="tool.id"
            :class="{ selected: draft.tool_refs.includes(tool.id) }"
            ><input
              v-model="draft.tool_refs"
              type="checkbox"
              :value="tool.id"
            /><span
              ><strong>{{ tool.name }}</strong
              ><small>{{ tool.description || "代码工具" }}</small></span
            ></label
          >
        </div>
        <SettingRow
          title="MCP 工具名称"
          description="填入需要拦截的 MCP 工具名，多个名称使用逗号分隔。"
          ><input
            v-model="mcpToolNamesText"
            class="field control-wide"
            placeholder="create_order, delete_record"
        /></SettingRow>
        <SettingRow
          title="允许信任选择"
          description="用户可以在后续调用中信任同类操作。"
          ><ToggleSwitch v-model="draft.enable_trust" /></SettingRow></template
      ><template v-else>
        <ConfigurationModes
          v-model="configurationMode"
          advanced-label="参数配置"
        >
          <template #basic>
            <div class="settings-group">
              <SettingRow
                title="代码注册项"
                description="选择 @register.intervention 或分组注册项。"
                ><SelectControl
                  v-model="draft.registration_id"
                  :disabled="!creating"
                  :options="registrationOptions('intervention')"
                  required
              /></SettingRow>
              <SettingRow
                title="异常策略"
                description="Intervention 自身异常时是抛错、继续还是拒绝。"
              >
                <SelectControl
                  v-model="draft.on_error"
                  :options="[
                    { value: 'throw', label: '抛出异常' },
                    { value: 'proceed', label: '继续执行' },
                    { value: 'deny', label: '拒绝执行' },
                  ]"
                />
              </SettingRow>
            </div>
          </template>
          <template #advanced>
            <ConfigSchemaForm
              v-model="draft.config"
              :schema="selectedRegistration?.config_schema || {}"
              :option-sources="configOptionSources"
              headerless
            />
          </template>
        </ConfigurationModes>
      </template>
    </section>

    <section v-else-if="type === 'sandbox_profiles'" class="editor-panel">
      <ConfigurationModes
        v-model="configurationMode"
        advanced-label="提供方配置"
      >
        <template #basic>
          <div class="settings-group">
            <SettingRow
              title="沙箱提供方"
              description="当前安装可用的原生 Strands 沙箱适配器。"
              ><SelectControl
                v-model="draft.provider"
                :options="
                  state.features.sandbox_providers.map((value) => ({
                    value,
                    label: value,
                  }))
                " /></SettingRow
            ><SettingRow title="允许联网" description="关闭时容器使用隔离网络。"
              ><ToggleSwitch v-model="draft.network_enabled" /></SettingRow
            ><SettingRow
              title="CPU 限额"
              description="单容器可使用的 CPU 核数。"
              ><input
                v-model.number="draft.cpu_limit"
                class="field compact-control"
                type="number"
                min="0.1"
                step="0.1" /></SettingRow
            ><SettingRow title="内存限额 MB" description="单容器内存上限。"
              ><input
                v-model.number="draft.memory_limit_mb"
                class="field compact-control"
                type="number"
                min="16"
            /></SettingRow>
          </div>
        </template>
        <template #advanced>
          <StructuredSettings
            v-model="draft.config"
            :schema="sandboxConfigSchema"
            headerless
          />
        </template>
      </ConfigurationModes>
    </section>

    <section
      v-else-if="type === 'observability_providers'"
      class="editor-panel"
    >
      <div class="settings-group">
        <SettingRow title="Langfuse 地址" description="自托管或云服务基础 URL。"
          ><input
            v-model="draft.host"
            class="field control-wide"
            type="url"
            required /></SettingRow
        ><SettingRow title="Public Key" description="项目公开标识。"
          ><input
            v-model="draft.public_key"
            class="field control-wide"
            required /></SettingRow
        ><SettingRow
          title="Secret Key"
          description="加密保存；留空不改变已有凭据。"
          ><input
            :value="secret"
            class="field control-wide"
            type="password"
            @input="emit('update:secret', $event.target.value)" /></SettingRow
        ><SettingRow title="采样比例" description="0 到 1 之间。"
          ><input
            v-model.number="draft.sample_rate"
            class="field compact-control"
            type="number"
            min="0"
            max="1"
            step="0.1" /></SettingRow
        ><SettingRow
          title="失败关闭"
          description="观测服务不可用时停止 Agent 推理。"
          ><ToggleSwitch v-model="draft.fail_closed"
        /></SettingRow>
      </div>
    </section>

    <section
      v-if="resultFacts.length && !result?.items && type !== 'skill_sources'"
      class="operation-result"
    >
      <strong>操作结果</strong>
      <dl>
        <template v-for="[key, value] in resultFacts" :key="key"
          ><dt>{{ key }}</dt>
          <dd>{{ value }}</dd></template
        >
      </dl>
    </section>
  </div>

  <ModalFrame
    v-if="discoveryOpen"
    title="发现模型"
    @dismiss="discoveryOpen = false"
  >
    <div class="model-discovery-toolbar">
      <SearchField
        v-model="discoverySearch"
        placeholder="检索模型…"
        autofocus
      />
      <span>{{ discoveredModels.length }} 个结果</span>
    </div>
    <div v-if="busy === 'discover'" class="modal-loading">
      正在读取服务商模型列表…
    </div>
    <div v-else-if="discoveredModels.length" class="model-discovery-list">
      <article v-for="item in discoveredModels" :key="item.id">
        <div>
          <strong>{{ item.id }}</strong
          ><small v-if="Object.keys(item.capabilities || {}).length">{{
            Object.keys(item.capabilities).join(" · ")
          }}</small>
        </div>
        <span v-if="registeredModelIds.has(item.id)" class="registered-mark"
          >已注册</span
        >
        <UiButton
          v-else
          variant="secondary"
          :busy="registering === item.id"
          :disabled="Boolean(registering)"
          @click="registerModel(item)"
          >注册</UiButton
        >
      </article>
    </div>
    <div v-else class="empty-state compact">没有匹配的模型。</div>
  </ModalFrame>
</template>
