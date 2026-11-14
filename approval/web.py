"""The approver web app: server-rendered pages, no JavaScript, strict CSP.

Routes
  GET  /login, POST /login, POST /logout
  GET  /drafts?status=pending&page=1        list (paginated)
  GET  /drafts/{id}                         email and proposed invoice side by side, audit trail
  POST /drafts/{id}/edit|approve|reject     approvers only; CSRF token and draft version required
  GET  /healthz, /readyz
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from approval import __version__
from approval.auth import Auth, LoginThrottled, Session
from approval.catalogue import Catalogue
from approval.config import Settings
from approval.db import Database, parse_iso, utc_now
from approval.drafts import STATUSES, DraftError, DraftService
from approval.posting import GovernedWriteApiPoster, LocalPoster, Poster, WriteApiClient

log = logging.getLogger("approval")
HERE = Path(__file__).resolve().parent

CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
MESSAGES = {  # fixed texts only: nothing from the query string is ever echoed
    "posted": "Approved and posted under your name.",
    "post_failed": "Approved, but posting failed. It's safe to retry: it will never post twice.",
    "edited": "Draft updated. Check it again before approving.",
    "rejected": "Draft rejected.",
    "signed_out": "You're signed out.",
}


class LoginRequired(Exception):
    pass


class RateLimiter:
    """Fixed one-minute window per client IP, in this process. Behind a load balancer, use its limits
    (or Redis) as well: this one counts per instance."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self._lock = threading.Lock()
        self._window = 0
        self._counts: dict[str, int] = {}

    def allow(self, key: str) -> bool:
        window = int(self.clock() // 60)
        with self._lock:
            if window != self._window:
                self._window, self._counts = window, {}
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key] <= self.per_minute


@dataclass
class AppState:
    settings: Settings
    db: Database
    catalogue: Catalogue
    auth: Auth
    drafts: DraftService
    limiter: RateLimiter


def build_poster(settings: Settings, db: Database, clock=utc_now) -> Poster:
    if settings.post_target == "governed-write-api":
        client = WriteApiClient(
            settings.writeapi_url, settings.writeapi_key_id, settings.writeapi_secret, settings.writeapi_timeout_seconds
        )
        return GovernedWriteApiPoster(client, settings.writeapi_due_in_days)
    return LocalPoster(db, clock)


def create_app(
    settings: Settings | None = None, db: Database | None = None, poster: Poster | None = None, clock=utc_now
) -> FastAPI:
    settings = settings or Settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = db or Database(settings.database_path)
    if settings.migrate_on_start:
        db.migrate()
    if not settings.approvers:
        log.warning("APPROVERS is empty: nobody can sign in (run: python scripts/make_env.py)")
    if not settings.cookie_secure:
        log.warning("COOKIE_SECURE=false: cookies are sent over plain HTTP. Use only on 127.0.0.1")
    catalogue = Catalogue(db)
    state = AppState(
        settings,
        db,
        catalogue,
        Auth(settings, db, clock),
        DraftService(settings, db, catalogue, poster or build_poster(settings, db, clock), clock),
        RateLimiter(settings.rate_limit_per_minute),
    )

    app = FastAPI(title="agent-approval-flow", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.approval = state
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")  # autoescape is on for .html
    secure = settings.cookie_secure
    session_cookie = "__Host-aaf_session" if secure else "aaf_session"
    login_cookie = "__Host-aaf_login" if secure else "aaf_login"

    def remaining(expires_at: str) -> str:
        left = parse_iso(expires_at) - clock()
        if left.total_seconds() <= 0:
            return "expired"
        hours, rem = divmod(int(left.total_seconds()), 3600)
        return f"{hours}h {rem // 60:02d}m"

    templates.env.globals["remaining"] = remaining

    def client_ip(request: Request) -> str:
        if settings.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[-1].strip()[:64]
        return request.client.host if request.client else "unknown"

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"version": __version__, **ctx}, status_code=status_code)

    def session_of(request: Request) -> Session:
        s = state.auth.session(request.cookies.get(session_cookie))
        if s is None:
            raise LoginRequired()
        return s

    def checked(request: Request, csrf: str) -> Session:
        s = session_of(request)
        if not s.csrf_ok(csrf):
            raise StarletteHTTPException(403, "Your form expired. Go back, reload the page and try again.")
        return s

    # --- middleware and errors ------------------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path not in ("/healthz", "/readyz") and not state.limiter.allow(client_ip(request)):
            response: Response = HTMLResponse("Too many requests. Wait a minute.", status_code=429)
        elif request.method == "POST" and int(request.headers.get("content-length") or 0) > settings.max_body_bytes:
            response = HTMLResponse("Request too large.", status_code=413)
        else:
            response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        if not request.url.path.startswith("/static"):
            h["Cache-Control"] = "no-store"
        if secure:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        message = (
            exc.detail if exc.status_code in (403, 404) and isinstance(exc.detail, str) else "Something went wrong."
        )
        if exc.status_code == 404 and exc.detail == "Not Found":
            message = "That page doesn't exist."
        return page(request, "error.html", exc.status_code, message=message, user=None)

    @app.exception_handler(RequestValidationError)
    async def bad_form(request: Request, exc: RequestValidationError):
        return page(request, "error.html", 422, message="That request wasn't valid.", user=None)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):  # pragma: no cover - last resort
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return page(request, "error.html", 500, message="Something went wrong. It has been logged.", user=None)

    # --- health -----------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        ready = state.db.ready()
        return JSONResponse({"status": "ready" if ready else "not_ready", "database": ready}, 200 if ready else 503)

    # --- sign-in ----------------------------------------------------------------------------------

    def login_page(request: Request, status: int = 200, error: str | None = None) -> HTMLResponse:
        token = secrets.token_urlsafe(32)
        message = MESSAGES.get(request.query_params.get("m", "")) if status == 200 else None
        response = page(request, "login.html", status, csrf=token, error=error, message=message, user=None)
        response.set_cookie(
            login_cookie, token, max_age=1800, httponly=True, secure=secure, samesite="strict", path="/"
        )
        return response

    @app.get("/login")
    def login_form(request: Request):
        if state.auth.session(request.cookies.get(session_cookie)):
            return RedirectResponse("/drafts", status_code=303)
        return login_page(request)

    @app.post("/login")
    def login(
        request: Request,
        username: str = Form(max_length=64),
        password: str = Form(max_length=256),
        csrf: str = Form(max_length=100),
    ):
        expected = request.cookies.get(login_cookie, "")
        if not (expected and secrets.compare_digest(expected, csrf)):
            return login_page(request, 403, "Your sign-in form expired. Try again.")
        ip = client_ip(request)
        try:
            result = state.auth.login(username, password, ip)
        except LoginThrottled:
            log.warning("sign-in throttled ip=%s", ip)
            return login_page(request, 429, "Too many failed sign-ins. Wait a few minutes and try again.")
        if result is None:
            log.info("sign-in failed ip=%s", ip)  # never the password; not even the username typed
            return login_page(request, 401, "Wrong username or password.")
        token, s = result
        log.info("signed in user=%s", s.user.username)
        response = RedirectResponse("/drafts", status_code=303)
        response.set_cookie(
            session_cookie,
            token,
            max_age=settings.session_ttl_minutes * 60,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/",
        )
        response.delete_cookie(login_cookie, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form(max_length=100)):
        checked(request, csrf)
        state.auth.logout(request.cookies.get(session_cookie))
        response = RedirectResponse("/login?m=signed_out", status_code=303)
        response.delete_cookie(session_cookie, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    # --- drafts -----------------------------------------------------------------------------------

    @app.get("/")
    def home():
        return RedirectResponse("/drafts", status_code=303)

    @app.get("/drafts")
    def list_drafts(request: Request, status: str = "pending", page_no: int = 1):
        s = session_of(request)
        state.drafts.sweep_expired()
        status = status if status in (*STATUSES, "all") else "pending"
        rows, has_next = state.drafts.list(None if status == "all" else status, page_no)
        return page(
            request,
            "drafts.html",
            user=s.user,
            csrf=s.csrf_token,
            rows=rows,
            status=status,
            statuses=("pending", "post_failed", "posted", "rejected", "expired", "all"),
            page_no=max(1, page_no),
            has_next=has_next,
        )

    def draft_page(request: Request, s: Session, draft_id: int, status: int = 200, error: str | None = None):
        d = state.drafts.get(draft_id)
        return page(
            request,
            "draft.html",
            status,
            user=s.user,
            csrf=s.csrf_token,
            d=d,
            blockers=state.drafts.blockers(d, s.user) if d["status"] in ("pending", "post_failed") else [],
            trail=state.drafts.audit_trail(draft_id),
            customers=state.catalogue.customers(),
            products=state.catalogue.products(),
            spare_rows=range(3),
            settings=settings,
            names={u: a.name for u, a in settings.approvers.items()},
            error=error,
            message=MESSAGES.get(request.query_params.get("m", "")),
        )

    @app.get("/drafts/{draft_id}")
    def show_draft(request: Request, draft_id: int):
        s = session_of(request)
        state.drafts.sweep_expired(draft_id)
        try:
            return draft_page(request, s, draft_id)
        except DraftError as exc:
            raise StarletteHTTPException(exc.status, exc.message) from None

    def act(request: Request, s: Session, draft_id: int, fn, done_message: str):
        try:
            result = fn()
        except DraftError as exc:
            if exc.status == 404:
                raise StarletteHTTPException(404, exc.message) from None
            if exc.status == 403:
                raise StarletteHTTPException(403, exc.message) from None
            return draft_page(request, s, draft_id, exc.status, exc.message)
        if isinstance(result, dict) and result.get("status") == "post_failed":
            done_message = "post_failed"
        return RedirectResponse(f"/drafts/{draft_id}?m={done_message}", status_code=303)

    @app.post("/drafts/{draft_id}/edit")
    def edit_draft(
        request: Request,
        draft_id: int,
        version: int = Form(),
        csrf: str = Form(max_length=100),
        customer_id: str = Form(default="", max_length=12),
        sku: list[str] = Form(default=[]),
        qty: list[str] = Form(default=[]),
    ):
        s = checked(request, csrf)

        def run():
            cid = int(customer_id) if customer_id.strip().isdigit() else None
            items = []
            for code, amount in zip(sku[:200], qty[:200], strict=False):
                code, amount = code.strip()[:40], amount.strip()[:12]
                if not code or amount in ("", "0"):
                    continue  # an empty row, or a line set to 0 to remove it
                if not amount.isdigit():
                    raise DraftError("bad_quantity", f"Quantity for {code} must be a whole number.", 422)
                items.append((code, int(amount)))
            state.drafts.edit(draft_id, version, s.user, cid, items)

        return act(request, s, draft_id, run, "edited")

    @app.post("/drafts/{draft_id}/approve")
    def approve_draft(request: Request, draft_id: int, version: int = Form(), csrf: str = Form(max_length=100)):
        s = checked(request, csrf)
        return act(request, s, draft_id, lambda: state.drafts.approve(draft_id, version, s.user), "posted")

    @app.post("/drafts/{draft_id}/reject")
    def reject_draft(
        request: Request,
        draft_id: int,
        version: int = Form(),
        csrf: str = Form(max_length=100),
        reason: str = Form(default="", max_length=1000),
    ):
        s = checked(request, csrf)
        return act(request, s, draft_id, lambda: state.drafts.reject(draft_id, version, s.user, reason), "rejected")

    return app
