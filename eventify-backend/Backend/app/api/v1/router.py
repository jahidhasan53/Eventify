from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    attendance,
    auth,
    events,
    notifications,
    organizer,
    registrations,
    reviews,
    tickets,
    users,
)

api_router = APIRouter()
for module in (
    auth,
    users,
    events,
    registrations,
    tickets,
    attendance,
    organizer,
    admin,
    reviews,
    notifications,
):
    api_router.include_router(module.router)
