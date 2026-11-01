"""Test configuration.

SAFETY: the suite only ever talks to TEST_DATABASE_URL, and refuses to run unless that database name
ends in "_test". Tables are TRUNCATEd between tests, so it must never point at real data.

The schema is created by running the real Alembic migrations (`alembic upgrade head`), so the
migration files are exercised by every run.
"""

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(BACKEND_DIR / ".env").get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("TEST_DATABASE_URL is not set (see .env.example). Refusing to run the tests.", returncode=2)
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL must point to a database whose name ends in '_test'.", returncode=2)
    return url


TEST_DB_URL = _test_database_url()

# Configure the app BEFORE any `app.*` import. Environment variables beat the .env file.
for _name in ("COOKIE_SECURE", "COOKIE_DOMAIN", "COOKIE_SAMESITE", "TICKET_HMAC_KEY", "FRONTEND_RESET_URL"):
    os.environ.pop(_name, None)
os.environ.update(
    ENVIRONMENT="testing",
    DATABASE_URL=TEST_DB_URL,
    TEST_DATABASE_URL=TEST_DB_URL,
    JWT_SECRET_KEY="pytest-only-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz",
    FRONTEND_ORIGIN="http://localhost:5173",
    EMAIL_BACKEND="disabled",
    RATE_LIMIT_ENABLED="false",  # individual tests switch it on
    ENABLE_BACKGROUND_JOBS="false",
    MAX_REQUEST_BODY_BYTES="1048576",
)

import httpx  # noqa: E402
from argon2 import PasswordHasher, Type  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core import security  # noqa: E402

# Test speed only: cheap Argon2id parameters (still Argon2id; production keeps the library defaults).
security._hasher = PasswordHasher(type=Type.ID, time_cost=1, memory_cost=8, parallelism=1)

from app.core.rate_limit import limiter  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.core.utils import utcnow  # noqa: E402
from app.database.connection import SessionLocal, engine  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.user import User  # noqa: E402

STRONG_PASSWORD = "Str0ng!Passw0rd"


def run_alembic(*args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_DB_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND_DIR, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    run_alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_state(migrated_database):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE audit_logs, notifications, reviews, tickets, registrations, event_reports, "
                "events, password_reset_tokens, refresh_tokens, users RESTART IDENTITY CASCADE"
            )
        )
    limiter.clear()
    yield


@pytest_asyncio.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@dataclass
class Account:
    id: uuid.UUID
    email: str
    password: str
    role: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class Factory:
    """Creates rows directly in the database (fast, and independent of the endpoints under test)."""

    def __init__(self) -> None:
        self._counter = 0

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    async def account(self, role: str = "attendee", *, status: str = "active", email: str | None = None) -> Account:
        n = self._next()
        email = email or f"{role}{n}@example.com"
        async with SessionLocal() as db:
            user = User(
                email=email,
                full_name=f"{role.title()} {n}",
                password_hash=hash_password(STRONG_PASSWORD),
                role=role,
                status=status,
                organization_name=f"Org {n}" if role == "organizer" else None,
            )
            db.add(user)
            await db.commit()
            token, _ = create_access_token(user.id)
            return Account(user.id, email, STRONG_PASSWORD, role, token)

    async def event(self, organizer: Account, *, status: str = "published", **overrides) -> Event:
        start = overrides.pop("start", utcnow() + timedelta(days=10))
        values = dict(
            organizer_id=organizer.id,
            title="Python Workshop",
            description="A hands-on Python workshop for beginners.",
            category="Technology",
            start_datetime=start,
            end_datetime=start + timedelta(hours=2),
            registration_deadline=start - timedelta(days=1),
            venue="Main Hall",
            location="Dhaka",
            event_type="offline",
            capacity=10,
            registered_count=0,
            ticket_type="free",
            ticket_price=Decimal("0.00"),
            status=status,
        )
        values.update(overrides)
        async with SessionLocal() as db:
            event = Event(**values)
            db.add(event)
            await db.commit()
            return event


@pytest.fixture
def factory() -> Factory:
    return Factory()


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def event_payload(**overrides) -> dict:
    """A valid POST /events/ body (future dates, offline, free)."""
    start = utcnow() + timedelta(days=10)
    payload = {
        "title": "Intro to FastAPI",
        "description": "Build a secure REST API with FastAPI and PostgreSQL.",
        "category": "Technology",
        "start_datetime": iso(start),
        "end_datetime": iso(start + timedelta(hours=3)),
        "registration_deadline": iso(start - timedelta(days=1)),
        "venue": "Room 101",
        "location": "Dhaka",
        "event_type": "offline",
        "capacity": 25,
        "ticket_type": "free",
        "ticket_price": 0,
    }
    payload.update(overrides)
    return payload


async def sql_scalar(statement: str, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(statement), params)).scalar()


async def sql_exec(statement: str, **params) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def register(client: httpx.AsyncClient, account: Account, event_id) -> httpx.Response:
    return await client.post("/api/v1/registrations/", json={"event_id": str(event_id)}, headers=account.headers)
