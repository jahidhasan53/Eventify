"""Role → permission matrix. This is the single source of truth for what each role may do;
endpoints enforce it through `require_permission(...)` in app.dependencies.authorization.

Ownership (e.g. "update OWN event") is a separate, per-resource check done in the services.
"""

from enum import StrEnum

from app.models.enums import Role


class Permission(StrEnum):
    # Anyone authenticated
    PROFILE_MANAGE_OWN = "profile:manage_own"
    # Attendee capabilities
    REGISTRATION_CREATE = "registration:create"
    REGISTRATION_CANCEL_OWN = "registration:cancel_own"
    TICKET_VIEW_OWN = "ticket:view_own"
    ATTENDANCE_VIEW_OWN = "attendance:view_own"
    REVIEW_CREATE = "review:create"
    EVENT_REPORT = "event:report"
    # Organizer capabilities
    EVENT_CREATE = "event:create"
    EVENT_MANAGE_OWN = "event:manage_own"
    ATTENDEE_MANAGE_OWN_EVENT = "attendee:manage_own_event"
    TICKET_VERIFY = "ticket:verify"
    ATTENDANCE_CHECK_IN = "attendance:check_in"
    ORGANIZER_ANALYTICS = "organizer:analytics"
    # Admin capabilities
    ADMIN_USERS_MANAGE = "admin:users_manage"
    ADMIN_EVENTS_MODERATE = "admin:events_moderate"
    ADMIN_REPORTS_MANAGE = "admin:reports_manage"
    ADMIN_STATS_VIEW = "admin:stats_view"
    ADMIN_AUDIT_VIEW = "admin:audit_view"
    ATTENDANCE_VIEW_ANY = "attendance:view_any"


_ATTENDEE = {
    Permission.PROFILE_MANAGE_OWN,
    Permission.REGISTRATION_CREATE,
    Permission.REGISTRATION_CANCEL_OWN,
    Permission.TICKET_VIEW_OWN,
    Permission.ATTENDANCE_VIEW_OWN,
    Permission.REVIEW_CREATE,
    Permission.EVENT_REPORT,
}

_ORGANIZER = _ATTENDEE | {
    Permission.EVENT_CREATE,
    Permission.EVENT_MANAGE_OWN,
    Permission.ATTENDEE_MANAGE_OWN_EVENT,
    Permission.TICKET_VERIFY,
    Permission.ATTENDANCE_CHECK_IN,
    Permission.ORGANIZER_ANALYTICS,
}

_ADMIN = {
    Permission.PROFILE_MANAGE_OWN,
    Permission.TICKET_VIEW_OWN,
    Permission.EVENT_REPORT,
    Permission.ADMIN_USERS_MANAGE,
    Permission.ADMIN_EVENTS_MODERATE,
    Permission.ADMIN_REPORTS_MANAGE,
    Permission.ADMIN_STATS_VIEW,
    Permission.ADMIN_AUDIT_VIEW,
    Permission.ATTENDANCE_VIEW_ANY,
}

ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    Role.ATTENDEE.value: _ATTENDEE,
    Role.ORGANIZER.value: _ORGANIZER,
    Role.ADMIN.value: _ADMIN,
}


def has_permission(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())
