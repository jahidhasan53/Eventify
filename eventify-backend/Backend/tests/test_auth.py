from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_access_token
from app.services import auth_service
from tests.conftest import STRONG_PASSWORD, sql_exec, sql_scalar

API = "/api/v1/auth"


def _register_body(**overrides):
    body = {"email": "New.User@Example.com", "password": STRONG_PASSWORD, "full_name": "New User"}
    body.update(overrides)
    return body


async def _login(client, email, password=STRONG_PASSWORD):
    return await client.post(f"{API}/login", json={"email": email, "password": password})


# ------------------------------------------------------------------ registration
async def test_register_success_hides_secrets_and_normalises_email(client):
    r = await client.post(f"{API}/register", json=_register_body())
    assert r.status_code == 201
    body = r.json()
    assert body["success"] is True
    user = body["data"]
    assert user["email"] == "new.user@example.com"
    assert user["role"] == "attendee"
    assert "password" not in r.text and "password_hash" not in r.text


async def test_password_is_stored_as_argon2id(client):
    await client.post(f"{API}/register", json=_register_body())
    stored = await sql_scalar("SELECT password_hash FROM users WHERE email = :e", e="new.user@example.com")
    assert stored.startswith("$argon2id$")


async def test_register_duplicate_email_is_case_insensitive(client):
    assert (await client.post(f"{API}/register", json=_register_body())).status_code == 201
    r = await client.post(f"{API}/register", json=_register_body(email="NEW.user@EXAMPLE.com"))
    assert r.status_code == 409
    assert r.json()["success"] is False


async def test_register_organizer_allowed_admin_rejected(client):
    ok = await client.post(f"{API}/register", json=_register_body(email="org@example.com", role="organizer"))
    assert ok.status_code == 201 and ok.json()["data"]["role"] == "organizer"

    r = await client.post(f"{API}/register", json=_register_body(email="evil@example.com", role="admin"))
    assert r.status_code == 422
    assert await sql_scalar("SELECT count(*) FROM users WHERE email = 'evil@example.com'") == 0


async def test_register_rejects_unknown_fields(client):
    r = await client.post(f"{API}/register", json=_register_body(status="active", is_superuser=True))
    assert r.status_code == 422


@pytest.mark.parametrize(
    "password",
    ["short1!A", "alllowercase1!", "ALLUPPERCASE1!", "NoDigitsHere!!", "NoSymbols12345"],
)
async def test_register_weak_password_rejected_with_validation_format(client, password):
    r = await client.post(f"{API}/register", json=_register_body(password=password))
    assert r.status_code == 422
    body = r.json()
    assert body["success"] is False and body["message"] == "Validation failed"
    assert isinstance(body["detail"], list) and body["detail"][0]["field"] == "password"
    assert password not in r.text  # the submitted secret is never echoed back


# ------------------------------------------------------------------ login / logout
async def test_login_success_sets_hardened_cookies(client, factory):
    account = await factory.account()
    r = await _login(client, account.email)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["token_type"] == "bearer" and data["expires_in"] == 15 * 60
    assert data["user"]["email"] == account.email and "password_hash" not in r.text

    cookies = {c.split("=", 1)[0]: c.lower() for c in r.headers.get_list("set-cookie")}
    assert "httponly" in cookies["access_token"] and "samesite=lax" in cookies["access_token"]
    assert "httponly" in cookies["refresh_token"] and "path=/api/v1/auth" in cookies["refresh_token"]
    assert "httponly" not in cookies["csrf_token"]  # must be readable by the frontend
    assert data["csrf_token"] == client.cookies.get("csrf_token")


async def test_login_wrong_password_and_unknown_user_look_identical(client, factory):
    account = await factory.account()
    wrong = await _login(client, account.email, "Wr0ng!Password1")
    unknown = await _login(client, "nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["message"] == unknown.json()["message"] == "Invalid email or password"


async def test_login_locks_account_after_repeated_failures(client, factory):
    account = await factory.account()
    for _ in range(auth_service.MAX_FAILED_LOGINS):
        assert (await _login(client, account.email, "Wr0ng!Password1")).status_code == 401
    r = await _login(client, account.email)  # even the right password is refused while locked
    assert r.status_code == 429 and "Retry-After" in r.headers


async def test_suspended_user_cannot_login(client, factory):
    account = await factory.account(status="suspended")
    r = await _login(client, account.email)
    assert r.status_code == 403


async def test_logout_revokes_refresh_token(client, factory):
    account = await factory.account()
    await _login(client, account.email)
    csrf = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    assert (await client.post(f"{API}/logout", headers=csrf)).status_code == 200
    assert await sql_scalar("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL") == 0
    assert client.cookies.get("access_token") is None


# ------------------------------------------------------------------ /me and token validation
async def test_me_requires_authentication(client, factory):
    account = await factory.account()
    assert (await client.get(f"{API}/me")).status_code == 401
    r = await client.get(f"{API}/me", headers=account.headers)
    assert r.status_code == 200 and r.json()["data"]["id"] == str(account.id)


async def test_expired_access_token_is_rejected(client, factory):
    account = await factory.account()
    token, _ = create_access_token(account.id, expires_delta=timedelta(seconds=-5))
    r = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and "expired" in r.json()["message"].lower()


async def test_forged_tokens_are_rejected(client, factory):
    account = await factory.account()
    now = datetime.now(timezone.utc)
    claims = {"sub": str(account.id), "type": "access", "iat": now, "exp": now + timedelta(minutes=5)}
    wrong_key = jwt.encode(claims, "x" * 48, algorithm="HS256")
    no_alg = jwt.encode(claims, None, algorithm="none")
    refresh_typed = jwt.encode(
        {**claims, "type": "refresh"}, get_settings().jwt_secret_key.get_secret_value(), algorithm="HS256"
    )
    for token in (wrong_key, no_alg, refresh_typed, "not.a.jwt"):
        r = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token


async def test_token_of_user_suspended_later_stops_working(client, factory):
    account = await factory.account()
    assert (await client.get(f"{API}/me", headers=account.headers)).status_code == 200
    await sql_exec("UPDATE users SET status = 'suspended' WHERE id = :id", id=account.id)
    assert (await client.get(f"{API}/me", headers=account.headers)).status_code == 403


# ------------------------------------------------------------------ refresh rotation and CSRF
async def test_refresh_rotates_token_and_detects_replay(client, factory):
    account = await factory.account()
    await _login(client, account.email)
    first = client.cookies.get("refresh_token")

    csrf = {"X-CSRF-Token": client.cookies.get("csrf_token")}
    r = await client.post(f"{API}/refresh", headers=csrf)
    assert r.status_code == 200 and r.json()["data"]["access_token"]
    second = client.cookies.get("refresh_token")
    assert second and second != first

    # Replay of the already-rotated token (outside the short race-grace window) = theft signal.
    await sql_exec("UPDATE refresh_tokens SET revoked_at = now() - interval '1 minute' WHERE revoked_at IS NOT NULL")
    current_csrf = client.cookies.get("csrf_token")
    # An explicit Cookie header takes precedence over the client's cookie jar.
    replay = await client.post(
        f"{API}/refresh",
        headers={"X-CSRF-Token": current_csrf, "Cookie": f"refresh_token={first}; csrf_token={current_csrf}"},
    )
    assert replay.status_code == 401
    # ...which revokes the whole token family, including the legitimately rotated one.
    assert await sql_scalar("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL") == 0


async def test_refresh_without_cookie_is_401(client):
    assert (await client.post(f"{API}/refresh")).status_code == 401


async def test_cookie_authenticated_writes_require_csrf_header(client, factory):
    account = await factory.account()
    await _login(client, account.email)
    body = {"current_password": STRONG_PASSWORD, "new_password": "An0ther!Passw0rd"}

    blocked = await client.post(f"{API}/change-password", json=body)  # cookies only, no CSRF header
    assert blocked.status_code == 403 and blocked.json()["message"] == "CSRF validation failed"

    wrong = await client.post(f"{API}/change-password", json=body, headers={"X-CSRF-Token": "forged"})
    assert wrong.status_code == 403

    allowed = await client.post(
        f"{API}/change-password", json=body, headers={"X-CSRF-Token": client.cookies.get("csrf_token")}
    )
    assert allowed.status_code == 200


async def test_cross_origin_cookie_write_is_blocked(client, factory):
    account = await factory.account()
    await _login(client, account.email)
    r = await client.post(
        f"{API}/logout",
        headers={"X-CSRF-Token": client.cookies.get("csrf_token"), "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


# ------------------------------------------------------------------ password management
async def test_change_password_flow(client, factory):
    account = await factory.account()
    path = f"{API}/change-password"
    new = "An0ther!Passw0rd"

    wrong = await client.post(path, json={"current_password": "Wr0ng!Password1", "new_password": new}, headers=account.headers)
    assert wrong.status_code == 400
    same = await client.post(path, json={"current_password": STRONG_PASSWORD, "new_password": STRONG_PASSWORD}, headers=account.headers)
    assert same.status_code == 400
    ok = await client.post(path, json={"current_password": STRONG_PASSWORD, "new_password": new}, headers=account.headers)
    assert ok.status_code == 200

    client.cookies.clear()
    assert (await _login(client, account.email, STRONG_PASSWORD)).status_code == 401
    assert (await _login(client, account.email, new)).status_code == 200


async def test_forgot_and_reset_password(client, factory, monkeypatch):
    account = await factory.account()
    sent: list[str] = []

    async def fake_send(to, full_name, link, minutes):
        sent.append(link)

    monkeypatch.setattr(auth_service, "send_password_reset_email", fake_send)

    unknown = await client.post(f"{API}/forgot-password", json={"email": "ghost@example.com"})
    known = await client.post(f"{API}/forgot-password", json={"email": account.email})
    assert unknown.status_code == known.status_code == 200
    assert unknown.json()["message"] == known.json()["message"]  # no account enumeration
    assert len(sent) == 1
    token = sent[0].split("token=", 1)[1]

    new = "Brand-N3w!Password"
    assert (await client.post(f"{API}/reset-password", json={"token": token, "new_password": new})).status_code == 200
    assert (await _login(client, account.email, new)).status_code == 200
    # a reset token works exactly once
    again = await client.post(f"{API}/reset-password", json={"token": token, "new_password": "Y3t-Another!Pass"})
    assert again.status_code == 400
    bogus = await client.post(f"{API}/reset-password", json={"token": "x" * 40, "new_password": new})
    assert bogus.status_code == 400


# ------------------------------------------------------------------ rate limiting
async def test_login_rate_limit_returns_429(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 3)

    statuses = [(await _login(client, "nobody@example.com")).status_code for _ in range(5)]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429]
    blocked = await _login(client, "nobody@example.com")
    assert blocked.headers["Retry-After"].isdigit()
    assert blocked.json() == {"success": False, "message": "Too many requests. Please try again later.", "detail": None}


async def test_sensitive_endpoints_are_rate_limited(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_sensitive_per_minute", 2)
    codes = [
        (await client.post(f"{API}/forgot-password", json={"email": "a@example.com"})).status_code for _ in range(4)
    ]
    assert codes == [200, 200, 429, 429]
