"""Safe renderer for the fixed MeldFabric prompt variable set."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from meldfabric.errors import PromptRenderError

_VARIABLE_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*}}")
_ALLOWED_VARIABLES = frozenset({"time.now", "time.date", "time.timezone", "user.id"})


class PromptRenderer:
    @property
    def allowed_variables(self) -> frozenset[str]:
        return _ALLOWED_VARIABLES

    def validate(self, template: str) -> None:
        matches = tuple(_VARIABLE_PATTERN.finditer(template))
        unknown = sorted({match.group(1) for match in matches} - _ALLOWED_VARIABLES)
        if unknown:
            raise PromptRenderError(
                f"prompt contains unsupported template variables: {unknown}",
                details={
                    "unknown_variables": unknown,
                    "allowed_variables": sorted(_ALLOWED_VARIABLES),
                },
            )
        # Any remaining template delimiter represents malformed syntax or an expression.
        # MeldFabric deliberately supports variable substitution only: no filters,
        # calls, indexing, conditionals, or attribute traversal beyond the allow-list.
        remainder = _VARIABLE_PATTERN.sub("", template)
        if "{{" in remainder or "}}" in remainder:
            raise PromptRenderError(
                "prompt contains unsupported template syntax; only fixed variables are allowed",
                details={"allowed_variables": sorted(_ALLOWED_VARIABLES)},
            )

    def render(self, template: str, *, user_id: str | None, timezone: str) -> str:
        self.validate(template)
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise PromptRenderError(
                f"unknown workspace timezone {timezone!r}",
                details={"timezone": timezone},
            ) from exc
        now = datetime.now(zone)
        values = {
            "time.now": now.isoformat(),
            "time.date": now.date().isoformat(),
            "time.timezone": timezone,
            "user.id": user_id,
        }

        def replace(match: re.Match[str]) -> str:
            variable = match.group(1)
            value = values[variable]
            if value is None:
                raise PromptRenderError(
                    f"prompt variable {variable!r} is required but unavailable",
                    details={"variable": variable},
                )
            return value

        return _VARIABLE_PATTERN.sub(replace, template)
