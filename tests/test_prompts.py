from __future__ import annotations

import pytest

from meldfabric.errors import PromptRenderError
from meldfabric.prompts import PromptRenderer


@pytest.mark.parametrize(
    "template",
    [
        "{{ user.id | upper }}",
        "{{ time.now() }}",
        "{{ user['id'] }}",
        "{{ user.id",
        "user.id }}",
    ],
)
def test_prompt_renderer_rejects_expressions_and_malformed_syntax(template: str) -> None:
    with pytest.raises(PromptRenderError, match="unsupported template syntax"):
        PromptRenderer().validate(template)


def test_prompt_renderer_accepts_only_documented_variables() -> None:
    renderer = PromptRenderer()

    renderer.validate("{{ time.now }} / {{time.date}} / {{ time.timezone }} / {{ user.id }}")


def test_prompt_renderer_reports_unknown_well_formed_variable() -> None:
    with pytest.raises(PromptRenderError, match="unsupported template variables"):
        PromptRenderer().validate("{{ arbitrary.value }}")
