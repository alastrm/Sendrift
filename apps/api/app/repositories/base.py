"""Base repository pattern for database transactions."""
from collections.abc import Sequence
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """Generic repository providing core database interactions."""

    def __init__(self, model: type[ModelType], session: AsyncSession) -> None:
        self.model = model
        self.session = session

    async def get_by_id(self, id_val: str) -> ModelType | None:
        result = await self.session.execute(
            select(self.model).filter(self.model.id == id_val)  # type: ignore[attr-defined]
        )
        return result.scalars().first()

    async def list_all(self) -> Sequence[ModelType]:
        result = await self.session.execute(select(self.model))
        return result.scalars().all()
