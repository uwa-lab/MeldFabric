<script setup>
import { computed } from "vue";
import { useRoute } from "vue-router";
import CodeExample from "../components/CodeExample.vue";
import {
  conditionCode,
  hookGroupCode,
  interventionCode,
  interventionGroupCode,
  pluginCode,
  standaloneHookCode,
  toolCode,
} from "../domain/developerExamples.js";

const route = useRoute();
const sections = [
  ["tools", "工具", "输入、输出与模型描述"],
  ["plugins", "插件", "初始化、Hook 与插件工具"],
  ["hooks", "Hooks", "函数模式与分组模式"],
  ["interventions", "Interventions", "运行边界与策略动作"],
  ["conditions", "Graph 条件", "控制边是否允许通行"],
];
const active = computed(() => {
  const requested = String(route.params.section || "tools");
  return sections.some(([id]) => id === requested) ? requested : "tools";
});

const toolParameters = [
  ["id", "否", "稳定注册 ID；省略后由类型、模块与 qualname 生成", "不可覆盖"],
  ["name", "否", "模型看到的工具名；默认使用函数名并规范化", "可覆盖"],
  ["description", "否", "说明何时使用；默认读取 docstring", "可覆盖"],
  ["requires_sandbox", "否", "要求挂载它的 Agent 配置沙箱", "不可覆盖"],
];
const registrationParameters = [
  ["id", "否", "全局唯一注册 ID；默认由类型、模块与 qualname 生成", "不可覆盖"],
  ["name", "否", "后台展示名称；不参与身份识别，允许与其他项同名", "不可覆盖"],
  ["description", "否", "代码能力说明；默认读取 docstring", "不可覆盖"],
  ["config_model", "否", "Pydantic BaseModel；生成后台配置表单", "填写字段值"],
];
const toolTypes = [
  ["基础值", "str · int · float · bool · None"],
  ["容器", "list[T] · tuple[T, ...] · dict[str, T]"],
  ["约束类型", "Literal · Enum · Annotated[T, Field(...)]"],
  ["结构对象", "Pydantic BaseModel"],
  ["组合", "T | None · Union[...] · 上述类型的嵌套组合"],
];
const configuredContextFields = [
  "config",
  "workspace_id",
  "agent_id",
  "resource_id",
  "resource_name",
];
const requestContextFields = [
  "user_id",
  "thread_id",
  "identity",
  "endpoint_id",
  "run_id",
  "trace_attributes",
];
const hookEvents = [
  [
    "Agent",
    "AgentInitializedEvent · BeforeInvocationEvent · AfterInvocationEvent",
  ],
  ["Model", "BeforeModelCallEvent · AfterModelCallEvent"],
  [
    "Tool",
    "BeforeToolsEvent · BeforeToolCallEvent · AfterToolCallEvent · AfterToolsEvent",
  ],
  ["Message", "MessageAddedEvent · MessageUpdatedEvent"],
  [
    "Multi-agent",
    "MultiAgentInitializedEvent · BeforeMultiAgentInvocationEvent · AfterMultiAgentInvocationEvent",
  ],
  ["Node", "BeforeNodeCallEvent · AfterNodeCallEvent"],
];
const interventionActions = [
  ["BeforeInvocationEvent", "Proceed · Deny · Guide · Transform"],
  ["BeforeToolCallEvent", "Proceed · Deny · Guide · Confirm · Transform"],
  ["AfterToolCallEvent", "Proceed · Transform"],
  ["BeforeModelCallEvent", "Proceed · Deny · Guide · Transform"],
  ["AfterModelCallEvent", "Proceed · Guide · Transform"],
];
</script>

<template>
  <div class="page developer-page">
    <section class="management-hero">
      <div>
        <p class="eyebrow">DEVELOPER GUIDE</p>
        <h1>从注册到运行，逐步扩展 Agent</h1>
        <UiDescription>
          先选择注册入口，再按照签名契约实现代码。模块被 Python
          导入后，能力才会出现在后台并可被挂载。省略 id
          时，移动模块或重命名目标会产生新的注册身份并解除旧挂载。
        </UiDescription>
      </div>
    </section>

    <div class="docs-layout">
      <nav class="docs-index" aria-label="开发指南目录">
        <span>开发主题</span>
        <RouterLink
          v-for="item in sections"
          :key="item[0]"
          :to="`/developer/${item[0]}`"
          :class="{ active: active === item[0] }"
        >
          <strong>{{ item[1] }}</strong>
          <small>{{ item[2] }}</small>
        </RouterLink>
      </nav>

      <main class="docs-content">
        <section v-if="active === 'tools'" class="docs-section">
          <header class="docs-section-copy">
            <span>01</span>
            <div>
              <h2>注册工具</h2>
              <p>把一个有严格输入输出契约的 Python 函数交给模型调用。</p>
            </div>
          </header>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>1</span>
              <div>
                <h3>先了解怎么用</h3>
                <p>工具只有一种注册方式。</p>
              </div>
            </header>
            <div class="decorator-deck single">
              <article class="decorator-card featured">
                <code>@register.tool</code>
                <strong>装饰函数</strong>
                <p>注册普通同步或异步函数；后台会据此创建可挂载的工具资源。</p>
              </article>
            </div>
            <ol class="docs-registration-path">
              <li>
                <span>1</span><strong>定义函数</strong
                ><small>写完整类型标注</small>
              </li>
              <li>
                <span>2</span><strong>添加装饰器</strong
                ><small>模块导入时登记</small>
              </li>
              <li>
                <span>3</span><strong>导入模块</strong
                ><small>启动路径必须执行 import</small>
              </li>
              <li>
                <span>4</span><strong>后台挂载</strong
                ><small>配置描述并绑定 Agent</small>
              </li>
            </ol>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>2</span>
              <div>
                <h3>装饰器传参</h3>
                <p>
                  全部参数均可省略；长期运行或持久化配置的项目建议显式填写稳定
                  id。
                </p>
              </div>
            </header>
            <div class="docs-table-shell">
              <table class="docs-parameter-table">
                <thead>
                  <tr>
                    <th>参数</th>
                    <th>必填</th>
                    <th>作用</th>
                    <th>后台</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in toolParameters" :key="row[0]">
                    <td>
                      <code>{{ row[0] }}</code>
                    </td>
                    <td>{{ row[1] }}</td>
                    <td>{{ row[2] }}</td>
                    <td>{{ row[3] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>3</span>
              <div>
                <h3>函数签名与类型</h3>
                <p>输入 Schema 来自参数标注；返回值会在每次调用后再次校验。</p>
              </div>
            </header>
            <div class="docs-contract-grid">
              <article class="docs-contract-card">
                <h4>允许的参数与普通返回类型</h4>
                <div class="type-catalog">
                  <div v-for="item in toolTypes" :key="item[0]">
                    <span>{{ item[0] }}</span
                    ><code>{{ item[1] }}</code>
                  </div>
                </div>
              </article>
              <article class="docs-contract-card return-card">
                <h4>两种返回方式</h4>
                <div>
                  <code>T</code>
                  <p>直接返回声明的 JSON 兼容类型。</p>
                </div>
                <div>
                  <code>ToolResult</code>
                  <p>返回 content、artifact 和 metadata。</p>
                </div>
                <small
                  >必须显式声明返回类型，实际结果必须与声明一致并可 JSON
                  序列化。</small
                >
              </article>
            </div>
            <div class="signature-lane">
              <div><span>参数名</span><strong>决定工具输入字段</strong></div>
              <i>→</i>
              <div>
                <span>类型标注</span><strong>决定字段类型与校验</strong>
              </div>
              <i>→</i>
              <div>
                <span>Annotated + Field</span><strong>补充模型可见描述</strong>
              </div>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>4</span>
              <div>
                <h3>完整示例</h3>
                <p>定义工具后，在启动模块中显式导入它所在的模块。</p>
              </div>
            </header>
            <CodeExample title="检索工具与富结果工具" :code="toolCode" />
          </section>

          <section class="docs-failure-panel">
            <header>
              <span>5</span>
              <div>
                <h3>注册失败检查</h3>
                <p>错误会在 import 阶段尽早暴露。</p>
              </div>
            </header>
            <ul>
              <li>
                <strong>模块没有被导入</strong
                ><span>装饰器从未执行，后台自然无法发现工具。</span>
              </li>
              <li>
                <strong>缺少类型标注</strong
                ><span>任一参数或返回值没有明确类型都会拒绝注册。</span>
              </li>
              <li>
                <strong>使用 Any、*args 或 **kwargs</strong
                ><span>无法生成确定的输入 Schema。</span>
              </li>
              <li>
                <strong>返回值不匹配</strong
                ><span>调用时返回了错误类型或不可 JSON 序列化对象。</span>
              </li>
              <li>
                <strong>ID 冲突</strong
                ><span>自定义 ID 已被另一条 Python 路径占用。</span>
              </li>
            </ul>
          </section>
        </section>

        <section v-else-if="active === 'plugins'" class="docs-section">
          <header class="docs-section-copy">
            <span>02</span>
            <div>
              <h2>注册插件</h2>
              <p>
                把初始化逻辑、生命周期 Hook 与实例工具组织成一个可复用能力。
              </p>
            </div>
          </header>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>1</span>
              <div>
                <h3>先了解怎么用</h3>
                <p>外层注册 class，成员装饰器只在该插件内部生效。</p>
              </div>
            </header>
            <div class="decorator-deck">
              <article class="decorator-card featured">
                <code>@register.plugin</code><strong>注册插件 class</strong>
                <p>创建后台可配置、可挂载的插件定义。</p>
              </article>
              <article class="decorator-card">
                <code>@plugin_hook</code><strong>声明成员 Hook</strong>
                <p>按事件类型挂入插件生命周期。</p>
              </article>
              <article class="decorator-card">
                <code>@plugin_tool</code><strong>声明成员工具</strong>
                <p>仅在插件被挂载时提供给模型。</p>
              </article>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>2</span>
              <div>
                <h3>装饰器传参</h3>
                <p>插件自身与成员使用不同参数。</p>
              </div>
            </header>
            <div class="docs-split-reference">
              <article>
                <h4>@register.plugin</h4>
                <div class="docs-table-shell compact">
                  <table class="docs-parameter-table">
                    <tbody>
                      <tr v-for="row in registrationParameters" :key="row[0]">
                        <td>
                          <code>{{ row[0] }}</code>
                        </td>
                        <td>{{ row[2] }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </article>
              <article>
                <h4>成员装饰器</h4>
                <dl class="member-parameter-list">
                  <div>
                    <dt><code>@plugin_hook(priority=0)</code></dt>
                    <dd>数值越小越早执行。</dd>
                  </div>
                  <div>
                    <dt><code>@plugin_tool(id, name, description)</code></dt>
                    <dd>沿用普通工具的签名规则。</dd>
                  </div>
                  <div>
                    <dt><code>requires_sandbox</code></dt>
                    <dd>插件工具不支持；危险操作应注册为独立工具。</dd>
                  </div>
                </dl>
              </article>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>3</span>
              <div>
                <h3>class 结构与运行上下文</h3>
                <p>每个 Agent 可执行实例都会创建独立插件对象。</p>
              </div>
            </header>
            <div class="lifecycle-strip">
              <article>
                <span>创建</span><code>__init__(self)</code
                ><small>或接收一个名为 context 的参数</small>
              </article>
              <i>→</i>
              <article>
                <span>初始化</span><code>init_agent(self, agent)</code
                ><small>可同步或异步，可调整受限 Agent 视图</small>
              </article>
              <i>→</i>
              <article>
                <span>运行</span><code>@plugin_hook / @plugin_tool</code
                ><small>共享当前插件实例状态</small>
              </article>
              <i>→</i>
              <article>
                <span>清理</span><code>close(self)</code
                ><small>可选，可同步或异步</small>
              </article>
            </div>
            <div class="docs-context-band">
              <strong>配置期可确定</strong>
              <div>
                <code v-for="field in configuredContextFields" :key="field">{{
                  field
                }}</code>
              </div>
            </div>
            <div class="docs-context-band">
              <strong>请求运行时产生</strong>
              <div>
                <code v-for="field in requestContextFields" :key="field">{{
                  field
                }}</code>
              </div>
            </div>
            <div class="docs-contract-grid">
              <article class="docs-contract-card">
                <h4>PluginAgent 允许访问</h4>
                <p>
                  id、name、system_prompt、state、tool_names、model_name。只有
                  system_prompt 暴露了直接赋值入口。
                </p>
              </article>
              <article class="docs-contract-card">
                <h4>配置如何进入后台</h4>
                <p>
                  config_model 的 JSON Schema 生成配置表单；用户填写的值经
                  Pydantic 校验后成为 context.config。
                </p>
              </article>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>4</span>
              <div>
                <h3>完整示例</h3>
                <p>一个插件同时追加系统提示词、监听调用并提供工具。</p>
              </div>
            </header>
            <CodeExample title="租户上下文插件" :code="pluginCode" />
          </section>

          <section class="docs-failure-panel">
            <header>
              <span>5</span>
              <div>
                <h3>注册失败检查</h3>
                <p>插件结构在 import 时校验。</p>
              </div>
            </header>
            <ul>
              <li>
                <strong>构造函数参数错误</strong
                ><span>只能无参数，或只有一个名为 context 的参数。</span>
              </li>
              <li>
                <strong>成员装饰器混用</strong
                ><span
                  >plugin_hook 不能放入 hook_group，plugin_tool 只能属于
                  plugin。</span
                >
              </li>
              <li>
                <strong>生命周期签名错误</strong
                ><span
                  >init_agent 必须是 self、agent；close 只能接收 self。</span
                >
              </li>
              <li>
                <strong>插件工具重名</strong
                ><span>同一个插件内的模型工具名必须唯一。</span>
              </li>
              <li>
                <strong>配置模型无效</strong
                ><span
                  >config_model 必须继承 BaseModel 且能生成 JSON Schema。</span
                >
              </li>
            </ul>
          </section>
        </section>

        <section v-else-if="active === 'hooks'" class="docs-section">
          <header class="docs-section-copy">
            <span>03</span>
            <div>
              <h2>注册 Hooks</h2>
              <p>观察或轻量调整生命周期；根据是否需要共享实例状态选择模式。</p>
            </div>
          </header>

          <div class="mode-choice">
            <article>
              <span>一个时机</span><strong>函数模式</strong>
              <p>一个注册项对应一个回调函数，结构最短。</p>
            </article>
            <article>
              <span>多个时机 / 共享状态</span><strong>分组模式</strong>
              <p>一个 class 内组织多个回调，并支持 close。</p>
            </article>
          </div>

          <section class="docs-mode-guide">
            <header class="docs-mode-title">
              <span>A</span>
              <div>
                <h3>函数模式</h3>
                <p>使用 @register.hook 直接装饰函数。</p>
              </div>
            </header>
            <div class="docs-mode-step">
              <h4><span>1</span>怎么注册</h4>
              <div class="decorator-deck single">
                <article class="decorator-card featured">
                  <code>@register.hook</code
                  ><strong>一个函数，一个 Hook 资源</strong>
                  <p>适合无实例状态的日志、追踪和轻量事件处理。</p>
                </article>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>2</span>装饰器参数</h4>
              <div class="docs-table-shell">
                <table class="docs-parameter-table">
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th>必填</th>
                      <th>作用</th>
                      <th>后台</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in registrationParameters" :key="row[0]">
                      <td>
                        <code>{{ row[0] }}</code>
                      </td>
                      <td>{{ row[1] }}</td>
                      <td>{{ row[2] }}</td>
                      <td>{{ row[3] }}</td>
                    </tr>
                    <tr>
                      <td><code>priority</code></td>
                      <td>否</td>
                      <td>执行顺序，数值越小越早</td>
                      <td>不可覆盖</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>3</span>函数契约</h4>
              <div class="signature-blueprint">
                <code
                  >def callback(event: Event, context: HookContext | None = ...)
                  -&gt; None</code
                >
                <p>
                  第一个参数必须有 Event 类型标注；可选第二参数必须命名为
                  context。同步和异步函数都支持。
                </p>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>4</span>完整示例</h4>
              <CodeExample
                title="函数 Hook：记录调用身份"
                :code="standaloneHookCode"
              />
            </div>
            <div class="mode-failures">
              <strong>常见失败</strong><span>event 未标注</span
              ><span>额外参数不叫 context</span><span>注册 ID 重复</span
              ><span>异步初始化事件</span>
            </div>
          </section>

          <section class="docs-mode-guide">
            <header class="docs-mode-title">
              <span>B</span>
              <div>
                <h3>分组模式</h3>
                <p>外层注册 class，成员方法分别声明触发时机。</p>
              </div>
            </header>
            <div class="docs-mode-step">
              <h4><span>1</span>怎么注册</h4>
              <div class="decorator-deck">
                <article class="decorator-card featured">
                  <code>@register.hook_group</code
                  ><strong>注册 Hook class</strong>
                  <p>创建一个可共享状态的 Hook 资源。</p>
                </article>
                <article class="decorator-card">
                  <code>@hook_callback</code><strong>标记回调方法</strong>
                  <p>priority 控制该方法的执行顺序。</p>
                </article>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>2</span>装饰器参数</h4>
              <div class="docs-table-shell">
                <table class="docs-parameter-table">
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th>必填</th>
                      <th>@register.hook_group</th>
                      <th>后台</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in registrationParameters" :key="row[0]">
                      <td>
                        <code>{{ row[0] }}</code>
                      </td>
                      <td>{{ row[1] }}</td>
                      <td>{{ row[2] }}</td>
                      <td>{{ row[3] }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="member-decorator-note">
                <code>@hook_callback(priority=0)</code>
                <span>priority 可省略，数值越小越早执行；没有其他参数。</span>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>3</span>class 与方法契约</h4>
              <div class="signature-lane two">
                <div>
                  <span>构造函数</span
                  ><strong>无参数，或 __init__(self, context)</strong>
                </div>
                <i>→</i>
                <div>
                  <span>回调方法</span
                  ><strong>self + 已标注的 event；可选 context</strong>
                </div>
                <i>→</i>
                <div>
                  <span>清理</span><strong>可选 close(self)，支持异步</strong>
                </div>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>4</span>完整示例</h4>
              <CodeExample
                title="分组 Hook：统计工具耗时"
                :code="hookGroupCode"
              />
            </div>
            <div class="mode-failures">
              <strong>常见失败</strong><span>没有任何 hook_callback</span
              ><span>构造参数不叫 context</span><span>误用 plugin_hook</span
              ><span>回调返回 awaitable 但函数不是 async</span>
            </div>
          </section>

          <section class="docs-guide-block event-reference">
            <header class="docs-block-head">
              <span>参考</span>
              <div>
                <h3>可监听的事件</h3>
                <p>
                  从 meldfabric.events 导入；一个 Hook 回调也可以标注 Event
                  联合类型。
                </p>
              </div>
            </header>
            <div class="event-catalog">
              <article v-for="row in hookEvents" :key="row[0]">
                <strong>{{ row[0] }}</strong
                ><code>{{ row[1] }}</code>
              </article>
            </div>
            <aside class="docs-note">
              AgentInitializedEvent 与 MultiAgentInitializedEvent
              的回调必须是同步函数。Hook
              位于推理链路内，耗时任务应投递到外部队列。
            </aside>
          </section>
        </section>

        <section v-else-if="active === 'interventions'" class="docs-section">
          <header class="docs-section-copy">
            <span>04</span>
            <div>
              <h2>注册 Interventions</h2>
              <p>
                在五个明确的运行边界返回策略动作；需要共享状态时再使用分组模式。
              </p>
            </div>
          </header>

          <div class="mode-choice">
            <article>
              <span>一条策略 / 一个边界</span><strong>函数模式</strong>
              <p>一个函数只处理一种 Event。</p>
            </article>
            <article>
              <span>一组策略 / 多个边界</span><strong>分组模式</strong>
              <p>一个 class 为不同 Event 分别决策。</p>
            </article>
          </div>

          <section class="docs-mode-guide">
            <header class="docs-mode-title">
              <span>A</span>
              <div>
                <h3>函数模式</h3>
                <p>使用 @register.intervention 装饰一个策略函数。</p>
              </div>
            </header>
            <div class="docs-mode-step">
              <h4><span>1</span>怎么注册</h4>
              <div class="decorator-deck single">
                <article class="decorator-card featured">
                  <code>@register.intervention</code
                  ><strong>一个函数，一个决策边界</strong>
                  <p>Event 类型决定它在哪个运行位置被调用。</p>
                </article>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>2</span>装饰器参数</h4>
              <div class="docs-table-shell">
                <table class="docs-parameter-table">
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th>必填</th>
                      <th>作用</th>
                      <th>后台</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in registrationParameters" :key="row[0]">
                      <td>
                        <code>{{ row[0] }}</code>
                      </td>
                      <td>{{ row[1] }}</td>
                      <td>{{ row[2] }}</td>
                      <td>{{ row[3] }}</td>
                    </tr>
                    <tr>
                      <td><code>on_error</code></td>
                      <td>否</td>
                      <td>异常时选择 throw、proceed 或 deny</td>
                      <td>可覆盖</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>3</span>函数契约</h4>
              <div class="signature-blueprint">
                <code
                  >def policy(event: SupportedEvent, context:
                  InterventionContext | None = ...) -&gt; Action</code
                >
                <p>
                  只能标注一个受支持 Event；必须返回该 Event 允许的
                  Action，支持同步或异步函数。
                </p>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>4</span>完整示例</h4>
              <CodeExample
                title="函数 Intervention：高风险工具确认"
                :code="interventionCode"
              />
            </div>
            <div class="mode-failures">
              <strong>常见失败</strong><span>使用 Event 联合类型</span
              ><span>Event 不属于五个策略边界</span
              ><span>返回了该边界不允许的 Action</span
              ><span>config_model 校验失败</span>
            </div>
          </section>

          <section class="docs-mode-guide">
            <header class="docs-mode-title">
              <span>B</span>
              <div>
                <h3>分组模式</h3>
                <p>一个策略对象覆盖多个不同的运行边界。</p>
              </div>
            </header>
            <div class="docs-mode-step">
              <h4><span>1</span>怎么注册</h4>
              <div class="decorator-deck">
                <article class="decorator-card featured">
                  <code>@register.intervention_group</code
                  ><strong>注册策略 class</strong>
                  <p>共享配置、上下文和实例状态。</p>
                </article>
                <article class="decorator-card">
                  <code>@intervention_callback</code
                  ><strong>标记策略方法</strong>
                  <p>方法的 Event 标注决定触发边界。</p>
                </article>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>2</span>装饰器参数</h4>
              <div class="docs-table-shell">
                <table class="docs-parameter-table">
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th>必填</th>
                      <th>@register.intervention_group</th>
                      <th>后台</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in registrationParameters" :key="row[0]">
                      <td>
                        <code>{{ row[0] }}</code>
                      </td>
                      <td>{{ row[1] }}</td>
                      <td>{{ row[2] }}</td>
                      <td>{{ row[3] }}</td>
                    </tr>
                    <tr>
                      <td><code>on_error</code></td>
                      <td>否</td>
                      <td>异常时选择 throw、proceed 或 deny</td>
                      <td>可覆盖</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div class="member-decorator-note">
                <code>@intervention_callback</code>
                <span
                  >装饰器本身没有参数；Event 和 Action
                  契约写在方法签名中。</span
                >
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>3</span>class 与方法契约</h4>
              <div class="signature-lane two">
                <div>
                  <span>构造函数</span
                  ><strong>无参数，或 __init__(self, context)</strong>
                </div>
                <i>→</i>
                <div>
                  <span>每个方法</span><strong>只能处理一种受支持 Event</strong>
                </div>
                <i>→</i>
                <div>
                  <span>同一 class</span
                  ><strong>每种 Event 最多一个回调</strong>
                </div>
              </div>
            </div>
            <div class="docs-mode-step">
              <h4><span>4</span>完整示例</h4>
              <CodeExample
                title="分组 Intervention：运行策略"
                :code="interventionGroupCode"
              />
            </div>
            <div class="mode-failures">
              <strong>常见失败</strong><span>没有 intervention_callback</span
              ><span>同一 Event 声明两次</span><span>误用 hook_callback</span
              ><span>生命周期签名不合法</span>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>参考</span>
              <div>
                <h3>Event 与允许返回的 Action</h3>
                <p>MeldFabric 会在运行时校验，不能跨边界返回不支持的动作。</p>
              </div>
            </header>
            <div class="action-matrix">
              <article v-for="row in interventionActions" :key="row[0]">
                <code>{{ row[0] }}</code
                ><span>{{ row[1] }}</span>
              </article>
            </div>
            <div class="action-legend">
              <span><strong>Proceed</strong>继续</span
              ><span><strong>Deny</strong>阻止</span
              ><span><strong>Guide</strong>反馈并引导</span
              ><span><strong>Transform</strong>原地修改 Event</span
              ><span><strong>Confirm</strong>暂停等待确认</span>
            </div>
          </section>
        </section>

        <section v-else class="docs-section">
          <header class="docs-section-copy">
            <span>05</span>
            <div>
              <h2>注册 Graph 条件</h2>
              <p>为 Graph 的边提供同步、确定性的通行判断。</p>
            </div>
          </header>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>1</span>
              <div>
                <h3>先了解怎么用</h3>
                <p>注册函数后，在 Graph 的具体边上选择它并填写配置。</p>
              </div>
            </header>
            <div class="decorator-deck single">
              <article class="decorator-card featured">
                <code>@register.graph_condition</code
                ><strong>装饰同步判断函数</strong>
                <p>每次准备沿边继续时执行；返回 true 才允许通行。</p>
              </article>
            </div>
            <ol class="docs-registration-path three">
              <li>
                <span>1</span><strong>注册条件</strong
                ><small>定义同步 bool 函数</small>
              </li>
              <li>
                <span>2</span><strong>创建 Graph</strong
                ><small>在画布中连接节点</small>
              </li>
              <li>
                <span>3</span><strong>配置边</strong
                ><small>选择条件并填写参数</small>
              </li>
            </ol>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>2</span>
              <div>
                <h3>装饰器传参</h3>
                <p>配置模型的字段会成为每条边各自的条件参数。</p>
              </div>
            </header>
            <div class="docs-table-shell">
              <table class="docs-parameter-table">
                <thead>
                  <tr>
                    <th>参数</th>
                    <th>必填</th>
                    <th>作用</th>
                    <th>后台</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in registrationParameters" :key="row[0]">
                    <td>
                      <code>{{ row[0] }}</code>
                    </td>
                    <td>{{ row[1] }}</td>
                    <td>{{ row[2] }}</td>
                    <td>{{ row[3] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>3</span>
              <div>
                <h3>函数契约</h3>
                <p>只有 state 必填，其余上下文参数按名称选择性声明。</p>
              </div>
            </header>
            <div class="condition-signature">
              <article class="required">
                <code>state: GraphState</code
                ><strong>必填 · 第一个位置参数</strong>
                <p>读取 results、completed_nodes、failed_nodes、status。</p>
              </article>
              <article>
                <code>context: GraphConditionContext</code><strong>可选</strong>
                <p>读取配置、编排、边、源节点和目标节点 ID。</p>
              </article>
              <article>
                <code>invocation_state: dict</code><strong>可选</strong>
                <p>读取本次调用的运行状态。</p>
              </article>
              <article class="result">
                <code>-&gt; bool</code><strong>必须返回 bool</strong>
                <p>true 放行；false 阻止沿该边继续。</p>
              </article>
            </div>
            <div class="docs-split-reference">
              <article>
                <h4>当前边已有的上下文</h4>
                <p>
                  context 直接提供 orchestration_id、edge_id、source_node_id 和
                  target_node_id；不要把这些值再次设计成管理员输入。
                </p>
              </article>
              <article>
                <h4>确实需要选择任意节点</h4>
                <p>
                  为 Pydantic 字段设置
                  <code>x-meldfabric-option-source: graph_nodes</code
                  >，后台会根据当前画布生成节点选择器。
                </p>
              </article>
            </div>
          </section>

          <section class="docs-guide-block">
            <header class="docs-block-head">
              <span>4</span>
              <div>
                <h3>完整示例</h3>
                <p>
                  分别展示直接读取当前路径上下文，以及让管理员选择任意节点。
                </p>
              </div>
            </header>
            <CodeExample
              title="当前路径与可配置节点条件"
              :code="conditionCode"
            />
          </section>

          <section class="docs-failure-panel">
            <header>
              <span>5</span>
              <div>
                <h3>注册与运行失败检查</h3>
                <p>条件应快速、只读、可重复。</p>
              </div>
            </header>
            <ul>
              <li>
                <strong>声明为 async</strong
                ><span>Graph 条件只支持同步函数。</span>
              </li>
              <li>
                <strong>第一个参数不是 state</strong
                ><span>state 必须是第一个位置参数且名称固定。</span>
              </li>
              <li>
                <strong>出现其他参数名</strong
                ><span>只允许 state、context、invocation_state。</span>
              </li>
              <li>
                <strong>返回值不是 bool</strong
                ><span>注册标注和运行结果都会被校验。</span>
              </li>
              <li>
                <strong>执行外部副作用</strong
                ><span>网络请求或写操作会让路径判断变慢且不可预测。</span>
              </li>
            </ul>
          </section>
        </section>
      </main>
    </div>
  </div>
</template>
