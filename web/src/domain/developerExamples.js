export const toolCode = `# my_agent/capabilities.py
from typing import Annotated
from pydantic import Field
from meldfabric import ToolResult, register

POLICIES = [
    {"title": "数据访问", "content": "用户只能访问所属租户的数据。"},
    {"title": "付款审批", "content": "超过一万元的付款必须经过人工审批。"},
]

@register.tool(
    id="docs.tool.search_policy",
    name="search_policy",
    description="检索与问题相关的制度条款。",
    requires_sandbox=False,
)
def search_policy(
    keyword: Annotated[str, Field(description="要检索的关键词")],
    limit: Annotated[int, Field(description="最多返回条数", ge=1, le=20)] = 5,
) -> list[dict[str, str]]:
    normalized = keyword.casefold()
    matches = [
        policy
        for policy in POLICIES
        if normalized in (policy["title"] + policy["content"]).casefold()
    ]
    return matches[:limit]

@register.tool(id="docs.tool.export_summary", description="生成制度摘要。")
def export_summary(topic: str) -> ToolResult:
    text = f"# {topic}\n\n这是一个可以交给业务代码继续处理的摘要。"
    return ToolResult(
        content="摘要已生成",
        artifact={"filename": "summary.md", "text": text},
    )

# my_agent/main.py
from my_agent import capabilities  # 导入时完成注册
from meldfabric import AgentKit

kit = AgentKit(control_database="sqlite:///meldfabric.db")`;

export const pluginCode = `import logging
from typing import Annotated
from pydantic import BaseModel, Field

from meldfabric import (
    PluginAgent,
    PluginContext,
    plugin_hook,
    plugin_tool,
    register,
)
from meldfabric.events import BeforeInvocationEvent

logger = logging.getLogger(__name__)

class TenantPluginConfig(BaseModel):
    prompt_prefix: str = Field(
        default="请遵守当前租户的数据边界。",
        description="追加到系统提示词前的租户约束。",
    )

@register.plugin(
    id="docs.plugin.tenant_context",
    name="tenant_context",
    description="注入租户约束，并提供租户资料工具。",
    config_model=TenantPluginConfig,
)
class TenantContextPlugin:
    def __init__(self, context: PluginContext[TenantPluginConfig]):
        self.context = context

    def init_agent(self, agent: PluginAgent) -> None:
        agent.system_prompt = (
            self.context.config.prompt_prefix + "\n\n" + str(agent.system_prompt)
        )

    @plugin_hook(priority=-10)
    def audit_start(self, event: BeforeInvocationEvent) -> None:
        logger.info("agent_started user_id=%s", self.context.user_id)

    @plugin_tool(description="读取当前用户的租户资料。")
    def tenant_profile(
        self,
        detail: Annotated[str, Field(description="需要读取的资料类别")],
    ) -> dict[str, str]:
        tenant_id = str(self.context.identity.attributes.get("tenant_id", "default"))
        return {
            "user_id": self.context.user_id,
            "tenant_id": tenant_id,
            "detail": detail,
        }

    def close(self) -> None:
        logger.info("tenant_context_closed user_id=%s", self.context.user_id)`;

export const standaloneHookCode = `import logging
from pydantic import BaseModel, Field
from meldfabric import HookContext, register
from meldfabric.events import BeforeInvocationEvent

logger = logging.getLogger(__name__)

class AuditConfig(BaseModel):
    channel: str = Field(default="agent-audit", description="审计日志频道")

@register.hook(
    id="docs.hook.audit_start",
    name="audit_start",
    config_model=AuditConfig,
    priority=0,
)
def audit_start(
    event: BeforeInvocationEvent,
    context: HookContext[AuditConfig],
) -> None:
    logger.info(
        "channel=%s user_id=%s agent_id=%s",
        context.config.channel,
        context.user_id,
        context.agent_id,
    )`;

export const hookGroupCode = `import logging
import time

from meldfabric import HookContext, hook_callback, register
from meldfabric.events import AfterToolCallEvent, BeforeToolCallEvent

logger = logging.getLogger(__name__)

@register.hook_group(id="docs.hook_group.tool_metrics", name="tool_metrics")
class ToolMetricsHooks:
    def __init__(self, context: HookContext):
        self.context = context
        self.started_at: dict[str, float] = {}

    @hook_callback(priority=-10)
    def before_tool(self, event: BeforeToolCallEvent) -> None:
        call_id = event.tool_use.get("toolUseId", "unknown")
        self.started_at[call_id] = time.perf_counter()

    @hook_callback(priority=10)
    def after_tool(self, event: AfterToolCallEvent) -> None:
        call_id = event.tool_use.get("toolUseId", "unknown")
        started = self.started_at.pop(call_id, time.perf_counter())
        duration = event.duration or (time.perf_counter() - started)
        logger.info(
            "tool=%s duration=%.3f failed=%s user_id=%s",
            event.tool_use.get("name", "unknown"),
            duration,
            event.exception is not None,
            self.context.user_id,
        )`;

export const interventionCode = `from pydantic import BaseModel, Field
from meldfabric import InterventionContext, register
from meldfabric.events import BeforeToolCallEvent
from meldfabric.interventions import Confirm, Deny, Proceed

class SafetyConfig(BaseModel):
    protected_tools: list[str] = Field(
        default_factory=lambda: ["delete_file", "send_payment"],
        description="需要审批或拒绝的工具名",
    )
    always_deny: bool = False

@register.intervention(
    id="docs.intervention.safe_tools",
    name="safe_tools",
    config_model=SafetyConfig,
    on_error="deny",
)
def safe_tools(
    event: BeforeToolCallEvent,
    context: InterventionContext[SafetyConfig],
):
    tool_name = event.tool_use.get("name", "")
    if tool_name not in context.config.protected_tools:
        return Proceed()
    if context.config.always_deny:
        return Deny("当前策略禁止执行该工具")
    return Confirm(prompt="是否允许执行高风险工具？")`;

export const interventionGroupCode = `from meldfabric import intervention_callback, register
from meldfabric.events import AfterToolCallEvent, BeforeModelCallEvent
from meldfabric.interventions import Guide, Proceed

@register.intervention_group(
    id="docs.intervention_group.runtime_policy",
    name="runtime_policy",
    on_error="throw",
)
class RuntimePolicy:
    @intervention_callback
    def before_model(self, event: BeforeModelCallEvent):
        if (event.projected_input_tokens or 0) > 100_000:
            return Guide(feedback="请先压缩上下文，再继续推理。")
        return Proceed()

    @intervention_callback
    def after_tool(self, event: AfterToolCallEvent):
        return Proceed()`;

export const conditionCode = `from pydantic import BaseModel, Field
from meldfabric import GraphConditionContext, GraphState, register

@register.graph_condition(
    id="docs.graph_condition.source_has_result",
    name="source_has_result",
    description="当前边的源节点已经产生结果时允许通过。",
)
def source_has_result(
    state: GraphState,
    context: GraphConditionContext,
) -> bool:
    # 当前边的 ID 已经由运行时提供，不需要让管理员重复填写。
    return context.source_node_id in state.results

class SelectedNodeConfig(BaseModel):
    node_id: str = Field(
        description="必须已经产生结果的节点",
        json_schema_extra={"x-meldfabric-option-source": "graph_nodes"},
    )

@register.graph_condition(
    id="docs.graph_condition.selected_node_has_result",
    name="selected_node_has_result",
    description="管理员选择的任意节点已经产生结果时允许通过。",
    config_model=SelectedNodeConfig,
)
def selected_node_has_result(
    state: GraphState,
    context: GraphConditionContext[SelectedNodeConfig],
) -> bool:
    # x-meldfabric-option-source 让后台把 node_id 渲染为当前 Graph 的节点选择器。
    return context.config.node_id in state.results`;
