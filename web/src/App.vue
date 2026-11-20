<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { credentials, rootPath } from "./api.js";
import { state, dirty, load, logout, applyLatestConfig, dismissNotification } from "./store.js";
import { confirmAction, confirmation } from "./dialogs.js";
import { titles } from "./domain/metadata.js";
import Icon from "./components/Icon.vue";
import MotionPresence from "./components/MotionPresence.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import ToastStack from "./components/ToastStack.vue";
import ReconciliationDialog from "./components/ReconciliationDialog.vue";

const route = useRoute();
const router = useRouter();
const key = ref("");
const loginError = ref("");
const authenticating = ref(false);
const starting = ref(true);
const mobileMenu = ref(false);
const mainContent = ref(null);
const reconciliationOpen = ref(false);
const reconciliationCount = computed(
  () => (state.reconciliation?.active?.total || 0) + (state.reconciliation?.latest?.total || 0),
);

const groups = [
  {
    label: "工作空间",
    items: [
      ["welcome", "欢迎", "/"],
      ["agents", "Agents", "/agents"],
      ["orchestrations", "编排", "/orchestrations"],
      ["endpoints", "发布端点", "/endpoints"],
      ["playground", "端点调试", "/playground"],
    ],
  },
  {
    label: "基础设施",
    items: [
      ["resources", "资源", "/resources"],
      ["extensions", "扩展", "/extensions"],
    ],
  },
  {
    label: "管理",
    items: [
      ["diagnostics", "运行诊断", "/diagnostics"],
      ["system", "系统设置", "/system"],
      ["developer", "开发指南", "/developer"],
    ],
  },
];

const section = computed(() => route.path.split("/")[1] || "welcome");
const pageTitle = computed(() => titles[section.value] || "MeldFabric");
const viewKey = computed(() => route.matched[0]?.name || route.name);

watch(
  () => route.fullPath,
  async () => {
    mobileMenu.value = false;
    await nextTick();
    mainContent.value?.scrollTo({ top: 0, behavior: "instant" });
  },
);

async function login() {
  authenticating.value = true;
  loginError.value = "";
  const candidate = key.value.trim();
  credentials.set(candidate);
  try {
    await load(candidate);
    key.value = "";
  } catch (error) {
    credentials.set("");
    loginError.value = error.status === 401 ? "管理员 Key 无效" : error.message;
  } finally {
    authenticating.value = false;
  }
}

async function reload() {
  if (dirty.value && !(await confirmAction("放弃当前页面尚未保存的修改？"))) return;
  await load();
}

async function exit() {
  if (dirty.value && !(await confirmAction("放弃当前页面尚未保存的修改并退出？"))) return;
  logout();
  router.replace("/");
}

function beforeUnload(event) {
  if (dirty.value || state.saving) {
    event.preventDefault();
    event.returnValue = "";
  }
}

function unauthorized() {
  if (!state.config) return;
  logout();
  loginError.value = "管理员 Key 已失效，请重新登录。";
}

onMounted(async () => {
  window.addEventListener("beforeunload", beforeUnload);
  window.addEventListener("meldfabric:unauthorized", unauthorized);
  try {
    if (credentials.get()) await load();
  } catch (error) {
    credentials.set("");
    loginError.value = error.status === 401 ? "请重新输入管理员 Key" : error.message;
  } finally {
    starting.value = false;
  }
});

onUnmounted(() => {
  window.removeEventListener("beforeunload", beforeUnload);
  window.removeEventListener("meldfabric:unauthorized", unauthorized);
});
</script>

<template>
  <section v-if="starting && credentials.get()" class="session-loading" role="status">
    <div class="brand-mark">MF</div>
    <p>正在打开工作空间…</p>
  </section>

  <section
    v-else-if="state.runtimeSuspended"
    class="session-loading runtime-suspended-screen"
    role="status"
  >
    <div class="brand-mark large">MF</div>
    <div>
      <h1>服务已临时关闭</h1>
      <UiDescription>管理后台与业务接口将在服务进程重启后恢复访问。</UiDescription>
    </div>
  </section>

  <section v-else-if="!state.config" class="login-screen">
    <form class="login-card" @submit.prevent="login">
      <div class="brand-mark large">MF</div>
      <div class="login-heading">
        <h1>MeldFabric</h1>
        <UiDescription>使用管理员 Key 进入配置控制台</UiDescription>
      </div>
      <label for="adminKey">管理员 Key</label>
      <input
        id="adminKey"
        v-model="key"
        class="field"
        type="password"
        autocomplete="current-password"
        placeholder="meldfabric_admin_…"
        required
      />
      <UiButton type="submit" variant="primary" class="wide" :busy="authenticating">
        {{ authenticating ? "正在验证…" : "进入工作台" }}
      </UiButton>
      <p v-if="loginError" class="form-error" role="alert">{{ loginError }}</p>
    </form>
  </section>

  <div v-else class="app-shell">
    <aside class="sidebar" :class="{ open: mobileMenu }">
      <div class="brand">
        <div class="brand-mark">MF</div>
        <div class="brand-copy"><strong>MeldFabric</strong><small>Strands 控制台</small></div>
      </div>
      <nav class="primary-nav" aria-label="主导航">
        <section v-for="group in groups" :key="group.label" class="nav-group">
          <h2>{{ group.label }}</h2>
          <RouterLink
            v-for="[id, label, path] in group.items"
            :key="id"
            :to="path"
            :class="{ active: section === id }"
          >
            <Icon :name="id" />
            <span>{{ label }}</span>
          </RouterLink>
        </section>
      </nav>
      <div class="sidebar-footer">
        <a :href="rootPath + '/docs'" target="_blank" rel="noopener">API 文档 ↗</a>
        <button type="button" @click="exit">退出</button>
      </div>
    </aside>

    <main class="main-shell">
      <header class="topbar">
        <div class="topbar-title">
          <button class="mobile-menu" type="button" @click="mobileMenu = !mobileMenu">
            <Icon name="menu" />
          </button>
          <span :title="state.config.workspace.name">{{ state.config.workspace.name }}</span>
          <span class="breadcrumb-separator">/</span>
          <strong :title="pageTitle">{{ pageTitle }}</strong>
        </div>
        <div class="topbar-actions">
          <button
            class="impact-bell"
            :class="{ active: reconciliationCount }"
            type="button"
            aria-label="查看代码与配置影响"
            @click="reconciliationOpen = true"
          >
            <Icon name="bell" />
            <span v-if="reconciliationCount">{{ reconciliationCount }}</span>
          </button>
          <span v-if="dirty" class="draft-state">未保存</span>
          <span v-else-if="state.configStatus?.has_unapplied_changes" class="apply-state">
            有配置更新待应用
          </span>
          <UiButton
            v-if="!dirty && state.configStatus?.has_unapplied_changes"
            variant="primary"
            :busy="state.applying"
            @click="applyLatestConfig"
            >应用配置</UiButton
          >
          <UiButton variant="ghost" :disabled="state.saving" @click="reload">刷新</UiButton>
        </div>
      </header>
      <div ref="mainContent" class="main-content">
        <RouterView v-slot="{ Component }">
          <MotionPresence enter-only>
            <component :is="Component" :key="viewKey" />
          </MotionPresence>
        </RouterView>
      </div>
    </main>
  </div>

  <ToastStack :items="state.notifications" @dismiss="dismissNotification" />
  <ConfirmDialog v-if="confirmation" :key="confirmation.id" />
  <ReconciliationDialog
    v-if="reconciliationOpen"
    :reports="state.reconciliation"
    @dismiss="reconciliationOpen = false"
  />
</template>
