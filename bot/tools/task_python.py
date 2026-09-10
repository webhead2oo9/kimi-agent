"""Versioned scheduled Python inputs and the bounded result-file contract."""

from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tools.downloads import validate_fetch_url

MAX_SCRIPT_BYTES = 100_000
MAX_RESULT_BYTES = 1024 * 1024
INPUT_STATE_KEY = "_task_python_inputs"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class DiscordPythonInput(_Input):
    kind: Literal["discord"]
    channel_id: str = Field(pattern=r"^[1-9][0-9]*$")
    window: Literal["since_success", "rolling"] = "since_success"
    lookback_seconds: int = Field(default=86_400, ge=1, le=31_536_000)


class HttpPythonInput(_Input):
    kind: Literal["https"]
    url: str = Field(min_length=1, max_length=8192)

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        validate_fetch_url(value)
        return value


PythonInput = Annotated[DiscordPythonInput | HttpPythonInput, Field(discriminator="kind")]


class TaskPythonSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=MAX_SCRIPT_BYTES)
    inputs: list[PythonInput] = Field(default_factory=list, max_length=50)

    @field_validator("code")
    @classmethod
    def valid_python(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_SCRIPT_BYTES or not value.strip():
            raise ValueError("Python source must be nonempty and at most 100000 bytes")
        try:
            ast.parse(value, filename="task.py")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError(f"Invalid Python source: {exc}") from exc
        return value

    @model_validator(mode="after")
    def unique_inputs(self) -> Self:
        if len({item.name for item in self.inputs}) != len(self.inputs):
            raise ValueError("Python input names must be unique")
        return self


class PythonOutputFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=1000)
    description: str | None = Field(default=None, max_length=1024)

    @field_validator("path")
    @classmethod
    def output_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or len(path.parts) < 2
            or path.parts[0] != "outputs"
        ):
            raise ValueError("File paths must be relative paths beneath outputs/")
        return value


class TaskPythonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: Literal["completed", "no_change", "invoke_llm", "needs_input"]
    state: dict[str, Any]
    detail: str = Field(max_length=4000)
    content: str = Field(default="", max_length=60_000)
    files: list[PythonOutputFile] = Field(default_factory=list, max_length=10)
    llm_context: str = Field(default="", max_length=64_000)

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        try:
            serialized = json.dumps(self.state, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("state must contain finite JSON data") from exc
        if len(serialized) > 64_000:
            raise ValueError("state must be at most 64000 characters")
        if any(key.startswith("_task_") for key in self.state):
            raise ValueError("State keys beginning with _task_ are reserved for the application")
        if self.outcome != "completed" and (self.content or self.files):
            raise ValueError("Only completed results may include content or files")
        if self.outcome != "invoke_llm" and self.llm_context:
            raise ValueError("Only invoke_llm results may include llm_context")
        if self.outcome == "needs_input" and not self.detail.strip():
            raise ValueError("Specify the question in detail")
        if self.outcome == "completed" and not (self.content.strip() or self.files):
            raise ValueError("completed requires content or files; use no_change to stay silent")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("Output file paths must be unique")
        return self


def user_task_state(state: dict[str, Any]) -> dict[str, Any]:
    """Application cursors and initialization cannot be supplied by executable code."""
    return {key: value for key, value in state.items() if not key.startswith("_task_")}
