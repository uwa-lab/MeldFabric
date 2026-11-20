<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api, post } from "../api.js";
import { load, logout, notify, state } from "../store.js";
import { confirmAction } from "../dialogs.js";
import StructuredSettings from "../components/StructuredSettings.vue";
import ModalFrame from "../components/ModalFrame.vue";
import SelectControl from "../components/SelectControl.vue";
import SettingRow from "../components/SettingRow.vue";
import ToggleSwitch from "../components/ToggleSwitch.vue";
import TechnicalId from "../components/TechnicalId.vue";
import ConfigurationModes from "../components/ConfigurationModes.vue";

const snapshots = ref([]);
const label = ref("");
const note = ref("");
const keyOpen = ref(false);
const keyBusy = ref(false);
const newAdminKey = ref("");
const confirmAdminKey = ref("");
const savingSystem = ref(false);
const suspendBusy = ref(false);
const runtimeAccessEnabled = ref(true);
const backupOpen = ref(false);
const backupBusy = ref(false);
const backupResult = ref(null);
const backupConfigurationMode = ref("basic");
const clone = (value) => JSON.parse(JSON.stringify(value));
const router = useRouter();

const systemDraft = reactive({
  workspace: clone(state.config.workspace),
  runtime: clone(state.config.runtime),
});
const backup = reactive({
  engine: "sqlite",
  url: "",
  options: {},
  admin_key: "",
  overwrite: false,
});
const backupOptionsSchema = computed(
  () =>
    state.features?.structured_schemas?.database_options?.[backup.engine] ||
    state.features?.structured_schemas?.json_object ||
    {},
);

async function loadSnapshots() {
  snapshots.value = (await api("/admin/config/snapshots")).items;
}

async function saveSystem() {
  savingSystem.value = true;
  try {
    await api("/admin/config", {
      method: "PATCH",
      body: JSON.stringify({
        patch: {
          workspace: clone(systemDraft.workspace),
          runtime: clone(systemDraft.runtime),
        },
        expected_generation: state.config.config_generation,
        expected_catalog_digest: state.features.code_catalog_digest,
      }),
    });
    await load();
    notify("系统设置已保存");
  } catch (error) {
    notify(error.message, true);
  } finally {
    savingSystem.value = false;
  }
}

async function createSnapshot() {
  try {
    await post("/admin/config/snapshots", {
      label: label.value || null,
      note: note.value || null,
      expected_generation: state.config.config_generation,
    });
    label.value = "";
    note.value = "";
    notify("配置快照已创建");
    await loadSnapshots();
  } catch (error) {
    notify(error.message, true);
  }
}

async function restore(item) {
  if (
    !(await confirmAction(
      `恢复快照 v${item.version} 后会创建一条新的配置修订记录，管理员 Key 不受影响。`,
      { title: "恢复配置快照？", confirmText: "恢复", danger: true },
    ))
  )
    return;
  try {
    await post(`/admin/config/snapshots/${item.version}/restore`, {
      confirm: true,
      expected_generation: state.config.config_generation,
      expected_catalog_digest: state.features.code_catalog_digest,
    });
    location.reload();
  } catch (error) {
    notify(error.message, true);
  }
}

async function removeSnapshot(item) {
  if (
    !(await confirmAction(`删除配置快照 v${item.version}。此操作无法撤销。`, {
      title: "删除配置快照？",
      confirmText: "删除",
      danger: true,
    }))
  )
    return;
  try {
    await api(`/admin/config/snapshots/${item.version}`, { method: "DELETE" });
    notify(`快照 v${item.version} 已删除`);
    await loadSnapshots();
  } catch (error) {
    notify(error.message, true);
  }
}

function openBackup() {
  Object.assign(backup, {
    engine: "sqlite",
    url: "",
    options: {},
    admin_key: "",
    overwrite: false,
  });
  backupResult.value = null;
  backupConfigurationMode.value = "basic";
  backupOpen.value = true;
}

async function runBackup() {
  backupBusy.value = true;
  try {
    backupResult.value = await post("/admin/system/backups", backup);
    backup.admin_key = "";
    notify("完整控制数据库备份已验证");
  } catch (error) {
    notify(error.message, true);
  } finally {
    backupBusy.value = false;
  }
}

function openKeyEditor() {
  newAdminKey.value = "";
  confirmAdminKey.value = "";
  keyOpen.value = true;
}

async function updateAdminKey() {
  if (newAdminKey.value.length < 16) {
    notify("管理员 Key 至少需要 16 个字符", true);
    return;
  }
  if (newAdminKey.value !== confirmAdminKey.value) {
    notify("两次输入的管理员 Key 不一致", true);
    return;
  }
  keyBusy.value = true;
  try {
    await api("/admin/security/admin-key", {
      method: "PUT",
      body: JSON.stringify({
        new_key: newAdminKey.value,
        confirm: true,
      }),
    });
    keyOpen.value = false;
    logout();
    await router.replace("/");
  } catch (error) {
    notify(error.message, true);
  } finally {
    keyBusy.value = false;
  }
}

async function suspendRuntime(enabled) {
  if (enabled || suspendBusy.value) return;
  const confirmed = await confirmAction(
    "关闭后，本次运行期间将无法继续访问管理后台和业务接口。只有重启服务进程才能恢复访问。健康探针不会被关闭。",
    {
      title: "临时关闭服务？",
      confirmText: "关闭服务",
      danger: true,
    },
  );
  if (!confirmed) {
    runtimeAccessEnabled.value = true;
    return;
  }
  suspendBusy.value = true;
  try {
    await post("/admin/runtime/suspend", { confirm: true });
    state.runtimeSuspended = true;
  } catch (error) {
    runtimeAccessEnabled.value = true;
    notify(error.message, true);
  } finally {
    suspendBusy.value = false;
  }
}

onMounted(loadSnapshots);
</script>

<template>
  <div class="route-view">
    <div class="page system-page">
      <section class="management-hero">
        <div>
          <p class="eyebrow">SYSTEM CONTROL</p>
          <h1 :title="systemDraft.workspace.name">
            {{ systemDraft.workspace.name }}
          </h1>
          <UiDescription
            >运行地址等设置重启后生效；快照与安全操作立即生效。</UiDescription
          >
        </div>
        <UiButton variant="primary" :busy="savingSystem" @click="saveSystem"
          >保存设置</UiButton
        >
      </section>

      <div class="system-config-grid">
        <section class="workspace-card">
          <header>
            <div>
              <h2>工作空间</h2>
              <UiDescription
                >在后台、发现信息与 Prompt 中识别当前服务。</UiDescription
              >
            </div>
          </header>
          <div class="workspace-fields">
            <TechnicalId
              label="Workspace ID"
              :value="systemDraft.workspace.id"
            />
            <label
              >工作空间名称<input
                v-model="systemDraft.workspace.name"
                class="field"
                required
            /></label>
            <label
              >时区<input
                v-model="systemDraft.workspace.timezone"
                class="field"
                required
            /></label>
          </div>
        </section>

        <section class="runtime-console">
          <header>
            <div>
              <span class="console-dot" /><span class="console-dot" /><span
                class="console-dot"
              />
            </div>
            <small>service.runtime</small>
          </header>
          <div class="console-grid">
            <label
              ><span>HOST</span
              ><input
                v-model="systemDraft.runtime.host"
                class="console-input"
                required
            /></label>
            <label
              ><span>PORT</span
              ><input
                v-model.number="systemDraft.runtime.port"
                class="console-input"
                type="number"
                min="1"
                max="65535"
            /></label>
            <label
              ><span>WORKERS</span
              ><input
                v-model.number="systemDraft.runtime.workers"
                class="console-input"
                type="number"
                min="1"
            /></label>
            <label
              ><span>ADMIN PATH</span
              ><input
                v-model="systemDraft.runtime.management.path"
                class="console-input"
                required
            /></label>
          </div>
          <footer>
            <div>
              <strong>运行时访问</strong>
              <small>临时关闭管理后台与业务接口，重启服务后自动恢复</small>
            </div>
            <ToggleSwitch
              v-model="runtimeAccessEnabled"
              label="正在运行"
              :disabled="suspendBusy"
              aria-label="运行时访问"
              @update:model-value="suspendRuntime"
            />
          </footer>
          <p>监听地址、端口与 Worker 数量在进程重启后生效。</p>
        </section>
      </div>

      <section class="snapshot-workbench">
        <header>
          <div>
            <p class="eyebrow">CONFIG HISTORY</p>
            <h2>配置快照</h2>
            <UiDescription
              >每次保存都会保留修订记录；只有主动创建快照才会增加快照版本号。</UiDescription
            >
          </div>
          <div class="snapshot-composer">
            <input v-model="label" class="field" placeholder="快照名称" />
            <input v-model="note" class="field" placeholder="备注（可选）" />
            <UiButton variant="secondary" @click="createSnapshot"
              >创建快照</UiButton
            >
          </div>
        </header>
        <div v-if="snapshots.length" class="snapshot-timeline">
          <article v-for="item in snapshots" :key="item.version">
            <div class="timeline-node">
              <span>v{{ item.version }}</span>
            </div>
            <div class="snapshot-copy">
              <strong>{{ item.label || "未命名快照" }}</strong>
              <small>{{ item.note || "没有备注" }}</small>
            </div>
            <time>{{ new Date(item.created_at).toLocaleString() }}</time>
            <div class="button-row">
              <UiButton variant="secondary" @click="restore(item)"
                >恢复</UiButton
              ><UiButton variant="danger" @click="removeSnapshot(item)"
                >删除</UiButton
              >
            </div>
          </article>
        </div>
        <p v-else class="empty-copy">还没有配置快照。</p>
      </section>

      <section class="system-actions">
        <article class="backup-action">
          <div>
            <h3>控制数据库备份</h3>
            <UiDescription
              >把配置历史、快照和密文完整复制到一次性提供的目标数据库。</UiDescription
            >
          </div>
          <UiButton variant="secondary" @click="openBackup">创建备份</UiButton>
        </article>
        <article class="key-action">
          <div>
            <h3>管理员 Key</h3>
            <UiDescription
              >设置新的管理员 Key；保存后当前登录会立即退出。</UiDescription
            >
          </div>
          <UiButton variant="danger" @click="openKeyEditor">修改 Key</UiButton>
        </article>
      </section>
    </div>

    <ModalFrame
      v-if="backupOpen"
      title="备份控制数据库"
      description="连接仅用于本次操作，不写入配置、日志或运行内存池。"
      @dismiss="backupOpen = false"
    >
      <form class="backup-form" @submit.prevent="runBackup">
        <ConfigurationModes
          v-model="backupConfigurationMode"
          advanced-label="连接选项"
        >
          <template #basic>
            <SettingRow
              title="数据库类型"
              description="目标数据库必须与当前控制库不同。"
              ><SelectControl
                v-model="backup.engine"
                :options="[
                  { value: 'sqlite', label: 'SQLite' },
                  { value: 'postgresql', label: 'PostgreSQL' },
                  { value: 'mysql', label: 'MySQL' },
                ]"
            /></SettingRow>
            <SettingRow
              title="连接 URL"
              description="可包含用户名与密码，提交后不会持久化。"
              ><input
                v-model="backup.url"
                class="field control-wide"
                type="password"
                autocomplete="off"
                required
            /></SettingRow>
          </template>
          <template #advanced>
            <StructuredSettings
              v-model="backup.options"
              :schema="backupOptionsSchema"
              layout="dialog"
              headerless
            />
          </template>
        </ConfigurationModes>
        <SettingRow
          title="管理员 Key"
          description="备份属于高风险操作，需要现场二次验证。"
          ><input
            v-model="backup.admin_key"
            class="field control-wide"
            type="password"
            autocomplete="current-password"
            required
        /></SettingRow>
        <SettingRow
          title="覆盖同一工作空间"
          description="允许覆盖目标库中属于本工作空间的旧备份。"
          ><ToggleSwitch v-model="backup.overwrite"
        /></SettingRow>
        <div v-if="backupResult" class="operation-result">
          <strong>备份已完成并验证</strong>
          <dl>
            <dt>备份 ID</dt>
            <dd>{{ backupResult.backup_id }}</dd>
          </dl>
        </div>
        <div class="dialog-actions">
          <UiButton variant="secondary" @click="backupOpen = false"
            >关闭</UiButton
          ><UiButton variant="primary" type="submit" :busy="backupBusy"
            >开始备份</UiButton
          >
        </div>
      </form>
    </ModalFrame>

    <ModalFrame
      v-if="keyOpen"
      title="修改管理员 Key"
      description="保存后旧 Key 立即失效，当前工作台会退出登录。"
      @dismiss="keyOpen = false"
    >
      <form class="admin-key-form" @submit.prevent="updateAdminKey">
        <label
          >新的管理员 Key
          <input
            v-model="newAdminKey"
            class="field"
            type="password"
            minlength="16"
            autocomplete="new-password"
            required
          />
          <small>至少 16 个字符。建议使用密码管理器生成并保存。</small>
        </label>
        <label
          >再次输入
          <input
            v-model="confirmAdminKey"
            class="field"
            type="password"
            minlength="16"
            autocomplete="new-password"
            required
          />
        </label>
        <div class="dialog-actions">
          <UiButton variant="secondary" @click="keyOpen = false">取消</UiButton>
          <UiButton variant="danger" type="submit" :busy="keyBusy"
            >保存并退出</UiButton
          >
        </div>
      </form>
    </ModalFrame>
  </div>
</template>
