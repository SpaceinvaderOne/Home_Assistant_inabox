"""The wizard's HTTP surface."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.installer import TERMINAL_STEP
from app.libvirtctl import Virsh
from app.web import routes_firstboot as rf
from app.web import routes_install as ri
from app.web import routes_status as rs
from app.web import routes_wizard as rw
from app.web import state_store, webui_registry
from app.web import watcher as wt
from app.web.events import EventBus, format_line, sse_frame
from app.web.session import ValidationError
from app.web.steps import STATUS_STEP
from app.web.steps import STEPS as WIZARD_STEPS

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

STEPS = [(s.key, s.label) for s in WIZARD_STEPS]

_STEPS_BY_KEY = {s.key: s for s in WIZARD_STEPS}
_STEPS_BY_KEY[STATUS_STEP.key] = STATUS_STEP


def _apply_step(key: str, form) -> tuple[str, dict | None]:
    """Validate and store one step's form, returning which step to render."""
    if key == "version":
        rw.apply_version(form)
        return rw.next_step_key(key), None

    if key == "placement":
        try:
            needs_confirmation = rw.apply_placement(form)
        except ValidationError as err:
            return key, rw.placement_context(
                name=form.get("name", ""), errors={err.field_name: str(err)}
            )
        if needs_confirmation:
            return key, rw.placement_context()
        return rw.next_step_key(key), None

    if key == "resources":
        try:
            needs_confirmation = rw.apply_resources(form)
        except ValidationError as err:
            return key, rw.resources_context(
                vcpus=form.get("vcpus", ""),
                memory_mib=form.get("memory", ""),
                size_gib=form.get("disk", ""),
                errors={err.field_name: str(err)},
            )
        if needs_confirmation:
            return key, rw.resources_context()
        return rw.next_step_key(key), None

    if key == "usb":
        rw.apply_usb(form)
        return rw.next_step_key(key), None

    # welcome, review, installing, firstboot and ready have no form of their
    # own (yet).
    return key, None


def create_app(*, start_watcher: bool = False, state_path: str | Path | None = None) -> FastAPI:
    bus = EventBus()

    if state_path is not None:
        restored = state_store.enable(state_path)
        for name, watch_enabled in restored.watch_enabled.items():
            if watch_enabled:
                wt.WATCHER.set_enabled(name, True)
        if restored.interval_minutes is not None:
            wt.WATCHER.set_interval(restored.interval_minutes)
        for name, url in restored.webui.items():
            webui_registry.remember(name, url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bus.bind_loop(asyncio.get_running_loop())
        watcher_task = (
            asyncio.create_task(wt.WATCHER.run_forever(bus.publish)) if start_watcher else None
        )
        try:
            yield
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher_task

    app = FastAPI(title="HomeAssistant_inabox", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.bus = bus
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def render_error_page(
        request: Request, *, full_page: bool, current: str, action: str, err: BaseException
    ) -> HTMLResponse:
        """The one page a failure ever becomes: a readable explanation, never
        FastAPI's bare, empty 500. Shared by every route below that wraps
        third-party or host-facing calls in a BaseException boundary, so the
        three call sites can't drift into showing different things for the
        same kind of failure.
        """
        error_context = {
            "action": action,
            "error_type": type(err).__name__,
            "error_message": str(err),
        }
        if full_page:
            error_context = {
                **error_context,
                "steps": STEPS,
                "current": current,
                "content_template": "_step_error.html",
                "install_busy": ri.RUNNER.busy(),
            }
            return TEMPLATES.TemplateResponse(
                request=request, name="base.html", context=error_context
            )
        return TEMPLATES.TemplateResponse(
            request=request, name="_step_error.html", context=error_context
        )

    def render_step(
        request: Request,
        key: str,
        *,
        full_page: bool,
        context: dict | None = None,
        content_template: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render one wizard step."""
        current = _STEPS_BY_KEY[key]
        try:
            if context is None:
                if key == "welcome":
                    context = rw.welcome_context(Virsh())
                elif key == "status":
                    context = rs.status_context(rw._discover(Virsh()))
                elif key == "version":
                    context = rw.version_context()
                elif key == "placement":
                    context = rw.placement_context()
                elif key == "resources":
                    context = rw.resources_context()
                elif key == "usb":
                    context = rw.usb_context()
                elif key == "review":
                    context = ri.review_context()
                    if rf.resume_step_key() is not None:
                        context = {**context, **rf.watch_conflict_context()}
                elif key == "installing":
                    context = ri.installing_context()
                elif key == "firstboot":
                    context = rf.firstboot_context()
                elif key == "ready":
                    context = rf.ready_context()
                else:
                    context = {}

            order = [step_key for step_key, _ in STEPS]
            done = set(order[: order.index(key)]) if key in order else set()
            context = {
                **context,
                "steps": STEPS,
                "current": key,
                "done": done,
                "content_template": content_template or current.template,
                "install_busy": ri.RUNNER.busy(),
            }

            if full_page:
                return TEMPLATES.TemplateResponse(
                    request=request, name="base.html", context=context, status_code=status_code
                )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="_fragment.html",
                context={**context, "rail_oob": True},
                status_code=status_code,
            )
        except (KeyboardInterrupt, SystemExit):
            # Both must still terminate the process rather than be swallowed into
            # a rendered page -- that would be a worse bug than the one below.
            raise
        except BaseException as err:
            action = (
                "checking your server"
                if key == "welcome"
                else f"loading the {current.label.lower()} step"
            )
            return render_error_page(
                request, full_page=full_page, current=key, action=action, err=err
            )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        landing = rf.home_resume_key()
        if landing is not None:
            return render_step(request, landing, full_page=True)
        if ri.RUNNER.busy():
            return render_step(request, "installing", full_page=True)

        try:
            key, context = rs.home_context(Virsh())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request, full_page=True, current="welcome", action="checking your server", err=err
            )
        return render_step(request, key, full_page=True, context=context)

    @app.get("/step/{key}", response_class=HTMLResponse)
    def step_fragment(key: str, request: Request) -> HTMLResponse:
        if key not in _STEPS_BY_KEY:
            raise HTTPException(status_code=404, detail=f"unknown step: {key!r}")

        is_htmx = request.headers.get("HX-Request") == "true"
        return render_step(request, key, full_page=not is_htmx)

    @app.post("/step/{key}", response_class=HTMLResponse)
    async def step_submit(key: str, request: Request) -> HTMLResponse:
        if key not in _STEPS_BY_KEY:
            raise HTTPException(status_code=404, detail=f"unknown step: {key!r}")

        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx
        current = _STEPS_BY_KEY[key]

        try:
            form = await request.form()
            target_key, context = _apply_step(key, form)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=full_page,
                current=key,
                action=f"saving the {current.label.lower()} step",
                err=err,
            )

        return render_step(request, target_key, full_page=full_page, context=context)

    @app.post("/install", response_class=HTMLResponse)
    async def start_install(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx

        if ri.RUNNER.busy():
            return render_step(
                request,
                "installing",
                full_page=full_page,
                context={},
                content_template="_install_busy.html",
                status_code=409,
            )

        conflict = rf.resume_step_key()
        if conflict is not None:
            return render_step(
                request,
                conflict,
                full_page=full_page,
                context=rf.watch_conflict_context(),
                content_template="_install_watching.html",
                status_code=409,
            )

        try:
            plan = ri._current_plan()

            def emit_and_advance(event):
                bus.publish(event)
                if event.step == TERMINAL_STEP and event.status == "ok":
                    rf.WATCHER.start(plan.settings.name, plan.settings.mac, bus.publish)
                    wt.WATCHER.set_enabled(plan.settings.name, True)

            ri.RUNNER.start(plan, ri.RealDeps(Virsh()), emit_and_advance)
            rf.WATCHER.reset()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=full_page,
                current="review",
                action="starting the install",
                err=err,
            )

        return render_step(
            request, "installing", full_page=full_page, context=ri.installing_context()
        )

    @app.post("/another-vm", response_class=HTMLResponse)
    def another_vm(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx
        if ri.RUNNER.busy():
            return render_step(
                request,
                "installing",
                full_page=full_page,
                context={},
                content_template="_install_busy.html",
                status_code=409,
            )
        rf.start_another()
        return render_step(request, "welcome", full_page=full_page)

    @app.post("/vm/start", response_class=HTMLResponse)
    async def start_vm(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx
        form = await request.form()
        name = (form.get("name") or "").strip()
        try:
            found = rs.start_vm(Virsh(), name)
            context = rs.status_context(found)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request, full_page=full_page, current="status", action="starting that VM", err=err
            )
        return render_step(request, "status", full_page=full_page, context=context)

    @app.post("/vm/watch", response_class=HTMLResponse)
    async def watch_vm(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx
        form = await request.form()
        name = (form.get("name") or "").strip()
        enabled = "enabled" in form
        try:
            found = rs.set_watch(Virsh(), name, enabled)
            context = rs.status_context(found)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=full_page,
                current="status",
                action="changing whether that VM is watched",
                err=err,
            )
        return render_step(request, "status", full_page=full_page, context=context)

    @app.post("/watcher/interval", response_class=HTMLResponse)
    async def watcher_interval(request: Request) -> HTMLResponse:
        is_htmx = request.headers.get("HX-Request") == "true"
        full_page = not is_htmx
        form = await request.form()
        try:
            minutes = int(form.get("minutes") or "")
        except ValueError:
            minutes = None
        try:
            if minutes is not None:
                rs.set_watcher_interval(minutes)
            context = rs.status_context(rw._discover(Virsh()))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=full_page,
                current="status",
                action="changing how often the watcher checks",
                err=err,
            )
        return render_step(request, "status", full_page=full_page, context=context)

    @app.get("/install/state")
    def install_state() -> dict:
        return ri.RUNNER.state()

    @app.get("/firstboot/state", response_class=HTMLResponse)
    def firstboot_state(request: Request) -> HTMLResponse:
        try:
            context = rf.firstboot_context()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=False,
                current="firstboot",
                action="checking on first boot",
                err=err,
            )
        return TEMPLATES.TemplateResponse(
            request=request, name="_firstboot_milestones.html", context=context
        )

    @app.get("/ready/state", response_class=HTMLResponse)
    def ready_state(request: Request) -> HTMLResponse:
        try:
            context = rf.ready_context()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as err:
            return render_error_page(
                request,
                full_page=False,
                current="ready",
                action="checking on Home Assistant's onboarding",
                err=err,
            )
        return TEMPLATES.TemplateResponse(request=request, name="_ready_live.html", context=context)

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            for event in bus.history():
                yield sse_frame(format_line(event))
            async for event in bus.subscribe():
                yield sse_frame(format_line(event))

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
