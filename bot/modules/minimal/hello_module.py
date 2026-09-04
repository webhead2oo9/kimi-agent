"""A one-tool module using only the public Kimi API."""

from collections.abc import Sequence
from typing import Any

from kimi_agent_module_api import (
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleSpec,
    ModuleToolContext,
)
from kimi_agent_module_api.contracts import ScopedModuleMigration


async def greet(arguments: dict[str, Any], ctx: ModuleToolContext) -> str:
    # Identity comes from trusted context, never model-supplied arguments.
    return f"Hello, {ctx.user_name}!"


class HelloModule:
    scoped_migrations: Sequence[ScopedModuleMigration] = ()

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        pass

    async def close(self) -> None:
        pass


def create(ctx: ModuleLoadContext) -> HelloModule:
    ctx.registry.register(
        "hello_member",
        "Greet the member who is speaking.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        greet,
    )
    return HelloModule()


SPEC = ModuleSpec(name="hello", version="1.0.0", api_version=2, create=create)
