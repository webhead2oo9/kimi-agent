from __future__ import annotations

import logging
import time

from storage.db import Database

log = logging.getLogger(__name__)


class PreferenceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_memory_enabled(self, user_id: str) -> bool:
        conn = self._db.conn
        async with conn.execute(
            "SELECT memory_enabled FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return True
            return bool(row["memory_enabled"])

    async def set_memory_enabled(self, user_id: str, enabled: bool) -> bool:
        """Toggle memory. Returns True if the value actually changed."""
        current = await self.is_memory_enabled(user_id)
        if current == enabled:
            return False

        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO user_preferences (user_id, memory_enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET memory_enabled = ?, updated_at = ?",
                (user_id, int(enabled), now, now, int(enabled), now),
            )
        return True

    async def has_consented(self, user_id: str) -> bool:
        """Whether the user accepted the privacy notice.

        Defaults to False when no row exists: consent must be explicit, unlike
        memory_enabled which defaults on.
        """
        conn = self._db.conn
        async with conn.execute(
            "SELECT privacy_consent FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return False
            return bool(row["privacy_consent"])

    async def set_consent(self, user_id: str, granted: bool) -> bool:
        """Record privacy consent. Returns True if the value actually changed.

        The UPSERT only touches the consent columns on conflict, so it coexists
        with set_memory_enabled on the same row without clobbering memory_enabled.
        """
        current = await self.has_consented(user_id)
        if current == granted:
            return False

        now = time.time()
        consented_at = now if granted else None
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO user_preferences "
                "(user_id, privacy_consent, privacy_consent_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "privacy_consent = ?, privacy_consent_at = ?, updated_at = ?",
                (user_id, int(granted), consented_at, now, now, int(granted), consented_at, now),
            )
        return True

    async def get_persona(self, user_id: str) -> str:
        conn = self._db.conn
        async with conn.execute(
            "SELECT persona_prompt FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return ""
        return str(row["persona_prompt"] or "").strip()

    async def set_persona(self, user_id: str, persona: str) -> bool:
        """Store the compiled persona prompt. Returns True if it changed."""
        normalized = persona.strip()
        current = await self.get_persona(user_id)
        if current == normalized:
            return False

        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO user_preferences "
                "(user_id, persona_prompt, persona_updated_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "persona_prompt = ?, persona_updated_at = ?, updated_at = ?",
                (user_id, normalized, now, now, now, normalized, now, now),
            )
        return True

    async def clear_persona(self, user_id: str) -> bool:
        """Clear the stored persona. Returns True if there was one."""
        current = await self.get_persona(user_id)
        if not current:
            return False

        now = time.time()
        async with self._db.write_transaction() as conn:
            await conn.execute(
                "UPDATE user_preferences "
                "SET persona_prompt = NULL, persona_updated_at = NULL, updated_at = ? "
                "WHERE user_id = ?",
                (now, user_id),
            )
        return True
