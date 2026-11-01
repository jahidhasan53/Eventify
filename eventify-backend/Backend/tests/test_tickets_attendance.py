import asyncio
from decimal import Decimal
import uuid

from tests.conftest import register, sql_exec, sql_scalar

V1 = "/api/v1"


async def _booked(client, factory, **event_overrides):
    """organizer + event + attendee with a confirmed registration. Returns (org, event, attendee, ticket_id, token)."""
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, **event_overrides)
    reg = (await register(client, attendee, event.id)).json()["data"]
    ticket_id = reg["ticket"]["id"]
    detail = await client.get(f"{V1}/tickets/{ticket_id}", headers=attendee.headers)
    return org, event, attendee, ticket_id, detail.json()["data"]["qr_token"]


async def _check_in(client, account, token):
    return await client.post(f"{V1}/attendance/check-in", json={"token": token}, headers=account.headers)


async def _verify(client, account, token):
    return await client.post(f"{V1}/tickets/verify", json={"token": token}, headers=account.headers)


# ------------------------------------------------------------------ ticket access and QR
async def test_ticket_access_and_opaque_token(client, factory):
    org, event, attendee, ticket_id, token = await _booked(client, factory)

    mine = await client.get(f"{V1}/tickets/my", headers=attendee.headers)
    assert mine.json()["data"]["total"] == 1 and "qr_token" not in mine.text  # tokens only on the detail endpoint

    # '<32 hex chars>.<signature>': opaque, random-looking, and free of personal data
    prefix, _, signature = token.partition(".")
    assert len(prefix) == 32 and prefix == uuid.UUID(ticket_id).hex and len(signature) >= 40
    for private in (attendee.email, str(attendee.id), "Attendee", str(event.id)):
        assert private not in token

    # Not even other users, organizers or admins can fetch someone else's ticket (or its token).
    stranger = await factory.account()
    admin = await factory.account("admin")
    for who in (stranger, org, admin):
        assert (await client.get(f"{V1}/tickets/{ticket_id}", headers=who.headers)).status_code == 404
        assert (await client.get(f"{V1}/tickets/{ticket_id}/qr", headers=who.headers)).status_code == 404
    assert (await client.get(f"{V1}/tickets/{ticket_id}")).status_code == 401


async def test_qr_endpoint_returns_a_png(client, factory):
    _, _, attendee, ticket_id, _ = await _booked(client, factory)
    r = await client.get(f"{V1}/tickets/{ticket_id}/qr", headers=attendee.headers)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "no-store" in r.headers["cache-control"]


async def test_ticket_tokens_are_unique_per_ticket(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org, capacity=5)
    tokens = set()
    for _ in range(3):
        attendee = await factory.account()
        reg = (await register(client, attendee, event.id)).json()["data"]
        detail = await client.get(f"{V1}/tickets/{reg['ticket']['id']}", headers=attendee.headers)
        tokens.add(detail.json()["data"]["qr_token"])
    assert len(tokens) == 3
    assert await sql_scalar("SELECT count(DISTINCT token_nonce) FROM tickets") == 3


# ------------------------------------------------------------------ verification
async def test_only_the_events_organizer_can_verify(client, factory):
    org, event, attendee, ticket_id, token = await _booked(client, factory)
    other_org = await factory.account("organizer")
    admin = await factory.account("admin")

    assert (await client.post(f"{V1}/tickets/verify", json={"token": token})).status_code == 401
    assert (await _verify(client, attendee, token)).status_code == 403  # the holder is not a verifier
    assert (await _verify(client, other_org, token)).status_code == 403  # another organizer's event
    assert (await _verify(client, admin, token)).status_code == 403

    r = await _verify(client, org, token)
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["valid"] is True and body["ticket_id"] == ticket_id and body["event_id"] == str(event.id)
    # verification is read-only: the ticket stays usable
    assert (await _verify(client, org, token)).status_code == 200
    assert await sql_scalar("SELECT status FROM tickets") == "valid"


async def test_forged_and_malformed_tokens_are_rejected(client, factory):
    org, _, _, ticket_id, token = await _booked(client, factory)
    prefix, _, signature = token.partition(".")
    candidates = [
        f"{prefix}.{'A' * len(signature)}",  # right ticket, wrong signature
        f"{uuid.uuid4().hex}.{signature}",  # signature of a different ticket
        token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB"),  # tampered tail
        prefix,  # no signature
        "garbage-token-without-structure",
        f"{prefix}.",
        "../../etc/passwd" + "x" * 20,
    ]
    for bad in candidates:
        r = await _verify(client, org, bad)
        assert r.status_code == 400, bad
        assert r.json()["message"] == "Invalid ticket"  # no hint about *why*
        assert (await _check_in(client, org, bad)).status_code == 400
    assert await sql_scalar("SELECT status FROM tickets") == "valid"


async def test_cancelled_and_expired_tickets_are_rejected(client, factory):
    org, event, attendee, ticket_id, token = await _booked(client, factory)

    # expired: the event has ended
    await sql_exec(
        "UPDATE events SET start_datetime = now() - interval '3 hours', end_datetime = now() - interval '1 hour', "
        "registration_deadline = now() - interval '4 hours' WHERE id = :i",
        i=event.id,
    )
    r = await _verify(client, org, token)
    assert r.status_code == 409 and "expired" in r.json()["message"].lower()
    assert (await _check_in(client, org, token)).status_code == 409

    # cancelled: registration cancelled by the attendee
    org2, event2, attendee2, _, token2 = await _booked(client, factory)
    reg_id = await sql_scalar("SELECT registration_id FROM tickets WHERE event_id = :e", e=event2.id)
    assert (await client.delete(f"{V1}/registrations/{reg_id}", headers=attendee2.headers)).status_code == 200
    r = await _verify(client, org2, token2)
    assert r.status_code == 409 and "cancelled" in r.json()["message"].lower()
    assert (await client.get(f"{V1}/tickets/{ticket_id}/qr", headers=attendee.headers)).status_code == 200  # untouched


async def test_ticket_for_unpublished_event_is_not_valid(client, factory):
    org, event, _, _, token = await _booked(client, factory)
    await sql_exec("UPDATE events SET status = 'approved' WHERE id = :i", i=event.id)  # admin unpublished it
    assert (await _verify(client, org, token)).status_code == 409


# ------------------------------------------------------------------ check-in
async def test_check_in_flow_and_duplicate_rejection(client, factory):
    org, event, attendee, ticket_id, token = await _booked(client, factory)
    other_org = await factory.account("organizer")

    assert (await _check_in(client, attendee, token)).status_code == 403
    assert (await _check_in(client, other_org, token)).status_code == 403
    assert await sql_scalar("SELECT status FROM tickets") == "valid"

    first = await _check_in(client, org, token)
    assert first.status_code == 200
    data = first.json()["data"]
    assert data["ticket_id"] == ticket_id and data["checked_in_by_id"] == str(org.id) and data["checked_in_at"]
    assert await sql_scalar("SELECT status FROM tickets") == "used"
    assert await sql_scalar("SELECT checked_in_by_id FROM tickets") == org.id

    again = await _check_in(client, org, token)
    assert again.status_code == 409 and "already been used" in again.json()["message"]
    assert again.json()["detail"]["checked_in_at"]
    assert (await _verify(client, org, token)).status_code == 409  # used tickets no longer verify

    # attendance listings
    roster = await client.get(f"{V1}/attendance/event/{event.id}", headers=org.headers)
    items = roster.json()["data"]["items"]
    assert roster.json()["data"]["total"] == 1 and items[0]["attendee_id"] == str(attendee.id)
    assert items[0]["checked_in_by_id"] == str(org.id)
    assert (await client.get(f"{V1}/attendance/event/{event.id}", headers=other_org.headers)).status_code == 403
    admin = await factory.account("admin")
    assert (await client.get(f"{V1}/attendance/event/{event.id}", headers=admin.headers)).status_code == 200

    history = await client.get(f"{V1}/attendance/my", headers=attendee.headers)
    assert history.json()["data"]["total"] == 1
    assert history.json()["data"]["items"][0]["event"]["id"] == str(event.id)

    # a checked-in registration can no longer be cancelled
    reg_id = await sql_scalar("SELECT registration_id FROM tickets")
    assert (await client.delete(f"{V1}/registrations/{reg_id}", headers=attendee.headers)).status_code == 409


async def test_concurrent_check_ins_succeed_exactly_once(client, factory):
    org, _, _, _, token = await _booked(client, factory)
    responses = await asyncio.gather(*(_check_in(client, org, token) for _ in range(12)))
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1 and codes.count(409) == 11, codes
    assert await sql_scalar("SELECT count(*) FROM tickets WHERE status = 'used'") == 1


async def test_organizer_statistics_and_dashboard(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org, capacity=10, ticket_type="paid", ticket_price=Decimal("20"))
    attendees = [await factory.account() for _ in range(4)]
    tokens = []
    for a in attendees:
        reg = (await register(client, a, event.id)).json()["data"]
        tokens.append((await client.get(f"{V1}/tickets/{reg['ticket']['id']}", headers=a.headers)).json()["data"]["qr_token"])
    await _check_in(client, org, tokens[0])
    await _check_in(client, org, tokens[1])
    reg_id = await sql_scalar("SELECT registration_id FROM tickets t JOIN users u ON u.id = t.user_id WHERE u.id = :u", u=attendees[3].id)
    await client.delete(f"{V1}/registrations/{reg_id}", headers=attendees[3].headers)

    stats = (await client.get(f"{V1}/organizer/events/{event.id}/statistics", headers=org.headers)).json()["data"]
    assert stats["active_registrations"] == 3 and stats["cancelled_registrations"] == 1
    assert stats["checked_in"] == 2 and stats["attendance_percentage"] == 66.7
    assert stats["registered_count"] == 3 and stats["seats_remaining"] == 7 and stats["revenue"] == 60.0

    dash = (await client.get(f"{V1}/organizer/dashboard", headers=org.headers)).json()["data"]
    assert dash["total_events"] == 1 and dash["published_events"] == 1 and dash["upcoming_events"] == 1
    assert dash["total_registrations"] == 3 and dash["total_attendees"] == 2
    assert dash["attendance_percentage"] == 66.7 and len(dash["recent_registrations"]) == 4

    people = (await client.get(f"{V1}/organizer/events/{event.id}/attendees", headers=org.headers)).json()["data"]
    assert people["total"] == 4
    only_confirmed = await client.get(
        f"{V1}/organizer/events/{event.id}/attendees", params={"status": "confirmed"}, headers=org.headers
    )
    assert only_confirmed.json()["data"]["total"] == 3
    assert {"full_name", "email", "ticket_status", "checked_in_at"} <= set(people["items"][0])

    profile = (await client.get(f"{V1}/organizer/profile", headers=org.headers)).json()["data"]
    assert profile["user"]["id"] == str(org.id) and profile["total_events"] == 1
    assert "password" not in str(profile)
