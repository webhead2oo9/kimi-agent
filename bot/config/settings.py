from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings

from branding import DEFAULT_BOT_NAME
from config.environment import selected_env_file

type EventLogContentMode = Literal["metadata", "redacted", "full"]


def _env_file() -> str:
    """Which dotenv file backs the settings, defaulting to ``.env``.

    ``ENV_FILE=.env.dev`` boots a second instance (its own bot token, database,
    workspaces, and config/skills roots) against a test guild without touching
    production state. See ``docs/development.md``.

    A missing file is an error rather than pydantic's silent "load nothing",
    because that failure mode looks identical to a valid-but-empty config: the
    bot boots with a blank token and dies somewhere far from the typo.
    """
    return selected_env_file()


class Settings(BaseSettings):
    # validate_assignment ensures attribute assignment (e.g. in tests) coerces
    # plain strings into SecretStr for the secret fields below.
    model_config = {
        "env_file": _env_file(),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "validate_assignment": True,
    }

    # Discord
    discord_bot_token: SecretStr = SecretStr("")
    staff_role_ids: str = ""
    regular_role_ids: str = ""
    staff_user_ids: str = ""
    allowed_channel_ids: str = ""
    # Optional boot-time guild approvals. Runtime also activates a guild whose
    # validated config/servers/<id>.md has bot_active: true; a validated false
    # overrides this list without deleting the setup. Guilds approved by neither
    # source stay connected but cannot invoke messages or guild-installed commands.
    # Trust roles are matched by stable Discord role ID, not mutable role name.
    allowed_guild_ids: str = ""
    bot_name: str = DEFAULT_BOT_NAME

    # Optional Discord user-install surface. It is deliberately off by default;
    # enabling it registers the personal /chat commands globally. Access is
    # granted by stable Discord user ID, independently from guild roles/trust.
    user_app_chat_enabled: bool = False
    user_app_member_ids: str = ""
    user_app_regular_ids: str = ""
    user_app_staff_ids: str = ""
    # Discord interaction tokens live for 15 minutes. Keep the complete turn
    # below that boundary so delivery and cleanup retain a valid token.
    user_app_chat_timeout_seconds: float = Field(default=840.0, ge=1.0, le=840.0)
    # Ambient direct messages as a second entry point onto the same personal
    # root. Off by default and separately switchable, but it shares the
    # USER_APP_* access lists and requires the /chat surface, which owns the
    # self-service /privacy, /chat-reset, /memory, and /stop commands the DM
    # transcript depends on.
    user_app_dm_enabled: bool = False

    # Privileged gateway intents. As of Discord's 2026 policy, apps over 10,000
    # users must apply for these in the Developer Portal and reauthorize yearly
    # (https://support-dev.discord.com/hc/en-us/articles/6207308062871).
    #
    # The Members intent is off by default: trust roles come from the message
    # author (populated without the intent) and member lookups use on-demand
    # fetch/query, so ordinary operation buys nothing from it. Optional modules
    # may need member lifecycle events. Turning this on
    # also requires enabling Server Members in the Developer Portal, or the
    # gateway rejects the identify.
    #
    # Message Content is the one intent the ambient invocation surface needs.
    # When this is False (not yet approved, or a reauthorization lapse), the bot
    # still connects and answers @mentions and replies; those deliver content
    # without the intent. It loses the "hey <bot_name>" text trigger, thread
    # auto-reply, and discord_text_search, so leave thread_handoff_enabled off
    # while this is False.
    message_content_intent: bool = True
    members_intent: bool = False

    # LLM Provider secrets and execution knobs. Provider/model identity lives in
    # config/models.yaml; secrets stay in .env and are referenced by env var name.
    # Neutral key for generic OpenAI-compatible provider profiles.
    model_api_key: SecretStr = SecretStr("")
    # OpenCode Go subscription key (serves every opencode.ai/zen/go/v1 profile:
    # openai_compat /chat/completions and anthropic_compat /messages).
    opencode_go_api_key: SecretStr = SecretStr("")
    # RunInfra gateway key for OpenAI-compatible routes at api.runinfra.ai.
    runinfra_gateway_key: SecretStr = SecretStr("")
    # Native Anthropic Messages API key (anthropic provider).
    anthropic_api_key: SecretStr = SecretStr("")
    # xAI Grok key (openai_compat profile pointing at https://api.x.ai/v1).
    grok_api_key: SecretStr = SecretStr("")
    # Fireworks AI key (openai_compat profile pointing at
    # https://api.fireworks.ai/inference/v1).
    fireworks_api_key: SecretStr = SecretStr("")
    # Z.AI GLM Coding Plan key (openai_compat profile pointing at the dedicated
    # Coding API; separate from Z.AI's pay-as-you-go general API endpoint).
    zai_api_key: SecretStr = SecretStr("")
    # Kimi Code membership coding-plan key from the Kimi Code Console
    # (anthropic_compat profile pointing at https://api.kimi.com/coding/v1).
    # Separate product from the pay-as-you-go Kimi Open Platform.
    kimi_coding_api_key: SecretStr = SecretStr("")
    react_max_iterations: int = 200
    react_max_tokens: int = 65536
    # Absolute wall-clock budget starting at turn entry. It includes preparation,
    # moderation, memory/persistence, every provider/tool iteration, and finalization.
    # Streamed provider calls also have the shorter chunk-silence deadline below.
    react_turn_timeout_seconds: float = 3600.0
    # Abort a streamed provider request when the stream produces no data (headers
    # or chunks) for this long; the failover chain then tries the next backend.
    # Distinct from the turn deadline above: silence means dead, duration does not.
    provider_stream_stall_timeout_seconds: float = 90.0
    # New-user onboarding: inject a system-prompt note while a user has fewer than this many
    # prior messages with the bot (so the model knows they're new and orients/moderates
    # accordingly). 0 disables the feature.
    new_user_onboarding_turns: int = 5
    # Max concurrent in-flight provider calls across all users/channels.
    llm_max_concurrency: int = 8
    # Immediate admission boundary around the whole user-triggered turn, including
    # preparation, tools, delivery, and persistence. Unlike the provider semaphore,
    # this has no waiter queue: excess work is rejected with a retry message.
    turn_max_concurrency: int = 16
    turn_max_concurrency_per_user: int = 2
    # Sampling temperature for chat-based providers (openai_compat/openrouter).
    # None omits the param so the endpoint default applies. Other providers
    # ignore this field.
    react_temperature: float | None = 1.0
    codex_token_file: str = "secrets/codex-auth.json"
    codex_model: str = "gpt-5.5"
    codex_reasoning_effort: str = "high"
    codex_image_quality: str = "auto"
    codex_image_format: str = "png"
    codex_ws_idle_timeout: int = 3000
    codex_ws_read_timeout: float = 120.0
    codex_verbose: bool = False

    # Programmatic input/output content moderation (disabled unless enabled + key).
    moderation_enabled: bool = False
    moderation_api_key: SecretStr = SecretStr("")
    moderation_base_url: str = ""
    moderation_model: str = "omni-moderation-latest"
    moderation_timeout_seconds: float = 5.0
    moderation_input_images: bool = True
    moderation_output_images: bool = True
    # Blank = no exemption. Otherwise regular/staff can skip final output moderation
    # at or above this tier; input moderation still applies.
    moderation_output_exempt_tier: str = ""
    # Three distinct events, three replies: the member's message was blocked,
    # the bot's own reply was blocked, or the check could not run at all. Each
    # says what happened and what to do next; none names the matched category.
    moderation_input_refusal: str = (
        "That message didn't pass my content filter, so I didn't read it. Try rewording it."
    )
    moderation_output_refusal: str = "I wrote a reply, but it didn't pass my content filter, so I'm not posting it. Nothing's wrong on your end; try asking a different way."
    moderation_error_refusal: str = "I can't run my content check right now, so I'm holding this one back. Try again in a minute."

    # Discord guild text search. The compatibility allowlist field is a migration
    # sentinel: any non-empty value fails validation rather than silently
    # inverting the configured access policy.
    discord_text_search_enabled: bool = True
    discord_search_excluded_channels: str = ""
    discord_search_channels: str = ""
    discord_search_timeout_seconds: float = 30.0

    # Live internet search. The tool registers when any provider key is set.
    # TinyFish is ordered first because its search and fetch endpoints are free;
    # Exa and TinyFish both read pages, Brave searches only.
    tinyfish_api_key: SecretStr = SecretStr("")
    tinyfish_search_url: str = "https://api.search.tinyfish.ai"
    tinyfish_fetch_url: str = "https://api.fetch.tinyfish.ai"
    exa_api_key: SecretStr = SecretStr("")
    exa_api_base: str = "https://api.exa.ai"
    exa_search_cost_usd: float | None = None
    exa_contents_cost_usd: float | None = None
    brave_api_key: SecretStr = SecretStr("")
    brave_context_url: str = "https://api.search.brave.com/res/v1/llm/context"
    brave_search_cost_usd: float | None = None
    internet_search_backend_timeout_seconds: float = 30.0
    internet_search_timeout_seconds: float = 45.0
    internet_search_max_results: int = 10
    internet_search_max_backend_calls_per_turn: int = 10
    internet_search_max_output_chars: int = 24_000
    internet_search_safesearch: str = "moderate"

    # Wolfram|Alpha computational knowledge (optional searchable tool). The
    # AppID is an environment-only secret and its presence gates registration.
    wolfram_alpha_app_id: SecretStr = SecretStr("")
    wolfram_alpha_timeout_seconds: float = 30.0
    wolfram_alpha_max_calls_per_turn: int = Field(default=3, ge=1, le=10)
    wolfram_alpha_max_output_chars: int = Field(default=6_800, ge=500, le=20_000)
    wolfram_alpha_call_cost_usd: float | None = None

    # Stateful public-YouTube understanding (optional searchable tool). The
    # Gemini key is dedicated to this tool and never participates in chat model
    # routing. Registration requires both the flag and a non-empty key.
    video_understanding_enabled: bool = False
    gemini_api_key: SecretStr = SecretStr("")
    video_understanding_max_concurrency: int = Field(default=4, ge=1, le=32)

    # OpenAI image generation (optional REGULAR-tier core tool). OAuth reuses
    # the Codex token manager; IMAGE_GEN_API_KEY is the dedicated fallback.
    image_gen_enabled: bool = False
    image_gen_backend: str = "openai"
    image_gen_auth_mode: str = "auto"
    image_gen_api_key: SecretStr = SecretStr("")
    image_gen_max_concurrency: int = Field(default=1, ge=1, le=8)
    image_gen_timeout_seconds: float = Field(default=300.0, ge=30.0, le=900.0)

    # The bot owner's Discord user id. Gates tools registered with owner_only at
    # dispatch (none ship today; the registry mechanism stays for future
    # owner-only surfaces); empty fails closed. Distinct from staff.
    owner_user_id: str = ""
    # --- Sandboxed code execution (MEMBER tier; docs/code-exec.md) ---
    # Disabled in tracked defaults. Enabling still requires the complete Linux
    # systemd-run/bwrap/prlimit/seccomp profile to pass an end-to-end startup probe.
    code_exec_enabled: bool = False
    code_exec_network_mode: str = "none"
    code_exec_python_bin: str = "/usr/bin/python3"
    # Optional packages venv mounted read-only, separate from the bot environment.
    code_exec_venv_dir: str = ""
    # Additional read-only host paths. They must never contain credentials.
    code_exec_extra_ro_binds: str = ""
    code_exec_bwrap_bin: str = "bwrap"
    code_exec_prlimit_bin: str = "prlimit"
    code_exec_systemd_run_bin: str = "systemd-run"
    code_exec_sudo_bin: str = "sudo"
    # The helper accepts the sandbox command but no namespace selector. Its
    # root-owned file chooses one namespace and drops privileges before exec.
    code_exec_netns_helper_bin: str = ""
    code_exec_netns_resolv_conf: str = ""
    # Known-open private endpoint that must be unreachable from netns mode.
    # Required for netns; unused by none and host.
    code_exec_network_probe_blocked_ip: str = ""
    # Build-capable limits: networked runs may install packages and compile projects.
    code_exec_wall_timeout_seconds: float = 300.0
    code_exec_max_cpu_seconds: int = 240
    code_exec_max_memory_mb: int = 3072
    code_exec_max_tasks: int = 256
    code_exec_max_total_memory_mb: int = 2048
    code_exec_cpu_quota_percent: int = 200
    code_exec_tmp_size_mb: int = 512
    code_exec_max_fsize_mb: int = 128
    code_exec_max_open_files: int = 1024
    code_exec_max_workspace_files: int = 50_000
    code_exec_workspace_quota_poll_seconds: float = 5.0
    code_exec_workspace_quota_scan_retries: int = 4
    code_exec_max_output_bytes: int = 40_000
    code_exec_max_concurrency: int = 1
    # Per-user allowances for regenerable workspace env dirs (.venv/.pio-style
    # tool caches), accounted outside the document quota. Bytes cap total env-dir
    # size; entries independently cap inode consumption from dependency trees.
    code_exec_env_dir_max_mb: int = 2048
    code_exec_env_dir_max_files: int = 200_000
    # Rolling seven-day cap in host/netns mode. Zero disables it; STAFF is exempt.
    code_exec_network_weekly_limit: int = 100

    # --- Durable coding agent (optional; docs/coding-agent.md) ---
    # Requires CODE_EXEC_ENABLED and an optional roles.coding assignment. The
    # general chat model never silently substitutes for a missing coding model.
    coding_tasks_enabled: bool = False
    coding_task_max_concurrency: int = 2
    coding_task_max_queued_per_workspace: int = 3
    coding_task_max_queued_per_user: int = 5
    coding_task_max_seconds: float = 7200.0
    coding_provider_call_timeout_seconds: float = 600.0
    coding_job_max_seconds: float = 2700.0
    coding_job_max_cpu_seconds: int = 2400
    coding_worker_stall_seconds: float = 120.0
    coding_status_min_interval_seconds: float = 10.0
    coding_stop_cleanup_wait_seconds: float = 10.0
    coding_task_max_iterations: int = 80

    # --- Persistent browser (BetterWright; docs/browser.md) ---
    # Disabled in tracked defaults. Enabling is still only a request: the tool
    # registers on Linux alone, and only after the pinned runtime and the
    # complete sandbox/network profile pass their startup checks.
    browser_enabled: bool = False
    browser_network_mode: str = "host"
    # Root-owned runtime from deploy/betterwright/install.sh, deliberately
    # outside the checkout so the bot account cannot rewrite what it executes.
    browser_runtime_dir: str = "/opt/kimi/betterwright"
    browser_profiles_dir: str = "data/browser_profiles"
    browser_bridge_script: str = "web_browser/bridge.mjs"
    browser_bwrap_bin: str = "bwrap"
    browser_prlimit_bin: str = "prlimit"
    browser_systemd_run_bin: str = "systemd-run"
    browser_systemctl_bin: str = "systemctl"
    browser_sudo_bin: str = "sudo"
    # Same contract as the code-exec helper: it takes the worker command and no
    # namespace selector, so the model can never choose where traffic egresses.
    browser_netns_helper_bin: str = ""
    browser_netns_resolv_conf: str = ""
    # Known-open private endpoint that must be unreachable from netns mode.
    # Required for netns; unused by host.
    browser_network_probe_blocked_ip: str = ""
    browser_call_timeout_seconds: float = 30.0
    browser_start_timeout_seconds: float = 20.0
    # Idle workers close; a worker past its maximum lifetime is recycled before
    # the next call, so a long-lived profile never rides one Chromium forever.
    browser_idle_ttl_seconds: float = 120.0
    browser_worker_max_lifetime_seconds: int = 3600
    # Profiles carry authenticated site state, so they age out on the same
    # inactivity sweep as workspaces and are capped in size.
    browser_profile_ttl_seconds: int = 7 * 24 * 60 * 60
    browser_max_profile_mb: int = 512
    browser_max_screenshot_bytes: int = 8 * 1024 * 1024
    # Whole-worker-cgroup aggregates, then per-process rlimits.
    browser_max_total_memory_mb: int = 2048
    browser_max_tasks: int = 256
    browser_cpu_quota_percent: int = 200
    browser_tmp_size_mb: int = 512
    browser_max_fsize_mb: int = 128
    browser_max_open_files: int = 1024
    # Presented to every page. Keep these consistent with wherever the selected
    # network mode actually egresses, or sites see a browser whose clock and
    # language disagree with its apparent location.
    browser_timezone: str = "UTC"
    browser_locale: str = "en-US"

    # --- ReAct-loop context compaction ---
    # Summarize stale in-loop tool history when a single turn's projected request
    # approaches the model window. Free on normal turns (never triggers).
    # Compact before the next request once projected input tokens reach this. Keep
    # compaction_trigger_tokens + react_max_tokens below the deployed model's window.
    compaction_trigger_tokens: int = 120000
    # Iterations kept verbatim; everything older is summarized into one progress note.
    # The floor on the kept tail: raising it keeps more verbatim and can re-approach
    # the trigger, since the kept window's assistant reasoning is not hard-truncatable.
    compaction_keep_recent_iterations: int = 3
    # Token budget for the verbatim tail: whole recent iterations are kept until this
    # is spent (never fewer than compaction_keep_recent_iterations). Keep it well
    # under compaction_trigger_tokens so a fresh compaction lands with headroom.
    compaction_keep_recent_tokens: int = 50000
    # Output cap for the summary note. Must be << compaction_trigger_tokens.
    compaction_max_tokens: int = 32768
    # Max cumulative tool-output tokens appended within ONE ReAct iteration before
    # remaining results in that iteration are stubbed (bounds mid-iteration growth).
    compaction_max_iteration_tool_output_tokens: int = 48000
    compaction_api_key: SecretStr = SecretStr("")

    # Attachments and image input
    attachment_store_dir: str = "data/attachments"
    attachment_max_bytes: int = 8 * 1024 * 1024
    # Aggregate bytes downloaded/staged by the normal message vision collector
    # across current, reply, and recent-history candidates in one turn.
    attachment_max_total_bytes: int = 32 * 1024 * 1024
    # Crash/cancellation fallback for staged images. Normal turn finalizers remove
    # their files immediately; this bounded sweeper handles process-death orphans.
    attachment_orphan_ttl_seconds: int = 24 * 60 * 60
    attachment_orphan_sweep_interval_seconds: int = 60 * 60
    attachment_orphan_sweep_max_files: int = 1000
    image_detail: str = "auto"
    recent_image_lookback: int = 10
    max_turn_images: int = 10

    # Hindsight
    hindsight_url: str = ""
    hindsight_api_key: SecretStr = SecretStr("")

    # User Memory
    # Recall the consolidated `observation` layer only by default. Including the raw
    # `world`/`experience` layers re-injects every fact two-to-four times (each
    # consolidated observation plus its raw sources), which floods the prompt; add
    # them back here only if you want the raw episodic detail. See docs/memory.md.
    memory_recall_types: str = "observation"
    # Hindsight recall budget knobs for automatic responding-turn recall.
    memory_recall_budget: str = "mid"
    memory_recall_max_tokens: int = 2048
    # Cap on proactive remember_user_memory writes per turn (bounds bloat).
    memory_max_writes_per_turn: int = 3

    # Auto-retain (docs/memory.md): background idle-flush of conversation
    # transcripts into each memory-enabled participant's Hindsight bank. The
    # per-user preference defaults enabled; /memory opt-out disables it.
    memory_auto_retain_enabled: bool = False
    memory_auto_retain_idle_minutes: int = 30
    memory_auto_retain_sweep_interval_seconds: int = 300
    # Slices with less user-authored text than this are skipped (watermark
    # still advances) so greetings and one-liners don't trigger extraction.
    memory_auto_retain_min_user_chars: int = 80
    memory_auto_retain_max_content_chars: int = 24000
    # Conversations first seen already idle longer than this are marked as
    # handled without retaining, so there is no surprise ingestion of old
    # history.
    memory_auto_retain_backfill_horizon_hours: int = 24
    memory_auto_retain_max_flushes_per_sweep: int = 20

    # Transcript retention (docs/privacy.md): a background sweep purges whole
    # conversations whose last activity is older than this many days. It touches
    # the raw SQLite transcript only. Distilled Hindsight memory is NOT on this
    # clock (it is governed by /memory opt-out and the /privacy memory-delete
    # path), so memory-enabled users keep their long-term memory while raw
    # conversation logs age out. On by default; the first sweep after this is
    # active deletes existing conversations already idle past the window. Set to
    # 0 to disable the sweep (keep transcripts forever).
    transcript_retention_days: int = 30
    transcript_retention_sweep_interval_seconds: int = 3600

    # Privacy consent gate (off by default). When enabled, a user's first
    # interaction posts a one-time accept/decline notice and holds the message
    # until they accept, so nothing reaches the third-party provider first.
    privacy_consent_enabled: bool = False
    privacy_consent_title: str = "Before we chat: a quick privacy note"
    privacy_consent_text: str = (
        "I'm powered by a third-party AI provider, so the messages you send me may be "
        "transmitted to and logged by that provider. Tap **Accept** to continue. You'll "
        "only see this once. Tap **Decline** and I'll ignore this message."
    )
    privacy_consent_timeout: float = 300.0

    # Where the full privacy policy is published for this deployment. Empty (the
    # default) drops the link from the /privacy embed rather than pointing users
    # at a URL this deployment does not control; the shipped source text is
    # docs/privacy-policy.md.
    privacy_policy_url: str = ""

    # Thread handoff lets the model move a conversation into a Discord thread
    # it creates and keep responding there without mentions
    # (docs/thread-handoff.md). Needs the Create Public Threads and Send
    # Messages in Threads permissions; no external dependency, so it defaults on.
    thread_handoff_enabled: bool = True

    # Auto-handoff backstop: when the model does NOT call move_to_thread but
    # produces a long reply in a channel that opted in via frontmatter
    # (auto_thread_always, or auto_thread_min_lines / auto_thread_min_chars,
    # in config/channels/<id>.md),
    # the Discord boundary synthesizes the handoff so the reply moves into a new
    # thread instead of cluttering the channel. Requires thread_handoff_enabled.
    # Successful handoffs react to the parent message with a thread emoji on both
    # the automatic and model-requested paths.
    thread_auto_handoff_enabled: bool = False
    # One-time, model-facing suggestion after this many substantive tool calls in
    # an eligible channel turn. The tool remains optional; 0 disables the note.
    thread_handoff_suggest_after_tool_calls: int = 5

    # Instance layout: where operator data lives. Defaults match the in-repo
    # tree; a deployment can point these (plus the data-path settings below) at
    # a directory outside the checkout so the repo stays free of instance data.
    # config_dir holds prompt.md/persona.md/models.yaml and the channels/,
    # servers/, prompts/ fragment trees; skills_dir is the instruction-skill
    # store scanned by skills/loader.py.
    config_dir: str = "config"
    skills_dir: str = "skills/store"
    # Plugins: comma-separated importable module paths, each exposing
    # register(ctx) -> None (see app/plugins.py). Loaded after every core tool
    # registers; a failing plugin is logged and skipped, never a boot abort.
    plugin_modules: str = ""
    # Required lifecycle-aware application modules discovered from installed
    # ``kimi_agent.modules`` entry points. A configured module that cannot load
    # aborts startup rather than silently removing a deployment capability.
    kimi_modules: str = ""
    # Lifecycle ceilings for each configured module. A start() that exceeds its
    # ceiling aborts startup like any other module failure; a close() that
    # exceeds its ceiling is logged and shutdown moves on to the next module.
    module_start_timeout_seconds: int = Field(default=60, ge=1)
    module_close_timeout_seconds: int = Field(default=15, ge=1)
    # Module scheduler jobs run concurrently up to this many, at most one per
    # module at a time so a module's own handlers never overlap.
    module_scheduler_max_concurrent_jobs: int = Field(default=4, ge=1)

    # Storage
    database_path: str = "data/bot.db"
    # SQLCipher encryption-at-rest for the bot DB. Empty = plaintext sqlite3
    # (unchanged behavior); when set, storage/db.py opens the DB through
    # sqlcipher3 and keys it with this passphrase before any access. Changing or
    # losing this key makes an existing encrypted DB unreadable. There is no
    # recovery. Convert an existing plaintext DB with sqlcipher_export (see
    # docs/database.md) rather than just setting this on a plaintext file.
    database_encryption_key: SecretStr = SecretStr("")
    personal_skills_dir: str = "data/personal_skills"
    user_persona_max_chars: int = 2000
    user_persona_request_max_chars: int = 8000
    user_persona_compiler_max_tokens: int = 32_000

    # Observability: append one JSON line per tool call to the event log.
    tool_event_log_enabled: bool = False
    tool_event_log_path: str = "logs/events.jsonl"
    tool_event_log_max_field_bytes: int = 8192
    # Environment-only: "full" is allowed to log secrets, so that call stays with
    # whoever owns the environment rather than the settings overlay.
    tool_event_log_content_mode: EventLogContentMode = "metadata"

    # Secrets
    secrets_file: str = "secrets/secrets.yaml"

    # Script Execution
    script_default_timeout: int = 1200
    script_max_timeout: int = 1200
    script_max_concurrency: int = 2
    script_output_max_chars: int = 200000
    script_output_max_files: int = 10
    script_output_max_file_bytes: int = 25 * 1024 * 1024
    script_output_max_scan_entries: int = 1000
    # Linux Bubblewrap sandbox. These inherited rlimits are per process, while
    # the tmpfs cap bounds the private scratch filesystem for each invocation.
    script_sandbox_memory_max_mb: int = 2048
    script_sandbox_cpu_seconds: int = 300
    script_sandbox_max_file_bytes: int = 100 * 1024 * 1024
    script_sandbox_max_open_files: int = 256
    script_sandbox_max_processes: int = 64
    script_sandbox_tmpfs_max_mb: int = 256

    # Workspaces
    workspace_dir: str = "workspaces"
    workspace_file_ttl: int = 604800
    workspace_max_size_mb: int = 150
    workspace_sweep_interval: int = 300
    workspace_tool_max_file_bytes: int = 50 * 1024 * 1024
    workspace_tool_max_user_bytes: int = 150 * 1024 * 1024
    workspace_tool_max_read_bytes: int = 25 * 1024 * 1024
    workspace_tool_max_pdf_pages: int = 500
    workspace_tool_max_text_chars: int = 65_536
    workspace_tool_max_attachments: int = 5
    workspace_tool_max_import_bytes: int = 25 * 1024 * 1024
    workspace_tool_max_zip_entries: int = 10_000
    workspace_tool_max_extract_total_bytes: int = 150 * 1024 * 1024
    workspace_tool_fetch_timeout_seconds: float = 30.0
    workspace_tool_max_redirects: int = 5
    workspace_tool_default_grep_results: int = 50
    workspace_tool_max_grep_results: int = 200
    workspace_tool_max_grep_context: int = 20
    workspace_tool_max_grep_line_chars: int = 1000
    workspace_tool_max_grep_pattern_chars: int = 256
    workspace_tool_grep_timeout_seconds: float = 5.0
    workspace_tool_glob_max_results: int = 200
    workspace_tool_multi_edit_max_ops: int = 50
    workspace_tool_view_image_max_bytes: int = 5 * 1024 * 1024
    workspace_tool_view_image_max_per_turn: int = 4
    workspace_tool_max_entries: int = 20_000

    @field_validator(
        "react_temperature",
        "exa_search_cost_usd",
        "exa_contents_cost_usd",
        "brave_search_cost_usd",
        "wolfram_alpha_call_cost_usd",
        mode="before",
    )
    @classmethod
    def _blank_optional_float_uses_default(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "code_exec_workspace_quota_poll_seconds",
        "code_exec_workspace_quota_scan_retries",
    )
    @classmethod
    def _require_positive_workspace_quota_monitor_value(
        cls, value: float | int, info: ValidationInfo
    ) -> float | int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be positive, got {value}"
            )
        if info.field_name == "code_exec_workspace_quota_scan_retries" and value > 10:
            raise ValueError(
                f"CODE_EXEC_WORKSPACE_QUOTA_SCAN_RETRIES must not exceed 10, got {value}"
            )
        return value

    @field_validator(
        "turn_max_concurrency",
        "turn_max_concurrency_per_user",
    )
    @classmethod
    def _require_positive_turn_concurrency(
        cls,
        value: int,
        info: ValidationInfo,
    ) -> int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator("thread_handoff_suggest_after_tool_calls")
    @classmethod
    def _require_non_negative_thread_handoff_suggestion_threshold(cls, value: int) -> int:
        if value < 0:
            raise ValueError(
                f"THREAD_HANDOFF_SUGGEST_AFTER_TOOL_CALLS must be 0 or greater, got {value}"
            )
        return value

    @field_validator(
        "attachment_max_bytes",
        "attachment_max_total_bytes",
        "attachment_orphan_ttl_seconds",
        "attachment_orphan_sweep_interval_seconds",
        "attachment_orphan_sweep_max_files",
    )
    @classmethod
    def _require_positive_attachment_lifecycle_value(
        cls,
        value: int,
        info: ValidationInfo,
    ) -> int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator(
        "script_default_timeout",
        "script_max_timeout",
        "script_max_concurrency",
        "script_output_max_chars",
        "script_output_max_files",
        "script_output_max_file_bytes",
        "script_output_max_scan_entries",
        "script_sandbox_memory_max_mb",
        "script_sandbox_cpu_seconds",
        "script_sandbox_max_file_bytes",
        "script_sandbox_max_open_files",
        "script_sandbox_max_processes",
        "script_sandbox_tmpfs_max_mb",
    )
    @classmethod
    def _require_positive_script_cap(cls, value: int, info: ValidationInfo) -> int:
        # Fail fast at startup: a 0 timeout fires immediately, Semaphore(0) is
        # permanently locked, and a 0 output cap silently drops all script output.
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator(
        "workspace_file_ttl",
        "workspace_max_size_mb",
        "workspace_sweep_interval",
    )
    @classmethod
    def _require_positive_workspace_lifecycle_value(
        cls,
        value: int,
        info: ValidationInfo,
    ) -> int:
        # Nonpositive TTL/size values make the next sweep delete every workspace
        # file; a nonpositive interval additionally turns the sweeper into a hot
        # loop. Reject these destructive configurations at startup.
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator("workspace_tool_max_pdf_pages")
    @classmethod
    def _require_positive_pdf_page_cap(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                f"WORKSPACE_TOOL_MAX_PDF_PAGES must be a positive integer, got {value}"
            )
        return value

    @field_validator(
        "user_persona_max_chars",
        "user_persona_request_max_chars",
        "user_persona_compiler_max_tokens",
    )
    @classmethod
    def _require_positive_user_persona_cap(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator(
        "code_exec_max_cpu_seconds",
        "code_exec_max_memory_mb",
        "code_exec_max_tasks",
        "code_exec_max_total_memory_mb",
        "code_exec_cpu_quota_percent",
        "code_exec_tmp_size_mb",
        "code_exec_max_fsize_mb",
        "code_exec_max_open_files",
        "code_exec_max_workspace_files",
        "code_exec_max_output_bytes",
        "code_exec_max_concurrency",
        "code_exec_env_dir_max_mb",
        "code_exec_env_dir_max_files",
        "coding_task_max_concurrency",
        "coding_task_max_queued_per_workspace",
        "coding_task_max_queued_per_user",
        "coding_task_max_iterations",
        "coding_job_max_cpu_seconds",
        "browser_worker_max_lifetime_seconds",
        "browser_profile_ttl_seconds",
        "browser_max_profile_mb",
        "browser_max_screenshot_bytes",
        "browser_max_total_memory_mb",
        "browser_max_tasks",
        "browser_cpu_quota_percent",
        "browser_tmp_size_mb",
        "browser_max_fsize_mb",
        "browser_max_open_files",
    )
    @classmethod
    def _require_positive_workspace_env_dir_cap(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        return value

    @field_validator(
        "code_exec_wall_timeout_seconds",
        "coding_task_max_seconds",
        "coding_provider_call_timeout_seconds",
        "coding_job_max_seconds",
        "coding_worker_stall_seconds",
        "coding_status_min_interval_seconds",
        "coding_stop_cleanup_wait_seconds",
    )
    @classmethod
    def _require_positive_code_exec_timeout(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be positive, got {value}"
            )
        return value

    @field_validator("code_exec_network_weekly_limit")
    @classmethod
    def _require_non_negative_code_exec_network_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"CODE_EXEC_NETWORK_WEEKLY_LIMIT must be >= 0, got {value}")
        return value

    @field_validator("image_gen_backend")
    @classmethod
    def _validate_image_gen_backend(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "openai":
            raise ValueError("IMAGE_GEN_BACKEND must be openai")
        return normalized

    @field_validator("image_gen_auth_mode")
    @classmethod
    def _validate_image_gen_auth_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "oauth", "api_key"}:
            raise ValueError("IMAGE_GEN_AUTH_MODE must be one of: auto, oauth, api_key")
        return normalized

    @field_validator("code_exec_network_mode")
    @classmethod
    def _validate_code_exec_network_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "host", "netns"}:
            raise ValueError("CODE_EXEC_NETWORK_MODE must be one of: none, host, netns")
        return normalized

    @field_validator("browser_network_mode")
    @classmethod
    def _validate_browser_network_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"host", "netns"}:
            raise ValueError("BROWSER_NETWORK_MODE must be one of: host, netns")
        return normalized

    @field_validator(
        "browser_call_timeout_seconds",
        "browser_start_timeout_seconds",
        "browser_idle_ttl_seconds",
    )
    @classmethod
    def _require_positive_browser_timeout(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise ValueError(f"{(info.field_name or 'value').upper()} must be positive")
        return value

    @model_validator(mode="after")
    def _require_complete_netns_code_exec_config(self) -> Settings:
        if self.code_exec_enabled and self.code_exec_network_mode == "netns":
            required = {
                "CODE_EXEC_NETNS_HELPER_BIN": self.code_exec_netns_helper_bin,
                "CODE_EXEC_NETNS_RESOLV_CONF": self.code_exec_netns_resolv_conf,
                "CODE_EXEC_NETWORK_PROBE_BLOCKED_IP": (self.code_exec_network_probe_blocked_ip),
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError("netns code execution requires non-empty " + ", ".join(missing))
        return self

    @model_validator(mode="after")
    def _require_complete_netns_browser_config(self) -> Settings:
        if self.browser_enabled and self.browser_network_mode == "netns":
            required = {
                "BROWSER_NETNS_HELPER_BIN": self.browser_netns_helper_bin,
                "BROWSER_NETNS_RESOLV_CONF": self.browser_netns_resolv_conf,
                "BROWSER_NETWORK_PROBE_BLOCKED_IP": (self.browser_network_probe_blocked_ip),
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError("netns browser requires non-empty " + ", ".join(missing))
        return self

    @field_validator(
        "react_turn_timeout_seconds",
        "provider_stream_stall_timeout_seconds",
        "internet_search_backend_timeout_seconds",
        "internet_search_timeout_seconds",
        "wolfram_alpha_timeout_seconds",
    )
    @classmethod
    def _require_positive_timeout(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be positive, got {value}"
            )
        return value

    @field_validator(
        "internet_search_max_results",
        "internet_search_max_backend_calls_per_turn",
        "internet_search_max_output_chars",
    )
    @classmethod
    def _require_positive_internet_search_cap(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError(
                f"{(info.field_name or 'value').upper()} must be a positive integer, got {value}"
            )
        if info.field_name == "internet_search_max_results" and value > 50:
            raise ValueError("INTERNET_SEARCH_MAX_RESULTS must be 50 or fewer")
        return value

    @field_validator(
        "exa_search_cost_usd",
        "exa_contents_cost_usd",
        "brave_search_cost_usd",
        "wolfram_alpha_call_cost_usd",
    )
    @classmethod
    def _require_non_negative_search_cost(
        cls, value: float | None, info: ValidationInfo
    ) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{(info.field_name or 'value').upper()} must be finite and >= 0")
        return value

    @field_validator("internet_search_safesearch")
    @classmethod
    def _validate_internet_search_safesearch(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"off", "moderate", "strict"}:
            raise ValueError("INTERNET_SEARCH_SAFESEARCH must be off, moderate, or strict")
        return normalized

    @field_validator("tool_event_log_max_field_bytes")
    @classmethod
    def _require_non_negative_field_bytes(cls, value: int, info: ValidationInfo) -> int:
        if value < 0:
            raise ValueError(f"{(info.field_name or 'value').upper()} must be >= 0, got {value}")
        return value

    @field_validator("tool_event_log_content_mode", mode="before")
    @classmethod
    def _validate_tool_event_log_content_mode(cls, value: object) -> EventLogContentMode:
        normalized = str(value).strip().lower()
        match normalized:
            case "metadata" | "redacted" | "full":
                return normalized
            case _:
                raise ValueError(
                    "TOOL_EVENT_LOG_CONTENT_MODE must be one of: metadata, redacted, full"
                )

    @field_validator(
        "memory_auto_retain_idle_minutes",
        "memory_auto_retain_sweep_interval_seconds",
        "memory_auto_retain_max_content_chars",
        "memory_auto_retain_max_flushes_per_sweep",
        "transcript_retention_sweep_interval_seconds",
    )
    @classmethod
    def _require_positive_auto_retain(cls, value: int, info: ValidationInfo) -> int:
        if value < 1:
            raise ValueError(f"{(info.field_name or 'value').upper()} must be >= 1, got {value}")
        return value

    @field_validator("transcript_retention_days")
    @classmethod
    def _require_non_negative_retention_days(cls, value: int, info: ValidationInfo) -> int:
        if value < 0:
            raise ValueError(f"{(info.field_name or 'value').upper()} must be >= 0, got {value}")
        return value

    @property
    def staff_role_id_set(self) -> set[str]:
        return {role_id.strip() for role_id in self.staff_role_ids.split(",") if role_id.strip()}

    @property
    def regular_role_id_set(self) -> set[str]:
        return {role_id.strip() for role_id in self.regular_role_ids.split(",") if role_id.strip()}

    @property
    def staff_ids(self) -> set[str]:
        return {uid.strip() for uid in self.staff_user_ids.split(",") if uid.strip()}

    @property
    def user_app_member_id_set(self) -> set[str]:
        return {uid.strip() for uid in self.user_app_member_ids.split(",") if uid.strip()}

    @property
    def user_app_regular_id_set(self) -> set[str]:
        return {uid.strip() for uid in self.user_app_regular_ids.split(",") if uid.strip()}

    @property
    def user_app_staff_id_set(self) -> set[str]:
        ids = {uid.strip() for uid in self.user_app_staff_ids.split(",") if uid.strip()}
        if self.owner_user_id.strip():
            ids.add(self.owner_user_id.strip())
        return ids

    @field_validator("staff_role_ids", "regular_role_ids")
    @classmethod
    def _validate_role_ids(cls, value: str, info: ValidationInfo) -> str:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if not token.isdigit():
                label = (info.field_name or "role_ids").upper()
                raise ValueError(f"{label} entry {token!r} is not a numeric Discord role ID")
        return value

    @field_validator(
        "staff_user_ids",
        "user_app_member_ids",
        "user_app_regular_ids",
        "user_app_staff_ids",
    )
    @classmethod
    def _validate_user_ids(cls, value: str, info: ValidationInfo) -> str:
        for token in value.split(","):
            token = token.strip()
            if token and not token.isdigit():
                label = (info.field_name or "user_ids").upper()
                raise ValueError(f"{label} entry {token!r} is not a numeric Discord user ID")
        return value

    @field_validator("owner_user_id")
    @classmethod
    def _validate_owner_user_id(cls, value: str) -> str:
        owner_id = value.strip()
        if owner_id and not owner_id.isdigit():
            raise ValueError("OWNER_USER_ID must be one numeric Discord user ID")
        return owner_id

    @model_validator(mode="after")
    def _validate_user_app_chat_access(self) -> Settings:
        if self.user_app_chat_enabled and not (
            self.user_app_member_id_set
            or self.user_app_regular_id_set
            or self.user_app_staff_id_set
        ):
            raise ValueError(
                "USER_APP_CHAT_ENABLED requires OWNER_USER_ID or at least one "
                "USER_APP_MEMBER_IDS/USER_APP_REGULAR_IDS/USER_APP_STAFF_IDS entry"
            )
        # A DM-only deployment would hand out a personal transcript with no way
        # to clear, cancel, or delete it: /privacy, /chat-reset, /memory, and
        # /stop reach user installs only through the /chat surface.
        if self.user_app_dm_enabled and not self.user_app_chat_enabled:
            raise ValueError("USER_APP_DM_ENABLED requires USER_APP_CHAT_ENABLED")
        return self

    @field_validator("allowed_channel_ids")
    @classmethod
    def _validate_allowed_channel_ids(cls, value: str) -> str:
        # Validated at construction so a malformed entry fails fast at startup
        # with a clear message, rather than raising lazily in the allowed_channels
        # property on every inbound message.
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                int(token)
            except ValueError as exc:
                raise ValueError(
                    f"ALLOWED_CHANNEL_IDS entry {token!r} is not a numeric Discord channel ID"
                ) from exc
        return value

    @field_validator("allowed_guild_ids")
    @classmethod
    def _validate_allowed_guild_ids(cls, value: str) -> str:
        # Fail fast at startup rather than lazily in the allowed_guilds property.
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                int(token)
            except ValueError as exc:
                raise ValueError(
                    f"ALLOWED_GUILD_IDS entry {token!r} is not a numeric Discord guild ID"
                ) from exc
        return value

    @field_validator("discord_search_excluded_channels")
    @classmethod
    def _validate_discord_search_excluded_channels(cls, value: str) -> str:
        seen_ids: set[str] = set()
        for raw_channel_id in value.split(","):
            channel_id = raw_channel_id.strip()
            if not channel_id:
                continue
            if not channel_id.isdigit():
                raise ValueError(
                    f"DISCORD_SEARCH_EXCLUDED_CHANNELS channel id {channel_id!r} is not numeric"
                )
            if channel_id in seen_ids:
                raise ValueError(
                    f"DISCORD_SEARCH_EXCLUDED_CHANNELS channel id {channel_id!r} is duplicated"
                )
            seen_ids.add(channel_id)
        return value

    @model_validator(mode="after")
    def _reject_legacy_discord_search_allowlist(self) -> Settings:
        if self.discord_search_channels.strip():
            raise ValueError(
                "DISCORD_SEARCH_CHANNELS has been replaced by "
                "DISCORD_SEARCH_EXCLUDED_CHANNELS; migrate the old allowlist "
                "before starting the bot"
            )
        return self

    @field_validator("moderation_output_exempt_tier")
    @classmethod
    def _validate_moderation_output_exempt_tier(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            return ""
        if normalized not in {"member", "regular", "staff"}:
            raise ValueError(
                "MODERATION_OUTPUT_EXEMPT_TIER must be blank or one of: member, regular, staff"
            )
        return normalized

    @property
    def allowed_channels(self) -> set[int]:
        return {int(cid.strip()) for cid in self.allowed_channel_ids.split(",") if cid.strip()}

    @property
    def allowed_guilds(self) -> set[int]:
        return {int(gid.strip()) for gid in self.allowed_guild_ids.split(",") if gid.strip()}

    @property
    def discord_search_excluded_channel_ids(self) -> frozenset[str]:
        return frozenset(
            channel_id.strip()
            for channel_id in self.discord_search_excluded_channels.split(",")
            if channel_id.strip()
        )

    @property
    def user_memory_recall_types(self) -> list[str]:
        return [kind.strip() for kind in self.memory_recall_types.split(",") if kind.strip()]

    @property
    def plugin_module_list(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.plugin_modules.split(",") if name.strip())

    @property
    def kimi_module_list(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.kimi_modules.split(",") if name.strip())


settings = Settings()
