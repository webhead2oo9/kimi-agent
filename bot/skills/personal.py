from __future__ import annotations

import re
from pathlib import Path

from skills import loader, manager
from skills.loader import Skill

_USER_ID_RE = re.compile(r"^[0-9]+$")
_INVALID_USER_ID = "User id must be a Discord snowflake"


class PersonalSkillManager:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def user_path(self, user_id: str) -> Path:
        if not _USER_ID_RE.match(str(user_id or "")):
            raise ValueError(_INVALID_USER_ID)
        return self.base_dir / str(user_id)

    def ensure_user_root(self, user_id: str) -> Path:
        root = self.user_path(user_id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def create(
        self,
        user_id: str,
        *,
        name: str,
        description: str,
        content: str,
        tags: list[str] | None = None,
    ) -> str | None:
        try:
            root = self.ensure_user_root(user_id)
        except ValueError as exc:
            return str(exc)
        return manager.create_skill(
            name=name,
            description=description,
            content=content,
            tags=tags,
            skills_dir=root,
        )

    def edit(
        self,
        user_id: str,
        *,
        name: str,
        content: str,
        description: str | None = None,
    ) -> str | None:
        try:
            root = self.user_path(user_id)
        except ValueError as exc:
            return str(exc)
        return manager.edit_skill(
            name=name,
            content=content,
            description=description,
            skills_dir=root,
        )

    def delete(self, user_id: str, name: str) -> str | None:
        try:
            root = self.user_path(user_id)
        except ValueError as exc:
            return str(exc)
        return manager.delete_skill(name, skills_dir=root)

    def get(self, user_id: str, name: str) -> Skill | None:
        root = self.user_path(user_id)
        return loader.load_skill(name, skills_dir=root)

    def index(self, user_id: str) -> str:
        try:
            root = self.user_path(user_id)
        except ValueError:
            return ""
        return loader.build_skills_index(loader.scan_skills(root))
