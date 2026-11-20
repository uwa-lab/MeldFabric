export const titles = {
  welcome: "欢迎",
  agents: "Agents",
  orchestrations: "编排",
  endpoints: "发布端点",
  playground: "端点调试",
  resources: "资源",
  extensions: "扩展",
  diagnostics: "运行诊断",
  system: "系统设置",
  developer: "开发指南",
};

export const resourceSections = {
  models: { label: "模型", description: "服务商连接、能力与推理参数" },
  databases: { label: "数据库", description: "为会话、记忆和插件提供存储" },
  tools: { label: "工具", description: "当前进程已导入的代码工具" },
  skill_sources: {
    label: "Skill 目录",
    description: "AgentSkills 读取的本地技能",
  },
  mcp_servers: { label: "MCP 服务", description: "连接并挂载远程工具" },
  remote_agents: { label: "远程 Agent", description: "发现并委派到其他服务" },
  sandbox_profiles: { label: "沙箱", description: "隔离命令、代码与文件操作" },
  observability_providers: {
    label: "观测",
    description: "将调用链路发送到 Langfuse",
  },
};

export const extensionSections = {
  plugins: { label: "插件", description: "组合初始化逻辑、工具与 Hook" },
  hooks: { label: "Hooks", description: "在运行生命周期中执行回调" },
  interventions: {
    label: "Interventions",
    description: "控制工具或模型调用是否继续",
  },
  graph_conditions: {
    label: "Graph 条件",
    description: "控制 Graph 路径是否允许通行",
  },
};

export const labels = {
  agents: "Agent",
  orchestrations: "编排",
  endpoints: "发布端点",
  model_providers: "模型服务商",
  models: "模型",
  databases: "数据库",
  tools: "工具",
  mcp_servers: "MCP 服务",
  remote_agents: "远程 Agent",
  sandbox_profiles: "沙箱",
  observability_providers: "观测服务",
  skill_sources: "Skill 目录",
  plugins: "插件",
  hooks: "Hook",
  interventions: "Intervention",
  graph_conditions: "Graph 条件",
};

export const orchestrationKinds = {
  agent_as_tool: "Agent as Tool",
  swarm: "Swarm",
  graph: "Graph",
  workflow: "Workflow",
};

export const valueLabel = (value) =>
  ({
    sqlite: "SQLite",
    postgresql: "PostgreSQL",
    mysql: "MySQL",
    chat_completions: "Chat Completions",
    responses: "Responses API",
    agent_as_tool: "Agent as Tool",
    swarm: "Swarm",
    graph: "Graph",
    workflow: "Workflow",
    sliding_window: "滑动窗口",
    summarizing: "摘要压缩",
    none: "不裁剪",
    disabled: "关闭",
    auto: "自动上下文管理",
    agentic: "Agent 主动管理",
  })[value] || value;
