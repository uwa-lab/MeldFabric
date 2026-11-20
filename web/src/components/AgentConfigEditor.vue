<script setup>
import { computed, ref } from "vue";
import { state, collection } from "../store.js";
import SettingRow from "./SettingRow.vue";
import ToggleSwitch from "./ToggleSwitch.vue";
import SelectControl from "./SelectControl.vue";
import BindingPicker from "./BindingPicker.vue";
import PromptEditor from "./PromptEditor.vue";
import StructuredSettings from "./StructuredSettings.vue";

const draft = defineModel("draft", { type: Object, required: true });
const tabs = [
  ["basic", "基本设置"],
  ["prompt", "Prompt"],
  ["capabilities", "能力挂载"],
  ["context", "上下文"],
  ["persistence", "会话与记忆"],
  ["extensions", "扩展"],
  ["runtime", "输出与运行"],
];
const tab = ref("basic");
const capabilityTab = ref("tools");

const options = (items, empty = "不使用") => [
  { value: null, label: empty },
  ...items
    .filter((item) => item.enabled !== false)
    .map((item) => ({
      value: item.id,
      label: item.display_name || item.name,
    })),
];
const modelOptions = computed(() => options(collection("models"), "尚未选择"));
const databaseOptions = computed(() => options(collection("databases")));
const sandboxOptions = computed(() => options(collection("sandbox_profiles")));
const observabilityOptions = computed(() => options(collection("observability_providers")));
const jsonObjectSchema = computed(
  () =>
    state.features?.structured_schemas?.json_object || {
      type: "object",
      additionalProperties: {},
    },
);
const remoteNames = computed(
  () =>
    new Map(collection("remote_agents").map((item) => [item.id, item.display_name || item.name])),
);
const sessionPolicyOptions = [
  { value: "per_call", label: "每次调用新会话" },
  { value: "parent_thread", label: "父会话复用" },
  { value: "per_user", label: "用户复用" },
];
const interactionPolicyOptions = [
  { value: "fail", label: "需要交互时失败" },
  { value: "bubble_to_parent", label: "交给父调用方处理" },
  { value: "parent_auto_answer", label: "父 Agent 自动回答" },
  { value: "direct_interaction", label: "远程直接交互" },
];

function setRemoteSession(binding, value) {
  binding.session_policy = value;
  if (value === "per_call") binding.interaction_policy = "fail";
}

function enableSession(value) {
  draft.value.session_manager = value
    ? {
        database_ref: collection("databases")[0]?.id || null,
        save_latest_on: "invocation",
        checkpointing: false,
      }
    : null;
}
function enableMemory(value) {
  draft.value.memory_manager = value
    ? {
        database_ref: collection("databases")[0]?.id || null,
        namespace: null,
        search_tool: true,
        add_tool: false,
        injection: true,
      }
    : null;
}
</script>

<template>
  <div class="entity-workspace">
    <nav class="editor-tabs" aria-label="Agent 配置区域">
      <button
        v-for="[id, label] in tabs"
        :key="id"
        type="button"
        :class="{ active: tab === id }"
        @click="tab = id"
      >
        {{ label }}
      </button>
    </nav>

    <section v-if="tab === 'basic'" class="editor-panel">
      <div class="stacked-fields">
        <label
          >名称<input v-model="draft.name" class="field" required placeholder="research_agent"
        /></label>
        <label
          >用途描述<textarea
            v-model="draft.description"
            class="field"
            placeholder="描述职责和适用场景，编排和模型会使用这段说明。"
          />
        </label>
      </div>
      <div class="settings-group">
        <SettingRow title="推理模型" description="未启用的模型不会出现在候选列表中。" required
          ><SelectControl v-model="draft.model_ref" :options="modelOptions"
        /></SettingRow>
      </div>
    </section>

    <section v-else-if="tab === 'prompt'" class="editor-panel flush">
      <PromptEditor v-model="draft.prompt" />
    </section>

    <section v-else-if="tab === 'capabilities'" class="editor-panel flush">
      <div class="split-editor capability-editor">
        <aside class="split-index">
          <button
            v-for="[id, label] in [
              ['tools', '工具'],
              ['mcp', 'MCP'],
              ['remote', '远程 Agent'],
            ]"
            :key="id"
            type="button"
            :class="{ active: capabilityTab === id }"
            @click="capabilityTab = id"
          >
            {{ label }}
          </button>
        </aside>
        <div class="split-editor-body">
          <BindingPicker
            v-if="capabilityTab === 'tools'"
            v-model="draft.tool_bindings"
            :items="collection('tools')"
            ref-field="tool_ref"
            empty-available="没有可用工具；先在代码中 import 使用 @register.tool 的函数。"
          />
          <BindingPicker
            v-else-if="capabilityTab === 'mcp'"
            v-model="draft.mcp_bindings"
            :items="collection('mcp_servers')"
            ref-field="mcp_ref"
          />
          <template v-else>
            <BindingPicker
              v-model="draft.remote_tool_bindings"
              :items="collection('remote_agents')"
              ref-field="remote_agent_ref"
              :defaults="{
                session_policy: 'parent_thread',
                interaction_policy: 'bubble_to_parent',
              }"
            />
            <div v-if="draft.remote_tool_bindings.length" class="remote-binding-settings">
              <header>
                <h3>调用策略</h3>
                <UiDescription>为每个远程 Agent 独立设置会话映射与交互处理方式。</UiDescription>
              </header>
              <article
                v-for="binding in draft.remote_tool_bindings"
                :key="binding.id || binding.remote_agent_ref"
              >
                <strong>{{ remoteNames.get(binding.remote_agent_ref) || "资源不可用" }}</strong>
                <SelectControl
                  :model-value="binding.session_policy"
                  :options="sessionPolicyOptions"
                  aria-label="远程会话策略"
                  @update:model-value="setRemoteSession(binding, $event)"
                />
                <SelectControl
                  v-model="binding.interaction_policy"
                  :options="
                    binding.session_policy === 'per_call'
                      ? interactionPolicyOptions.filter((item) => item.value === 'fail')
                      : interactionPolicyOptions
                  "
                  aria-label="远程交互策略"
                />
              </article>
            </div>
          </template>
        </div>
      </div>
    </section>

    <section v-else-if="tab === 'context'" class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="上下文管理"
          description="Strands ContextManager；启用时不会同时运行 ConversationManager。"
          ><SelectControl
            v-model="draft.context_manager.strategy"
            :options="
              [
                ['auto', '自动管理'],
                ['agentic', 'Agent 主动管理'],
                ['disabled', '使用会话裁剪'],
              ].map(([value, label]) => ({ value, label }))
            "
        /></SettingRow>
      </div>
      <template v-if="draft.context_manager.strategy === 'disabled'">
        <div class="section-divider" />
        <div class="settings-group">
          <SettingRow title="会话裁剪策略" description="只在上下文管理关闭时生效。"
            ><SelectControl
              v-model="draft.conversation_manager.strategy"
              :options="
                [
                  ['sliding_window', '滑动窗口'],
                  ['summarizing', '摘要压缩'],
                  ['none', '不裁剪'],
                ].map(([value, label]) => ({ value, label }))
              "
          /></SettingRow>
          <SettingRow
            v-if="draft.conversation_manager.strategy === 'sliding_window'"
            title="窗口大小"
            description="保留的消息窗口规模。"
            ><input
              v-model.number="draft.conversation_manager.window_size"
              class="field compact-control"
              type="number"
              min="2"
          /></SettingRow>
          <SettingRow
            title="固定首部消息"
            description="始终保留最前方的消息数量；留空表示使用 Strands 默认值。"
            ><input
              v-model.number="draft.conversation_manager.pin_first"
              class="field compact-control"
              type="number"
              min="0"
          /></SettingRow>
          <SettingRow title="主动压缩" description="接近模型上下文限制前主动整理内容。"
            ><ToggleSwitch v-model="draft.conversation_manager.proactive_compression"
          /></SettingRow>
        </div>
      </template>
    </section>

    <section v-else-if="tab === 'persistence'" class="editor-panel">
      <div class="subsection-heading">
        <div>
          <h3>会话快照</h3>
          <UiDescription>保存 Strands Agent 的消息、状态和中断恢复数据。</UiDescription>
        </div>
        <ToggleSwitch :model-value="!!draft.session_manager" @update:model-value="enableSession" />
      </div>
      <div v-if="draft.session_manager" class="settings-group">
        <SettingRow title="存储数据库" description="需要支持 session_storage 能力。" required
          ><SelectControl v-model="draft.session_manager.database_ref" :options="databaseOptions"
        /></SettingRow>
        <SettingRow title="保存时机" description="一次调用结束或每条消息后写入快照。"
          ><SelectControl
            v-model="draft.session_manager.save_latest_on"
            :options="[
              { value: 'invocation', label: '调用结束' },
              { value: 'message', label: '每条消息' },
            ]"
        /></SettingRow>
        <SettingRow title="启用 Checkpoint" description="为原生干预和恢复记录中间检查点。"
          ><ToggleSwitch v-model="draft.session_manager.checkpointing"
        /></SettingRow>
      </div>
      <div class="section-divider" />
      <div class="subsection-heading">
        <div>
          <h3>长期记忆</h3>
          <UiDescription>按 Agent 与用户隔离的 Strands MemoryManager。</UiDescription>
        </div>
        <ToggleSwitch :model-value="!!draft.memory_manager" @update:model-value="enableMemory" />
      </div>
      <div v-if="draft.memory_manager" class="settings-group">
        <SettingRow title="存储数据库" description="需要支持 memory_storage 能力。" required
          ><SelectControl v-model="draft.memory_manager.database_ref" :options="databaseOptions"
        /></SettingRow>
        <SettingRow title="命名空间" description="在默认隔离键上增加业务分区。"
          ><input
            v-model="draft.memory_manager.namespace"
            class="field compact-control"
            placeholder="default"
        /></SettingRow>
        <SettingRow title="记忆搜索工具" description="允许模型主动检索长期记忆。"
          ><ToggleSwitch v-model="draft.memory_manager.search_tool"
        /></SettingRow>
        <SettingRow title="记忆写入工具" description="允许模型主动写入长期记忆。"
          ><ToggleSwitch v-model="draft.memory_manager.add_tool"
        /></SettingRow>
        <SettingRow title="自动注入" description="运行时自动把相关记忆注入上下文。"
          ><ToggleSwitch v-model="draft.memory_manager.injection"
        /></SettingRow>
      </div>
    </section>

    <section v-else-if="tab === 'extensions'" class="editor-panel">
      <div class="extension-stack">
        <div>
          <h3>插件</h3>
          <UiDescription>插件可在初始化阶段补充提示词、工具和生命周期行为。</UiDescription>
          <BindingPicker
            v-model="draft.plugin_bindings"
            :items="collection('plugins')"
            ref-field="plugin_ref"
            :defaults="{ order: 0 }"
            adaptive-height
          />
        </div>
        <div>
          <h3>Hooks</h3>
          <UiDescription>在已注册的生命周期事件发生时执行应用代码。</UiDescription>
          <BindingPicker
            v-model="draft.hook_bindings"
            :items="collection('hooks')"
            ref-field="hook_ref"
            :defaults="{ order: 0 }"
            adaptive-height
          />
        </div>
        <div>
          <h3>Interventions</h3>
          <UiDescription>在工具调用期间介入；人工确认需要启用会话快照。</UiDescription>
          <BindingPicker
            v-model="draft.intervention_bindings"
            :items="collection('interventions')"
            ref-field="intervention_ref"
            :defaults="{ order: 0 }"
            adaptive-height
          />
        </div>
      </div>
    </section>

    <section v-else class="editor-panel">
      <div class="settings-group">
        <SettingRow
          title="失败重试"
          description="对可重试的模型服务错误使用 Strands ModelRetryStrategy。"
          ><ToggleSwitch v-model="draft.retry_strategy.enabled"
        /></SettingRow>
        <SettingRow
          v-if="draft.retry_strategy.enabled"
          title="最大尝试次数"
          description="包括首次请求。"
          ><input
            v-model.number="draft.retry_strategy.max_attempts"
            class="field compact-control"
            type="number"
            min="1"
            max="20"
        /></SettingRow>
        <SettingRow title="后台任务" description="允许工具在当前调用结束前并行执行长耗时工作。"
          ><ToggleSwitch v-model="draft.background_tasks.enabled"
        /></SettingRow>
        <SettingRow title="执行沙箱" description="需要隔离执行的工具只有选择沙箱后才能运行。"
          ><SelectControl v-model="draft.sandbox_ref" :options="sandboxOptions"
        /></SettingRow>
        <SettingRow title="运行观测" description="把调用链路发送到已配置的 Langfuse 服务。"
          ><SelectControl v-model="draft.observability_ref" :options="observabilityOptions"
        /></SettingRow>
      </div>
      <div class="section-divider" />
      <div class="subsection-heading">
        <div>
          <h3>结构化输出</h3>
          <UiDescription>让 Agent 结果按 JSON Schema 解析为结构化对象。</UiDescription>
        </div>
        <ToggleSwitch v-model="draft.structured_output.enabled" />
      </div>
      <div v-if="draft.structured_output.enabled" class="stacked-fields">
        <label
          >输出类型名称<input v-model="draft.structured_output.name" class="field" required
        /></label>
        <StructuredSettings
          v-model="draft.structured_output.json_schema"
          title="JSON Schema"
          description="定义结果对象的字段、类型和必填约束。"
          :schema="jsonObjectSchema"
        />
      </div>
      <div class="section-divider" />
      <StructuredSettings
        v-model="draft.initial_state"
        title="初始状态"
        description="在每个新 Agent 实例中可用的 JSON 状态；没有需要时保持默认。"
        :schema="jsonObjectSchema"
        collapsible
      />
    </section>
  </div>
</template>
