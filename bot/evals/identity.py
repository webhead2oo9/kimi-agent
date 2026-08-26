from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass

EVAL_USER_NAME = "webhead"

# Keep synthetic ids numeric for seams that apply Discord-id validation, but place
# every value above the unsigned 64-bit range used by Discord snowflakes so an eval
# identity can never equal a real Discord user id.
_SYNTHETIC_ID_BASE = 1 << 64
_SYNTHETIC_ID_SPAN = 10**20 - _SYNTHETIC_ID_BASE


def new_eval_run_nonce() -> str:
    """Return the per-run salt that prevents state reuse across eval invocations."""

    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class EvalIdentity:
    """Synthetic caller identity for one model arm and scenario repetition."""

    run_nonce: str
    arm: str
    scenario_id: str
    repetition: int

    def __post_init__(self) -> None:
        if not self.run_nonce:
            raise ValueError("eval identity run_nonce must not be empty")
        if not self.arm:
            raise ValueError("eval identity arm must not be empty")
        if not self.scenario_id:
            raise ValueError("eval identity scenario_id must not be empty")
        if self.repetition < 0:
            raise ValueError("eval identity repetition must be >= 0")

    @property
    def digest(self) -> str:
        payload = "\0".join(
            (self.run_nonce, self.arm, self.scenario_id, str(self.repetition))
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def user_id(self) -> str:
        value = _SYNTHETIC_ID_BASE + int(self.digest[:16], 16) % _SYNTHETIC_ID_SPAN
        return str(value)

    @property
    def context_key(self) -> str:
        """Conversation/output namespace unique to this arm and repetition."""

        return f"eval:{self.scenario_id}:{self.digest}"

    def as_dict(self) -> dict[str, str | int]:
        return {
            **asdict(self),
            "digest": self.digest,
            "user_id": self.user_id,
            "context_key": self.context_key,
        }
