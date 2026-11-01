# Eventify Backend

Secure REST API for the Eventify event & workshop platform.
**FastAPI · Python 3.12+ · PostgreSQL · SQLAlchemy 2.0 (async) · asyncpg · Alembic · Pydantic v2 · Argon2id · JWT**

The existing React frontend talks to this API under `/api/v1`. All authorisation is enforced on the
server: the role, user id, organiser id and event status are never taken from the request body.

---

## Contents

1. [Setup](#1-setup)
2. [Running, docs, tests](#2-running-docs-and-tests)
3. [Project layout](#3-project-layout)
4. [Conventions: envelope, pagination, errors](#4-conventions)
5. [Authentication and CSRF](#5-authentication-and-csrf)
6. [Roles and permissions](#6-roles-and-permissions)
7. [Endpoint reference](#7-endpoint-reference)
8. [Request / response examples](#8-request--response-examples)
9. [Frontend integration (Axios)](#9-frontend-integration-axios)
10. [Design notes: concurrency, tickets, database](#10-design-notes)
11. [Security: what is covered, assumptions, remaining risks](#11-security)
12. [Production deployment](#12-production-deployment)

---

## 1. Setup

### 1.1 Python
Python **3.12 or newer** is required (3.13 works).

```bash
python3 --version          # must print 3.12 or higher
```
Install from <https://www.python.org/downloads/> (Windows/macOS) or `sudo apt install python3 python3-venv` (Debian/Ubuntu).

### 1.2 Virtual environment and dependencies
```bash
cd Backend
python3 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
`requirements.txt` uses compatible-release ranges. After a successful install, freeze exact versions for
reproducible deployments: `pip freeze > requirements.lock` (git-ignored by default; commit it if you want).

### 1.3 PostgreSQL
Install PostgreSQL 14+ and create a role plus **two** databases: one for the app, one only for the tests.

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE eventify WITH LOGIN PASSWORD 'choose-a-strong-password';
CREATE DATABASE eventify      OWNER eventify;
CREATE DATABASE eventify_test OWNER eventify;
SQL
```

### 1.4 Environment variables
```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"     # paste the output as JWT_SECRET_KEY
```
Edit `.env`:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://eventify:<password>@localhost:5432/eventify` |
| `TEST_DATABASE_URL` | Used **only** by pytest. The database name **must end in `_test`** or the tests refuse to run. |
| `JWT_SECRET_KEY` | ≥ 32 random chars. The app refuses to start with a placeholder or short value. |
| `TICKET_HMAC_KEY` | Optional separate key for QR tokens (derived from `JWT_SECRET_KEY` when empty). |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Defaults 15 minutes / 7 days. |
| `FRONTEND_ORIGIN` | Comma-separated CORS allow-list (default `http://localhost:5173`). `*` is rejected. |
| `COOKIE_SECURE`, `COOKIE_SAMESITE`, `COOKIE_DOMAIN` | Cookie flags. `Secure` is automatic in production. |
| `RATE_LIMIT_*`, `TRUST_PROXY_HEADERS`, `MAX_REQUEST_BODY_BYTES` | Limits (see `.env.example`). |
| `EMAIL_BACKEND` | `console` (dev, logs mails) · `smtp` · `disabled`. SMTP credentials come only from env vars. |
| `ENABLE_BACKGROUND_JOBS` | Marks ended events completed and sends 24-hour reminders. |

`.env` is git-ignored. Never commit it.

### 1.5 Database migrations (Alembic)
The app **never** calls `create_all()`. The schema is managed only by Alembic.

```bash
alembic upgrade head                           # apply all migrations
alembic current                                # show the applied revision
alembic check                                  # fail if models and migrations have drifted
alembic revision --autogenerate -m "describe change"   # after changing a model; REVIEW the file
alembic downgrade -1                           # roll back one revision
```
To target another database for one command: `ALEMBIC_DATABASE_URL=postgresql+asyncpg://... alembic upgrade head`.

### 1.6 Create the first administrator (securely)
Admins cannot be created via the API: public registration accepts only `attendee` or `organizer`, and
no endpoint changes a user's role. Bootstrap the first admin from the server:

```bash
python scripts/create_admin.py --email admin@example.com --name "Site Admin"
```
The password is typed at a hidden prompt (never in shell history or the process list) and must satisfy
the password policy. The action is written to the audit log. For automation, export
`EVENTIFY_ADMIN_PASSWORD` for the single command and unset it afterwards.

---

## 2. Running, docs and tests

```bash
uvicorn app.main:app --reload            # development, http://127.0.0.1:8000
```
* Swagger UI: <http://127.0.0.1:8000/docs> · ReDoc: `/redoc` · OpenAPI: `/openapi.json`
  (**disabled automatically when `ENVIRONMENT=production`**)
* Health probe: `GET /health`

### Tests
```bash
pytest -v
```
* Uses `TEST_DATABASE_URL` only, and aborts unless the database name ends in `_test`.
* The schema is created by running the real migrations (`alembic upgrade head`), so migrations are tested every run.
* Tables are truncated between tests. **Never point `TEST_DATABASE_URL` at real data.**
* Covered areas: registration/login/logout/refresh, invalid credentials, token expiry, RBAC, organiser
  ownership, event CRUD and moderation, duplicate and full-event registration, **concurrent registration
  vs. seat limit**, cancellation, QR verification, duplicate check-in (including concurrent), unauthorised
  verification, admin-only endpoints, input validation, rate limiting, CSRF, security headers.

Dependency audit: `pip-audit -r requirements.txt`

---

## 3. Project layout

```
Backend/
├── app/
│   ├── main.py                  # app factory, middleware, CORS, routers
│   ├── core/                    # config, security (Argon2id/JWT/HMAC), permissions matrix,
│   │                            #   exceptions, middleware (CSRF/size/headers), rate limiter, cookies
│   ├── database/                # async engine + session, declarative base
│   ├── models/                  # user, event, registration, ticket, review, notification, audit
│   ├── schemas/                 # Pydantic v2 request/response models (extra fields forbidden)
│   ├── dependencies/            # authentication (current user), authorization (permissions), pagination
│   ├── services/                # business logic: auth, event, registration, ticket, organizer,
│   │                            #   admin, review, notification, email, audit, maintenance
│   └── api/v1/{router.py, endpoints/*.py}
├── migrations/                  # Alembic (env.py + versions/)
├── scripts/create_admin.py
├── tests/
├── .env.example · .gitignore · alembic.ini · pytest.ini · requirements.txt · README.md
```

---

## 4. Conventions

**Success envelope**
```json
{ "success": true, "message": "Event created successfully", "data": { } }
```
**Error envelope** (never contains stack traces or internal details)
```json
{ "success": false, "message": "Registration is closed", "detail": null }
```
**Validation error (422)** – one entry per invalid field; submitted values are never echoed back
```json
{
  "success": false,
  "message": "Validation failed",
  "detail": [
    { "field": "password", "message": "Value error, Password must contain a symbol", "type": "value_error" }
  ]
}
```
**Pagination** – every list endpoint accepts `?page=1&page_size=10` (max 100) and returns
```json
{ "success": true, "message": "Success",
  "data": { "items": [ ], "total": 42, "page": 1, "page_size": 10, "pages": 5 } }
```

| Status | Meaning |
|---|---|
| 400 | Bad request / business rule on the input (e.g. invalid ticket, wrong current password) |
| 401 | Missing, invalid or expired credentials |
| 403 | Authenticated but not permitted (wrong role, not the owner, CSRF failure, suspended) |
| 404 | Not found (also used for other users' private resources, so ids cannot be probed) |
| 409 | State conflict (duplicate registration, event full, ticket already used, registration closed) |
| 413 | Request body too large |
| 422 | Validation error |
| 429 | Rate limited (`Retry-After` header set) |
| 500 | Unexpected error (generic message; details only in server logs) |

---

## 5. Authentication and CSRF

Two ways to authenticate; the frontend may use either.

1. **Bearer token** – `Authorization: Bearer <access_token>` (token returned in the login body). No CSRF token needed.
2. **HttpOnly cookies** – set automatically by `login`/`refresh`:
   * `access_token` – HttpOnly, 15 min, path `/`
   * `refresh_token` – HttpOnly, 7 days, **path-scoped to `/api/v1/auth`**
   * `csrf_token` – readable by JavaScript. For every `POST/PUT/PATCH/DELETE` made with cookies, send
     it back as header **`X-CSRF-Token`** (double-submit cookie). The `Origin` header, when present, must also be on the CORS allow-list.
   * `Secure` is on in production; `SameSite` defaults to `Lax`.

**Lifecycle**
* Access tokens are short-lived JWTs holding only the user id; **role and status are re-read from the database on every request**, so suspending a user or changing a role takes effect immediately.
* Refresh tokens are random, stored only as SHA-256 hashes, and **rotated on every use**. Re-presenting an already-used token (after a 10 s grace window for racing tabs) revokes the whole token family.
* Password reset and password change revoke all refresh tokens.
* 5 failed logins lock the account for 15 minutes; login is additionally rate limited per IP.
* Passwords: Argon2id; policy ≥ 10 characters with upper, lower, digit and symbol.
* Suspended accounts cannot log in or refresh.

---

## 6. Roles and permissions

The single source of truth is `app/core/permissions.py`; endpoints enforce it through `require_permission(...)`.
Ownership ("**own** event") is an additional per-resource check in the services.

| Capability | attendee | organizer | admin |
|---|:--:|:--:|:--:|
| View published events (public) | ✅ | ✅ | ✅ |
| Register for / cancel own registration | ✅ | ✅ (not own events) | – |
| View own tickets and QR | ✅ | ✅ | ✅ |
| View own attendance history | ✅ | ✅ | – |
| Review a completed event they attended | ✅ | ✅ | – |
| Report an event | ✅ | ✅ | ✅ |
| Manage own profile | ✅ | ✅ | ✅ |
| Create events; update / cancel / delete **own** | – | ✅ | – |
| View / remove attendees of **own** events | – | ✅ | – |
| Verify tickets and check in (**own** events only) | – | ✅ | – |
| Organizer dashboard and own event analytics | – | ✅ | – |
| Approve / reject / publish / unpublish / feature events | – | – | ✅ |
| List users and organizers, suspend / reactivate users | – | – | ✅ |
| Reports queue, platform statistics, audit log | – | – | ✅ |
| View check-in records of any event | – | own | ✅ |

**Event status flow:** `draft → pending` (organiser submits) `→ approved | rejected` (admin) `→ published` (admin) · `unpublish` returns to `approved` · `cancelled` (organiser) · `completed` (automatic after the end time).
A rejected event can be edited and re-submitted.

---

## 7. Endpoint reference

Base path `/api/v1`. "Auth" = valid credentials required. Full schemas are in Swagger at `/docs`.

### Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | Public. `role` is `attendee` (default) or `organizer`; `admin` is rejected (422). |
| POST | `/auth/login` | Rate limited. Sets cookies and returns `access_token`, `csrf_token`, `user`. |
| POST | `/auth/logout` | Revokes the refresh token, clears cookies. |
| POST | `/auth/refresh` | Uses the refresh cookie; rotates it. |
| GET | `/auth/me` | Current user. |
| POST | `/auth/forgot-password` | Always answers the same way (no account enumeration). Rate limited. |
| POST | `/auth/reset-password` | `{token, new_password}`; single use. |
| POST | `/auth/change-password` | `{current_password, new_password}`; revokes other sessions. |

### Users
`GET /users/me` · `PATCH /users/me` (name, phone, bio, organisation only; never role/status/email).

### Events
| Method | Path | Who |
|---|---|---|
| GET | `/events/` | Public – published events. Filters: `q` (title), `category`, `location`, `event_type`, `ticket_type`, `date_from`, `date_to`, `include_past`, `sort_by` (`start_datetime`,`created_at`,`title`,`ticket_price`,`registered_count`), `order`, `page`, `page_size` |
| GET | `/events/featured` · `/events/upcoming` · `/events/categories` | Public |
| GET | `/events/{id}` | Public if published; otherwise owner or admin only (404 for others) |
| POST | `/events/` | Organizer. Starts as `draft`. |
| PUT | `/events/{id}` | Owner. Dates/capacity re-validated; capacity can't drop below registered seats. Registered attendees are notified of changes. |
| DELETE | `/events/{id}` | Owner. Hard-deletes only never-published events without registrations; otherwise cancels. |
| POST | `/events/{id}/submit` | Owner – send draft/rejected event to admin review |
| POST | `/events/{id}/cancel` | Owner – cancels, releases all registrations, notifies attendees |
| POST | `/events/{id}/report` | Any authenticated user (one report per event per user) |

### Registrations
`POST /registrations/` `{event_id}` · `GET /registrations/my` · `GET /registrations/{id}` · `DELETE /registrations/{id}` (cancel, releases the seat).

### Tickets
| Method | Path | Notes |
|---|---|---|
| GET | `/tickets/my` | Own tickets (`?status=valid|used|cancelled`) |
| GET | `/tickets/{id}` | Own ticket incl. `qr_token` |
| GET | `/tickets/{id}/qr` | PNG image of the QR code |
| POST | `/tickets/verify` | Organizer of that event only. Body `{token}` (never in the URL). Read-only. |

### Attendance
`POST /attendance/check-in` `{token}` (organizer of that event; single use, atomic) · `GET /attendance/event/{event_id}` (owner organizer or admin) · `GET /attendance/my`.

### Organizer
`GET /organizer/dashboard` · `/organizer/events` · `/organizer/events/{id}/attendees` (`?status=&q=`) · `/organizer/events/{id}/statistics` · `/organizer/profile` · `DELETE /organizer/registrations/{id}` (remove an attendee).
Statistics: total / published / upcoming events, total registrations, total attendees, attendance %, recent registrations.

### Admin (admin only)
`GET /admin/dashboard` · `/admin/users` · `/admin/organizers` · `/admin/events` ·
`PATCH /admin/events/{id}/approve | reject | publish | unpublish | feature` ·
`PATCH /admin/users/{id}/status` · `GET /admin/reports` · `PATCH /admin/reports/{id}` · `GET /admin/audit-logs`.
Every state-changing admin action is written to the audit log. Admins cannot suspend themselves.

### Reviews
`POST /reviews/` `{event_id, rating 1-5, comment?}` – only for a **completed** event the user **checked in to**; one review per user per event · `GET /reviews/event/{event_id}` (includes `average_rating`) · `DELETE /reviews/{id}` (own; admins may moderate).

### Notifications
`GET /notifications/` (`?unread_only=`) · `PATCH /notifications/{id}/read` · `PATCH /notifications/read-all`.
Types: registration confirmed/cancelled, event approved/rejected/published/updated/cancelled, 24-hour reminder. Each is stored in-app and e-mailed through the configured backend.

---

## 8. Request / response examples

**Register**
```http
POST /api/v1/auth/register
{ "email": "ayesha@example.com", "password": "Str0ng!Passw0rd", "full_name": "Ayesha Rahman", "role": "attendee" }
```
```json
{ "success": true, "message": "Account created successfully",
  "data": { "id": "6f1c…", "email": "ayesha@example.com", "full_name": "Ayesha Rahman", "role": "attendee", "status": "active" } }
```

**Login**
```http
POST /api/v1/auth/login
{ "email": "ayesha@example.com", "password": "Str0ng!Passw0rd" }
```
```json
{ "success": true, "message": "Login successful",
  "data": { "access_token": "eyJ…", "token_type": "bearer", "expires_in": 900, "csrf_token": "…", "user": { "id": "…", "role": "attendee" } } }
```

**Create event (organizer)**
```http
POST /api/v1/events/
{
  "title": "Intro to FastAPI", "description": "Build a secure REST API with FastAPI and PostgreSQL.",
  "category": "Technology", "cover_image": "https://example.com/cover.jpg",
  "start_datetime": "2026-12-01T10:00:00Z", "end_datetime": "2026-12-01T13:00:00Z",
  "registration_deadline": "2026-11-30T18:00:00Z",
  "venue": "Room 101", "location": "Dhaka", "event_type": "offline",
  "capacity": 25, "ticket_type": "free", "ticket_price": 0
}
```
Datetimes **must include a timezone** (`Z` or `+06:00`).

**Register for an event, then fetch the ticket**
```http
POST /api/v1/registrations/        { "event_id": "<uuid>" }          → 201, data.ticket.id
GET  /api/v1/tickets/<ticket_id>   → data.qr_token                    (render with the /qr PNG endpoint or any QR library)
```

**Organizer check-in**
```http
POST /api/v1/attendance/check-in   { "token": "<scanned qr_token>" }
```
```json
{ "success": true, "message": "Check-in successful",
  "data": { "ticket_id": "…", "event_id": "…", "attendee_name": "Ayesha Rahman", "checked_in_at": "2026-12-01T10:04:12Z", "checked_in_by_id": "…" } }
```
Second scan → `409 {"success": false, "message": "Ticket has already been used", "detail": {"checked_in_at": "…"}}`.

---

## 9. Frontend integration (Axios)

Set the backend origin in the Vite `.env` of the frontend: `VITE_API_URL=http://localhost:8000/api/v1`, and make sure the
frontend origin is in the backend's `FRONTEND_ORIGIN`.

```js
// src/api/client.js
import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL,   // http://localhost:8000/api/v1
  withCredentials: true,                   // send/receive the HttpOnly cookies
});

const getCookie = (name) =>
  document.cookie.split("; ").find((c) => c.startsWith(name + "="))?.split("=")[1];

// CSRF header for state-changing requests (cookie auth). Harmless with Bearer auth.
api.interceptors.request.use((config) => {
  if (["post", "put", "patch", "delete"].includes(config.method)) {
    const csrf = getCookie("csrf_token");
    if (csrf) config.headers["X-CSRF-Token"] = decodeURIComponent(csrf);
  }
  return config;
});

// One automatic refresh on 401, then retry the original request.
let refreshing = null;
api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config;
    const isAuthCall = original?.url?.startsWith("/auth/");
    if (error.response?.status === 401 && !original._retry && !isAuthCall) {
      original._retry = true;
      refreshing ??= api.post("/auth/refresh").finally(() => (refreshing = null));
      try {
        await refreshing;
        return api(original);
      } catch {
        window.location.assign("/login");
      }
    }
    return Promise.reject(error);
  }
);

export default api;
```

```js
// usage
const { data } = await api.post("/auth/login", { email, password });   // data.data.user
const events   = await api.get("/events/", { params: { q: "python", event_type: "online", page: 1, page_size: 12 } });
// events.data.data → { items, total, page, page_size, pages }

try {
  await api.post("/registrations/", { event_id });
} catch (e) {
  const body = e.response?.data;                  // { success:false, message, detail }
  if (e.response?.status === 422) body.detail.forEach((d) => console.log(d.field, d.message));
  else console.log(body.message);
}
```
**Cross-site deployments** (frontend and API on different registrable domains): set `COOKIE_SAMESITE=none` (requires HTTPS) –
or use the Bearer token from the login response (`Authorization: Bearer …`) and keep it in memory.

---

## 10. Design notes

**Overbooking protection** (`registration_service.py`). One transaction: (1) a single conditional
`UPDATE events SET registered_count = registered_count + 1 WHERE status='published' AND registered_count < capacity AND deadline open`
– PostgreSQL row-locks the event so concurrent requests are serialised and each re-evaluates the condition; (2) insert the registration, guarded by a
**partial unique index** `(user_id, event_id) WHERE status='confirmed'` (a violation rolls back the seat claim too); (3) insert the ticket.
`CHECK (registered_count <= capacity)` is a final backstop. Cancellation flips ticket → registration → releases the seat, each as an atomic conditional update.

**Tickets.** The QR holds only `<ticket-id-hex>.<HMAC-SHA256 signature>`; the HMAC covers a per-ticket random nonce stored in the DB and a server-side key.
It contains no personal data, and a database ID alone is useless as a credential. Verification is server-side, in constant time, restricted to the organiser of the event,
and rejects used / cancelled / expired / invalid tickets. Check-in is a single atomic `UPDATE … WHERE status='valid' RETURNING`, so two simultaneous scans can't both succeed;
it records the timestamp and the organiser. Tokens travel in request bodies (never URLs) and are never logged. A DB `CHECK` keeps `status='used'` and `checked_in_at` consistent.

**Database.** UUID primary keys, FKs with explicit cascade rules, timezone-aware timestamps, unique case-insensitive e-mail index, CHECK constraints for roles/status/capacity/dates/rating,
indexes on the common filters. Schema changes go through Alembic only.

---

## 11. Security

**Implemented**

| Area | Measure |
|---|---|
| Passwords | Argon2id (library defaults, auto-rehash), strength policy, timing equalisation for unknown e-mails |
| Tokens | Pinned-algorithm JWT with required `exp`/`iat`/`sub`/`type`; hashed, rotating, revocable refresh tokens with reuse detection |
| AuthZ | Central role→permission matrix + per-resource ownership checks; role/status re-read from DB each request; 404 for other users' resources (IDOR) |
| Mass assignment | Request models reject unknown fields (`extra="forbid"`) – a client cannot send `role`, `organizer_id`, `status`, `registered_count` |
| Escalation | No endpoint changes roles; `admin` can't be self-registered; admins created only by CLI; audit-logged |
| Input | Pydantic v2 validation everywhere; SQLAlchemy parameterised queries only (no raw SQL with user input) |
| Browser | Strict CORS allow-list (wildcard rejected), CSRF double-submit + Origin check for cookie auth, HttpOnly/Secure/SameSite cookies, security headers (CSP `default-src 'none'`, nosniff, frame deny, HSTS in production, `no-store`) |
| Abuse | Per-IP rate limits (login, forgot/reset/change-password, ticket verify, check-in, registration, global), account lockout, request-size limit (default 1 MiB) |
| Errors | Uniform envelope; unhandled errors return a generic 500 and are logged server-side only; validation errors never echo input |
| Secrets | Everything from environment variables; app refuses to start with placeholder/short JWT secret, wildcard CORS, insecure production cookies/origins |
| Audit | Admin and sensitive actions recorded in `audit_logs` |
| Uploads | Not implemented: `cover_image` is a validated http(s) URL, so there is no upload attack surface |

**Assumptions**
* TLS is terminated in front of the app (reverse proxy / platform) and the DB is not publicly reachable.
* The host, the `.env` file and the DB credentials are protected; the first admin's password is chosen securely.
* `TRUST_PROXY_HEADERS=true` is set only behind a proxy that overwrites `X-Forwarded-For`.

**Remaining risks / not done** (no system is "unhackable"; these are the known gaps)
* **Rate limiting and lockout state:** the limiter is in-process memory – with several workers each has its own counters. Use a shared store (Redis) or enforce limits at the proxy for multi-instance deployments.
* **Payments:** `paid` events record price and registration amount but there is **no payment gateway**; paid registrations are not charged.
* **Access tokens** cannot be revoked before expiry (15 min); suspension is effective immediately because status is re-read on each request, but a stolen token of an *active* user stays valid until it expires.
* **E-mail:** SMTP delivery is fire-and-forget in a background task (no retry queue); reminders run in an in-process loop.
* **Image upload / CDN** is not implemented (URLs only).
* No account e-mail verification step on registration, and no MFA.
* Dependency vulnerabilities must be re-checked regularly (`pip-audit`).
* The Argon2id parameters are library defaults; benchmark and tune on production hardware.

---

## 12. Production deployment

* `ENVIRONMENT=production`: enables `Secure` cookies and HSTS, requires `https://` origins, disables `/docs`, `/redoc` and `/openapi.json`, and forbids `EMAIL_BACKEND=console`.
* Serve behind HTTPS (nginx/Caddy/cloud LB). Set `TRUST_PROXY_HEADERS=true` only if that proxy sets `X-Forwarded-For`.
* Run migrations as a deploy step: `alembic upgrade head` (never rely on table auto-creation).
* Run with several workers: `gunicorn -k uvicorn.workers.UvicornWorker -w 4 app.main:app` – and move rate limiting to Redis/the proxy first (see risks).
* Use a dedicated DB role with the minimum privileges; enable DB backups and TLS to the database.
* Keep secrets in the platform's secret store, rotate `JWT_SECRET_KEY` deliberately (it invalidates all sessions), and set `FRONTEND_ORIGIN` to the exact production origin(s).
* Configure SMTP (`EMAIL_BACKEND=smtp`, `SMTP_*`); monitor logs; run `pip-audit` in CI.
