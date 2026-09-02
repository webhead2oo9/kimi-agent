from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent.attachments import AttachmentStore
from workspace import ENV_DIR_NAMES, WorkspaceManager
from app.memory import MemoryManager
from app.modules import ModuleManager
from app.plugins import PluginLoadState, build_plugin_context, load_plugins_with_settings
from app.providers import ProviderManager
from config.plugin_settings import PluginSettingsRegistry
from config.settings import Settings
from discord_adapter.gateway import DiscordGateway
from image_gen.factory import ImageBackendConfig, build_image_backend
from image_gen.service import ImageGenService
from providers.factory import get_codex_auth_manager, get_xai_auth_manager
from skills.admin import SkillAdminService
from skills.loader import SharedSkillCatalog, scan_skills
from skills.personal import PersonalSkillManager
from skills.registration import build_script_sandbox_limits, reload_all_skill_tools
from skills.sandbox import ScriptSandboxLimits, validate_sandbox_runtime
from skills.secrets import load_secrets
from search.brave import BraveSearchBackend
from search.chain import SearchChain
from search.exa import ExaSearchBackend
from search.tinyfish import TinyFishSearchBackend
from search.types import SearchBackend
from sandbox.runner import SandboxConfig, SandboxNetworkMode, sandbox_available
from sandbox.netns_lease import NetnsLease
from tools.block_users import BlockedUserStoreProtocol, init_block_user_tool
from tools.browse import init_browse_tools
from tools.channel_context import init_channel_context_tool
from tools.code_exec import CodeExecRuntimeGuards, init_code_exec_tool
from tools.discord_text_search import (
    DiscordSearchApiClient,
    DiscordTextSearchConfig,
    DiscordTextSearchScopeResolver,
    init_discord_text_search_tool,
)
from tools.embeds import init_embed_tool
from tools.image_gen import init_image_gen_tool
from tools.internet_search import InternetSearchConfig, init_internet_search_tool
from tools.member import init_member_lookup_tool
from tools.persona import (
    LLMPersonaCompiler,
    PersonaPreferenceStore,
    PersonaToolConfig,
    init_persona_tools,
)
from tools.personal_skills import init_personal_skill_tools
from tools.plan import init_plan_tool
from tools.registry import ToolRegistry
from tools.learn import LearnHook
from tools.skills import init_skill_tools
from tools.threads import (
    ThreadLifecyclePermissionChecker,
    ThreadTargetResolver,
    init_thread_tools,
)
from tools.video import init_video_tool
from tools.wolfram_alpha import (
    WolframAlphaClient,
    WolframAlphaConfig,
    init_wolfram_alpha_tool,
)
from tools.x_search import XSearchConfig, init_x_search_tool
from tools.workspace import UserLocks, WorkspaceToolConfig, init_workspace_tools
from trust.tiers import trust_tier_from_value
from video_understanding.client import GeminiVideoClient
from video_understanding.service import VideoUnderstandingService
from video_understanding.service import VideoSessionRepository
from web_browser.service import BrowserService, BrowserServiceConfig
from web_browser.visual_service import VisualService

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.threads import ThreadHandoffManager


def _validate_executable_skill_sandbox(skills_store: Path, limits: ScriptSandboxLimits) -> bool:
    """Validate the OS boundary iff this store contains executable tools."""

    if not any(meta.tools for meta in scan_skills(skills_store).values()):
        return False
    validate_sandbox_runtime(limits)
    return True


@dataclass
class RuntimeTools:
    registry: ToolRegistry
    skills_store: Path
    skill_catalog: SharedSkillCatalog
    workspace_dir: Path
    attachment_dir: Path
    workspace_manager: WorkspaceManager
    workspace_locks: UserLocks
    workspace_config: WorkspaceToolConfig
    attachment_store: AttachmentStore
    personal_skill_manager: PersonalSkillManager
    skill_admin_service: SkillAdminService
    script_semaphore: asyncio.Semaphore
    reload_executable_skill_tools: Callable[[], int]
    browser_service: BrowserService
    video_service: VideoUnderstandingService
    code_sandbox_config: SandboxConfig | None = None
    code_exec_guards: CodeExecRuntimeGuards | None = None
    plugin_load_state: PluginLoadState = field(default_factory=PluginLoadState)
    plugin_settings: PluginSettingsRegistry | None = None
    module_manager: ModuleManager = field(default_factory=ModuleManager)


def build_runtime_tools(
    settings: Settings,
    gateway: DiscordGateway,
    provider_manager: ProviderManager,
    memory_manager: MemoryManager,
    *,
    get_preference_store: Callable[[], PersonaPreferenceStore | None] | None = None,
    get_blocked_user_store: Callable[[], BlockedUserStoreProtocol | None] | None = None,
    get_video_session_store: Callable[[], VideoSessionRepository | None] | None = None,
    get_thread_handoff: Callable[[], ThreadHandoffManager | None] | None = None,
    resolve_thread_target: ThreadTargetResolver | None = None,
    can_manage_thread: ThreadLifecyclePermissionChecker | None = None,
    registry: ToolRegistry | None = None,
    on_learn: LearnHook | None = None,
) -> RuntimeTools:
    registry = registry or ToolRegistry(owner_user_id=settings.owner_user_id)
    # Apply the owner gate authoritatively: a registry constructed elsewhere and
    # passed in (app/runtime.py) would otherwise have no owner, masking
    # owner_only tools for everyone, including the owner.
    registry.set_owner_user_id(settings.owner_user_id)
    skills_store = Path(settings.skills_dir).resolve()
    skill_catalog = SharedSkillCatalog(skills_store, bot_name=settings.bot_name)
    builtin_skills = skill_catalog.validate_builtin()
    log.info("Validated %d built-in skill(s)", len(builtin_skills))
    workspace_dir = Path(settings.workspace_dir)
    attachment_dir = Path(settings.attachment_store_dir)
    workspace_manager = WorkspaceManager(
        base_dir=workspace_dir,
        file_ttl=settings.workspace_file_ttl,
        max_size_bytes=settings.workspace_max_size_mb * 1024 * 1024,
        env_max_bytes=settings.code_exec_env_dir_max_mb * 1024 * 1024,
        env_max_files=settings.code_exec_env_dir_max_files,
    )
    personal_skill_manager = PersonalSkillManager(Path(settings.personal_skills_dir))
    attachment_store = AttachmentStore(
        base_dir=attachment_dir,
        max_bytes=settings.attachment_max_bytes,
        max_total_bytes=settings.attachment_max_total_bytes,
    )
    script_semaphore = asyncio.Semaphore(settings.script_max_concurrency)
    netns_lease = NetnsLease()
    sandbox_runtime_validated = False

    def ensure_executable_skill_sandbox() -> None:
        nonlocal sandbox_runtime_validated
        if not sandbox_runtime_validated:
            sandbox_runtime_validated = _validate_executable_skill_sandbox(
                skills_store, build_script_sandbox_limits(settings)
            )

    init_browse_tools(registry)
    init_plan_tool(registry)
    init_channel_context_tool(registry, gateway)
    init_member_lookup_tool(registry, gateway)
    if get_blocked_user_store is not None:
        init_block_user_tool(registry, get_blocked_user_store)
    _register_discord_text_search(settings, registry, gateway)
    _register_internet_search(settings, registry)
    _register_x_search(settings, registry)
    _register_wolfram_alpha(settings, registry)
    workspace_config = _workspace_tool_config(settings)
    workspace_locks = init_workspace_tools(
        registry,
        workspace_manager,
        config=workspace_config,
    )
    _register_image_gen(
        settings,
        registry,
        workspace_manager,
        workspace_locks=workspace_locks,
        workspace_config=workspace_config,
    )
    video_service = _register_video(
        settings,
        registry,
        get_video_session_store or (lambda: None),
        workspace_manager=workspace_manager,
        workspace_locks=workspace_locks,
    )
    browser_service = _register_browser(
        settings,
        registry,
        workspace_manager,
        workspace_locks=workspace_locks,
        netns_lease=netns_lease,
    )
    code_exec_guards = CodeExecRuntimeGuards.create(
        max_concurrency=settings.code_exec_max_concurrency,
        network_weekly_limit=settings.code_exec_network_weekly_limit,
        netns_lease=netns_lease,
        netns_conflict=browser_service.has_active_turn,
        netns_yield=browser_service.close_idle_owner,
    )
    code_sandbox_config = _register_code_exec(
        settings,
        registry,
        workspace_manager,
        workspace_locks,
        netns_lease=netns_lease,
        netns_conflict=browser_service.has_active_turn,
        runtime_guards=code_exec_guards,
    )
    init_personal_skill_tools(registry, personal_skill_manager)
    _register_persona_tools(
        settings,
        registry,
        provider_manager,
        get_preference_store or (lambda: None),
    )
    init_embed_tool(registry, workspace_manager, workspace_locks)
    if settings.thread_handoff_enabled and get_thread_handoff is not None:
        init_thread_tools(
            registry,
            get_thread_handoff,
            bot_name=settings.bot_name,
            resolve_target=resolve_thread_target,
            can_manage_thread=can_manage_thread,
        )
        log.info("Thread handoff tools registered")

    # Operator plugins load after every core tool, so a duplicate name raises
    # inside the plugin and resolves in core's favor.
    plugin_context = build_plugin_context(settings, registry, gateway)
    loaded_plugins, plugin_settings = load_plugins_with_settings(
        settings.plugin_module_list,
        plugin_context,
        settings_registry=PluginSettingsRegistry(config_dir=Path(settings.config_dir)),
    )
    plugin_load_state = PluginLoadState.from_loaded(settings.plugin_module_list, loaded_plugins)
    module_manager = ModuleManager.load(
        settings.kimi_module_list,
        core_settings=settings,
        registry=registry,
    )

    def reload_executable_skill_tools() -> int:
        ensure_executable_skill_sandbox()
        all_secrets = load_secrets(Path(settings.secrets_file))
        count = reload_all_skill_tools(
            skills_store=skills_store,
            registry=registry,
            secrets=all_secrets,
            settings=settings,
            workspace_manager=workspace_manager,
            script_semaphore=script_semaphore,
            workspace_locks=workspace_locks,
        )
        log.info("Executable skill tool reload complete: %d tool(s)", count)
        return count

    def reload_skills_on_change() -> None:
        reload_executable_skill_tools()

    skill_admin_service = SkillAdminService(
        skills_store,
        on_skills_changed=reload_skills_on_change,
        reserved_names=frozenset(builtin_skills),
    )
    init_skill_tools(
        registry,
        on_skills_changed=reload_skills_on_change,
        skill_admin_service=skill_admin_service,
        skill_catalog=skill_catalog,
        on_learn=on_learn,
    )
    # Deliberately outside the compatibility wrapper below: if executable tools
    # are configured, failure to create their isolation boundary aborts startup
    # instead of silently leaving an unsafe or half-configured deployment.
    ensure_executable_skill_sandbox()
    _safe_reload_executable_skill_tools(reload_executable_skill_tools)
    _log_capability_summary(registry)

    return RuntimeTools(
        registry=registry,
        skills_store=skills_store,
        skill_catalog=skill_catalog,
        workspace_dir=workspace_dir,
        attachment_dir=attachment_dir,
        workspace_manager=workspace_manager,
        workspace_locks=workspace_locks,
        workspace_config=workspace_config,
        attachment_store=attachment_store,
        personal_skill_manager=personal_skill_manager,
        skill_admin_service=skill_admin_service,
        script_semaphore=script_semaphore,
        reload_executable_skill_tools=reload_executable_skill_tools,
        browser_service=browser_service,
        video_service=video_service,
        code_sandbox_config=code_sandbox_config,
        code_exec_guards=code_exec_guards if code_sandbox_config is not None else None,
        plugin_load_state=plugin_load_state,
        plugin_settings=plugin_settings,
        module_manager=module_manager,
    )


# Headline capabilities and the config each one needs, for the boot summary.
# Deliberately not a registry dump: these are the tools whose absence an operator
# would otherwise have to infer from the model's behavior. Memory tools are
# absent by design: they register later, once Hindsight answers (app/memory.py).
#
# Each entry is (label, tools, gate). The FIRST tool is the registration probe.
# Thread creation intentionally excludes leave_thread so disabling new handoffs
# never strands an already-managed conversation.
CAPABILITY_PROBES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("thread handoff creation", ("move_to_thread",), "THREAD_HANDOFF_ENABLED"),
    (
        "discord search",
        ("discord_text_search",),
        "DISCORD_TEXT_SEARCH_ENABLED + MESSAGE_CONTENT_INTENT",
    ),
    (
        "internet search",
        ("internet_search",),
        "TINYFISH_API_KEY or EXA_API_KEY or BRAVE_API_KEY",
    ),
    (
        "X search",
        ("x_search",),
        "X_SEARCH_ENABLED + xAI OAuth or GROK_API_KEY",
    ),
    ("Wolfram|Alpha", ("wolfram_alpha",), "WOLFRAM_ALPHA_APP_ID"),
    (
        "image generation",
        ("generate_image",),
        "IMAGE_GEN_ENABLED + Codex OAuth or IMAGE_GEN_API_KEY",
    ),
    (
        "video understanding",
        ("video",),
        "VIDEO_UNDERSTANDING_ENABLED + GEMINI_API_KEY",
    ),
    ("code execution", ("run_code",), "CODE_EXEC_ENABLED + Linux sandbox support"),
    ("persistent browser", ("browser",), "BROWSER_ENABLED + BetterWright runtime"),
    (
        "visual rendering",
        ("render_chart", "render_diagram"),
        "BROWSER_ENABLED + BetterWright and Mermaid runtime",
    ),
)


def _log_capability_summary(registry: ToolRegistry) -> None:
    """One boot line for what came up and what did not.

    Registration is fail-closed everywhere, so a missing key silently registers
    nothing, which is correct but invisible. This makes "hidden because
    unconfigured" distinguishable from "broken" without reading the whole log.
    """
    on = [label for label, tools, _ in CAPABILITY_PROBES if registry.is_registered(tools[0])]
    off = [
        (label, gate)
        for label, tools, gate in CAPABILITY_PROBES
        if not registry.is_registered(tools[0])
    ]
    log.info("Capabilities enabled: %s", ", ".join(on) if on else "none")
    if off:
        log.info(
            "Capabilities unavailable: %s",
            "; ".join(f"{label} (needs {gate})" for label, gate in off),
        )


def _register_discord_text_search(
    settings: Settings,
    registry: ToolRegistry,
    scope_resolver: DiscordTextSearchScopeResolver,
) -> None:
    if not settings.discord_text_search_enabled:
        log.info("Discord text search disabled; DISCORD_TEXT_SEARCH_ENABLED is false")
        return
    if not settings.message_content_intent:
        log.info("Discord text search disabled; MESSAGE_CONTENT_INTENT is false")
        return
    init_discord_text_search_tool(
        registry,
        DiscordSearchApiClient(
            settings.discord_bot_token.get_secret_value(),
            timeout_seconds=settings.discord_search_timeout_seconds,
        ),
        scope_resolver,
        DiscordTextSearchConfig(
            excluded_channel_ids=settings.discord_search_excluded_channel_ids,
            timeout_seconds=settings.discord_search_timeout_seconds,
        ),
    )
    log.info(
        "Discord text search enabled with %d excluded channels",
        len(settings.discord_search_excluded_channel_ids),
    )


def _register_internet_search(settings: Settings, registry: ToolRegistry) -> None:
    tinyfish_key = settings.tinyfish_api_key.get_secret_value()
    exa_key = settings.exa_api_key.get_secret_value()
    brave_key = settings.brave_api_key.get_secret_value()
    backends: list[SearchBackend] = []
    # TinyFish leads: its search and fetch endpoints are free at any balance.
    if tinyfish_key:
        backends.append(
            TinyFishSearchBackend(
                tinyfish_key,
                search_url=settings.tinyfish_search_url,
                fetch_url=settings.tinyfish_fetch_url,
                timeout_seconds=settings.internet_search_backend_timeout_seconds,
            )
        )
    if exa_key:
        backends.append(
            ExaSearchBackend(
                exa_key,
                api_base=settings.exa_api_base,
                timeout_seconds=settings.internet_search_backend_timeout_seconds,
            )
        )
    if brave_key:
        backends.append(
            BraveSearchBackend(
                brave_key,
                context_url=settings.brave_context_url,
                timeout_seconds=settings.internet_search_backend_timeout_seconds,
                safesearch=settings.internet_search_safesearch,
            )
        )
    if not backends:
        log.info(
            "Internet search disabled; TINYFISH_API_KEY, EXA_API_KEY and BRAVE_API_KEY are not set"
        )
        return

    chain = SearchChain(
        backends,
        timeout_seconds=settings.internet_search_backend_timeout_seconds,
    )
    init_internet_search_tool(
        registry,
        InternetSearchConfig(
            chain=chain,
            max_results=settings.internet_search_max_results,
            max_backend_calls_per_turn=settings.internet_search_max_backend_calls_per_turn,
            max_output_chars=settings.internet_search_max_output_chars,
            timeout_seconds=settings.internet_search_timeout_seconds,
            fallback_cost_usd={
                ("exa", "search"): settings.exa_search_cost_usd,
                ("exa", "contents"): settings.exa_contents_cost_usd,
                ("brave", "search"): settings.brave_search_cost_usd,
            },
        ),
    )
    log.info("Internet search enabled with providers: %s", ", ".join(b.name for b in backends))


def _register_x_search(settings: Settings, registry: ToolRegistry) -> None:
    if not settings.x_search_enabled:
        log.info("X search disabled; X_SEARCH_ENABLED is false")
        return

    from xai.auth import XaiAuthError
    from xai.credentials import AUTH_MODE_API_KEY, XaiCredentialResolver
    from xai.responses import XaiResponsesClient

    try:
        manager = (
            None
            if settings.x_search_auth_mode == AUTH_MODE_API_KEY
            else get_xai_auth_manager(settings.xai_oauth_token_file)
        )
        resolver = XaiCredentialResolver(
            auth_mode=settings.x_search_auth_mode,
            oauth_manager=manager,
            api_key=settings.grok_api_key.get_secret_value(),
        )
        available = resolver.is_available()
    except XaiAuthError as exc:
        log.warning("X search OAuth credentials could not be loaded (%s); tool not registered", exc)
        return
    if not available:
        log.warning(
            "X search requested but no credential allowed by X_SEARCH_AUTH_MODE is available; "
            "tool not registered"
        )
        return
    client = XaiResponsesClient(
        resolver,
        timeout_seconds=settings.x_search_timeout_seconds,
        max_retries=2,
        user_agent=settings.bot_name,
    )
    init_x_search_tool(
        registry,
        XSearchConfig(
            client=client,
            credential_resolver=resolver,
            model=settings.x_search_model,
            max_calls_per_turn=settings.x_search_max_calls_per_turn,
        ),
    )
    log.info(
        "X search enabled with %s auth (OAuth-first in auto mode)",
        settings.x_search_auth_mode,
    )


def _register_wolfram_alpha(settings: Settings, registry: ToolRegistry) -> None:
    app_id = settings.wolfram_alpha_app_id.get_secret_value().strip()
    if not app_id:
        log.info("Wolfram|Alpha disabled; WOLFRAM_ALPHA_APP_ID is not set")
        return
    init_wolfram_alpha_tool(
        registry,
        WolframAlphaConfig(
            client=WolframAlphaClient(app_id),
            max_calls_per_turn=settings.wolfram_alpha_max_calls_per_turn,
            max_output_chars=settings.wolfram_alpha_max_output_chars,
            timeout_seconds=settings.wolfram_alpha_timeout_seconds,
            call_cost_usd=settings.wolfram_alpha_call_cost_usd,
        ),
    )
    log.info("Wolfram|Alpha enabled with the LLM API")


def _register_image_gen(
    settings: Settings,
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    *,
    workspace_locks: UserLocks,
    workspace_config: WorkspaceToolConfig,
) -> None:
    if not settings.image_gen_enabled:
        log.info("Image generation disabled; IMAGE_GEN_ENABLED is false")
        return
    auth_manager = (
        get_codex_auth_manager(settings.codex_token_file)
        if settings.image_gen_auth_mode in {"auto", "oauth"}
        else None
    )
    backend = build_image_backend(
        ImageBackendConfig(
            backend=settings.image_gen_backend,
            auth_mode=settings.image_gen_auth_mode,
            api_key=settings.image_gen_api_key.get_secret_value().strip(),
            timeout_seconds=settings.image_gen_timeout_seconds,
        ),
        auth_manager,
    )
    if backend is None:
        log.warning(
            "Image generation requested but no usable OAuth or API-key credentials "
            "are available; tool not registered"
        )
        return
    service = ImageGenService(
        backend,
        max_concurrency=settings.image_gen_max_concurrency,
    )
    init_image_gen_tool(
        registry,
        service,
        workspace_manager,
        workspace_locks,
        workspace_config,
    )
    log.info("Image generation enabled with %s (%s auth)", backend.name, backend.auth_mode)


def _register_video(
    settings: Settings,
    registry: ToolRegistry,
    get_store: Callable[[], VideoSessionRepository | None],
    *,
    workspace_manager: WorkspaceManager,
    workspace_locks: UserLocks,
) -> VideoUnderstandingService:
    key = settings.gemini_api_key.get_secret_value().strip()
    service = VideoUnderstandingService(
        client=(
            GeminiVideoClient(
                key,
                max_concurrency=settings.video_understanding_max_concurrency,
            )
            if key
            else None
        ),
        get_store=get_store,
    )
    if not settings.video_understanding_enabled:
        log.info("Video understanding disabled; VIDEO_UNDERSTANDING_ENABLED is false")
        return service
    if not key:
        log.warning(
            "Video understanding requested but GEMINI_API_KEY is not set; tool not registered"
        )
        return service
    init_video_tool(
        registry,
        service,
        workspace_manager=workspace_manager,
        workspace_locks=workspace_locks,
    )
    log.info("Video understanding enabled with Gemini 3.7 Flash")
    return service


def build_sandbox_config(settings: Settings) -> SandboxConfig:
    """The code-execution profile startup runs, derived from the live settings.

    Shared with scripts/sandbox_probe.py so an operator's diagnostic exercises
    the same interpreter, network mode, binds, and workspace root that
    registration will, rather than the dataclass defaults.
    """
    venv_dir = settings.code_exec_venv_dir.strip()
    python_bin = (
        str(Path(venv_dir) / "bin" / "python3") if venv_dir else settings.code_exec_python_bin
    )
    extra_ro_binds = tuple(
        path
        for path in ([venv_dir] if venv_dir else [])
        + [value.strip() for value in settings.code_exec_extra_ro_binds.split(",")]
        if path
    )
    network_mode = cast(SandboxNetworkMode, settings.code_exec_network_mode)
    return SandboxConfig(
        python_bin=python_bin,
        bwrap_bin=settings.code_exec_bwrap_bin,
        prlimit_bin=settings.code_exec_prlimit_bin,
        systemd_run_bin=settings.code_exec_systemd_run_bin,
        wall_timeout_seconds=settings.code_exec_wall_timeout_seconds,
        max_cpu_seconds=settings.code_exec_max_cpu_seconds,
        max_memory_mb=settings.code_exec_max_memory_mb,
        max_tasks=settings.code_exec_max_tasks,
        max_total_memory_mb=settings.code_exec_max_total_memory_mb,
        cpu_quota_percent=settings.code_exec_cpu_quota_percent,
        tmp_size_mb=settings.code_exec_tmp_size_mb,
        max_fsize_mb=settings.code_exec_max_fsize_mb,
        max_open_files=settings.code_exec_max_open_files,
        max_workspace_bytes=settings.workspace_tool_max_user_bytes,
        max_workspace_files=settings.code_exec_max_workspace_files,
        workspace_quota_poll_seconds=settings.code_exec_workspace_quota_poll_seconds,
        workspace_quota_scan_retries=settings.code_exec_workspace_quota_scan_retries,
        max_output_bytes=settings.code_exec_max_output_bytes,
        workspace_probe_root=str(Path(settings.workspace_dir).resolve()),
        extra_ro_binds=extra_ro_binds,
        network_mode=network_mode,
        sudo_bin=settings.code_exec_sudo_bin,
        netns_helper_bin=settings.code_exec_netns_helper_bin,
        netns_resolv_conf=settings.code_exec_netns_resolv_conf,
        network_probe_blocked_ip=settings.code_exec_network_probe_blocked_ip,
        extra_env=(
            ("PLATFORMIO_CORE_DIR", "/work/.pio-core"),
            ("PLATFORMIO_SETTING_ENABLE_TELEMETRY", "No"),
            ("PLATFORMIO_SETTING_CHECK_PLATFORMIO_INTERVAL", "9999999"),
            ("PLATFORMIO_SETTING_CHECK_PLATFORMS_INTERVAL", "9999999"),
            ("PLATFORMIO_SETTING_CHECK_LIBRARIES_INTERVAL", "9999999"),
            ("GIT_AUTHOR_NAME", settings.bot_name),
            ("GIT_AUTHOR_EMAIL", "code-exec@localhost"),
            ("GIT_COMMITTER_NAME", settings.bot_name),
            ("GIT_COMMITTER_EMAIL", "code-exec@localhost"),
            ("GIT_TERMINAL_PROMPT", "0"),
        ),
        env_dir_names=tuple(sorted(ENV_DIR_NAMES)),
        max_env_bytes=settings.code_exec_env_dir_max_mb * 1024 * 1024,
        max_env_files=settings.code_exec_env_dir_max_files,
    )


def _register_code_exec(
    settings: Settings,
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    locks: UserLocks,
    *,
    netns_lease: NetnsLease | None = None,
    netns_conflict: Callable[[str, str], bool] | None = None,
    runtime_guards: CodeExecRuntimeGuards | None = None,
) -> SandboxConfig | None:
    if not settings.code_exec_enabled:
        log.info("Code execution disabled; CODE_EXEC_ENABLED is not set")
        return None

    sandbox_config = build_sandbox_config(settings)
    network_mode = sandbox_config.network_mode
    if not sandbox_available(sandbox_config):
        log.warning(
            "Code execution enabled but the %s sandbox profile failed its startup "
            "probe; run_code was not registered. See docs/code-exec.md.",
            network_mode,
        )
        return None

    init_code_exec_tool(
        registry,
        workspace_manager,
        sandbox_config,
        locks=locks,
        min_tier=trust_tier_from_value(
            settings.code_exec_min_tier,
            label="CODE_EXEC_MIN_TIER",
        ),
        max_concurrency=settings.code_exec_max_concurrency,
        max_user_bytes=settings.workspace_tool_max_user_bytes,
        network_weekly_limit=settings.code_exec_network_weekly_limit,
        netns_lease=netns_lease,
        netns_conflict=netns_conflict,
        runtime_guards=runtime_guards,
    )
    log.info(
        "Code execution enabled in %s network mode at %s tier",
        network_mode,
        settings.code_exec_min_tier,
    )
    return sandbox_config


def _register_browser(
    settings: Settings,
    registry: ToolRegistry,
    workspace_manager: WorkspaceManager,
    *,
    workspace_locks: UserLocks,
    netns_lease: NetnsLease,
) -> BrowserService:
    bridge = Path(__file__).resolve().parent.parent / settings.browser_bridge_script
    network_mode = cast(Any, settings.browser_network_mode)
    service = BrowserService(
        BrowserServiceConfig(
            runtime_dir=Path(settings.browser_runtime_dir),
            profiles_dir=Path(settings.browser_profiles_dir),
            bridge_script=bridge,
            network_mode=network_mode,
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
        ),
        netns_lease=netns_lease,
    )
    # Retention and privacy deletion remain wired even when execution is off or
    # temporarily unavailable.
    if not settings.browser_enabled:
        log.info("Persistent browser disabled; BROWSER_ENABLED is false")
        return service
    unavailable = service.availability_error()
    if unavailable is not None:
        log.warning("Persistent browser unavailable: %s", unavailable)
        return service

    probe = SandboxConfig(
        python_bin=settings.code_exec_python_bin,
        bwrap_bin=settings.browser_bwrap_bin,
        prlimit_bin=settings.browser_prlimit_bin,
        systemd_run_bin=settings.browser_systemd_run_bin,
        wall_timeout_seconds=min(settings.browser_start_timeout_seconds, 30.0),
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
    if not sandbox_available(probe):
        log.warning(
            "Persistent browser %s network sandbox failed its startup probe",
            settings.browser_network_mode,
        )
        return service
    from tools.browser import BrowserToolConfig, init_browser_tool

    init_browser_tool(
        registry,
        service,
        workspace_manager,
        BrowserToolConfig(
            max_screenshot_bytes=settings.browser_max_screenshot_bytes,
            max_attachments=settings.workspace_tool_max_attachments,
        ),
        workspace_locks,
    )
    log.info(
        "Persistent browser enabled in %s mode (per-user profiles)",
        settings.browser_network_mode,
    )

    visual_bridge = Path(__file__).resolve().parent.parent / "web_browser/visual_bridge.mjs"
    visual_service = VisualService(
        replace(service.config, bridge_script=visual_bridge),
        max_output_bytes=settings.browser_max_screenshot_bytes,
    )
    visual_unavailable = visual_service.availability_error()
    if visual_unavailable is not None:
        log.warning(
            "Visual rendering unavailable; persistent browser remains enabled: %s",
            visual_unavailable,
        )
        return service

    from tools.visuals import VisualToolConfig, init_visual_tool

    init_visual_tool(
        registry,
        visual_service,
        workspace_manager,
        VisualToolConfig(
            max_png_bytes=settings.browser_max_screenshot_bytes,
            max_attachments=settings.workspace_tool_max_attachments,
        ),
        workspace_locks,
    )
    log.info("Visual rendering enabled with the pinned Mermaid runtime")
    return service


def _register_persona_tools(
    settings: Settings,
    registry: ToolRegistry,
    provider_manager: ProviderManager,
    get_preference_store: Callable[[], PersonaPreferenceStore | None],
) -> None:
    model_config = provider_manager.model_config
    model = model_config.roles.persona if model_config is not None else None
    if model is None:
        log.info("Persona tools disabled; config/models.yaml assigns no persona role")
        return

    compiler = LLMPersonaCompiler(
        provider_manager.ensure_persona(),
        max_chars=settings.user_persona_max_chars,
        max_output_tokens=settings.user_persona_compiler_max_tokens,
    )
    if init_persona_tools(
        registry,
        get_preference_store,
        compiler,
        PersonaToolConfig(max_request_chars=settings.user_persona_request_max_chars),
    ):
        log.info("Persona tools enabled with model %s", model)


def _workspace_tool_config(settings: Settings) -> WorkspaceToolConfig:
    return WorkspaceToolConfig(
        max_file_bytes=settings.workspace_tool_max_file_bytes,
        max_user_bytes=settings.workspace_tool_max_user_bytes,
        max_read_bytes=settings.workspace_tool_max_read_bytes,
        max_pdf_pages=settings.workspace_tool_max_pdf_pages,
        max_text_chars=settings.workspace_tool_max_text_chars,
        max_attachments=settings.workspace_tool_max_attachments,
        max_import_bytes=settings.workspace_tool_max_import_bytes,
        max_zip_entries=settings.workspace_tool_max_zip_entries,
        max_extract_total_bytes=settings.workspace_tool_max_extract_total_bytes,
        fetch_timeout_seconds=settings.workspace_tool_fetch_timeout_seconds,
        max_redirects=settings.workspace_tool_max_redirects,
        default_grep_results=settings.workspace_tool_default_grep_results,
        max_grep_results=settings.workspace_tool_max_grep_results,
        max_grep_context=settings.workspace_tool_max_grep_context,
        max_grep_line_chars=settings.workspace_tool_max_grep_line_chars,
        max_grep_pattern_chars=settings.workspace_tool_max_grep_pattern_chars,
        grep_timeout_seconds=settings.workspace_tool_grep_timeout_seconds,
        glob_max_results=settings.workspace_tool_glob_max_results,
        multi_edit_max_ops=settings.workspace_tool_multi_edit_max_ops,
        view_image_max_bytes=settings.workspace_tool_view_image_max_bytes,
        view_image_max_per_turn=settings.workspace_tool_view_image_max_per_turn,
        max_workspace_entries=settings.workspace_tool_max_entries,
    )


def _safe_reload_executable_skill_tools(reload_tools: Callable[[], int]) -> None:
    try:
        reload_tools()
    except Exception:
        log.exception("Executable skill tool reload failed; continuing with core tools only")
