from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.utils import utcnow
from tests.conftest import event_payload, iso, register, sql_scalar

V1 = "/api/v1"


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"capacity": 0}, "capacity"),
        ({"capacity": -5}, "capacity"),
        ({"capacity": 1_000_000}, "capacity"),
        ({"title": "ab"}, "title"),
        ({"ticket_type": "paid", "ticket_price": 0}, None),
        ({"ticket_type": "free", "ticket_price": 10}, None),
        ({"event_type": "offline", "venue": None}, None),
        ({"cover_image": "javascript:alert(1)"}, "cover_image"),
        ({"category": ""}, "category"),
    ],
)
async def test_event_creation_validation(client, factory, overrides, field):
    organizer = await factory.account("organizer")
    r = await client.post(f"{V1}/events/", json=event_payload(**overrides), headers=organizer.headers)
    assert r.status_code == 422
    assert r.json()["success"] is False
    if field:
        assert any(err["field"] == field for err in r.json()["detail"])


async def test_event_date_rules(client, factory):
    organizer = await factory.account("organizer")
    start = utcnow() + timedelta(days=10)

    async def post(**kw):
        return await client.post(f"{V1}/events/", json=event_payload(**kw), headers=organizer.headers)

    assert (await post(end_datetime=iso(start - timedelta(hours=1)), start_datetime=iso(start))).status_code == 422
    assert (await post(start_datetime=iso(start), registration_deadline=iso(start + timedelta(hours=1)))).status_code == 422
    assert (await post(start_datetime=iso(utcnow() - timedelta(days=1)),
                       end_datetime=iso(utcnow() + timedelta(days=1)),
                       registration_deadline=iso(utcnow() - timedelta(days=2)))).status_code == 422
    naive = start.replace(tzinfo=None).isoformat()  # no timezone offset
    assert (await post(start_datetime=naive)).status_code == 422


# ------------------------------------------------------------------ lifecycle and visibility
async def test_event_lifecycle_and_public_visibility(client, factory):
    organizer = await factory.account("organizer")
    admin = await factory.account("admin")
    attendee = await factory.account()

    created = await client.post(f"{V1}/events/", json=event_payload(), headers=organizer.headers)
    assert created.status_code == 201
    event = created.json()["data"]
    eid = event["id"]
    assert event["status"] == "draft" and event["organizer_id"] == str(organizer.id)
    assert event["registered_count"] == 0 and event["seats_remaining"] == 25

    async def public_ids():
        r = await client.get(f"{V1}/events/")
        return [e["id"] for e in r.json()["data"]["items"]]

    # Drafts are hidden from visitors and other users, visible to the owner and admins.
    assert eid not in await public_ids()
    assert (await client.get(f"{V1}/events/{eid}")).status_code == 404
    assert (await client.get(f"{V1}/events/{eid}", headers=attendee.headers)).status_code == 404
    assert (await client.get(f"{V1}/events/{eid}", headers=organizer.headers)).status_code == 200
    assert (await client.get(f"{V1}/events/{eid}", headers=admin.headers)).status_code == 200

    # draft -> pending
    assert (await client.post(f"{V1}/events/{eid}/submit", headers=organizer.headers)).json()["data"]["status"] == "pending"
    assert eid not in await public_ids()
    # An organizer cannot approve or publish their own event.
    assert (await client.patch(f"{V1}/admin/events/{eid}/approve", headers=organizer.headers)).status_code == 403
    # pending -> approved -> published
    approved = await client.patch(f"{V1}/admin/events/{eid}/approve", headers=admin.headers)
    assert approved.status_code == 200 and approved.json()["data"]["status"] == "approved"
    assert eid not in await public_ids()
    published = await client.patch(f"{V1}/admin/events/{eid}/publish", headers=admin.headers)
    assert published.json()["data"]["status"] == "published"
    assert eid in await public_ids()
    assert (await client.get(f"{V1}/events/{eid}")).status_code == 200

    # unpublish hides it again
    assert (await client.patch(f"{V1}/admin/events/{eid}/unpublish", headers=admin.headers)).status_code == 200
    assert eid not in await public_ids()
    # Invalid transition: cannot approve something that is not pending.
    assert (await client.patch(f"{V1}/admin/events/{eid}/approve", headers=admin.headers)).status_code == 409

    # moderation is audited and visible to admins only
    logs = await client.get(f"{V1}/admin/audit-logs", params={"action": "event."}, headers=admin.headers)
    actions = [row["action"] for row in logs.json()["data"]["items"]]
    assert {"event.approve", "event.publish", "event.unpublish"} <= set(actions)
    # approving notified the organizer
    notes = await client.get(f"{V1}/notifications/", headers=organizer.headers)
    assert "event_approved" in [n["type"] for n in notes.json()["data"]["items"]]


async def test_reject_requires_reason_and_reason_is_private(client, factory):
    organizer = await factory.account("organizer")
    admin = await factory.account("admin")
    event = await factory.event(organizer, status="pending")

    assert (await client.patch(f"{V1}/admin/events/{event.id}/reject", json={}, headers=admin.headers)).status_code == 422
    r = await client.patch(
        f"{V1}/admin/events/{event.id}/reject", json={"reason": "Missing venue details"}, headers=admin.headers
    )
    assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"

    own = await client.get(f"{V1}/events/{event.id}", headers=organizer.headers)
    assert own.json()["data"]["rejection_reason"] == "Missing venue details"
    # rejected events can be fixed and resubmitted
    again = await client.post(f"{V1}/events/{event.id}/submit", headers=organizer.headers)
    assert again.json()["data"]["status"] == "pending"
    assert again.json()["data"]["rejection_reason"] is None
    notes = await client.get(f"{V1}/notifications/", headers=organizer.headers)
    assert "event_rejected" in [n["type"] for n in notes.json()["data"]["items"]]


# ------------------------------------------------------------------ browsing
async def test_listing_filters_sorting_and_pagination(client, factory):
    org = await factory.account("organizer")
    soon = utcnow() + timedelta(days=3)
    later = utcnow() + timedelta(days=30)
    await factory.event(org, title="Alpha Python", category="Technology", location="Dhaka", start=soon)
    await factory.event(org, title="Beta Cooking", category="Food", location="Chittagong", start=later,
                        event_type="online", venue=None, ticket_type="paid", ticket_price=Decimal("15"))
    await factory.event(org, title="Gamma Python Advanced", category="Technology", location="Sylhet", start=later)
    await factory.event(org, title="Hidden Draft", status="draft")

    async def get(**params):
        r = await client.get(f"{V1}/events/", params=params)
        assert r.status_code == 200, r.text
        return r.json()["data"]

    page = await get()
    assert page["total"] == 3 and page["page"] == 1 and page["page_size"] == 10 and page["pages"] == 1
    assert [e["title"] for e in page["items"]][0] == "Alpha Python"  # default: soonest first

    assert {e["title"] for e in (await get(q="python"))["items"]} == {"Alpha Python", "Gamma Python Advanced"}
    assert [e["title"] for e in (await get(category="food"))["items"]] == ["Beta Cooking"]
    assert [e["title"] for e in (await get(location="chitta"))["items"]] == ["Beta Cooking"]
    assert [e["title"] for e in (await get(event_type="online"))["items"]] == ["Beta Cooking"]
    assert [e["title"] for e in (await get(ticket_type="paid"))["items"]] == ["Beta Cooking"]
    assert (await get(ticket_type="free"))["total"] == 2
    assert (await get(date_from=(utcnow() + timedelta(days=2)).date().isoformat(),
                      date_to=(utcnow() + timedelta(days=4)).date().isoformat()))["total"] == 1

    by_title = await get(sort_by="title", order="desc")
    assert [e["title"] for e in by_title["items"]][0] == "Gamma Python Advanced"

    first = await get(page=1, page_size=2)
    second = await get(page=2, page_size=2)
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert first["pages"] == 2 and first["total"] == 3

    for bad in ({"page": 0}, {"page_size": 1000}, {"sort_by": "password"}, {"event_type": "hybrid"}):
        assert (await client.get(f"{V1}/events/", params=bad)).status_code == 422
    assert (await client.get(f"{V1}/events/", params={"date_from": "2030-02-01", "date_to": "2030-01-01"})).status_code == 400


async def test_featured_upcoming_and_categories_routes(client, factory):
    org = await factory.account("organizer")
    admin = await factory.account("admin")
    plain = await factory.event(org, title="Plain")
    star = await factory.event(org, title="Star", category="Science")
    await factory.event(org, title="Draft one", status="draft", category="Hidden")

    assert (await client.get(f"{V1}/events/featured")).json()["data"]["total"] == 0
    await client.patch(f"{V1}/admin/events/{star.id}/feature", json={"featured": True}, headers=admin.headers)
    featured = (await client.get(f"{V1}/events/featured")).json()["data"]
    assert [e["title"] for e in featured["items"]] == ["Star"]

    upcoming = await client.get(f"{V1}/events/upcoming")
    assert upcoming.status_code == 200 and upcoming.json()["data"]["total"] == 2

    cats = await client.get(f"{V1}/events/categories")
    assert cats.json()["data"] == ["Science", "Technology"]  # drafts' categories are not leaked
    assert plain.id != star.id


# ------------------------------------------------------------------ update / delete
async def test_owner_can_update_and_rules_are_revalidated(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org, status="draft", capacity=10)
    ok = await client.put(f"{V1}/events/{event.id}", json={"title": "New title", "capacity": 40}, headers=org.headers)
    assert ok.status_code == 200 and ok.json()["data"]["title"] == "New title" and ok.json()["data"]["capacity"] == 40

    start = utcnow() + timedelta(days=5)
    bad = await client.put(
        f"{V1}/events/{event.id}",
        json={"start_datetime": iso(start), "registration_deadline": iso(start + timedelta(days=1))},
        headers=org.headers,
    )
    assert bad.status_code == 400
    assert (await client.put(f"{V1}/events/{event.id}", json={}, headers=org.headers)).status_code == 400
    assert (await client.put(f"{V1}/events/{event.id}", json={"title": None}, headers=org.headers)).status_code == 400


async def test_capacity_cannot_drop_below_registered_seats(client, factory):
    org = await factory.account("organizer")
    event = await factory.event(org, capacity=5)
    for _ in range(3):
        assert (await register(client, await factory.account(), event.id)).status_code == 201
    r = await client.put(f"{V1}/events/{event.id}", json={"capacity": 2}, headers=org.headers)
    assert r.status_code == 400
    assert (await client.put(f"{V1}/events/{event.id}", json={"capacity": 3}, headers=org.headers)).status_code == 200


async def test_delete_removes_unused_drafts_and_cancels_events_with_registrations(client, factory):
    org = await factory.account("organizer")
    draft = await factory.event(org, status="draft")
    r = await client.delete(f"{V1}/events/{draft.id}", headers=org.headers)
    assert r.status_code == 200 and r.json()["data"]["result"] == "deleted"
    assert (await client.get(f"{V1}/events/{draft.id}", headers=org.headers)).status_code == 404

    live = await factory.event(org, capacity=5)
    attendee = await factory.account()
    reg = await register(client, attendee, live.id)
    assert reg.status_code == 201
    r = await client.delete(f"{V1}/events/{live.id}", headers=org.headers)
    assert r.json()["data"]["result"] == "cancelled"
    assert await sql_scalar("SELECT status FROM events WHERE id = :i", i=live.id) == "cancelled"
    assert await sql_scalar("SELECT registered_count FROM events WHERE id = :i", i=live.id) == 0
    assert await sql_scalar("SELECT status FROM tickets") == "cancelled"
    assert await sql_scalar("SELECT status FROM registrations") == "cancelled"
    notes = await client.get(f"{V1}/notifications/", headers=attendee.headers)
    assert "event_cancelled" in [n["type"] for n in notes.json()["data"]["items"]]
    # cancelled events cannot be registered for or edited
    assert (await register(client, await factory.account(), live.id)).status_code == 409
    assert (await client.put(f"{V1}/events/{live.id}", json={"title": "Zombie"}, headers=org.headers)).status_code == 409


async def test_reporting_events(client, factory):
    org = await factory.account("organizer")
    admin = await factory.account("admin")
    attendee = await factory.account()
    event = await factory.event(org)
    body = {"reason": "spam", "details": "Looks fake"}

    assert (await client.post(f"{V1}/events/{event.id}/report", json=body)).status_code == 401
    r = await client.post(f"{V1}/events/{event.id}/report", json=body, headers=attendee.headers)
    assert r.status_code == 201
    assert (await client.post(f"{V1}/events/{event.id}/report", json=body, headers=attendee.headers)).status_code == 409
    assert (await client.post(f"{V1}/events/{event.id}/report", json={"reason": "bored"}, headers=attendee.headers)).status_code == 422

    queue = await client.get(f"{V1}/admin/reports", headers=admin.headers)
    assert queue.json()["data"]["total"] == 1
    rid = queue.json()["data"]["items"][0]["id"]
    done = await client.patch(f"{V1}/admin/reports/{rid}", json={"status": "resolved", "note": "Handled"}, headers=admin.headers)
    assert done.status_code == 200 and done.json()["data"]["status"] == "resolved"
    assert (await client.patch(f"{V1}/admin/reports/{rid}", json={"status": "dismissed"}, headers=admin.headers)).status_code == 409
