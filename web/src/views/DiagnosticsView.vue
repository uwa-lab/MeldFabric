<script setup>
import { computed, onMounted, ref } from "vue";
import { api } from "../api.js";
import { state } from "../store.js";

const loading = ref(false);
const status = ref(state.configStatus);
const capabilities = ref(state.features);

const adapters = computed(() =>
  Object.entries(capabilities.value?.features || {})
    .filter(([key]) => !key.startsWith("database:") && !key.includes(":"))
    .map(([key, item]) => ({ key, ...item })),
);
const issues = computed(() => capabilities.value?.issues || []);
const instances = computed(() => status.value?.instances || []);
const readyInstances = computed(
  () => instances.value.filter((item) => item.status === "ready").length,
);
const availableAdapters = computed(
  () => adapters.value.filter((item) => item.available).length,
);
const health = computed(() => {
  if (issues.value.length) return { label: "需要处理", tone: "danger" };
  if (status.value?.has_unapplied_changes)
    return { label: "有配置待发布", tone: "warning" };
  if (!status.value?.fully_applied)
    return { label: "正在同步", tone: "warning" };
  return { label: "运行正常", tone: "success" };
});
const latestApplied = computed(() => !status.value?.has_unapplied_changes);
const localSynchronized = computed(
  () => status.value?.local_generation === status.value?.active_generation,
);

async function refresh() {
  loading.value = true;
  try {
    [status.value, capabilities.value] = await Promise.all([
      api("/admin/runtime/config-status"),
      api("/admin/runtime/capabilities"),
    ]);
    state.configStatus = status.value;
    state.features = capabilities.value;
  } finally {
    loading.value = false;
  }
}
onMounted(refresh);
</script>

<template>
  <div class="page diagnostics-page">
    <section class="management-hero diagnostic-hero" :class="health.tone">
      <div>
        <p class="eyebrow">RUNTIME HEALTH</p>
        <h1>{{ health.label }}</h1>
        <UiDescription>
          {{ readyInstances }}/{{ instances.length }} 个实例就绪 ·
          {{ availableAdapters }}/{{ adapters.length }} 项可选能力可用
        </UiDescription>
      </div>
      <UiButton variant="secondary" :busy="loading" @click="refresh">
        重新检查
      </UiButton>
    </section>

    <section class="config-progress" aria-label="配置应用状态">
      <article>
        <span>01</span>
        <div>
          <small>最新配置</small>
          <strong>{{ latestApplied ? "已应用" : "待应用" }}</strong>
        </div>
        <p>后台最近一次保存的内容</p>
      </article>
      <i>→</i>
      <article>
        <span>02</span>
        <div>
          <small>运行配置</small>
          <strong>{{ status?.fully_applied ? "已同步" : "同步中" }}</strong>
        </div>
        <p>共享控制数据库中的运行状态</p>
      </article>
      <i>→</i>
      <article>
        <span>03</span>
        <div>
          <small>当前进程</small>
          <strong>{{ localSynchronized ? "已更新" : "待更新" }}</strong>
        </div>
        <p>本进程内存中实际使用的配置</p>
      </article>
    </section>

    <div class="diagnostic-grid">
      <section class="runtime-board">
        <header>
          <div>
            <h2>运行实例</h2>
            <UiDescription
              >仅展示连接同一控制数据库且心跳未过期的服务进程。</UiDescription
            >
          </div>
          <span>{{ instances.length }}</span>
        </header>
        <div v-if="instances.length" class="instance-board">
          <article v-for="item in instances" :key="item.instance_id">
            <span
              class="instance-mark"
              :class="item.status === 'ready' ? 'on' : 'off'"
            >
              {{
                (item.hostname || item.instance_id).slice(0, 1).toUpperCase()
              }}
            </span>
            <div>
              <strong>{{ item.hostname || item.instance_id }}</strong>
              <small
                >PID {{ item.process_id }} ·
                {{
                  item.config_generation === status?.active_generation
                    ? "配置已同步"
                    : "配置待同步"
                }}</small
              >
            </div>
            <span
              class="status-word"
              :class="item.status === 'ready' ? 'on' : 'off'"
            >
              {{ item.status === "ready" ? "就绪" : item.status }}
            </span>
          </article>
        </div>
        <p v-else class="empty-copy">暂时没有运行实例心跳。</p>
      </section>

      <section class="capability-board">
        <header>
          <div>
            <h2>运行能力</h2>
            <UiDescription>缺少依赖的能力不会进入当前运行配置。</UiDescription>
          </div>
        </header>
        <div class="capability-matrix">
          <article
            v-for="item in adapters"
            :key="item.key"
            :class="{ unavailable: !item.available }"
          >
            <span class="state-dot" :class="item.available ? 'on' : 'off'" />
            <div>
              <strong>{{ item.label }}</strong
              ><small>{{ item.available ? "可用" : item.reason }}</small>
            </div>
          </article>
        </div>
      </section>
    </div>

    <section v-if="issues.length" class="issue-board">
      <header>
        <span>!</span>
        <div>
          <h2>需要处理</h2>
          <UiDescription>配置引用了当前进程不能提供的适配器。</UiDescription>
        </div>
      </header>
      <div>
        <article v-for="issue in issues" :key="issue.key">
          <div>
            <strong>{{ issue.label }}</strong>
            <p>{{ issue.message }}</p>
          </div>
          <code v-if="issue.extra"
            >pip install "meldfabric[{{ issue.extra }}]"</code
          >
        </article>
      </div>
    </section>
  </div>
</template>
