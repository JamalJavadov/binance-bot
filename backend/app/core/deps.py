from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db


def get_app_state(request: Request):
    return request.app.state


async def get_session(db: AsyncSession = Depends(get_db)) -> AsyncSession:
    return db

