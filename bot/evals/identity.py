from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict, dataclass

EVAL_USER_NAME = "webhead"

# Keep synthetic ids numeric for seams that apply Discord-id validation while
# reserving an 18-digit range that is clearly detached from any configured user.
_SYNTHETIC_ID_BASE = 100_000_000_000_000_000
_SYNTHETIC_ID_SPAN = 900_000_000_000_000_000


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

    def as_dict(self) -> dict[str, str | int]:
        return {**asdict(self), "digest": self.digest, "user_id": self.user_id}
