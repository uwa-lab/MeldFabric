from __future__ import annotations

from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from meldfabric import AgentKit, register
from meldfabric.config.models import ToolBinding
from meldfabric.errors import ToolRegistrationError, ToolResultError
from meldfabric.tools import build_strands_tool, get_registered_tools
from tests.support.strands_model import ScriptedStrandsModel
from tests.support.workspace import configure_strands_workspace


class UnsupportedReturn:
    pass


class AnnotatedToolProfile(BaseModel):
    name: str
    active: bool = True


class AnnotatedToolResult(BaseModel):
    label: str
    score: float


def test_tool_decorator_registers_typed_function() -> None:
    @register.tool(id="tool_test_add", description="Add two values")
    def add(left: int, right: int) -> int:
        return left + right

    definition = next(item for item in get_registered_tools() if item.id == "tool_test_add")

    assert add(2, 3) == 5
    assert definition.name == "add"
    assert definition.description == "Add two values"
    assert set(definition.input_schema["properties"]) == {"left", "right"}


def test_tool_rejects_untyped_parameter() -> None:
    with pytest.raises(ToolRegistrationError, match="needs a type annotation"):

        @register.tool(id="tool_test_untyped")
        def invalid(value) -> str:
            return str(value)


def test_tool_rejects_non_serializable_return() -> None:
    with pytest.raises(ToolRegistrationError, match="unsupported return type"):

        @register.tool(id="tool_test_return")
        def invalid() -> UnsupportedReturn:
            return UnsupportedReturn()


def test_tool_rejects_non_json_set_annotations() -> None:
    with pytest.raises(ToolRegistrationError, match="unsupported return type"):

        @register.tool(id="tool_test_set_return")
        def invalid() -> set[str]:
            return {"not-json"}


def test_tool_accepts_json_literal_annotations() -> None:
    @register.tool(id="tool_test_literal")
    def literal_tool(value: Literal["short", "long"]) -> Literal["ok"]:
        assert value in {"short", "long"}
        return "ok"

    definition = next(item for item in get_registered_tools() if item.id == "tool_test_literal")
    compiled = build_strands_tool(
        definition,
        name=definition.name,
        description=definition.description,
    )

    assert compiled(value="short") == "ok"


def test_tool_accepts_json_container_and_validates_actual_result() -> None:
    @register.tool(id="tool_test_json_container")
    def json_container(value: dict[str, Any]) -> dict:
        return value

    @register.tool(id="tool_test_invalid_runtime_result")
    def invalid_runtime_result() -> int:
        return "not-an-integer"  # type: ignore[return-value]

    json_definition = next(
        item for item in get_registered_tools() if item.id == "tool_test_json_container"
    )
    invalid_definition = next(
        item for item in get_registered_tools() if item.id == "tool_test_invalid_runtime_result"
    )

    assert build_strands_tool(
        json_definition,
        name=json_definition.name,
        description=json_definition.description,
    )(value={"nested": [1, True]}) == {"nested": [1, True]}
    with pytest.raises(ToolResultError, match="declared return type"):
        build_strands_tool(
            invalid_definition,
            name=invalid_definition.name,
            description=invalid_definition.description,
        )()


def test_tool_compiles_annotated_field_metadata_and_nested_models() -> None:
    @register.tool(id="tool_test_annotated_field")
    def annotated_tool(
        profile: Annotated[
            AnnotatedToolProfile,
            Field(description="Profile to score"),
        ],
        score: Annotated[
            float,
            Field(ge=0, le=1, description="Normalized score"),
        ],
    ) -> AnnotatedToolResult:
        return AnnotatedToolResult(label=profile.name, score=score)

    definition = next(
        item for item in get_registered_tools() if item.id == "tool_test_annotated_field"
    )
    compiled = build_strands_tool(
        definition,
        name=definition.name,
        description=definition.description,
    )

    assert compiled(profile={"name": "Acme"}, score=0.8) == {
        "label": "Acme",
        "score": 0.8,
    }
    assert definition.input_schema["properties"]["score"]["maximum"] == 1


def test_executed_decorators_are_visible_without_config_changes(tmp_path) -> None:
    first = AgentKit(state_dir=tmp_path / "first")

    @register.tool(id="tool_registered_after_first_workspace")
    def late_tool(value: str) -> str:
        return value

    second = AgentKit(state_dir=tmp_path / "second")

    assert "tool_registered_after_first_workspace" in {item["id"] for item in first.tool_catalog()}
    assert "tool_registered_after_first_workspace" in {item["id"] for item in second.tool_catalog()}
    assert late_tool("ok") == "ok"


@pytest.mark.asyncio
async def test_decorated_tool_gets_one_config_resource_and_can_be_executed(tmp_path) -> None:
    @register.tool(id="tool_auto_echo", description="Echo a value")
    def auto_echo(value: str) -> str:
        return f"echo:{value}"

    model = ScriptedStrandsModel(
        lambda _messages, _tools, index: (
            {"tool": {"name": "auto_echo", "input": {"value": "hello"}}}
            if index == 1
            else "tool completed"
        )
    )
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, model, include_orchestrations=False)
    agent = kit.config.agents[0].model_copy(
        update={"tool_bindings": [ToolBinding(tool_ref="tool_auto_echo")]}
    )
    loaded = await kit.apatch_config({"agents": [agent.model_dump(mode="json")]})
    await kit.aapply_latest_config(expected_generation=loaded.config.config_generation)
    resource = next(item for item in kit.config.resources.tools if item.id == "tool_auto_echo")
    assert resource.registration_digest

    await kit.storage.initialize(kit.runtime_config)
    await kit.persistence.initialize(kit.runtime_config)
    try:
        result, _revision, _run_id = await kit.agent_runtime.invoke(
            "endpoint_agent",
            [{"role": "user", "content": "use the echo tool"}],
            user_id="user-one",
            thread_id="thread-tool",
        )
    finally:
        await kit.persistence.close()
        await kit.storage.close()

    assert result["output"].strip() == "tool completed"
    assert any(spec["name"] == "auto_echo" for spec in model.calls[0]["tools"])
