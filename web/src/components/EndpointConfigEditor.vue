<script setup>
import { computed, onMounted, ref } from "vue";
import { api, post } from "../api.js";
import { state, collection, notify } from "../store.js";
import SelectControl from "./SelectControl.vue";
import SettingRow from "./SettingRow.vue";
import ModalFrame from "./ModalFrame.vue";

const props = defineProps({
  draft: Object,
  entityId: String,
  creating: Boolean,
});
const keys = ref([]);
const revealed = ref(null);
const loadingKeys = ref(false);

const targetItems = computed(() =>
  (props.draft.target_type === "agent"
    ? collection("agents")
    : collection("orchestrations")
  )
    .filter((item) => item.enabled !== false)
    .map((item) => ({ value: item.id, label: item.display_name || item.name })),
);

function changeTargetType(type) {
  props.draft.target_type = type;
  props.draft.target_ref = targetItems.value[0]?.value || null;
  if (type === "orchestration")
    props.draft.protocols = props.draft.protocols.filter(
      (item) => item !== "a2a",
    );
}

function toggleProtocol(protocol) {
  props.draft.protocols = props.draft.protocols.includes(protocol)
    ? props.draft.protocols.filter((item) => item !== protocol)
    : [...props.draft.protocols, protocol];
}

async function loadKeys() {
  if (props.creating) return;
  loadingKeys.value = true;
  try {
    keys.value = (
      await api(
        `/admin/endpoints/${props.entityId}/keys?include_inactive=false`,
      )
    ).items;
  } finally {
    loadingKeys.value = false;
  }
}

async function createKey(kind) {
  revealed.value = await post(`/admin/endpoints/${props.entityId}/keys`, {
    kind,
  });
  await loadKeys();
}

async function setStatus(key, status) {
  await api(`/admin/keys/${key.id}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  notify(status === "revoked" ? "Key 已撤销" : "Key 状态已更新");
  await loadKeys();
}

async function copyKey() {
  await navigator.clipboard.writeText(revealed.value.value);
  notify("Key 已复制");
}

onMounted(loadKeys);
</script>

<template>
  <div class="endpoint-editor">
    <section class="editor-panel">
      <div class="stacked-fields">
        <label
          >名称<input
            v-model="draft.name"
            class="field"
            required
            placeholder="public_assistant"
        /></label>
        <label
          >用途描述<textarea
            v-model="draft.description"
            class="field"
            placeholder="说明这个端点面向的调用方和用途。"
          />
        </label>
      </div>
    </section>

    <section class="editor-panel">
      <div class="subsection-heading">
        <div>
          <h3>执行目标</h3>
          <UiDescription
            >每个端点只发布一个目标；修改目标不会复制或改动其配置。</UiDescription
          >
        </div>
      </div>
      <div class="target-switch">
        <button
          type="button"
          :class="{ active: draft.target_type === 'agent' }"
          @click="changeTargetType('agent')"
        >
          Agent</button
        ><button
          type="button"
          :class="{ active: draft.target_type === 'orchestration' }"
          @click="changeTargetType('orchestration')"
        >
          编排
        </button>
      </div>
      <div class="settings-group">
        <SettingRow
          title="目标"
          description="端点调用时实际执行的 Agent 或编排。"
          required
          ><SelectControl v-model="draft.target_ref" :options="targetItems"
        /></SettingRow>
      </div>
    </section>

    <section class="editor-panel">
      <div class="subsection-heading">
        <div>
          <h3>开放协议</h3>
          <UiDescription
            >A2A 只能直接发布 Agent，其他协议同时支持 Agent
            与编排。</UiDescription
          >
        </div>
      </div>
      <div class="protocol-grid">
        <label
          v-for="protocol in ['rest', 'ag_ui', 'meldfabric_remote', 'a2a']"
          :key="protocol"
          :class="{
            selected: draft.protocols.includes(protocol),
            disabled: protocol === 'a2a' && draft.target_type !== 'agent',
          }"
          ><input
            type="checkbox"
            :checked="draft.protocols.includes(protocol)"
            :disabled="protocol === 'a2a' && draft.target_type !== 'agent'"
            @change="toggleProtocol(protocol)"
          /><span
            ><strong>{{
              {
                rest: "REST",
                ag_ui: "AG-UI",
                meldfabric_remote: "MeldFabric Remote",
                a2a: "A2A",
              }[protocol]
            }}</strong
            ><small>{{
              {
                rest: "同步调用与结果返回",
                ag_ui: "标准化流式事件",
                meldfabric_remote: "MeldFabric 远程委派",
                a2a: "Strands 原生 Agent-to-Agent",
              }[protocol]
            }}</small></span
          ></label
        >
      </div>
    </section>

    <section v-if="!creating" class="editor-panel">
      <div class="collection-heading">
        <div>
          <h3>访问 Key</h3>
          <UiDescription
            >Key 只在创建时完整显示；后台仅保留前缀和状态。</UiDescription
          >
        </div>
        <div class="button-row">
          <UiButton variant="secondary" @click="createKey('agent_api')"
            >生成 API Key</UiButton
          ><UiButton
            variant="secondary"
            :disabled="
              !draft.protocols.includes('meldfabric_remote') &&
              !draft.protocols.includes('a2a')
            "
            @click="createKey('remote_access')"
            >生成远程 Key</UiButton
          >
        </div>
      </div>
      <div v-if="keys.length" class="key-list">
        <article v-for="key in keys" :key="key.id">
          <div>
            <code>{{ key.prefix }}…</code
            ><small
              >{{ key.kind === "agent_api" ? "API 调用" : "远程访问" }} ·
              {{ key.scopes.join(", ") }}</small
            >
          </div>
          <span
            class="state-pill"
            :class="key.status === 'active' ? 'on' : 'off'"
            >{{ key.status }}</span
          ><UiButton
            v-if="key.status === 'active'"
            variant="danger"
            @click="setStatus(key, 'revoked')"
          >
            撤销
          </UiButton>
        </article>
      </div>
      <p v-else-if="!loadingKeys" class="empty-copy">
        还没有为这个端点生成访问 Key。
      </p>
    </section>
  </div>

  <ModalFrame
    v-if="revealed"
    title="访问 Key 已生成"
    description="完整内容只展示一次，关闭后无法再次查看。"
    @dismiss="revealed = null"
  >
    <div class="generated-key-dialog">
      <code>{{ revealed.value }}</code>
      <p>请立即复制并保存到安全位置。</p>
      <div class="dialog-actions">
        <UiButton variant="secondary" @click="revealed = null">关闭</UiButton>
        <UiButton variant="primary" @click="copyKey">复制 Key</UiButton>
      </div>
    </div>
  </ModalFrame>
</template>
