from datetime import datetime
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import User, UserStatus


class UserAccount(Protocol):
    id: str
    email: str
    normalized_email: str
    password_hash: str
    display_name: str
    status: str
    email_verified_at: datetime | None
    token_version: int


class DuplicateUserError(ValueError):
    pass


def normalize_email(email: str) -> str:
    return email.strip().casefold()


class UserService:
    async def by_email(self, session: AsyncSession, email: str) -> UserAccount | None:
        return cast(
            User | None,
            await session.scalar(
                select(User).where(User.normalized_email == normalize_email(email))
            ),
        )

    async def by_id(self, session: AsyncSession, user_id: str) -> UserAccount | None:
        return cast(User | None, await session.get(User, user_id))

    async def create(
        self,
        session: AsyncSession,
        *,
        email: str,
        password_hash: str,
        display_name: str,
    ) -> UserAccount:
        user = User(
            email=email.strip(),
            normalized_email=normalize_email(email),
            password_hash=password_hash,
            display_name=display_name.strip(),
        )
        try:
            async with session.begin_nested():
                session.add(user)
                await session.flush()
        except IntegrityError as error:
            raise DuplicateUserError("Unable to register account") from error
        return user

    @staticmethod
    def activate(user: UserAccount, verified_at: datetime) -> None:
        user.email_verified_at = verified_at
        user.status = UserStatus.ACTIVE

    @staticmethod
    def change_password(user: UserAccount, password_hash: str) -> None:
        user.password_hash = password_hash
        user.token_version += 1

    @staticmethod
    def disable(user: UserAccount) -> None:
        user.status = UserStatus.DISABLED
        user.token_version += 1

    @staticmethod
    def active(user: UserAccount) -> bool:
        return user.status == UserStatus.ACTIVE
