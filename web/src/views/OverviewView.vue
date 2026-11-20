<script setup>
import { computed } from "vue";
import { state } from "../store.js";

const stats = computed(() => [
  ["Agents", state.config.agents.length, "/agents"],
  ["编排", state.config.orchestrations.length, "/orchestrations"],
  ["发布端点", state.config.endpoints.length, "/endpoints"],
  ["模型", state.config.resources.models.length, "/resources/models"],
  ["工具", state.toolCatalog.length, "/resources/tools"],
  ["数据库", state.config.resources.databases.length, "/resources/databases"],
]);

const steps = [
  [
    "连接模型",
    "配置服务商凭据，发现并注册一个模型。",
    "/resources/models",
    "配置模型",
  ],
  [
    "检查工具",
    "先在 Python 代码中使用 @register.tool，启动后在这里确认注册结果。",
    "/resources/tools",
    "查看工具",
  ],
  [
    "创建 Agent",
    "选择模型、编写系统 Prompt，并挂载需要的工具与扩展。",
    "/agents/new",
    "创建 Agent",
  ],
  [
    "设计协作",
    "需要多 Agent 时，选择 Agent as Tool、Swarm、Graph 或 Workflow。",
    "/orchestrations",
    "创建编排",
  ],
  [
    "发布服务",
    "将 Agent 或编排绑定到发布端点，并为调用方生成 API Key。",
    "/endpoints",
    "发布端点",
  ],
];
</script>

<template>
  <div class="page welcome-page">
    <header class="welcome-heading">
      <div>
        <p class="eyebrow">WORKSPACE</p>
        <h1 :title="state.config.workspace.name">
          {{ state.config.workspace.name }}
        </h1>
        <UiDescription>配置修改需要先保存，再应用到运行服务。</UiDescription>
      </div>
    </header>

    <section class="stat-grid" aria-label="工作空间状态">
      <RouterLink
        v-for="[label, count, path] in stats"
        :key="label"
        :to="path"
        class="stat-card"
      >
        <span>{{ label }}</span
        ><strong>{{ count }}</strong>
      </RouterLink>
    </section>

    <section class="quickstart">
      <header>
        <div>
          <h2>快速启动</h2>
          <UiDescription
            >从可运行的最小配置开始，再逐步增加协作与扩展能力。</UiDescription
          >
        </div>
      </header>
      <ol>
        <li
          v-for="([title, description, path, action], index) in steps"
          :key="title"
        >
          <span class="step-number">{{
            String(index + 1).padStart(2, "0")
          }}</span>
          <div>
            <h3>{{ title }}</h3>
            <UiDescription>{{ description }}</UiDescription>
          </div>
          <RouterLink :to="path">{{ action }}<span>→</span></RouterLink>
        </li>
      </ol>
    </section>
  </div>
</template>
