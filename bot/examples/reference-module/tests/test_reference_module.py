from __future__ import annotations

from typing import Any

from kimi_agent_module_api import ModuleCapabilities, ModuleLoadContext
from community_agent_reference_module import ReferenceGreeter, ReferenceSettings, create


class RecordingRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.tools[name] = (description, parameters, handler, args, kwargs)


def test_create_registers_the_documented_tool() -> None:
    registry = RecordingRegistry()
    labels: dict[str, str] = {}
    ctx = ModuleLoadContext(
        capabilities=ModuleCapabilities(frozenset(), False, False),
        registry=registry,
        module_settings=ReferenceSettings(greeting="Welcome"),
        _register_tool_labels=labels.update,
        _declare_surface_tools=lambda _surface, _names: None,
    )

    module = create(ctx)

    assert isinstance(module, ReferenceGreeter)
    assert "reference_greet" in registry.tools
    assert labels == {"reference_greet": "Greeting someone"}
