from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from providers.base import LLMProvider
from providers.types import ContentPart, ProviderRequest, ProviderResponse
from tools._common import get_string, tool_error
from tools.registry import MessageContext, ToolRegistry
from trust.tiers import TrustTier
from usage.normalization import LLMUsageCall, normalize_usage
from utils.json_payload import extract_json_object


class PersonaPreferenceStore(Protocol):
    async def get_persona(self, user_id: str) -> str: ...

    async def set_persona(self, user_id: str, persona: str) -> bool: ...

    async def clear_persona(self, user_id: str) -> bool: ...


PersonaUsageRecorder = Callable[[ProviderResponse, str], Awaitable[None]]


class PersonaCompiler(Protocol):
    async def compile(
        self,
        *,
        request: str,
        user_name: str,
        on_response: PersonaUsageRecorder | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class PersonaToolConfig:
    max_request_chars: int = 8000


class PersonaCompileError(ValueError):
    pass


_COMPILER_SYSTEM = """\
You compile a Discord user's requested character/persona into a safe persona
block for a 13+ community bot.

Return JSON only, with one of these shapes:
{"ok": true, "persona": "..."}
{"ok": false, "reason": "..."}

Rules:
- Preserve the user's requested fictional character, archetype, tone, voice,
  speaking style, relationship style, and benign quirks as much as possible.
- Named fictional or copyrighted/trademarked characters are allowed; do not
  reject a persona solely because it references a protected character, series,
  franchise, or other IP.
- Be specific to the user's request. Preserve requested versions, eras,
  relationships, catchphrases, attitude, recurring bits, and concrete details
  when they are safe, rather than replacing them with a generic archetype.
- The compiled persona is for replies to this user only.
- Keep it appropriate for a 13+ Discord community.
- Remove sexual, erotic, adult-roleplay, graphic, hateful, harassing, illegal,
  self-harm, weapons-instruction, or drug-instruction content.
- Remove claims that the bot is Discord staff, a real human, has real-world
  credentials, can bypass rules, can change tool permissions, can ignore safety,
  or can act on other users.
- If the request mixes safe and unsafe content, keep the safe persona and drop
  only the unsafe content. Reject only when no safe persona remains.
- Write the persona as concise instructions to the assistant. It may say the bot
  is roleplaying as a fictional character/persona, but it must not claim real
  authority or capabilities.
- Do not include markdown headings, code fences, tool instructions, policy text,
  or meta-explanations in the persona.
"""


class LLMPersonaCompiler:
    """Compiles a persona request through whichever model serves `roles.persona`.

    The provider is injected and its lifecycle belongs to ProviderManager, the
    same arrangement the compactor has.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_chars: int,
        max_output_tokens: int = 32_000,
    ) -> None:
        self._provider = provider
        self._max_chars = max_chars
        self._max_output_tokens = max_output_tokens

    async def compile(
        self,
        *,
        request: str,
        user_name: str,
        on_response: PersonaUsageRecorder | None = None,
    ) -> str:
        prompt = (
            f"Current user display name: {user_name or 'unknown'}\n"
            f"Maximum compiled persona length: {self._max_chars} characters\n\n"
            "User requested persona:\n"
            f"{request}"
        )
        response = await self._provider.run_turn(
            ProviderRequest(
                conversation_id=0,
                system_prompt=_COMPILER_SYSTEM,
                messages=[],
                current_user_parts=[ContentPart.from_text(prompt)],
                tools=[],
                max_tokens=self._max_output_tokens,
                reasoning_enabled=False,
            )
        )
        if on_response is not None:
            await on_response(response, self._provider.model)
        payload = _parse_compiler_json(response.content or "")
        if not bool(payload.get("ok")):
            reason = _normalize_one_line(str(payload.get("reason") or "Persona rejected"))
            raise PersonaCompileError(reason[:300] or "Persona rejected")
        persona = _normalize_persona(str(payload.get("persona") or ""), self._max_chars)
        if not persona:
            raise PersonaCompileError("Persona compiler returned an empty persona")
        return persona


def init_persona_tools(
    registry: ToolRegistry,
    get_store: Callable[[], PersonaPreferenceStore | None],
    compiler: PersonaCompiler | None,
    config: PersonaToolConfig,
) -> bool:
    if compiler is None:
        return False

    async def set_handler(args: dict, ctx: MessageContext) -> str:
        store = get_store()
        if store is None:
            return tool_error("Persona storage is not available yet")
        try:
            request = get_string(
                args,
                "request",
                required=True,
                max_chars=config.max_request_chars,
                message="persona request is required",
            )

            async def record_response(
                response: ProviderResponse,
                provider_model: str,
            ) -> None:
                await _capture_compiler_usage(ctx, response, provider_model)

            persona = await compiler.compile(
                request=request,
                user_name=ctx.user_name,
                on_response=record_response,
            )
            changed = await store.set_persona(ctx.user_id, persona)
        except PersonaCompileError as exc:
            return tool_error(str(exc))
        except ValueError as exc:
            return tool_error(str(exc))
        return json.dumps(
            {
                "saved": True,
                "changed": changed,
                "persona": persona,
                "note": (
                    "This persona will replace the default persona block on future "
                    "normal turns for this user."
                ),
            }
        )

    async def show_handler(_args: dict, ctx: MessageContext) -> str:
        store = get_store()
        if store is None:
            return tool_error("Persona storage is not available yet")
        persona = await store.get_persona(ctx.user_id)
        return json.dumps({"has_persona": bool(persona), "persona": persona})

    async def clear_handler(_args: dict, ctx: MessageContext) -> str:
        store = get_store()
        if store is None:
            return tool_error("Persona storage is not available yet")
        changed = await store.clear_persona(ctx.user_id)
        return json.dumps({"cleared": changed})

    registry.register(
        name="persona_set",
        description=(
            "Create or replace the current user's hidden character/persona override. "
            "Use only when the current user explicitly asks you to change how you "
            "talk to them, roleplay as a character/persona, or set a personal bot "
            "persona. Put the user's raw request in `request`; this tool compiles it "
            "through the configured persona model and stores only the 13+-safe result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The current user's raw requested character/persona.",
                },
            },
            "required": ["request"],
        },
        handler=set_handler,
        min_tier=TrustTier.REGULAR,
        searchable=True,
        category="Personalization",
    )
    registry.register(
        name="persona_show",
        description="Show the current user's stored character/persona override.",
        parameters={"type": "object", "properties": {}},
        handler=show_handler,
        min_tier=TrustTier.REGULAR,
        searchable=True,
        category="Personalization",
    )
    registry.register(
        name="persona_clear",
        description="Clear the current user's stored character/persona override.",
        parameters={"type": "object", "properties": {}},
        handler=clear_handler,
        min_tier=TrustTier.REGULAR,
        searchable=True,
        category="Personalization",
    )
    return True


async def _capture_compiler_usage(
    ctx: MessageContext,
    response: ProviderResponse,
    provider_model: str,
) -> None:
    # Emitted unpriced: the usage recorder prices every call against the live
    # ModelConfig when it flushes, so pricing here would be recomputed anyway.
    call = LLMUsageCall(
        model=response.model or provider_model,
        role="persona_compile",
        usage=normalize_usage(response.usage),
        usage_present=response.has_reported_usage,
        pricing_model=response.pricing_model or provider_model,
    )
    if ctx.record_usage_call is not None:
        await ctx.record_usage_call(call)
    elif ctx.usage_sink is not None:
        ctx.usage_sink.append(call)


def _parse_compiler_json(text: str) -> dict[str, Any]:
    payload = extract_json_object(text)
    if payload is None:
        raise PersonaCompileError("Persona compiler returned invalid JSON")
    return payload


def _normalize_persona(value: str, max_chars: int) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        char for char in normalized if char == "\n" or char == "\t" or ord(char) >= 32
    )
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
    normalized = "\n".join(line for line in lines if line)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if len(normalized) > max_chars:
        raise PersonaCompileError(
            f"Compiled persona is {len(normalized)} characters, over the {max_chars} limit"
        )
    return normalized


def _normalize_one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
