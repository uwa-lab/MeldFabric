<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, post, streamSSE } from "../api.js";
import ModalFrame from "../components/ModalFrame.vue";
import Icon from "../components/Icon.vue";
import { confirmAction } from "../dialogs.js";
import { notify, state } from "../store.js";

const route = useRoute();
const router = useRouter();
const conversations = ref([]);
const endpoints = ref([]);
const current = ref(null);
const messages = ref([]);
const loading = ref(true);
const loadingConversation = ref(false);
const creating = ref(false);
const deleting = ref(false);
const running = ref(false);
const draft = ref("");
const query = ref("");
const endpointDialog = ref(false);
const selectedEndpoint = ref("");
const messagePane = ref(null);
const composer = ref(null);
const activeRunId = ref("");
const streamingMessageId = ref("");
const awaitingOutput = ref(false);
const interruptDraft = ref("");
const composingDraft = ref(false);
let controller = null;
const textSegmenter =
  typeof Intl !== "undefined" && Intl.Segmenter
    ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
    : null;

const filteredConversations = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase();
  if (!needle) return conversations.value;
  return conversations.value.filter((item) =>
    [item.title, item.endpoint_name, item.last_message_preview]
      .filter(Boolean)
      .some((value) => String(value).toLocaleLowerCase().includes(needle)),
  );
});
const selectedEndpointItem = computed(() =>
  endpoints.value.find((item) => item.id === selectedEndpoint.value),
);
const canCreate = computed(
  () => selectedEndpointItem.value?.latest_enabled && selectedEndpointItem.value?.runnable,
);
const pendingInterrupt = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const item = messages.value[index];
    if (item.kind !== "interrupt") continue;
    const handled = messages.value
      .slice(index + 1)
      .some(
        (candidate) =>
          (candidate.kind === "interrupt_response" &&
            candidate.payload?.interrupt_id === item.payload?.id) ||
          (candidate.kind === "status" && candidate.run_id === item.run_id),
      );
    if (!handled) return item;
  }
  return null;
});
const canSend = computed(
  () =>
    Boolean(current.value?.endpoint?.runnable) &&
    Boolean(draft.value.trim()) &&
    !pendingInterrupt.value &&
    !running.value,
);
const workspaceName = computed(() => state.config?.workspace?.name || "工作空间");

function displayName(endpoint) {
  return endpoint?.display_name || endpoint?.name || "未知端点";
}

function formatTime(value, detailed = false) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    ...(detailed ? { month: "2-digit", day: "2-digit" } : { hour: "2-digit", minute: "2-digit" }),
    ...(detailed ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function interruptOptions(message) {
  const values = message?.payload?.metadata?.strands?.allowed_decisions;
  return Array.isArray(values) ? values : [];
}

function handleComposerKeydown(event) {
  if (event.key !== "Enter") return;
  if (
    event.isComposing ||
    composingDraft.value ||
    event.keyCode === 229 ||
    event.shiftKey ||
    event.ctrlKey ||
    event.altKey ||
    event.metaKey
  )
    return;
  event.preventDefault();
  send();
}

async function scrollToEnd(behavior = "auto") {
  await nextTick();
  messagePane.value?.scrollTo({
    top: messagePane.value.scrollHeight,
    behavior,
  });
}

function nextPaint() {
  return new Promise((resolve) => {
    let frameId = 0;
    const fallbackId = window.setTimeout(() => {
      cancelAnimationFrame(frameId);
      resolve();
    }, 40);
    frameId = requestAnimationFrame(() => {
      window.clearTimeout(fallbackId);
      resolve();
    });
  });
}

function textUnits(value) {
  if (!value) return [];
  if (!textSegmenter) return Array.from(value);
  return Array.from(textSegmenter.segment(value), (item) => item.segment);
}

async function appendStreamingText(message, delta) {
  const units = textUnits(delta);
  if (!units.length) return;
  const baseline = units.length > 120 ? 8 : units.length > 32 ? 4 : 2;
  const chunkSize = Math.max(baseline, Math.ceil(units.length / 90));
  for (let index = 0; index < units.length; index += chunkSize) {
    if (controller?.signal.aborted) return;
    message.content += units.slice(index, index + chunkSize).join("");
    await nextTick();
    if (messagePane.value) messagePane.value.scrollTop = messagePane.value.scrollHeight;
    await nextPaint();
  }
}

async function loadDirectory() {
  const [conversationData, endpointData] = await Promise.all([
    api("/admin/playground/conversations"),
    api("/admin/playground/endpoints"),
  ]);
  conversations.value = conversationData.items || [];
  endpoints.value = endpointData.items || [];
}

async function loadConversation(id, { silent = false } = {}) {
  if (!id) {
    current.value = null;
    messages.value = [];
    streamingMessageId.value = "";
    return;
  }
  if (!silent) loadingConversation.value = true;
  try {
    const detail = await api(`/admin/playground/conversations/${encodeURIComponent(id)}`);
    current.value = detail.conversation;
    messages.value = detail.messages || [];
    streamingMessageId.value = "";
    await scrollToEnd("instant");
  } catch (error) {
    if (error.status === 404) {
      await loadDirectory();
      await router.replace("/playground");
      notify("这条调试会话已不存在", true);
      return;
    }
    throw error;
  } finally {
    if (!silent) loadingConversation.value = false;
  }
}

function openNewConversation() {
  selectedEndpoint.value =
    endpoints.value.find((item) => item.latest_enabled && item.runnable)?.id || "";
  endpointDialog.value = true;
}

async function createConversation() {
  if (!canCreate.value || creating.value) return;
  creating.value = true;
  try {
    const conversation = await post("/admin/playground/conversations", {
      endpoint_id: selectedEndpoint.value,
    });
    endpointDialog.value = false;
    await loadDirectory();
    await router.push(`/playground/${encodeURIComponent(conversation.id)}`);
    await nextTick();
    composer.value?.focus();
  } catch (error) {
    notify(error.message, true);
  } finally {
    creating.value = false;
  }
}

async function acceptEvent(event) {
  if (event.type === "RUN_STARTED") {
    activeRunId.value = event.runId;
    return;
  }
  if (event.type === "TEXT_MESSAGE_START") {
    awaitingOutput.value = false;
    streamingMessageId.value = event.messageId;
    messages.value.push({
      id: event.messageId,
      run_id: activeRunId.value,
      kind: "message",
      role: "assistant",
      content: "",
      payload: {},
      created_at: new Date().toISOString(),
    });
    await scrollToEnd();
    return;
  }
  if (event.type === "TEXT_MESSAGE_CONTENT") {
    const message = messages.value.find((item) => item.id === event.messageId);
    if (message) await appendStreamingText(message, event.delta || "");
    return;
  }
  if (event.type === "TEXT_MESSAGE_END") {
    if (streamingMessageId.value === event.messageId) streamingMessageId.value = "";
    return;
  }
  if (event.type === "TOOL_CALL_START") {
    awaitingOutput.value = false;
    messages.value.push({
      id: `tool-${event.toolCallId}`,
      run_id: activeRunId.value,
      kind: "tool",
      role: "tool",
      content: "",
      payload: {
        tool_call_id: event.toolCallId,
        name: event.toolCallName || "工具",
        arguments: "",
        result: null,
        status: "running",
      },
      created_at: new Date().toISOString(),
    });
    await scrollToEnd();
    return;
  }
  if (event.type === "TOOL_CALL_ARGS") {
    const tool = messages.value.find((item) => item.payload?.tool_call_id === event.toolCallId);
    if (tool) tool.payload.arguments += event.delta || "";
    return;
  }
  if (event.type === "TOOL_CALL_END") {
    const tool = messages.value.find((item) => item.payload?.tool_call_id === event.toolCallId);
    if (tool) tool.payload.status = "completed";
    return;
  }
  if (event.type === "TOOL_CALL_RESULT") {
    const tool = messages.value.find((item) => item.payload?.tool_call_id === event.toolCallId);
    if (tool) {
      tool.content = event.content || "";
      tool.payload.result = event.content || "";
      tool.payload.status = "completed";
    }
    awaitingOutput.value = true;
    return;
  }
  if (event.type === "RUN_INPUT_REQUIRED") {
    awaitingOutput.value = false;
    messages.value.push({
      id: `interrupt-${event.interrupt?.id || crypto.randomUUID()}`,
      run_id: activeRunId.value,
      kind: "interrupt",
      role: "system",
      content: event.interrupt?.message || "Agent 正在等待外部输入。",
      payload: event.interrupt || {},
      created_at: new Date().toISOString(),
    });
    await scrollToEnd();
    return;
  }
  if (event.type === "RUN_ERROR") {
    awaitingOutput.value = false;
    messages.value.push({
      id: `error-${crypto.randomUUID()}`,
      run_id: activeRunId.value,
      kind: "error",
      role: "system",
      content: event.message || "运行失败",
      payload: { code: event.code },
      created_at: new Date().toISOString(),
    });
    await scrollToEnd();
  }
}

async function executeStream(path, body, conversationId) {
  running.value = true;
  activeRunId.value = "";
  awaitingOutput.value = true;
  controller = new AbortController();
  await scrollToEnd();
  try {
    await streamSSE(path, body, {
      signal: controller.signal,
      onOpen: ({ runId }) => {
        if (runId) activeRunId.value = runId;
      },
      onEvent: acceptEvent,
    });
  } catch (error) {
    if (error.name !== "AbortError") {
      messages.value.push({
        id: `error-${crypto.randomUUID()}`,
        kind: "error",
        role: "system",
        content: error.message,
        payload: {},
        created_at: new Date().toISOString(),
      });
      notify(error.message, true);
    }
  } finally {
    running.value = false;
    activeRunId.value = "";
    streamingMessageId.value = "";
    awaitingOutput.value = false;
    controller = null;
    const refreshes = [loadDirectory()];
    if (route.params.conversationId === conversationId)
      refreshes.push(loadConversation(conversationId, { silent: true }));
    await Promise.all(refreshes).catch(() => {});
    await nextTick();
    composer.value?.focus();
  }
}

async function send() {
  const content = draft.value.trim();
  if (!canSend.value || !content) return;
  const conversationId = current.value.id;
  const messageId = `message_${crypto.randomUUID().replaceAll("-", "")}`;
  messages.value.push({
    id: messageId,
    kind: "message",
    role: "user",
    content,
    payload: {},
    created_at: new Date().toISOString(),
  });
  draft.value = "";
  await executeStream(
    `/admin/playground/conversations/${encodeURIComponent(conversationId)}/runs`,
    { message_id: messageId, content },
    conversationId,
  );
}

async function resumeInterrupt(value) {
  if (!pendingInterrupt.value || running.value) return;
  const response = typeof value === "string" ? value.trim() : value;
  if (response === "" || response === null || response === undefined) return;
  const conversationId = current.value.id;
  const interruptId = pendingInterrupt.value.payload.id;
  messages.value.push({
    id: `response-${crypto.randomUUID()}`,
    kind: "interrupt_response",
    role: "user",
    content: String(response),
    payload: { interrupt_id: interruptId, status: "resolved", response },
    created_at: new Date().toISOString(),
  });
  interruptDraft.value = "";
  await executeStream(
    `/admin/playground/conversations/${encodeURIComponent(conversationId)}/resume`,
    { interrupt_id: interruptId, status: "resolved", payload: response },
    conversationId,
  );
}

async function abandonInterrupt() {
  if (!pendingInterrupt.value || running.value) return;
  const confirmed = await confirmAction(
    "结束当前等待？\n这会放弃本次中断，不会删除会话快照或长期记忆。",
    { danger: true, confirmText: "结束等待" },
  );
  if (!confirmed) return;
  try {
    const conversationId = current.value.id;
    await post(
      `/admin/playground/conversations/${encodeURIComponent(conversationId)}/interrupt/abandon`,
    );
    await Promise.all([loadDirectory(), loadConversation(conversationId)]);
    notify("已结束等待");
  } catch (error) {
    notify(error.message, true);
  }
}

async function stop() {
  if (!running.value || !activeRunId.value || !current.value) return;
  try {
    await post(
      `/admin/playground/conversations/${encodeURIComponent(current.value.id)}/runs/${encodeURIComponent(activeRunId.value)}/cancel`,
    );
  } catch (error) {
    notify(error.message, true);
  }
}

async function removeConversation() {
  if (!current.value || running.value || deleting.value) return;
  const confirmed = await confirmAction(
    "删除这条调试历史？\n只会删除调试台中的会话与消息，不会清除 Agent 的会话快照或长期记忆。",
    { danger: true, confirmText: "删除" },
  );
  if (!confirmed) return;
  deleting.value = true;
  try {
    await api(`/admin/playground/conversations/${encodeURIComponent(current.value.id)}`, {
      method: "DELETE",
    });
    await router.replace("/playground");
    current.value = null;
    messages.value = [];
    await loadDirectory();
    notify("调试历史已删除");
  } catch (error) {
    notify(error.message, true);
  } finally {
    deleting.value = false;
  }
}

watch(
  () => route.params.conversationId,
  (id, previous) => {
    if (running.value && id !== previous) controller?.abort();
    loadConversation(id);
  },
);

onMounted(async () => {
  try {
    await loadDirectory();
    await loadConversation(route.params.conversationId);
  } catch (error) {
    notify(error.message, true);
  } finally {
    loading.value = false;
  }
});

onUnmounted(() => controller?.abort());
</script>

<template>
  <div class="route-view">
    <div class="page pool-page playground-layout">
      <aside class="pool-navigation playground-history">
        <header>
          <h1>端点调试</h1>
          <UiDescription>以工作空间测试用户持续对话</UiDescription>
        </header>
        <UiButton
          class="playground-new-conversation"
          variant="primary"
          :disabled="running"
          @click="openNewConversation"
        >
          新建会话
        </UiButton>
        <div class="playground-search search-control">
          <Icon name="search" />
          <input v-model="query" class="field" placeholder="搜索历史会话" />
        </div>
        <nav class="conversation-list" aria-label="调试历史会话">
          <RouterLink
            v-for="item in filteredConversations"
            :key="item.id"
            :to="`/playground/${encodeURIComponent(item.id)}`"
            :class="{ active: current?.id === item.id }"
            :aria-disabled="running || undefined"
            @click="running && $event.preventDefault()"
          >
            <strong :title="item.title">{{ item.title }}</strong>
            <span>{{ item.last_message_preview || "尚未发送消息" }}</span>
            <small>
              <span :title="item.endpoint_name">{{ item.endpoint_name }}</span>
              <time>{{ formatTime(item.updated_at, true) }}</time>
            </small>
          </RouterLink>
          <div v-if="!loading && !filteredConversations.length" class="history-empty">
            <span>暂无会话</span>
            <small>{{ query ? "没有匹配结果" : "选择端点开始第一次对话" }}</small>
          </div>
        </nav>
      </aside>

      <section class="resource-outlet playground-chat">
        <div v-if="loading || loadingConversation" class="chat-loading">正在读取会话…</div>
        <template v-else-if="current">
          <header class="chat-header">
            <div>
              <div class="chat-title-line">
                <h2 :title="current.title">{{ current.title }}</h2>
                <span
                  class="endpoint-state"
                  :class="current.endpoint.runnable ? 'ready' : 'blocked'"
                >
                  {{ current.endpoint.runnable ? "运行中" : "不可调用" }}
                </span>
              </div>
              <UiDescription> 测试身份：{{ current.user_id }} </UiDescription>
            </div>
            <UiButton
              variant="danger"
              :busy="deleting"
              :disabled="running"
              @click="removeConversation"
            >
              删除历史
            </UiButton>
          </header>

          <div ref="messagePane" class="message-timeline" aria-live="polite">
            <div v-if="!messages.length" class="conversation-empty">
              <span class="conversation-endpoint-mark">{{
                current.endpoint_name.slice(0, 1).toUpperCase()
              }}</span>
              <h2>开始测试 {{ current.endpoint_name }}</h2>
              <UiDescription>
                调试台单独保存消息用于展示与续聊；Agent 仍按自身配置维护会话上下文与长期记忆。
              </UiDescription>
            </div>

            <article
              v-for="message in messages"
              :key="message.id"
              class="timeline-entry"
              :class="[message.kind, message.role]"
            >
              <template v-if="message.kind === 'message'">
                <span class="speaker-mark">{{ message.role === "user" ? "你" : "AI" }}</span>
                <div class="message-copy">
                  <header>
                    <strong>{{
                      message.role === "user" ? workspaceName : current.endpoint_name
                    }}</strong>
                    <time>{{ formatTime(message.created_at) }}</time>
                  </header>
                  <p
                    :class="{
                      streaming: message.role === 'assistant' && streamingMessageId === message.id,
                    }"
                  >
                    {{ message.content || (running ? "正在生成…" : "") }}
                  </p>
                </div>
              </template>

              <details v-else-if="message.kind === 'tool'" class="tool-entry">
                <summary>
                  <span class="tool-state-dot" />
                  <strong>{{ message.payload?.name || "工具调用" }}</strong>
                  <small>{{ message.payload?.status === "running" ? "执行中" : "已完成" }}</small>
                </summary>
                <div>
                  <section v-if="message.payload?.arguments">
                    <span>输入</span>
                    <pre>{{ message.payload.arguments }}</pre>
                  </section>
                  <section v-if="message.content">
                    <span>结果</span>
                    <pre>{{ message.content }}</pre>
                  </section>
                </div>
              </details>

              <div v-else class="timeline-notice" :class="message.kind">
                <strong>{{
                  message.kind === "interrupt"
                    ? "等待输入"
                    : message.kind === "interrupt_response"
                      ? "你的响应"
                      : message.kind === "error"
                        ? "运行失败"
                        : "运行状态"
                }}</strong>
                <span>{{ message.content }}</span>
                <code v-if="message.payload?.code">{{ message.payload.code }}</code>
                <div v-if="pendingInterrupt?.id === message.id" class="interrupt-actions">
                  <div v-if="interruptOptions(message).length" class="interrupt-options">
                    <UiButton
                      v-for="option in interruptOptions(message)"
                      :key="option"
                      variant="secondary"
                      :disabled="running"
                      @click="resumeInterrupt(option)"
                    >
                      {{ option }}
                    </UiButton>
                  </div>
                  <div v-else class="interrupt-response-input">
                    <input
                      v-model="interruptDraft"
                      class="field"
                      placeholder="输入响应"
                      :disabled="running"
                      @keydown.enter.prevent="resumeInterrupt(interruptDraft)"
                    />
                    <UiButton
                      variant="primary"
                      :disabled="!interruptDraft.trim() || running"
                      @click="resumeInterrupt(interruptDraft)"
                    >
                      继续
                    </UiButton>
                  </div>
                  <UiButton variant="danger" :disabled="running" @click="abandonInterrupt">
                    结束等待
                  </UiButton>
                </div>
              </div>
            </article>
            <article
              v-if="running && awaitingOutput"
              class="timeline-entry message assistant stream-pending"
            >
              <span class="speaker-mark">AI</span>
              <div class="message-copy">
                <header>
                  <strong>{{ current.endpoint_name }}</strong>
                </header>
                <div class="stream-pending-dots" aria-label="正在生成">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            </article>
          </div>

          <footer class="chat-composer">
            <div v-if="!current.endpoint.runnable" class="endpoint-unavailable">
              <strong>该端点当前无法调用</strong>
              <span v-if="!current.endpoint.exists_in_runtime">
                该端点尚未应用，请先应用最新的配置更新。
              </span>
              <span v-else>请先启用端点并应用配置。</span>
            </div>
            <div v-else-if="pendingInterrupt" class="interrupt-composer-state">
              <strong>Agent 正在等待你的响应</strong>
              <span>请在上方的等待卡片中选择或输入处理结果。</span>
            </div>
            <div v-else class="composer-surface">
              <textarea
                ref="composer"
                v-model="draft"
                rows="1"
                :disabled="running"
                placeholder="输入消息。Enter 发送，Shift + Enter 换行"
                @compositionstart="composingDraft = true"
                @compositionend="composingDraft = false"
                @keydown="handleComposerKeydown"
              />
              <UiButton v-if="running" variant="secondary" @click="stop">停止</UiButton>
              <UiButton v-else variant="primary" :disabled="!canSend" @click="send">发送</UiButton>
            </div>
          </footer>
        </template>

        <div v-else class="playground-landing">
          <span class="landing-mark"><Icon name="playground" /></span>
          <h1>在这里验证已发布的 Agent</h1>
          <UiDescription>
            新建会话时选择一个已应用端点。每条历史会话都可以重新打开并继续交流。
          </UiDescription>
          <UiButton variant="primary" @click="openNewConversation">选择端点</UiButton>
        </div>
      </section>
    </div>

    <ModalFrame
      v-if="endpointDialog"
      title="新建调试会话"
      description="选择最新配置中的端点；开始后端点保持固定。"
      @dismiss="endpointDialog = false"
    >
      <div class="endpoint-picker">
        <button
          v-for="endpoint in endpoints"
          :key="endpoint.id"
          type="button"
          :class="{ selected: selectedEndpoint === endpoint.id }"
          :disabled="!endpoint.latest_enabled || !endpoint.runnable"
          @click="selectedEndpoint = endpoint.id"
        >
          <span class="endpoint-picker-mark">{{
            displayName(endpoint).slice(0, 1).toUpperCase()
          }}</span>
          <span>
            <strong :title="displayName(endpoint)">{{ displayName(endpoint) }}</strong>
            <small>{{
              endpoint.description || `${endpoint.target_type === "agent" ? "Agent" : "编排"} 端点`
            }}</small>
          </span>
          <em v-if="!endpoint.exists_in_runtime">尚未应用</em>
          <em v-else-if="!endpoint.latest_enabled || !endpoint.runtime_enabled">已禁用</em>
          <em v-else-if="endpoint.configuration_changed">使用运行版本</em>
          <em v-else class="ready">可用</em>
        </button>
        <div v-if="!endpoints.length" class="picker-empty">
          <strong>还没有发布端点</strong>
          <UiDescription>请先创建并应用一个发布端点。</UiDescription>
        </div>
      </div>
      <template #footer>
        <UiButton variant="ghost" @click="endpointDialog = false">取消</UiButton>
        <UiButton
          variant="primary"
          :busy="creating"
          :disabled="!canCreate"
          @click="createConversation"
        >
          创建会话
        </UiButton>
      </template>
    </ModalFrame>
  </div>
</template>
