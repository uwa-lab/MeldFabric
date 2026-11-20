"""Human-readable references for the Strands-native workspace model."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

MODULES = {
    "agents": "Agents",
    "orchestrations": "编排",
    "endpoints": "发布端点",
    "models": "模型",
    "model_providers": "模型服务商",
    "databases": "数据库",
    "tools": "工具",
    "mcp_servers": "MCP 服务",
    "skill_sources": "Skill 目录",
    "plugins": "插件",
    "hooks": "Hooks",
    "interventions": "Interventions",
    "remote_agents": "远程 Agent",
    "sandbox_profiles": "沙箱",
    "observability_providers": "观测服务",
}

USAGES = {
    "model_ref": ("推理模型", "model"),
    "provider_ref": ("所属服务商", "provider"),
    "tool_ref": ("工具绑定", "tools"),
    "mcp_ref": ("MCP 绑定", "tools"),
    "remote_agent_ref": ("远程 Agent 工具", "tools"),
    "plugin_ref": ("插件绑定", "extensions"),
    "hook_ref": ("Hook 绑定", "extensions"),
    "intervention_ref": ("Intervention 绑定", "extensions"),
    "skill_source_refs": ("Skill 来源", "extensions"),
    "sandbox_ref": ("执行沙箱", "runtime"),
    "observability_ref": ("运行观测", "runtime"),
    "database_ref": ("持久化数据库", "persistence"),
    "agent_ref": ("编排成员", "members"),
    "agent_refs": ("Swarm 成员", "members"),
    "coordinator_ref": ("协调 Agent", "members"),
    "target_ref": ("发布或节点目标", "target"),
    "entry_agent_ref": ("Swarm 入口", "members"),
}


def _collections(document: dict[str, Any]):
    yield "agents", document.get("agents", []), "/agents"
    yield "orchestrations", document.get("orchestrations", []), "/orchestrations"
    yield "endpoints", document.get("endpoints", []), "/endpoints"
    for name, items in document.get("resources", {}).items():
        if name == "model_providers":
            base = "/resources/models"
        elif name == "skill_sources":
            base = "/resources/skill_sources"
        elif name in {"plugins", "hooks", "interventions"}:
            base = f"/extensions/{name}"
        else:
            base = f"/resources/{name}"
        yield name, items, base


def resource_references(document: dict[str, Any], target_id: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}

    def scan(value: Any, *, owner: dict[str, Any], module: str, href: str, active: bool) -> None:
        if isinstance(value, list):
            for item in value:
                scan(item, owner=owner, module=module, href=href, active=active)
            return
        if not isinstance(value, dict):
            return
        effective = active and value.get("enabled", True)
        for field, child in value.items():
            metadata = USAGES.get(field)
            matches = child == target_id or (isinstance(child, list) and target_id in child)
            if metadata and matches:
                usage, section = metadata
                target = href
                if module == "model_providers":
                    target += "?entity=model_providers"
                groups.setdefault(module, []).append(
                    {
                        "owner_id": owner.get("id"),
                        "owner_name": owner.get("display_name") or owner.get("name") or "未命名",
                        "usage": usage,
                        "section": section,
                        "href": target,
                        "enabled": effective,
                    }
                )
            if field not in {"metadata", "headers", "parameters", "initial_state", "json_schema"}:
                scan(child, owner=owner, module=module, href=href, active=effective)

    for module, items, base in _collections(document):
        if module not in MODULES or not isinstance(items, list):
            continue
        for owner in items:
            if not isinstance(owner, dict):
                continue
            href = f"{base}/{quote(str(owner.get('id', '')), safe='')}"
            scan(owner, owner=owner, module=module, href=href, active=True)
    return {
        "target_id": target_id,
        "total": sum(len(items) for items in groups.values()),
        "groups": [
            {"module": module, "label": MODULES[module], "items": items}
            for module, items in groups.items()
        ],
    }
