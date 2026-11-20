from __future__ import annotations

import math
from typing import Annotated, ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from meldfabric import AgentKit, register
from meldfabric.config.models import DatabaseConnection, StructuredOutputConfig
from meldfabric.config.structured import validate_json_schema, validate_json_value
from meldfabric.errors import CodeRegistrationError, ConfigurationError


def test_json_contract_rejects_non_json_values_and_invalid_numbers() -> None:
    with pytest.raises(ValueError, match="non-string object key"):
        validate_json_value({1: "value"})
    with pytest.raises(ValueError, match="finite JSON number"):
        validate_json_value({"value": math.nan})
    with pytest.raises(ValueError, match="unsupported JSON value"):
        validate_json_value({"value": {"not", "json"}})


def test_json_schema_accepts_local_pointers_and_anchors_only() -> None:
    schema = {
        "type": "object",
        "$defs": {
            "item": {
                "$anchor": "item",
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
        "allOf": [{"$ref": "#item"}, {"$ref": "#/$defs/item"}],
    }
    assert validate_json_schema(schema, source="test schema") == schema

    with pytest.raises(ConfigurationError, match="invalid JSON Schema reference"):
        validate_json_schema(
            {"type": "object", "$ref": "#/$defs/missing"},
            source="missing reference",
        )
    with pytest.raises(ConfigurationError, match="invalid JSON Schema reference"):
        validate_json_schema(
            {"type": "object", "$ref": "https://schemas.invalid/object.json"},
            source="remote reference",
        )


def test_database_options_are_typed_instead_of_accepting_arbitrary_text() -> None:
    connection = DatabaseConnection(
        name="agent_data",
        engine="sqlite",
        url="sqlite+aiosqlite:///agent.db",
        options={"pool_size": 3, "connect_args": {"timeout": 15}},
    )
    assert connection.options["connect_args"] == {"timeout": 15}

    with pytest.raises(ValidationError, match="pool_sze"):
        DatabaseConnection(
            name="agent_data",
            engine="sqlite",
            url="sqlite+aiosqlite:///agent.db",
            options={"pool_sze": 3},
        )


def test_control_database_options_use_the_same_contract(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="does not match its JSON Schema"):
        AgentKit(
            state_dir=tmp_path,
            admin_key="test-admin-key",
            database_options={"pool_sze": 3},
        )


def test_disabled_structured_output_still_rejects_an_invalid_schema() -> None:
    with pytest.raises(ValidationError, match="not a valid JSON Schema"):
        StructuredOutputConfig(
            enabled=False,
            json_schema={"type": "object", "properties": "not-an-object"},
        )


def test_registration_rejects_a_config_model_with_an_external_schema_reference() -> None:
    class InvalidSettings(BaseModel):
        value: str

        @classmethod
        def model_json_schema(cls, *args, **kwargs):
            del args, kwargs
            return {"type": "object", "$ref": "https://schemas.invalid/settings.json"}

    with pytest.raises(CodeRegistrationError, match="could not build JSON Schema"):

        @register.plugin(
            name="invalid_schema_registration",
            config_model=InvalidSettings,
        )
        class InvalidPlugin:
            pass


def test_adapter_schema_drives_capabilities_and_config_persistence(tmp_path) -> None:
    class StrictAdapter:
        name = "strict_parameters"
        requires_api_key = False
        parameters_schema: ClassVar[dict] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"budget": {"type": "integer", "minimum": 1, "maximum": 5}},
        }

        def build_strands(self, _provider, _model, *, api_key):
            del api_key
            return object()

        async def discover(self, _provider, *, api_key):
            del api_key
            return []

    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    kit.model_adapters.register(StrictAdapter())
    headers = {"Authorization": "Bearer test-admin-key"}
    with TestClient(kit.create_app()) as client:
        capabilities = client.get("/admin/runtime/capabilities", headers=headers).json()
        schemas = capabilities["structured_schemas"]
        catalog_digest = capabilities["code_catalog_digest"]
        assert (
            schemas["model_parameters"]["strict_parameters"]["properties"]["budget"]["maximum"] == 5
        )

        generation = client.get("/admin/config", headers=headers).json()["config_generation"]
        provider = client.post(
            "/admin/resources/model_providers",
            headers=headers,
            json={
                "resource": {
                    "name": "Strict",
                    "adapter": "strict_parameters",
                    "base_url": "https://models.invalid/v1",
                },
                "expected_generation": generation,
                "expected_catalog_digest": catalog_digest,
            },
        ).json()
        generation += 1
        invalid = client.post(
            "/admin/resources/models",
            headers=headers,
            json={
                "resource": {
                    "name": "strict-model",
                    "provider_ref": provider["id"],
                    "model_name": "strict-model",
                    "parameters": {"budget": 6},
                },
                "expected_generation": generation,
                "expected_catalog_digest": catalog_digest,
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"


def test_tool_catalog_exposes_one_effective_description_and_resettable_overrides(
    tmp_path,
) -> None:
    @register.tool(
        id="structured_description_tool",
        name="structured_description_tool",
        description="Description from code",
    )
    def described_tool(
        query: Annotated[str, Field(description="Query from code")],
    ) -> str:
        return query

    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    headers = {"Authorization": "Bearer test-admin-key"}
    with TestClient(kit.create_app()) as client:
        catalog_digest = client.get("/admin/runtime/capabilities", headers=headers).json()[
            "code_catalog_digest"
        ]
        original = client.get("/admin/resources/tools", headers=headers).json()["items"]
        original = next(item for item in original if item["id"] == "structured_description_tool")
        assert original["description"] == "Description from code"
        assert original["description_overridden"] is False
        assert original["parameters"][0]["description"] == "Query from code"

        generation = client.get("/admin/config", headers=headers).json()["config_generation"]
        changed = client.patch(
            "/admin/resources/tools/structured_description_tool",
            headers=headers,
            json={
                "description": "Description from admin",
                "parameter_descriptions": {"query": "Query from admin"},
                "requires_sandbox": True,
                "expected_generation": generation,
                "expected_catalog_digest": catalog_digest,
            },
        )
        assert changed.status_code == 200, changed.text
        effective = client.get("/admin/resources/tools", headers=headers).json()["items"]
        effective = next(item for item in effective if item["id"] == "structured_description_tool")
        assert effective["description"] == "Description from admin"
        assert effective["description_default"] == "Description from code"
        assert effective["parameters"][0]["description"] == "Query from admin"
        assert effective["requires_sandbox"] is True

        reset = client.patch(
            "/admin/resources/tools/structured_description_tool",
            headers=headers,
            json={
                "description": "",
                "parameter_descriptions": {},
                "expected_generation": changed.json()["config_generation"],
                "expected_catalog_digest": catalog_digest,
            },
        )
        assert reset.status_code == 200, reset.text
        effective = client.get("/admin/resources/tools", headers=headers).json()["items"]
        effective = next(item for item in effective if item["id"] == "structured_description_tool")
        assert effective["description"] == "Description from code"
        assert effective["description_overridden"] is False
        assert effective["parameters"][0]["description"] == "Query from code"
        assert effective["parameters"][0]["description_overridden"] is False
