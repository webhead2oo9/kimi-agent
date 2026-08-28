"""Versioned inter-module services: declared, proxied, retired on close."""

from __future__ import annotations

from typing import Any

from pathlib import Path

import pytest

from kimi_agent_module_api import (
    ModuleLoadContext,
    ModuleRuntimeContext,
    ModuleSpec,
    ServiceDeclaration,
    ServiceRequirement,
)
from kimi_agent_module_api.contracts import ModuleContractError, ServiceUnavailable
from modules.services import ModuleServiceView, ServiceRegistryImpl
from modules.testing import build_test_runtime


class CaseService:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_case(self, reason: str) -> int:
        self.created.append(reason)
        return len(self.created)


def test_view_enforces_declarations_and_proxy_dies_with_provider() -> None:
    registry = ServiceRegistryImpl()
    provider = ModuleServiceView(
        registry, "mod", provides=(ServiceDeclaration("cases", 1),), consumes=()
    )
    consumer = ModuleServiceView(
        registry,
        "img",
        provides=(),
        consumes=(ServiceRequirement("cases", 1, provider="mod"),),
    )
    with pytest.raises(ModuleContractError):
        provider.provide("other", 1, object())
    with pytest.raises(ModuleContractError):
        consumer.get("cases", 2)
    with pytest.raises(ServiceUnavailable):
        consumer.get("cases", 1)

    impl = CaseService()
    registration = provider.provide("cases", 1, impl)
    with pytest.raises(ModuleContractError):
        provider.provide("cases", 1, CaseService())
    proxy = consumer.get("cases", 1)
    assert proxy.created is impl.created  # type: ignore[attr-defined]
    registration.close()
    with pytest.raises(ServiceUnavailable):
        _ = proxy.created  # type: ignore[attr-defined]
    with pytest.raises(ServiceUnavailable):
        consumer.get("cases", 1)
    # A provider may re-provide after retiring, e.g. on restart.
    provider.provide("cases", 1, CaseService())


class Provider:
    migrations = ()

    def __init__(self) -> None:
        self.service = CaseService()

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        assert ctx.services is not None
        ctx.services.provide("moderation.cases", 1, self.service)

    async def close(self) -> None:
        pass


class Consumer:
    migrations = ()

    def __init__(self) -> None:
        self.proxy: object | None = None

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        assert ctx.services is not None
        self.proxy = ctx.services.get("moderation.cases", 1)

    async def close(self) -> None:
        pass


class Forgetful:
    migrations = ()

    async def start(self, ctx: ModuleRuntimeContext) -> None:
        pass

    async def close(self) -> None:
        pass


def _spec(name: str, instance: object, **overrides: object) -> ModuleSpec:
    def create(_ctx: ModuleLoadContext) -> object:
        return instance

    return ModuleSpec(name=name, version="1.0.0", create=create, **overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_services_flow_provider_to_consumer_and_retire_on_close(tmp_path: Path) -> None:
    provider = Provider()
    consumer = Consumer()
    runtime = await build_test_runtime(
        tmp_path,
        ["img", "mod"],
        installed={
            "mod": _spec("mod", provider, provides=(ServiceDeclaration("moderation.cases", 1),)),
            "img": _spec(
                "img",
                consumer,
                dependencies=("mod",),
                consumes=(ServiceRequirement("moderation.cases", 1, provider="mod"),),
            ),
        },
    )
    try:
        assert consumer.proxy is not None
        assert await consumer.proxy.create_case("spam") == 1  # type: ignore[attr-defined]
        assert provider.service.created == ["spam"]
    finally:
        await runtime.close()
    with pytest.raises(ServiceUnavailable):
        await consumer.proxy.create_case("late")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_declared_but_unprovided_service_degrades_health(tmp_path: Path) -> None:
    runtime = await build_test_runtime(
        tmp_path,
        ["lazy"],
        installed={"lazy": _spec("lazy", Forgetful(), provides=(ServiceDeclaration("x.y", 1),))},
    )
    try:
        health = runtime.manager.health.get("lazy")
        assert health is not None
        assert health.state == "degraded"
        assert "x.y@1" in health.detail
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_consumer_does_not_receive_same_service_from_wrong_provider(tmp_path: Path) -> None:
    rogue = Provider()
    consumer = Consumer()
    service = ServiceDeclaration("moderation.cases", 1)
    installed = {
        "intended": _spec("intended", Forgetful(), provides=(service,)),
        "rogue": _spec("rogue", rogue, provides=(service,)),
        "consumer": _spec(
            "consumer",
            consumer,
            dependencies=("intended",),
            consumes=(ServiceRequirement("moderation.cases", 1, provider="intended"),),
        ),
    }

    with pytest.raises(ServiceUnavailable, match="module 'intended'"):
        await build_test_runtime(
            tmp_path,
            ["rogue", "consumer", "intended"],
            installed=installed,
        )


@pytest.mark.asyncio
async def test_typed_get_checks_the_provided_implementation(tmp_path: Path) -> None:
    from kimi_agent_module_api.contracts import ServiceDeclaration, ServiceRequirement
    from modules.services import ModuleServiceView, ServiceRegistryImpl

    class Board: ...

    registry = ServiceRegistryImpl()
    provider = ModuleServiceView(registry, "p", (ServiceDeclaration("s", 1),), ())
    consumer = ModuleServiceView(registry, "c", (), (ServiceRequirement("s", 1, "p"),))
    provider.provide("s", 1, Board())

    typed: Any = consumer.get("s", 1, Board)
    assert isinstance(typed._provided.implementation, Board)
    with pytest.raises(TypeError):
        consumer.get("s", 1, int)
