"""Module declaration and load-time registration."""

from kimi_agent_module_api import AppModule, ModuleLoadContext, ModuleSpec

from community_agent_reference_module.module import ReferenceGreeter
from community_agent_reference_module.settings import SETTINGS, ReferenceSettings


def create(ctx: ModuleLoadContext) -> AppModule:
    settings = ctx.settings_for(ReferenceSettings)
    module = ReferenceGreeter(settings.greeting)
    ctx.registry.register(
        "reference_greet",
        "Greet someone and report this module's persistent invocation count.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Who to greet."}},
            "required": [],
            "additionalProperties": False,
        },
        module.greet,
    )
    ctx.register_tool_labels({"reference_greet": "Greeting someone"})
    return module


SPEC = ModuleSpec(
    name="reference_greeter",
    version="1.0.0",
    create=create,
    settings=SETTINGS,
)

__all__ = ["SPEC", "create"]
