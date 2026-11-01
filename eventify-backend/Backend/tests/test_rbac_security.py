"""Role permissions, ownership (IDOR), privilege escalation, and transport-level protections."""

import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.permissions import Permission, has_permission
from tests.conftest import event_payload, sql_exec, sql_scalar

V1 = "/api/v1"


# ------------------------------------------------------------------ permission matrix
def test_permission_matrix_matches_the_spec():
    assert has_permission("attendee", Permission.REGISTRATION_CREATE)
    assert not has_permission("attendee", Permission.EVENT_CREATE)
    assert not has_permission("attendee", Permission.TICKET_VERIFY)
    assert has_permission("organizer", Permission.EVENT_CREATE)
    assert has_permission("organizer", Permission.TICKET_VERIFY)
    assert not has_permission("organizer", Permission.ADMIN_USERS_MANAGE)
    assert has_permission("admin", Permission.ADMIN_AUDIT_VIEW)
    assert not has_permission("admin", Permission.EVENT_CREATE)
    assert not has_permission("nonsense-role", Permission.PROFILE_MANAGE_OWN)


# ------------------------------------------------------------------ authentication required
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/auth/me"),
        ("GET", "/users/me"),
        ("POST", "/events/"),
        ("POST", "/registrations/"),
        ("GET", "/registrations/my"),
        ("GET", "/tickets/my"),
        ("POST", "/tickets/verify"),
        ("POST", "/attendance/check-in"),
        ("GET", "/attendance/my"),
        ("GET", "/organizer/dashboard"),
        ("GET", "/admin/dashboard"),
        ("GET", "/admin/users"),
        ("GET", "/admin/audit-logs"),
        ("GET", "/notifications/"),
        ("POST", "/reviews/"),
    ],
)
async def test_protected_endpoints_reject_anonymous_requests(client, method, path):
    r = await client.request(method, f"{V1}{path}", json={} if method == "POST" else None)
    assert r.status_code == 401
    assert r.json()["success"] is False


# ------------------------------------------------------------------ role gates
ADMIN_ENDPOINTS = [
    ("GET", "/admin/dashboard"),
    ("GET", "/admin/users"),
    ("GET", "/admin/organizers"),
    ("GET", "/admin/events"),
    ("GET", "/admin/audit-logs"),
    ("GET", "/admin/reports"),
]


@pytest.mark.parametrize("role", ["attendee", "organizer"])
@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
async def test_admin_endpoints_are_admin_only(client, factory, role, method, path):
    account = await factory.account(role)
    r = await client.request(method, f"{V1}{path}", headers=account.headers)
    assert r.status_code == 403


async def test_admin_mutations_are_admin_only(client, factory):
    organizer = await factory.account("organizer")
    victim = await factory.account()
    event = await factory.event(organizer, status="pending")
    for path, body in [
        (f"/admin/events/{event.id}/approve", None),
        (f"/admin/events/{event.id}/reject", {"reason": "nope nope"}),
        (f"/admin/events/{event.id}/publish", None),
        (f"/admin/events/{event.id}/unpublish", None),
        (f"/admin/users/{victim.id}/status", {"status": "suspended"}),
    ]:
        r = await client.patch(f"{V1}{path}", json=body, headers=organizer.headers)
        assert r.status_code == 403, path
    assert await sql_scalar("SELECT status FROM events WHERE id = :i", i=event.id) == "pending"
    assert await sql_scalar("SELECT status FROM users WHERE id = :i", i=victim.id) == "active"


async def test_only_organizers_create_events(client, factory):
    attendee = await factory.account("attendee")
    admin = await factory.account("admin")
    organizer = await factory.account("organizer")
    assert (await client.post(f"{V1}/events/", json=event_payload(), headers=attendee.headers)).status_code == 403
    assert (await client.post(f"{V1}/events/", json=event_payload(), headers=admin.headers)).status_code == 403
    assert (await client.post(f"{V1}/events/", json=event_payload(), headers=organizer.headers)).status_code == 201


async def test_attendee_cannot_use_organizer_tools(client, factory):
    attendee = await factory.account("attendee")
    for method, path in [("GET", "/organizer/dashboard"), ("GET", "/organizer/profile"), ("GET", "/organizer/events")]:
        assert (await client.request(method, f"{V1}{path}", headers=attendee.headers)).status_code == 403
    for path in ("/tickets/verify", "/attendance/check-in"):
        r = await client.post(f"{V1}{path}", json={"token": "x" * 40}, headers=attendee.headers)
        assert r.status_code == 403


# ------------------------------------------------------------------ ownership (IDOR)
async def test_organizer_cannot_touch_another_organizers_event(client, factory):
    owner = await factory.account("organizer")
    other = await factory.account("organizer")
    event = await factory.event(owner, status="published")
    eid = event.id

    assert (await client.put(f"{V1}/events/{eid}", json={"title": "Hijacked"}, headers=other.headers)).status_code == 403
    assert (await client.delete(f"{V1}/events/{eid}", headers=other.headers)).status_code == 403
    assert (await client.post(f"{V1}/events/{eid}/cancel", headers=other.headers)).status_code == 403
    assert (await client.get(f"{V1}/organizer/events/{eid}/attendees", headers=other.headers)).status_code == 403
    assert (await client.get(f"{V1}/organizer/events/{eid}/statistics", headers=other.headers)).status_code == 403
    assert (await client.get(f"{V1}/attendance/event/{eid}", headers=other.headers)).status_code == 403
    assert await sql_scalar("SELECT title FROM events WHERE id = :i", i=eid) == "Python Workshop"
    # the owner is fine
    assert (await client.get(f"{V1}/organizer/events/{eid}/statistics", headers=owner.headers)).status_code == 200


async def test_organizer_listing_only_contains_own_events(client, factory):
    a = await factory.account("organizer")
    b = await factory.account("organizer")
    await factory.event(a, status="draft", title="Mine A")
    await factory.event(b, status="draft", title="Mine B")
    r = await client.get(f"{V1}/organizer/events", headers=a.headers)
    titles = [e["title"] for e in r.json()["data"]["items"]]
    assert titles == ["Mine A"]
    dash = (await client.get(f"{V1}/organizer/dashboard", headers=a.headers)).json()["data"]
    assert dash["total_events"] == 1


async def test_user_ids_in_requests_are_never_trusted(client, factory):
    attendee = await factory.account()
    victim = await factory.account()
    organizer = await factory.account("organizer")
    event = await factory.event(organizer)
    r = await client.post(
        f"{V1}/registrations/",
        json={"event_id": str(event.id), "user_id": str(victim.id)},
        headers=attendee.headers,
    )
    assert r.status_code == 422  # unknown field is rejected, not silently honoured
    ok = await client.post(f"{V1}/registrations/", json={"event_id": str(event.id)}, headers=attendee.headers)
    assert ok.status_code == 201
    assert await sql_scalar("SELECT user_id FROM registrations") == attendee.id


# ------------------------------------------------------------------ privilege escalation
async def test_profile_update_cannot_change_role_or_status(client, factory):
    attendee = await factory.account()
    for body in ({"role": "admin"}, {"status": "active"}, {"email": "x@example.com"}, {"password_hash": "x"}):
        assert (await client.patch(f"{V1}/users/me", json=body, headers=attendee.headers)).status_code == 422
    r = await client.patch(f"{V1}/users/me", json={"full_name": "Renamed Person"}, headers=attendee.headers)
    assert r.status_code == 200 and r.json()["data"]["full_name"] == "Renamed Person"
    assert await sql_scalar("SELECT role FROM users WHERE id = :i", i=attendee.id) == "attendee"


async def test_event_update_cannot_change_owner_status_or_counts(client, factory):
    organizer = await factory.account("organizer")
    event = await factory.event(organizer, status="draft")
    for body in (
        {"organizer_id": str(uuid.uuid4())},
        {"status": "published"},
        {"registered_count": 5},
        {"is_featured": True},
    ):
        r = await client.put(f"{V1}/events/{event.id}", json=body, headers=organizer.headers)
        assert r.status_code == 422, body
    assert await sql_scalar("SELECT status FROM events WHERE id = :i", i=event.id) == "draft"


async def test_admin_status_changes_are_restricted(client, factory):
    admin = await factory.account("admin")
    other_admin = await factory.account("admin")
    r1 = await client.patch(f"{V1}/admin/users/{admin.id}/status", json={"status": "suspended"}, headers=admin.headers)
    r2 = await client.patch(f"{V1}/admin/users/{other_admin.id}/status", json={"status": "suspended"}, headers=admin.headers)
    assert r1.status_code == r2.status_code == 403


# ------------------------------------------------------------------ transport protections
async def test_security_headers_present(client):
    r = await client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_cors_uses_an_allow_list(client):
    allowed = await client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    denied = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers

    preflight = await client.options(
        f"{V1}/events/",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_oversized_request_bodies_are_rejected(client):
    big = "x" * (1_048_576 + 10)
    r = await client.post(f"{V1}/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["success"] is False


async def test_error_responses_use_the_standard_envelope_and_leak_nothing(client):
    for r in (await client.get(f"{V1}/nope"), await client.get(f"{V1}/auth/me"), await client.get(f"{V1}/events/not-a-uuid")):
        body = r.json()
        assert set(body) == {"success", "message", "detail"} and body["success"] is False
        assert "Traceback" not in r.text and "sqlalchemy" not in r.text.lower()
    assert (await client.get(f"{V1}/events/not-a-uuid")).status_code == 422
    assert (await client.delete(f"{V1}/auth/me")).status_code == 405


async def test_search_input_is_parameterised_and_wildcards_are_literal(client, factory):
    organizer = await factory.account("organizer")
    await factory.event(organizer, title="Plain Title")
    injection = await client.get(f"{V1}/events/", params={"q": "'; DROP TABLE events; --"})
    assert injection.status_code == 200 and injection.json()["data"]["total"] == 0
    assert await sql_scalar("SELECT count(*) FROM events") == 1  # table still there

    wildcard = await client.get(f"{V1}/events/", params={"q": "%"})
    assert wildcard.json()["data"]["total"] == 0  # '%' is matched literally, not as "everything"
    underscore = await client.get(f"{V1}/events/", params={"q": "P_ain"})
    assert underscore.json()["data"]["total"] == 0


# ------------------------------------------------------------------ configuration guards
_BASE = dict(
    database_url="postgresql+asyncpg://u:p@localhost/db",
    jwt_secret_key="s" * 48,
    frontend_origin="https://app.example.com",
)


def test_settings_reject_unsafe_configuration():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**_BASE, "jwt_secret_key": "CHANGE_ME"}, environment="production", email_backend="disabled")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**_BASE, "jwt_secret_key": "short"}, environment="development")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**_BASE, "frontend_origin": "*"}, environment="development")
    with pytest.raises(ValidationError):  # production must use https origins
        Settings(_env_file=None, **{**_BASE, "frontend_origin": "http://app.example.com"}, environment="production", email_backend="disabled")
    with pytest.raises(ValidationError):  # plain-http database driver is not accepted
        Settings(_env_file=None, **{**_BASE, "database_url": "postgresql://u:p@localhost/db"}, environment="development")


def test_production_settings_are_hardened():
    s = Settings(_env_file=None, **_BASE, environment="production", email_backend="disabled")
    assert s.use_secure_cookies is True
    assert s.docs_enabled is False
    assert "*" not in s.cors_origins
