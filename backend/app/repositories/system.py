"""System settings repository (M02, M21)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.system import SystemSetting
from app.repositories.base import BaseRepository


class SystemSettingRepository(BaseRepository[SystemSetting]):
    model = SystemSetting

    async def get_by_key(self, key: str) -> SystemSetting | None:
        return await self.session.get(SystemSetting, key)

    async def get_value(self, key: str, default: Any = None) -> Any:
        setting = await self.get_by_key(key)
        return default if setting is None else setting.value

    async def list_by_category(self, category: str | None = None) -> Sequence[SystemSetting]:
        stmt = select(SystemSetting)
        if category:
            stmt = stmt.where(SystemSetting.category == category)
        stmt = stmt.order_by(SystemSetting.category, SystemSetting.key)
        return (await self.session.execute(stmt)).scalars().all()

    async def upsert(
        self,
        key: str,
        value: Any,
        *,
        description: str | None = None,
        category: str = "general",
        is_system: bool = False,
    ) -> None:
        """Insert or update in one statement.

        A native ``ON CONFLICT`` rather than read-then-write: the seeder is
        re-runnable and two concurrent runs must not race into a duplicate-key
        error. ``description``/``category`` are only overwritten when supplied, so
        re-seeding never clobbers an operator's edits.
        """
        stmt = insert(SystemSetting).values(
            key=key,
            value=value,
            description=description,
            category=category,
            is_system=is_system,
        )
        update_set: dict[str, Any] = {"value": stmt.excluded.value}
        if description is not None:
            update_set["description"] = stmt.excluded.description
        if category != "general":
            update_set["category"] = stmt.excluded.category

        await self.session.execute(
            stmt.on_conflict_do_update(index_elements=[SystemSetting.key], set_=update_set)
        )
