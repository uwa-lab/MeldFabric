<script setup>
import { computed, ref } from "vue";
import ModalFrame from "./ModalFrame.vue";

const props = defineProps({ reports: { type: Object, default: () => ({}) } });
defineEmits(["dismiss"]);
const plane = ref("latest");
const report = computed(() => props.reports?.[plane.value] || { issues: [], counts: {} });
const kindLabels = {
  tool: "工具",
  plugin: "插件",
  hook: "Hook",
  intervention: "Intervention",
  graph_condition: "Graph 条件",
};
const effectTypeLabels = {
  resource: "资源",
  agent: "Agent",
  orchestration: "编排",
  endpoint: "发布端点",
};
const relationLabels = {
  "code-backed configuration": "代码配置受到影响",
  "has an affected binding": "包含受影响的挂载",
  "uses affected condition": "使用了受影响的条件",
  "uses affected Agent": "使用了受影响的 Agent",
  "publishes affected target": "发布目标受到影响",
};

function issueTitle(issue) {
  const kind = kindLabels[issue.registration_kind] || issue.registration_kind;
  const name = issue.subject_name || issue.registration_id;
  if (issue.code === "REGISTRATION_MISSING") return `${kind} ${name} 丢失`;
  if (issue.code === "REGISTRATION_CHANGED") return `${kind} ${name} 发生变动`;
  if (issue.code === "CONFIG_NORMALIZED") return `${kind} ${name} 配置已调整`;
  if (issue.code === "CONFIG_INCOMPATIBLE") return `${kind} ${name} 配置不兼容`;
  return `${kind} ${name}`;
}

function fieldList(value) {
  return Array.isArray(value) && value.length ? value.join("、") : "";
}

function issueDetails(issue) {
  const details = issue.details || {};
  if (issue.code === "REGISTRATION_MISSING") {
    if (issue.registration_kind === "graph_condition")
      return `代码中已找不到该条件。编排 ${details.orchestration_id || "—"} 的边 ${details.edge_id || "—"} 已在有效配置中改为无条件通行。已有配置记录不会被改写。`;
    return "代码中已找不到对应的注册项。该资源及其挂载不会进入有效运行配置；已有配置记录不会被改写，下次保存时会从最新配置中剔除。";
  }
  if (issue.code === "REGISTRATION_CHANGED") {
    return "代码公开契约已经变化，当前运行视图将使用新的函数签名、Schema 或元数据。已有资源和挂载保持不变；如果配置字段也发生变化，会另行列出调整结果。";
  }
  if (issue.code === "CONFIG_INCOMPATIBLE") {
    const location = details.edge_id ? `编排边 ${details.edge_id} 的条件` : "现有实例配置";
    return `${location} 无法通过当前代码声明的 Schema 校验，因此不会进入有效配置${details.edge_id ? "，该边已改为无条件通行" : "，相关挂载也已暂时移除"}。${details.validation_error ? ` 校验信息：${details.validation_error}` : ""}`;
  }
  const parts = [];
  const removedParameters = fieldList(details.removed_parameter_descriptions);
  const removed = fieldList(details.removed_fields);
  const defaults = fieldList(details.defaulted_fields);
  const normalized = fieldList(details.normalized_fields);
  if (removedParameters)
    parts.push(`参数 ${removedParameters} 已不存在，对应的后台描述覆盖已移除。`);
  if (removed) parts.push(`已移除当前 Schema 不再接受的字段：${removed}。`);
  if (defaults) parts.push(`已新增字段：${defaults}，并使用代码中声明的默认值。`);
  if (normalized) parts.push(`已按当前字段类型重新规范化：${normalized}。`);
  return parts.join(" ") || "配置已按照当前代码声明的 Schema 重新整理。";
}
</script>

<template>
  <ModalFrame
    title="资源失效预警"
    description="代码变动可能影响资源及其挂载。失效资源不会应用于服务，下一次保存会剔除最新配置中的不合法项。"
    @dismiss="$emit('dismiss')"
  >
    <div class="reconciliation-tabs" role="tablist">
      <button
        v-for="item in [
          ['latest', '最新配置'],
          ['active', '运行配置'],
        ]"
        :key="item[0]"
        type="button"
        :class="{ active: plane === item[0] }"
        @click="plane = item[0]"
      >
        {{ item[1] }}
        <span>{{ reports?.[item[0]]?.total || 0 }}</span>
      </button>
    </div>
    <div v-if="report.issues?.length" class="reconciliation-list">
      <article
        v-for="(issue, index) in report.issues"
        :key="`${issue.registration_id}-${issue.code}-${index}`"
        :class="['reconciliation-item', issue.severity]"
      >
        <header>
          <span>{{ issueTitle(issue) }}</span>
          <code>{{ issue.registration_kind }} · {{ issue.registration_id }}</code>
        </header>
        <p>{{ issueDetails(issue) }}</p>
        <section
          v-if="issue.effects?.length"
          class="reconciliation-effects"
          aria-label="受影响范围"
        >
          <header>
            <strong>受影响范围</strong><span>{{ issue.effects.length }} 项</span>
          </header>
          <div>
            <article
              v-for="effect in issue.effects"
              :key="`${effect.entity_type}-${effect.entity_id}-${effect.relation}`"
            >
              <span>{{ effectTypeLabels[effect.entity_type] }}</span>
              <strong>{{ effect.entity_name }}</strong>
              <small>{{ relationLabels[effect.relation] || effect.relation }}</small>
            </article>
          </div>
        </section>
      </article>
    </div>
    <div v-else class="reconciliation-empty">
      <strong>代码目录与配置一致</strong>
      <span>当前配置没有需要关注的代码变动影响。</span>
    </div>
  </ModalFrame>
</template>
