from __future__ import annotations

from fastapi.testclient import TestClient

from meldfabric import AgentKit, register


def write_tool(path, tool_id):
    path.write_text(
        "from meldfabric import register\n"
        f"@register.tool(id='{tool_id}')\n"
        f"def {tool_id}(value: str) -> str:\n    return value\n"
    )


def test_decorators_after_kit_construction_are_available_immediately(tmp_path):
    kit = AgentKit(state_dir=tmp_path)

    @register.tool(id="late_startup_tool")
    def late_startup(value: str) -> str:
        return value

    assert "late_startup_tool" in {item["id"] for item in kit.tool_catalog()}
    with TestClient(kit.create_app()) as client:
        response = client.get("/admin/resources/tools", headers={"X-Admin-Key": kit.admin_key})
        assert "late_startup_tool" in {item["id"] for item in response.json()["items"]}


def test_normal_import_after_server_start_needs_no_tool_modules(tmp_path, monkeypatch):
    kit = AgentKit(state_dir=tmp_path)
    module = tmp_path / "runtime_tools.py"
    write_tool(module, "imported_after_startup")
    monkeypatch.syspath_prepend(str(tmp_path))
    revision = kit.config.config_generation
    with TestClient(kit.create_app()) as client:
        import importlib

        importlib.import_module("runtime_tools")
        response = client.get("/admin/resources/tools", headers={"X-Admin-Key": kit.admin_key})
        assert any(item["id"] == "imported_after_startup" for item in response.json()["items"])
        assert "tool_modules" not in kit.config.model_dump()
        assert kit.config.config_generation == revision


def test_retired_paths_are_rejected_without_import(tmp_path):
    kit = AgentKit(state_dir=tmp_path)
    assert "tool_modules" not in kit.config.model_json_schema()["properties"]
    from meldfabric.config.models import WorkspaceConfig

    old = kit.config.model_dump()
    old["tool_modules"] = ["must_never_be_imported"]
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WorkspaceConfig.model_validate(old)
