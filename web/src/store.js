import { computed, reactive } from "vue";
import { api, credentials, post } from "./api.js";

export const state = reactive({
  config: null,
  schema: null,
  features: null,
  configStatus: null,
  reconciliation: null,
  overview: null,
  toolCatalog: [],
  loading: false,
  saving: false,
  applying: false,
  runtimeSuspended: false,
  editorDirty: false,
  notifications: [],
});

export const dirty = computed(() => state.editorDirty);
const notificationTimers = new Map();

export function notify(message, bad = false) {
  const id = crypto.randomUUID();
  state.notifications.push({ id, message, bad });
  while (state.notifications.length > 5)
    dismissNotification(state.notifications[0].id);
  notificationTimers.set(
    id,
    setTimeout(() => dismissNotification(id), 8000),
  );
  return id;
}

export function dismissNotification(id) {
  if (!id) return;
  clearTimeout(notificationTimers.get(id));
  notificationTimers.delete(id);
  state.notifications = state.notifications.filter((item) => item.id !== id);
}

function clearNotifications() {
  for (const timer of notificationTimers.values()) clearTimeout(timer);
  notificationTimers.clear();
  state.notifications = [];
}

export function setEditorDirty(value) {
  state.editorDirty = Boolean(value);
}

export async function load(key = credentials.get()) {
  state.loading = true;
  try {
    const [
      config,
      schema,
      features,
      configStatus,
      reconciliation,
      overview,
      tools,
    ] = await Promise.all(
      [
        "/admin/config",
        "/admin/config-schema",
        "/admin/runtime/capabilities",
        "/admin/runtime/config-status",
        "/admin/runtime/reconciliation",
        "/admin/overview",
        "/admin/resources/tools",
      ].map((path) => api(path, { key })),
    );
    Object.assign(state, {
      config,
      schema,
      features,
      configStatus,
      reconciliation,
      overview,
      toolCatalog: tools.items || [],
      editorDirty: false,
    });
    return config;
  } finally {
    state.loading = false;
  }
}

export function logout() {
  credentials.set("");
  clearNotifications();
  Object.assign(state, {
    config: null,
    schema: null,
    features: null,
    configStatus: null,
    reconciliation: null,
    overview: null,
    toolCatalog: [],
    runtimeSuspended: false,
    editorDirty: false,
  });
}

export function collection(type) {
  if (type === "agents") return state.config?.agents || [];
  if (type === "orchestrations") return state.config?.orchestrations || [];
  if (type === "endpoints") return state.config?.endpoints || [];
  if (type === "tools") {
    return state.toolCatalog;
  }
  return state.config?.resources?.[type] || [];
}

function entityPath(type, id = null) {
  const root = ["agents", "orchestrations", "endpoints"].includes(type)
    ? `/admin/${type}`
    : `/admin/resources/${type}`;
  return id ? `${root}/${encodeURIComponent(id)}` : root;
}

export async function saveEntity(type, draft, id = null) {
  if (state.saving) return null;
  state.saving = true;
  try {
    const result = await api(entityPath(type, id), {
      method: id ? "PUT" : "POST",
      body: JSON.stringify({
        resource: draft,
        expected_generation: state.config.config_generation,
        expected_catalog_digest: state.features.code_catalog_digest,
      }),
    });
    await load();
    notify("配置已保存");
    return result;
  } catch (error) {
    notify(error.message, true);
    throw error;
  } finally {
    state.saving = false;
  }
}

export async function saveTool(id, patch) {
  if (state.saving) return null;
  state.saving = true;
  try {
    const result = await api(
      `/admin/resources/tools/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          ...patch,
          expected_generation: state.config.config_generation,
          expected_catalog_digest: state.features.code_catalog_digest,
        }),
      },
    );
    await load();
    notify("工具配置已保存");
    return result;
  } catch (error) {
    notify(error.message, true);
    throw error;
  } finally {
    state.saving = false;
  }
}

export async function setEntityEnabled(type, item, enabled) {
  const draft = JSON.parse(JSON.stringify({ ...item, enabled }));
  if (type === "tools")
    return saveTool(item.id, {
      enabled,
      display_name: item.display_name,
      description: item.description_overridden ? item.description : "",
      requires_sandbox: item.requires_sandbox,
      parameter_descriptions: item.parameter_descriptions,
    });
  return saveEntity(type, draft, item.id);
}

export async function deleteEntity(type, id) {
  if (state.saving) return null;
  state.saving = true;
  try {
    const result = await api(
      `${entityPath(type, id)}?expected_generation=${state.config.config_generation}&expected_catalog_digest=${encodeURIComponent(state.features.code_catalog_digest)}`,
      { method: "DELETE" },
    );
    await load();
    notify("配置已删除，相关挂载已解除");
    return result;
  } catch (error) {
    notify(error.message, true);
    throw error;
  } finally {
    state.saving = false;
  }
}

export async function saveSecret(type, id, value) {
  const path =
    type === "model_providers"
      ? `/admin/resources/model-providers/${encodeURIComponent(id)}/secret`
      : `/admin/resources/${type}/${encodeURIComponent(id)}/secret`;
  const result = await api(path, {
    method: "PUT",
    body: JSON.stringify({
      value,
      expected_generation: state.config.config_generation,
      expected_catalog_digest: state.features.code_catalog_digest,
    }),
  });
  await load();
  notify("凭据已加密保存");
  return result;
}

export async function refreshConfigStatus() {
  state.configStatus = await api("/admin/runtime/config-status");
  return state.configStatus;
}

export async function applyLatestConfig() {
  if (state.applying || state.editorDirty) return false;
  state.applying = true;
  try {
    state.configStatus = await post("/admin/runtime/apply", {
      expected_generation: state.configStatus.latest_generation,
      expected_catalog_digest: state.features.code_catalog_digest,
    });
    notify("最新配置已应用");
    return true;
  } catch (error) {
    notify(error.message, true);
    await refreshConfigStatus().catch(() => {});
    return false;
  } finally {
    state.applying = false;
  }
}

export async function references(id) {
  return api(`/admin/resource-references/${encodeURIComponent(id)}`);
}
