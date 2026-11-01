import asyncio
from datetime import timedelta

from app.core.utils import utcnow
from tests.conftest import register, sql_exec, sql_scalar

V1 = "/api/v1"


async def test_registration_creates_ticket_notification_and_takes_a_seat(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, capacity=3)

    r = await register(client, attendee, event.id)
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["reference"].startswith("REG-") and data["status"] == "confirmed"
    assert data["ticket"]["status"] == "valid" and data["event"]["id"] == str(event.id)
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 1

    mine = await client.get(f"{V1}/registrations/my", headers=attendee.headers)
    assert mine.json()["data"]["total"] == 1
    one = await client.get(f"{V1}/registrations/{data['id']}", headers=attendee.headers)
    assert one.status_code == 200

    notes = await client.get(f"{V1}/notifications/", headers=attendee.headers)
    assert notes.json()["data"]["items"][0]["type"] == "registration_confirmed"


async def test_duplicate_registration_is_rejected(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org)
    assert (await register(client, attendee, event.id)).status_code == 201
    again = await register(client, attendee, event.id)
    assert again.status_code == 409 and "already registered" in again.json()["message"]
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 1
    assert await sql_scalar("SELECT count(*) FROM registrations") == 1


async def test_full_event_rejects_new_registrations(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org, capacity=2)
    assert (await register(client, await factory.account(), event.id)).status_code == 201
    assert (await register(client, await factory.account(), event.id)).status_code == 201
    r = await register(client, await factory.account(), event.id)
    assert r.status_code == 409 and "full" in r.json()["message"].lower()
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 2


async def test_registration_closed_after_deadline_and_for_unavailable_events(client, factory):
    org = await factory.account("organizer")
    now = utcnow()
    late = await factory.event(
        org, start=now + timedelta(hours=5), registration_deadline=now - timedelta(minutes=1)
    )
    r = await register(client, await factory.account(), late.id)
    assert r.status_code == 409 and r.json()["message"] == "Registration is closed"

    for status in ("draft", "pending", "approved", "rejected", "cancelled"):
        event = await factory.event(org, status=status)
        assert (await register(client, await factory.account(), event.id)).status_code == 409, status

    import uuid

    assert (await register(client, await factory.account(), uuid.uuid4())).status_code == 404


async def test_organizer_cannot_register_for_own_event(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org)
    assert (await register(client, org, event.id)).status_code == 403


async def test_admin_accounts_cannot_register(client, factory):
    org = await factory.account("organizer")
    admin = await factory.account("admin")
    event = await factory.event(org)
    assert (await register(client, admin, event.id)).status_code == 403


async def test_cancellation_releases_the_seat_and_invalidates_the_ticket(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, capacity=1)
    reg = (await register(client, attendee, event.id)).json()["data"]

    r = await client.delete(f"{V1}/registrations/{reg['id']}", headers=attendee.headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 0
    assert await sql_scalar("SELECT status FROM tickets") == "cancelled"

    # double cancel is a conflict, not a second seat release
    assert (await client.delete(f"{V1}/registrations/{reg['id']}", headers=attendee.headers)).status_code == 409
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 0

    # the freed seat is bookable again, by the same user too (history rows are kept)
    assert (await register(client, await factory.account(), event.id)).status_code == 201
    assert await sql_scalar("SELECT count(*) FROM registrations WHERE status = 'cancelled'") == 1
    types = [n["type"] for n in (await client.get(f"{V1}/notifications/", headers=attendee.headers)).json()["data"]["items"]]
    assert "registration_cancelled" in types


async def test_users_cannot_see_or_cancel_other_users_registrations(client, factory):
    org = await factory.account("organizer")
    alice = await factory.account()
    mallory = await factory.account()
    event = await factory.event(org)
    reg = (await register(client, alice, event.id)).json()["data"]

    assert (await client.get(f"{V1}/registrations/{reg['id']}", headers=mallory.headers)).status_code == 404
    assert (await client.delete(f"{V1}/registrations/{reg['id']}", headers=mallory.headers)).status_code == 404
    assert (await client.get(f"{V1}/registrations/my", headers=mallory.headers)).json()["data"]["total"] == 0
    assert await sql_scalar("SELECT status FROM registrations") == "confirmed"


async def test_cannot_cancel_after_event_started(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org)
    reg = (await register(client, attendee, event.id)).json()["data"]
    await sql_exec(
        "UPDATE events SET start_datetime = now() - interval '1 hour', end_datetime = now() + interval '1 hour', "
        "registration_deadline = now() - interval '2 hours' WHERE id = :i",
        i=event.id,
    )
    assert (await client.delete(f"{V1}/registrations/{reg['id']}", headers=attendee.headers)).status_code == 409


async def test_organizer_can_remove_an_attendee_from_own_event_only(client, factory):
    org = await factory.account("organizer")
    other = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org)
    reg = (await register(client, attendee, event.id)).json()["data"]

    assert (await client.delete(f"{V1}/organizer/registrations/{reg['id']}", headers=other.headers)).status_code == 403
    r = await client.delete(f"{V1}/organizer/registrations/{reg['id']}", headers=org.headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 0


# ------------------------------------------------------------------ concurrency (real parallel DB transactions)
async def test_concurrent_registrations_never_exceed_capacity(client, factory):
    org = await factory.account("organizer")
    capacity, contenders = 5, 25
    event = await factory.event(org, capacity=capacity)
    accounts = [await factory.account() for _ in range(contenders)]

    responses = await asyncio.gather(*(register(client, a, event.id) for a in accounts))
    codes = sorted(r.status_code for r in responses)

    assert codes.count(201) == capacity, codes
    assert codes.count(409) == contenders - capacity, codes
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == capacity
    assert await sql_scalar("SELECT count(*) FROM registrations WHERE status = 'confirmed'") == capacity
    assert await sql_scalar("SELECT count(*) FROM tickets WHERE status = 'valid'") == capacity
    assert await sql_scalar("SELECT count(DISTINCT reference) FROM registrations") == capacity


async def test_concurrent_duplicate_submissions_create_one_registration(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, capacity=10)

    responses = await asyncio.gather(*(register(client, attendee, event.id) for _ in range(8)))
    codes = [r.status_code for r in responses]
    assert codes.count(201) == 1 and codes.count(409) == 7, codes
    assert await sql_scalar("SELECT count(*) FROM registrations") == 1
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 1


async def test_concurrent_cancel_releases_seat_exactly_once(client, factory):
    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, capacity=3)
    reg = (await register(client, attendee, event.id)).json()["data"]

    responses = await asyncio.gather(
        *(client.delete(f"{V1}/registrations/{reg['id']}", headers=attendee.headers) for _ in range(6))
    )
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1 and codes.count(409) == 5, codes
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=event.id) == 0


async def test_database_constraints_back_up_the_application_checks(client, factory):
    """Even raw SQL cannot overbook or double-book (defence in depth)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    org = await factory.account("organizer")
    attendee = await factory.account()
    event = await factory.event(org, capacity=2)
    with pytest.raises(IntegrityError):
        await sql_exec("UPDATE events SET registered_count = 3 WHERE id = :i", i=event.id)

    assert (await register(client, attendee, event.id)).status_code == 201
    with pytest.raises(IntegrityError):
        await sql_exec(
            "INSERT INTO registrations (id, reference, user_id, event_id, status, amount) "
            "VALUES (gen_random_uuid(), 'REG-DUPLICATE1', :u, :e, 'confirmed', 0)",
            u=attendee.id,
            e=event.id,
        )
