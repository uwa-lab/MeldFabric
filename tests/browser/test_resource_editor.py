from __future__ import annotations

import asyncio

import pytest
from playwright.sync_api import expect, sync_playwright
from pydantic import BaseModel, Field

from meldfabric import AgentKit, register
from meldfabric.config.models import SkillSource
from meldfabric.config.reconciliation import (
    ReconciliationEffect,
    ReconciliationIssue,
    ReconciliationReport,
)
from meldfabric.events import BeforeInvocationEvent, BeforeToolCallEvent
from meldfabric.interventions import Proceed
from meldfabric.models import DiscoveredModel


def test_reconciliation_warning_explains_subject_impact_and_sums_planes(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    kit = AgentKit(state_dir=tmp_path)
    issue = ReconciliationIssue(
        code="REGISTRATION_MISSING",
        severity="warning",
        registration_kind="tool",
        registration_id="orders.lookup",
        subject_name="order_lookup",
        resource_id="orders.lookup",
        message="legacy backend message",
        effects=[
            ReconciliationEffect(
                entity_type="agent",
                entity_id="agent_orders",
                entity_name="订单助手",
                relation="has an affected binding",
            )
        ],
    )
    normalized_issue = ReconciliationIssue(
        code="CONFIG_NORMALIZED",
        severity="info",
        registration_kind="hook",
        registration_id="audit.lifecycle",
        subject_name="audit_hook",
        resource_id="hook_audit",
        message="legacy backend message",
        details={
            "removed_fields": ["legacy_channel"],
            "defaulted_fields": ["audit_level"],
            "normalized_fields": ["retry_count"],
        },
    )
    report = ReconciliationReport(
        config_generation=kit.config.config_generation,
        catalog_digest=kit.catalog_digest,
        changed=True,
        issues=[issue, normalized_issue],
    )
    kit._active_reconciliation = report
    kit._latest_reconciliation = report
    origin = serve_admin(kit)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(origin + "/admin/ui")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        bell = page.get_by_role("button", name="查看代码与配置影响")
        expect(bell.locator("span")).to_have_text("4")
        bell.click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_role("heading", name="资源失效预警")).to_be_visible()
        expect(dialog).to_contain_text("工具 order_lookup 丢失")
        expect(dialog).to_contain_text("下次保存时会从最新配置中剔除")
        expect(dialog).to_contain_text("Hook audit_hook 配置已调整")
        expect(dialog).to_contain_text(
            "audit_level，并使用代码中声明的默认值"  # noqa: RUF001
        )
        expect(dialog.get_by_text("受影响范围", exact=True)).to_be_visible()
        expect(dialog).to_contain_text("订单助手")
        warning_list = dialog.locator(".reconciliation-list")
        assert warning_list.evaluate("element => getComputedStyle(element).overflowY") == "auto"
        browser.close()


@pytest.mark.parametrize("missing_master_key", [False, True])
def test_provider_is_saved_then_discovers_and_registers_models(
    tmp_path, monkeypatch, serve_admin, missing_master_key
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")

    @register.tool(name="browser_catalog_tool", description="catalog tool")
    def browser_catalog_tool(value: str) -> str:
        return value

    if missing_master_key:
        monkeypatch.delenv("MELDFABRIC_MASTER_KEY", raising=False)
    kit = AgentKit(state_dir=tmp_path)
    calls: list[str | None] = []

    class Adapter:
        name = "openai_compatible"
        requires_api_key = True

        async def discover(self, provider, *, api_key):
            calls.append(api_key)
            return [
                DiscoveredModel(
                    id=name,
                    metadata={"capabilities": {"image_inputs": True}},
                )
                for name in ["chat-alpha", "chat-beta", "vision"]
            ]

    kit.model_adapters.register(Adapter())
    origin = serve_admin(kit)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/admin/ui/resources/models/new?entity=model_providers")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        page.get_by_role("tab", name="自定义 Header").click()
        expect(page.get_by_text("自定义请求 Header", exact=True)).to_have_count(0)
        page.get_by_role("tab", name="基本配置").click()
        page.get_by_label("名称").fill("公司服务")
        page.get_by_label("服务地址").fill("https://models.test/v1")
        page.get_by_label("API Key").fill("browser-model-key")
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".toast", has_text="凭据已加密保存")).to_be_visible()
        provider_id = kit.config.resources.model_providers[0].id
        expect(page).to_have_url(
            origin + f"/admin/ui/resources/models/{provider_id}?entity=model_providers"
        )
        assert b"browser-model-key" not in (tmp_path / "control.db").read_bytes()

        page.get_by_role("button", name="发现模型").click()
        expect(page.get_by_role("heading", name="发现模型")).to_be_visible()
        expect(page.locator(".model-discovery-list article")).to_have_count(3)
        page.get_by_placeholder("检索模型…").fill("chat-")
        expect(page.locator(".model-discovery-list article")).to_have_count(2)
        alpha = page.locator(".model-discovery-list article").filter(has_text="chat-alpha")
        alpha.get_by_role("button", name="注册").click()
        expect(alpha).to_contain_text("已注册")
        beta = page.locator(".model-discovery-list article").filter(has_text="chat-beta")
        beta.get_by_role("button", name="注册").click()
        expect(beta).to_contain_text("已注册")
        expect(page).to_have_url(
            origin + f"/admin/ui/resources/models/{provider_id}?entity=model_providers"
        )

        assert [model.model_name for model in kit.config.resources.models] == [
            "chat-alpha",
            "chat-beta",
        ]
        assert kit.config.resources.models[0].capabilities.image_inputs is True
        assert calls == ["browser-model-key"]
        assert not errors
        browser.close()


def test_skill_source_shows_scanned_skills_and_plugin_templates(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    skill_dir = tmp_path / "skills" / "reviewer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review changes carefully\n"
        "allowed-tools: [read_file]\n---\nInstructions\n",
        encoding="utf-8",
    )
    kit = AgentKit(state_dir=tmp_path)
    source = SkillSource(id="skills_local", name="Local skills", path=str(tmp_path / "skills"))
    loaded = asyncio.run(
        kit.apatch_config({"resources": {"skill_sources": [source.model_dump(mode="json")]}})
    )
    asyncio.run(kit.aapply_latest_config(expected_generation=loaded.config.config_generation))
    origin = serve_admin(kit)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(origin + "/admin/ui/resources/skill_sources/skills_local")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()
        expect(page.locator(".skill-catalog article")).to_have_count(1)
        expect(page.locator(".skill-catalog article")).to_contain_text("reviewer")
        expect(page.locator(".skill-catalog article")).to_contain_text("Review changes carefully")
        page.goto(origin + "/admin/ui/resources/skill_sources")
        enabled = page.get_by_role("switch", name="Local skills 启用状态")
        expect(enabled).to_be_checked()
        page.locator(".entity-row .toggle-switch").click()
        expect(enabled).not_to_be_checked()
        expect(page.locator(".toast", has_text="配置已保存")).to_be_visible()
        assert kit.config.resources.skill_sources[0].enabled is False
        page.goto(origin + "/admin/ui/extensions/plugins")
        expect(page.locator(".template-grid button")).to_have_count(3)
        expect(page.get_by_text("AgentSkills", exact=True)).to_be_visible()
        expect(page.get_by_text("ContextOffloader", exact=True)).to_be_visible()
        expect(page.get_by_text("GoalLoop", exact=True)).to_be_visible()
        browser.close()


def test_agent_skills_editor_exposes_and_persists_required_skill_sources(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    skill_dir = tmp_path / "skills" / "reviewer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review changes carefully\n---\nInstructions\n",
        encoding="utf-8",
    )
    kit = AgentKit(state_dir=tmp_path)
    source = SkillSource(id="skills_local", name="Local skills", path=str(tmp_path / "skills"))
    asyncio.run(
        kit.apatch_config({"resources": {"skill_sources": [source.model_dump(mode="json")]}})
    )
    origin = serve_admin(kit)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(origin + "/admin/ui/extensions/plugins/new?kind=agent_skills")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        expect(page.get_by_role("heading", name="Skill 目录")).to_be_visible()
        source_choice = page.locator(".plugin-skill-config label").filter(has_text="Local skills")
        expect(source_choice).to_be_visible()
        expect(page.get_by_text("请选择至少一个 Skill 目录后再保存。")).to_be_visible()
        source_choice.click()
        expect(page.get_by_text("已选择 1 项")).to_be_visible()
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".toast", has_text="配置已保存")).to_be_visible()

        assert len(kit.config.resources.plugins) == 1
        plugin = kit.config.resources.plugins[0]
        assert plugin.kind == "agent_skills"
        assert plugin.skill_source_refs == ["skills_local"]
        browser.close()


def test_agent_skills_editor_guides_missing_skill_source_creation(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")
    origin = serve_admin(AgentKit(state_dir=tmp_path))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(origin + "/admin/ui/extensions/plugins/new?kind=agent_skills")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        expect(page.get_by_text("还没有可用的 Skill 目录")).to_be_visible()
        page.get_by_role("button", name="创建 Skill 目录").click()
        expect(page).to_have_url(origin + "/admin/ui/resources/skill_sources/new")
        browser.close()


def test_registered_code_capabilities_are_visible_in_their_own_catalogs(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")

    @register.plugin(name="browser_catalog_plugin", description="catalog plugin")
    class BrowserCatalogPlugin:
        pass

    @register.hook(name="browser_catalog_hook", description="catalog hook")
    def browser_catalog_hook(event: BeforeInvocationEvent) -> None:
        return None

    @register.intervention(
        name="browser_catalog_intervention",
        description="catalog intervention",
    )
    def browser_catalog_intervention(event: BeforeToolCallEvent):
        return Proceed()

    @register.graph_condition(
        name="browser_catalog_condition",
        description="catalog condition",
    )
    def browser_catalog_condition(state) -> bool:
        return True

    origin = serve_admin(AgentKit(state_dir=tmp_path))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(origin + "/admin/ui/extensions/plugins")
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        expect(page.locator(".template-grid button")).to_have_count(3)
        expect(
            page.locator(".template-grid").get_by_text("browser_catalog_plugin", exact=True)
        ).to_have_count(0)
        plugin = page.locator(".code-catalog-list article").filter(
            has_text="browser_catalog_plugin"
        )
        expect(plugin).to_be_visible()
        expect(plugin).to_contain_text("catalog plugin")
        plugin.get_by_role("button", name="创建配置").click()
        expect(page.get_by_role("tab", name="基本配置")).to_have_attribute("aria-selected", "true")
        expect(
            page.locator(
                f'.select-trigger[data-value="{BrowserCatalogPlugin.__meldfabric_registration__.id}"]'
            )
        ).to_be_visible()
        page.get_by_role("tab", name="参数配置").click()
        expect(page.locator(".schema-config-workspace")).to_be_visible()
        expect(page.get_by_text("实现参数", exact=True)).to_have_count(0)

        page.goto(origin + "/admin/ui/extensions/hooks")
        expect(page.get_by_role("heading", name="Hook", exact=True)).to_be_visible()
        hook = page.locator(".code-catalog-list article").filter(has_text="browser_catalog_hook")
        expect(hook).to_be_visible()
        hook.get_by_role("button", name="创建配置").click()
        expect(
            page.locator(
                f'.select-trigger[data-value="{browser_catalog_hook.__meldfabric_registration__.id}"]'
            )
        ).to_be_visible()
        page.get_by_role("tab", name="参数配置").click()
        expect(page.locator(".schema-config-workspace")).to_be_visible()

        page.goto(origin + "/admin/ui/extensions/interventions")
        expect(page.get_by_role("heading", name="Intervention", exact=True)).to_be_visible()
        intervention = page.locator(".code-catalog-list article").filter(
            has_text="browser_catalog_intervention"
        )
        expect(intervention).to_be_visible()
        intervention.get_by_role("button", name="创建配置").click()
        expect(
            page.locator(
                f'.select-trigger[data-value="{browser_catalog_intervention.__meldfabric_registration__.id}"]'
            )
        ).to_be_visible()
        page.get_by_role("tab", name="参数配置").click()
        expect(page.locator(".schema-config-workspace")).to_be_visible()

        page.goto(origin + "/admin/ui/extensions/graph_conditions")
        condition = page.locator(".code-catalog-list article").filter(
            has_text="browser_catalog_condition"
        )
        expect(condition).to_be_visible()
        expect(condition).to_contain_text("在 Graph 边中配置")
        browser.close()


def test_structured_configuration_uses_one_responsive_editor(
    tmp_path, monkeypatch, serve_admin
) -> None:
    monkeypatch.setenv("MELDFABRIC_ADMIN_KEY", "browser-admin")

    class Channel(BaseModel):
        name: str = Field(description="Channel name")
        weight: int = Field(default=1, description="Route weight")
        active: bool = Field(default=True, description="Whether this route participates")

    class PluginConfig(BaseModel):
        tenant: str = Field(description="Tenant identifier")
        audit_enabled: bool = Field(default=True, description="Record audit events")
        retries: int = Field(default=2, description="Retry count")
        tags: list[str] = Field(default_factory=list, description="Routing tags")
        channels: list[Channel] = Field(default_factory=list, description="Routes")
        metadata: dict[str, str] = Field(default_factory=dict, description="Metadata")

    @register.plugin(
        name="browser_structured_plugin",
        description="structured editor fixture",
        config_model=PluginConfig,
    )
    class BrowserStructuredPlugin:
        pass

    origin = serve_admin(AgentKit(state_dir=tmp_path))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 760, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(
            origin
            + "/admin/ui/extensions/plugins/new?kind=registered"
            + f"&registration_id={BrowserStructuredPlugin.__meldfabric_registration__.id}"
            + "&registration_name=browser_structured_plugin"
        )
        page.get_by_label("管理员 Key").fill("browser-admin")
        page.get_by_role("button", name="进入工作台").click()

        expect(page.get_by_role("tab", name="基本配置")).to_have_attribute("aria-selected", "true")
        page.get_by_role("tab", name="参数配置").click()
        editor = page.locator(".schema-config-workspace .structured-settings")
        expect(editor).to_be_visible()
        expect(editor.locator(".structured-field-row")).to_have_count(3)
        expect(editor.locator(".structured-array")).to_have_count(2)
        expect(editor.locator(".structured-collection")).to_have_count(1)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )

        channels = editor.locator(".structured-array").filter(has_text="Channels")
        channels.get_by_role("button", name="添加条目").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("Channel name", exact=True)).to_be_visible()
        expect(dialog.get_by_text("Route weight", exact=True)).to_be_visible()
        expect(dialog.get_by_text("Whether this route participates", exact=True)).to_be_visible()
        expect(dialog.get_by_role("button", name="重置")).to_have_count(0)
        dialog.get_by_label("Name").fill("primary")
        dialog.get_by_role("button", name="确认").click()
        expect(channels.locator(".structured-entry-list")).to_contain_text("primary")

        page.goto(origin + "/admin/ui/resources/databases/new")
        expect(page.get_by_role("tab", name="基本配置")).to_have_attribute("aria-selected", "true")
        page.get_by_role("tab", name="连接选项").click()
        options = page.locator(".configuration-mode-content .structured-settings")
        expect(options).to_be_visible()
        expect(options.get_by_text("连接选项", exact=True)).to_have_count(0)
        expect(options.locator(".structured-field-row").first).to_be_visible()
        expect(options.locator(".choice-control")).to_have_count(2)
        expect(options.locator(".structured-union")).to_have_count(0)
        expect(
            options.locator(".choice-control").first.get_by_role("radio", name="调试模式")
        ).to_be_visible()
        assert not errors
        browser.close()
