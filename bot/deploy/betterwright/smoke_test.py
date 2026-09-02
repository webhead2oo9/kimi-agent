"""Launch the production browser boundary and verify one public page."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast

from config.operator_settings import apply_operator_settings
from config.settings import Settings
from sandbox.runner import SandboxConfig, SandboxNetworkMode, sandbox_available
from tools.visuals import verify_rendered_png
from web_browser.service import (
    BrowserNetworkMode,
    BrowserService,
    BrowserServiceConfig,
)
from web_browser.visual_service import (
    ScatterPoint,
    VisualRenderRequest,
    VisualSeries,
    VisualService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_service(settings: Settings) -> BrowserService:
    return BrowserService(
        BrowserServiceConfig(
            runtime_dir=Path(settings.browser_runtime_dir),
            profiles_dir=Path(settings.browser_profiles_dir),
            bridge_script=PROJECT_ROOT / settings.browser_bridge_script,
            network_mode=cast(BrowserNetworkMode, settings.browser_network_mode),
            bwrap_bin=settings.browser_bwrap_bin,
            prlimit_bin=settings.browser_prlimit_bin,
            systemd_run_bin=settings.browser_systemd_run_bin,
            systemctl_bin=settings.browser_systemctl_bin,
            sudo_bin=settings.browser_sudo_bin,
            netns_helper_bin=settings.browser_netns_helper_bin,
            netns_resolv_conf=settings.browser_netns_resolv_conf,
            call_timeout_seconds=settings.browser_call_timeout_seconds,
            start_timeout_seconds=settings.browser_start_timeout_seconds,
            idle_ttl_seconds=settings.browser_idle_ttl_seconds,
            worker_max_lifetime_seconds=settings.browser_worker_max_lifetime_seconds,
            profile_ttl_seconds=settings.browser_profile_ttl_seconds,
            max_profile_bytes=settings.browser_max_profile_mb * 1024 * 1024,
            max_total_memory_mb=settings.browser_max_total_memory_mb,
            max_tasks=settings.browser_max_tasks,
            cpu_quota_percent=settings.browser_cpu_quota_percent,
            tmp_size_mb=settings.browser_tmp_size_mb,
            max_fsize_mb=settings.browser_max_fsize_mb,
            max_open_files=settings.browser_max_open_files,
            timezone=settings.browser_timezone,
            locale=settings.browser_locale,
        )
    )


def build_probe(settings: Settings) -> SandboxConfig:
    return SandboxConfig(
        python_bin=settings.code_exec_python_bin,
        bwrap_bin=settings.browser_bwrap_bin,
        prlimit_bin=settings.browser_prlimit_bin,
        systemd_run_bin=settings.browser_systemd_run_bin,
        wall_timeout_seconds=min(settings.browser_start_timeout_seconds, 30),
        max_tasks=settings.browser_max_tasks,
        max_total_memory_mb=settings.browser_max_total_memory_mb,
        cpu_quota_percent=settings.browser_cpu_quota_percent,
        tmp_size_mb=settings.browser_tmp_size_mb,
        max_fsize_mb=settings.browser_max_fsize_mb,
        max_open_files=settings.browser_max_open_files,
        workspace_probe_root=str(Path(settings.workspace_dir).resolve()),
        network_mode=cast(SandboxNetworkMode, settings.browser_network_mode),
        sudo_bin=settings.browser_sudo_bin,
        netns_helper_bin=settings.browser_netns_helper_bin,
        netns_resolv_conf=settings.browser_netns_resolv_conf,
        network_probe_blocked_ip=settings.browser_network_probe_blocked_ip,
    )


def load_effective_settings() -> Settings:
    settings = Settings()
    apply_operator_settings(settings, config_dir=Path(settings.config_dir).resolve())
    return settings


async def main(settings: Settings) -> None:
    service = build_service(settings)
    unavailable = service.availability_error()
    if unavailable:
        raise RuntimeError(unavailable)
    probe = build_probe(settings)
    if not sandbox_available(probe):
        raise RuntimeError("browser sandbox probe failed")
    owner_a = "deployment-smoke-a"
    owner_b = "deployment-smoke-b"
    try:
        await service.acquire_turn(owner_a, "smoke-a-write")
        result = await service.run(
            owner_id=owner_a,
            turn_id="smoke-a-write",
            session="deployment-smoke",
            code=(
                "await page.goto('https://example.com'); "
                "await page.evaluate(() => localStorage.setItem('deployment-smoke', 'persisted')); "
                "await screenshot({kind:'proof'}); "
                "return {title: await page.title(), url: page.url()}"
            ),
        )
        if not result.get("ok") or result.get("result", {}).get("title") != "Example Domain":
            raise RuntimeError(f"unexpected browser result: {result}")
        await service.release_turn(owner_a, "smoke-a-write")

        await service.acquire_turn(owner_b, "smoke-b-read")
        isolated = await service.run(
            owner_id=owner_b,
            turn_id="smoke-b-read",
            session="deployment-smoke",
            code=(
                "await page.goto('https://example.com'); "
                "return await page.evaluate(() => localStorage.getItem('deployment-smoke'))"
            ),
        )
        if not isolated.get("ok") or isolated.get("result") is not None:
            raise RuntimeError(f"browser profiles were not isolated: {isolated}")
        await service.release_turn(owner_b, "smoke-b-read")

        await service.acquire_turn(owner_a, "smoke-a-read")
        persisted = await service.run(
            owner_id=owner_a,
            turn_id="smoke-a-read",
            session="deployment-smoke",
            code=(
                "await page.goto('https://example.com'); "
                "return await page.evaluate(() => localStorage.getItem('deployment-smoke'))"
            ),
        )
        if not persisted.get("ok") or persisted.get("result") != "persisted":
            raise RuntimeError(f"browser profile did not persist: {persisted}")
        await service.release_turn(owner_a, "smoke-a-read")
        await service.close()

        visual_service = VisualService(
            replace(
                service.config,
                bridge_script=PROJECT_ROOT / "web_browser/visual_bridge.mjs",
            ),
            max_output_bytes=settings.browser_max_screenshot_bytes,
        )
        with tempfile.TemporaryDirectory(prefix="kimi-visual-smoke-") as temporary:
            root = Path(temporary)
            chart_dir = root / "chart"
            chart_dir.mkdir()
            chart = await visual_service.render(
                VisualRenderRequest(
                    kind="chart",
                    chart_type="scatter",
                    title="Deployment smoke scatter chart",
                    alt_text=(
                        "Two observations overlap at zero while values span both signs and "
                        "several orders of magnitude."
                    ),
                    x_scale="symlog",
                    y_scale="symlog",
                    overlap_mode="count",
                    series=(
                        VisualSeries(
                            name="Values",
                            points=(
                                ScatterPoint(0.0, 0.0),
                                ScatterPoint(0.0, 0.0),
                                ScatterPoint(-0.001, 0.002),
                                ScatterPoint(1_000_000.0, -1_000_000.0),
                            ),
                        ),
                    ),
                ),
                chart_dir,
            )
            verify_rendered_png(chart.output_path, settings.browser_max_screenshot_bytes)

            mermaid_dir = root / "mermaid"
            mermaid_dir.mkdir()
            mermaid = await visual_service.render(
                VisualRenderRequest(
                    kind="mermaid",
                    title="Deployment smoke diagram",
                    alt_text="Start leads to done.",
                    source="flowchart LR\n  A[Start] --> B[Done]",
                ),
                mermaid_dir,
            )
            verify_rendered_png(mermaid.output_path, settings.browser_max_screenshot_bytes)

        print(
            "browser smoke passed: public navigation, persistence, user isolation, "
            "symlog/count scatter rendering, and Mermaid rendering"
        )
    finally:
        for owner, turn in (
            (owner_a, "smoke-a-write"),
            (owner_a, "smoke-a-read"),
            (owner_b, "smoke-b-read"),
        ):
            await service.release_turn(owner, turn)
        await service.delete_user_data(owner_a)
        await service.delete_user_data(owner_b)
        await service.close()


if __name__ == "__main__":
    asyncio.run(main(load_effective_settings()))
