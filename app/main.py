import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.agents import router as agents_router
from app.api.routes.ask import router as ask_router
from app.api.routes.backup import router as backup_router
from app.api.routes.chat import router as chat_router
from app.api.routes.autonomy import router as autonomy_router
from app.api.routes.delegations import router as delegations_router
from app.api.routes.memory import router as memory_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.workflows import router as workflows_router
from app.api.routes.pairing import router as pairing_router
from app.api.routes.settings import router as settings_router
from app.auth import OperatorAuthMiddleware

async def _scheduler_loop() -> None:
    """Background tick: due schedules fire, events drain to triggers.

    Interval from NEXUS_SCHEDULER_INTERVAL (seconds, default 60).
    Everything is guarded: a tick failure logs and retries next
    interval, never crashing the server. Each tick opens fresh
    connections (never shares request state).
    """
    from app import autonomy
    from app.agent.context import db_path
    from app.store import migrate, open_db

    log = logging.getLogger("nexus.scheduler")
    while True:
        try:
            interval = int(
                os.environ.get("NEXUS_SCHEDULER_INTERVAL", "60") or "60")
        except ValueError:
            interval = 60
        await asyncio.sleep(max(15, interval))
        try:
            conn = open_db(db_path())
            migrate(conn)
            try:
                fired = await autonomy.scheduler_tick(conn)
                if fired:
                    log.info("scheduler fired %d task(s)", len(fired))
                await autonomy.process_events(conn)
            finally:
                conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("scheduler tick failed: %s: %s",
                        type(exc).__name__, str(exc)[:200])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: reconcile crash leftovers, recover workflows, schedule.

    Restart recovery lives here so no in-memory state is ever required:
    timed-out tasks fail, worker-gone RUNNING rows fail, expired
    approvals close, and interrupted workflows return to PENDING for an
    explicit resume. Nothing auto-executes on boot.
    """
    log = logging.getLogger("nexus.startup")
    try:
        from app import tasks as task_tracker
        from app import workflows
        from app.agent.context import db_path
        from app.store import migrate, open_db

        conn = open_db(db_path())
        migrate(conn)
        try:
            report = task_tracker.reconcile_tasks(conn)
            if report["failed"]:
                log.info("startup reconciled %d task(s)",
                         len(report["failed"]))
            recovered = workflows.recover_workflows(
                lambda: _fresh_conn())
            if recovered:
                log.info("startup recovered %d workflow(s)",
                         len(recovered))
        finally:
            conn.close()
    except Exception as exc:
        log.warning("startup recovery failed: %s: %s",
                    type(exc).__name__, str(exc)[:200])
    scheduler_task = None
    if os.environ.get("NEXUS_SCHEDULER", "on").strip().lower() not in (
            "0", "off", "false", "no"):
        scheduler_task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()


def _fresh_conn():
    from app.agent.context import db_path
    from app.store import migrate, open_db

    conn = open_db(db_path())
    migrate(conn)
    return conn


app = FastAPI(lifespan=lifespan)


def configure_logging() -> None:
    """One stdlib handler when nothing configured yet (uvicorn's own
    setup wins when it runs first). No secrets ever pass through here —
    log callsites must pass ids/codes, never tokens or keys."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )


configure_logging()


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Accept or mint ``X-Request-ID``; echo it back for tracing logs."""
    request_id = request.headers.get("x-request-id", "").strip() or (
        f"req_{uuid.uuid4().hex[:12]}"
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


#: MVP request-size bound (5 MiB). Prevents accidental/giant uploads
#: from exhausting the single-process server. Reads Content-Length
#: only — chunked bodies are still bounded by route-level validation.
MAX_REQUEST_BYTES = int(os.environ.get("NEXUS_MAX_REQUEST_BYTES", "") or 5 * 1024 * 1024)


@app.middleware("http")
async def request_size_limit_middleware(request: Request, call_next):
    """Reject oversized bodies with 413 before routing."""
    try:
        length = int(request.headers.get("content-length", "") or "0")
    except ValueError:
        length = 0
    if length > MAX_REQUEST_BYTES:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=413,
            content={
                "detail": f"Request body too large ({length} bytes, max {MAX_REQUEST_BYTES}).",
                "code": "PAYLOAD_TOO_LARGE",
            },
        )
    return await call_next(request)

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]


def _cors_origins() -> list[str]:
    """Explicit origins, environment-driven (dev vs prod differ here)."""
    raw = os.environ.get("NEXUS_CORS_ORIGINS", "").strip()
    if not raw:
        return list(_DEFAULT_CORS_ORIGINS)
    return [part.strip() for part in raw.split(",") if part.strip()]


# The local frontend(s) call cross-origin in dev; same-origin production
# deployments should narrow NEXUS_CORS_ORIGINS accordingly.
# Localhost regex covers any dev port (3000/3001/3002/3003/…) so the UI
# never breaks just because the default port was taken. External origins
# are still rejected.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Auth runs inside CORS (preflights are exempt in the middleware itself),
# so browsers can negotiate before presenting the bearer token.
app.add_middleware(OperatorAuthMiddleware)
app.include_router(pairing_router)
app.include_router(agents_router)
app.include_router(ask_router)
app.include_router(backup_router)
app.include_router(chat_router)
app.include_router(autonomy_router)
app.include_router(delegations_router)
app.include_router(memory_router)
app.include_router(tasks_router)
app.include_router(workflows_router)
app.include_router(settings_router)


@app.get("/health")
def health():
    return {"status": "ok"}
