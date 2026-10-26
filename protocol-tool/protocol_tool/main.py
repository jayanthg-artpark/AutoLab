"""Protocol tool web server: the scientist UI, the planner services (AI chat, simulation,
workflow), approval, and the link to the master node."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .analysis import analyse_run, analysis_csv
from .assistant import Assistant, Attachment, ChatBusy, ModelClient, ModelUnavailable
from .knowledge import Knowledge
from .model_status import ModelChecker, ModelError, build_checker
from .providers import resolve
from .config import Settings, load_settings
from .master import MasterClient, MasterError, MasterUnavailable
from .models import ChatEntry, DesignPatch, SendRequest, Session
from .simulator import DeckRules, SimulatorUnavailable, simulate
from .store import SessionStore
from .workflow import WorkflowError, build_submission, plan_hash, slug

INSTRUMENTS_TTL_S = 30.0
FINISHED = {"succeeded", "failed", "aborted", "interrupted"}

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
KEEPALIVE_S = 15.0  # seconds of silence before the chat stream sends a keep-alive
UPLOAD_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp",
    "text/plain", "text/csv", "text/markdown", "text/x-python", "application/json",
}
TEXT_EXTENSIONS = {".txt": "text/plain", ".csv": "text/csv", ".md": "text/markdown", ".py": "text/x-python", ".json": "application/json"}


def create_app(
    settings: Settings | None = None,
    model: ModelClient | None = None,
    master: MasterClient | None = None,
    checker: ModelChecker | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = SessionStore(settings.server.database)
    provider = resolve(settings.assistant)
    # With a substitute model (tests, demos) there is no account to check.
    if checker is None and model is None:
        checker = build_checker(provider)
    master = master or MasterClient(settings.master)

    knowledge = Knowledge(settings.knowledge.folder)

    async def tip_racks_on_deck() -> list[dict[str, Any]]:
        try:
            response = await master.request("GET", "/tips")
            return response.json().get("racks", []) if response.is_success else []
        except (MasterUnavailable, ValueError):
            return []

    registry_cache: dict[str, Any] = {"at": 0.0, "instruments": None}

    async def lab_instruments() -> list[dict[str, Any]] | None:
        """The master node's instrument registry; None if it can't be reached."""
        if time.monotonic() - registry_cache["at"] < INSTRUMENTS_TTL_S and registry_cache["instruments"] is not None:
            return registry_cache["instruments"]
        try:
            response = await master.request("GET", "/instruments")
            instruments = response.json().get("instruments") if response.is_success else None
        except (MasterUnavailable, ValueError):
            instruments = None
        registry_cache.update(at=time.monotonic(), instruments=instruments)
        return instruments

    async def preview_plan(submission: dict[str, Any]) -> tuple[str, Any]:
        """The master node's plan for a submission, without queuing it: ("ok", plan),
        ("rejected", reason) or ("unavailable", reason)."""
        try:
            response = await master.request("POST", "/plan", json=submission)
        except MasterUnavailable as exc:
            return "unavailable", str(exc)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}
        if response.is_success:
            return "ok", body
        if response.status_code == 422:
            detail = body.get("detail")
            return "rejected", detail if isinstance(detail, str) else json.dumps(detail)[:1000]
        return "unavailable", f"master node returned {response.status_code}: {str(body)[:300]}"

    assistant = Assistant(
        settings, store, model, checker, tip_racks=tip_racks_on_deck,
        instruments=lab_instruments, preview_plan=preview_plan, knowledge=knowledge,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Log whether Claude is usable without delaying startup.
        startup_check = asyncio.create_task(checker.log_startup()) if checker else None
        # Index the knowledge folder in the background, so the first lookup doesn't wait for it.
        warm_knowledge = asyncio.create_task(asyncio.to_thread(knowledge.documents))
        yield
        warm_knowledge.cancel()
        if startup_check:
            startup_check.cancel()
        await master.aclose()
        store.close()

    app = FastAPI(title="Protocol tool", version=__version__, lifespan=lifespan)
    if settings.server.access_password:
        app.add_middleware(PasswordGate, password=settings.server.access_password)

    def get_session(session_id: str) -> Session:
        session = store.get(session_id)
        if session is None:
            raise HTTPException(404, "Design session not found")
        return session

    def require_idle(session: Session) -> None:
        if assistant.is_busy(session.id):
            raise HTTPException(409, "The assistant is still working on this design; wait for the reply to finish")

    # --- Design sessions -------------------------------------------------------

    @app.post("/api/sessions", status_code=201)
    async def new_session() -> dict[str, Any]:
        session = Session()
        store.save(session)
        return session.view()

    @app.get("/api/sessions/{session_id}")
    async def read_session(session_id: str) -> dict[str, Any]:
        return get_session(session_id).view()

    @app.patch("/api/sessions/{session_id}/design")
    async def edit_design(session_id: str, patch: DesignPatch) -> dict[str, Any]:
        session = get_session(session_id)
        if patch.reader is not None:
            design = session.design
            step = design.step(patch.step_id) if patch.step_id else next(iter(design.read_steps()), None)
            if step is None or step.type != "read":
                raise HTTPException(404, "No such read step in the workflow")
            step.settings = patch.reader
        store.save(session)
        return session.view()

    @app.post("/api/sessions/{session_id}/chat")
    async def chat(
        session_id: str,
        message: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ) -> StreamingResponse:
        session = get_session(session_id)
        require_idle(session)
        attachments = [await _read_upload(f, settings.assistant.max_upload_mb) for f in files]
        if not message.strip() and not attachments:
            raise HTTPException(422, "Write a message or attach a file")

        async def events():
            # The turn runs in its own task so the stream can send keep-alives while the
            # model or simulator is slow; proxies such as Cloudflare drop silent connections.
            queue: asyncio.Queue = asyncio.Queue()

            async def pump():
                try:
                    async for event in assistant.chat(session, message, attachments):
                        await queue.put(event)
                except ChatBusy:
                    await queue.put({"type": "error", "message": "The assistant is already replying"})
                finally:
                    await queue.put(None)

            task = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                if not task.done():
                    task.cancel()  # the browser left; the turn saves what it has

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{session_id}/simulate")
    async def run_simulation(session_id: str, protocol: str = "") -> dict[str, Any]:
        """Simulates one protocol, or every protocol in the design."""
        session = get_session(session_id)
        require_idle(session)
        design = session.design
        targets = [design.protocol(protocol)] if protocol else [p for p in design.protocols if p.code]
        if not targets or targets[0] is None or not targets[0].code:
            raise HTTPException(400, "There is no protocol to simulate yet")
        for p in targets:
            try:
                p.simulation = await simulate(
                    p.code, p.plate_slot,
                    python=settings.simulator.python, timeout_s=settings.simulator.timeout_s,
                    rules=DeckRules.from_settings(settings.lab, settings.simulator.python),
                )
            except SimulatorUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
            store.save(session)
        return session.view()

    @app.get("/api/sessions/{session_id}/protocol.py")
    async def download_protocol(session_id: str, protocol: str = "") -> Response:
        design = get_session(session_id).design
        p = design.protocol(protocol) if protocol else next(iter(design.protocols), None)
        if p is None or not p.code:
            raise HTTPException(404, "No protocol yet")
        return Response(
            p.code, media_type="text/x-python",
            headers={"Content-Disposition": f'attachment; filename="{slug(p.title or p.id)}.py"'},
        )

    async def submission_for(session: Session, request: SendRequest) -> dict[str, Any]:
        # A protocol checked under older lab rules (API level, labware list) is checked again first.
        rules = DeckRules.from_settings(settings.lab, settings.simulator.python)
        for p in session.design.protocols:
            if p.code and p.simulation_current and p.simulation.rules != rules.signature:
                try:
                    p.simulation = await simulate(p.code, p.plate_slot, python=settings.simulator.python,
                                                  timeout_s=settings.simulator.timeout_s, rules=rules)
                except SimulatorUnavailable as exc:
                    raise HTTPException(503, str(exc)) from exc
                store.save(session)
        try:
            return build_submission(
                session.design, name=request.name, scientist=request.scientist, storage_slot=request.storage_slot,
                barcode=request.barcode, notes=request.notes, labware=request.labware,
            )
        except WorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sessions/{session_id}/plan")
    async def review_plan(session_id: str, request: SendRequest) -> dict[str, Any]:
        """What the master node would do with this design: the instrument chosen for each step,
        the plate moves and the safety checks. The scientist approves this before sending."""
        session = get_session(session_id)
        require_idle(session)
        submission = await submission_for(session, request)
        status, data = await preview_plan(submission)
        if status == "unavailable":
            raise HTTPException(502, f"The master node is not reachable: {data}")
        if status == "rejected":
            raise HTTPException(422, f"The master node can't run this: {data}")
        instruments = await lab_instruments() or []
        return {
            "plan_hash": plan_hash(submission),
            "steps": data["steps"],
            "warnings": data.get("warnings", []),
            "instruments": {i["id"]: {"name": i["name"], "kind": i["kind"], "driver": i["driver"]} for i in instruments},
        }

    @app.post("/api/sessions/{session_id}/send", status_code=201)
    async def send_to_lab(session_id: str, request: SendRequest) -> dict[str, Any]:
        session = get_session(session_id)
        require_idle(session)
        submission = await submission_for(session, request)
        if not request.plan_hash:
            raise HTTPException(409, "Review the plan and approve it before sending")
        if request.plan_hash != plan_hash(submission):
            raise HTTPException(409, "The design or run details changed after you reviewed the plan; review it again")
        try:
            run = await master.submit_run(submission)
        except MasterUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
        except MasterError as exc:
            raise HTTPException(exc.status_code if exc.status_code < 500 else 502, exc.detail) from exc
        session.submitted_runs.append(run["id"])
        store.save(session)
        store.save_run_design(run["id"], session, request.model_dump(exclude={"plan_hash"}))
        return {"run": run, "session": session.view()}

    # --- Runs back to designs: open the design a run was sent from, to change it or run it again ---

    @app.get("/api/runs/{run_id}/design")
    async def run_design_info(run_id: str) -> dict[str, Any]:
        found = store.run_design(run_id)
        if found is None:
            return {"available": False}
        session, send, source = found
        return {"available": True, "source": source, "session_id": session.id, "title": session.design.title}

    @app.post("/api/runs/{run_id}/reopen", status_code=201)
    async def reopen_design(run_id: str) -> dict[str, Any]:
        """A new design session copied from the one a run was sent from (as it was sent, when known),
        with its chat history, so the scientist can change it with the AI or send it again."""
        found = store.run_design(run_id)
        if found is None:
            raise HTTPException(404, f"The design of run {run_id} wasn't saved by this protocol tool")
        original, send, source = found
        copy = Session.model_validate(original.model_dump(exclude={"id", "created_at", "updated_at", "submitted_runs"}))
        when = "as it was sent" if source == "snapshot" else "as it is now (this run was sent before designs were saved with runs)"
        copy.transcript.append(ChatEntry(
            role="assistant",
            text=f"Reopened the design of run {run_id}, {when}. Review the plan and send it to run it again, "
                 "or tell me what to change.",
        ))
        store.save(copy)
        return {"session": copy.view(), "send": send, "source": source}

    # --- Knowledge -------------------------------------------------------------------

    @app.get("/api/knowledge")
    async def knowledge_documents() -> dict[str, Any]:
        return {
            "folder": str(knowledge.root.resolve()),
            "documents": [d.__dict__ for d in await asyncio.to_thread(knowledge.documents)],
        }

    # --- Results: analysis (no AI) and AI summary (optional) ------------------------------

    async def run_and_results(run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            response = await master.request("GET", f"/runs/{run_id}")
            if response.status_code == 404:
                raise HTTPException(404, f"No run {run_id}")
            if response.is_error:
                raise HTTPException(502, f"The master node returned {response.status_code}")
            run = response.json()
            results = None
            if run.get("has_results"):
                upstream = await master.request("GET", f"/runs/{run_id}/results")
                results = upstream.json() if upstream.is_success else None
        except MasterUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
        return run, results

    @app.get("/api/runs/{run_id}/analysis")
    async def run_analysis(run_id: str) -> dict[str, Any]:
        """The run's planned analyses (curve fits, interpolated samples, comparisons) on its readings."""
        run, results = await run_and_results(run_id)
        specs = run.get("protocol", {}).get("analysis") or []
        return {"run_id": run_id, "analyses": await asyncio.to_thread(analyse_run, specs, results)}

    @app.get("/api/runs/{run_id}/analysis.csv")
    async def run_analysis_csv(run_id: str) -> Response:
        run, results = await run_and_results(run_id)
        analyses = await asyncio.to_thread(analyse_run, run.get("protocol", {}).get("analysis") or [], results)
        return Response(analysis_csv(run_id, analyses), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{run_id}-analysis.csv"'})

    @app.get("/api/runs/{run_id}/explain")
    async def read_explanation(run_id: str) -> dict[str, Any]:
        return {"enabled": settings.results.explain, "note": store.run_note(run_id)}

    @app.post("/api/runs/{run_id}/explain")
    async def explain(run_id: str) -> dict[str, Any]:
        if not settings.results.explain:
            raise HTTPException(403, "Run summaries are turned off ([results] explain = false)")
        run, results = await run_and_results(run_id)
        if run["status"] not in FINISHED:
            raise HTTPException(409, "The run hasn't finished yet")
        # The model summarises the computed results; it never calculates them itself.
        analyses = await asyncio.to_thread(analyse_run, run.get("protocol", {}).get("analysis") or [], results)
        try:
            text = await assistant.explain_run(run, results, analyses)
        except (ModelUnavailable, ModelError) as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:
            log.exception("Run summary failed")
            raise HTTPException(503, f"The AI model could not summarise the run: {exc}") from exc
        if not text:
            raise HTTPException(503, "The AI model returned no summary")
        return {"enabled": True, "note": store.save_run_note(run_id, run["status"], text)}

    # --- Runs, relayed from the master node -----------------------------------------

    async def relay(request: Request, method: str, path: str) -> Response:
        try:
            upstream = await master.request(method, path, params=dict(request.query_params))
        except MasterUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "content-disposition")}
        return Response(upstream.content, status_code=upstream.status_code, headers=headers)

    @app.get("/api/health")
    async def health(refresh: bool = False) -> dict[str, Any]:
        try:
            upstream = await master.request("GET", "/health")
            master_status = upstream.json() if upstream.is_success else {"status": f"HTTP {upstream.status_code}"}
        except (MasterUnavailable, ValueError) as exc:
            master_status = {"status": "unreachable", "detail": str(exc)}
        if checker:
            try:
                await checker.check(refresh=refresh)
                assistant_status = checker.view()
            except Exception as exc:
                log.exception("Model status check failed")
                assistant_status = {**checker.view(), "ok": False, "state": "error", "detail": f"Status check failed: {exc}"}
        else:
            assistant_status = {
                "ok": True, "state": "ready", "detail": "Using a substitute model",
                "provider": provider.name, "label": provider.label, "model": provider.model, "free": provider.free,
            }
        lab = settings.lab
        deck = {"thor_slots": lab.thor_slots, "trash_slot": lab.trash_slot, "waste_chute_slot": lab.waste_chute_slot,
                "blocked_slots": lab.blocked_slots}
        return {
            "status": "ok", "version": __version__, "assistant": assistant_status,
            "master": master_status, "deck": deck, "explain": settings.results.explain,
        }

    @app.get("/api/devices")
    async def devices(request: Request) -> Response:
        return await relay(request, "GET", "/devices")

    @app.get("/api/instruments")
    async def instruments(request: Request) -> Response:
        return await relay(request, "GET", "/instruments")

    @app.post("/api/transport/reset")
    async def reset_transport(request: Request) -> Response:
        return await relay(request, "POST", "/transport/reset")

    @app.get("/api/runs")
    async def runs(request: Request) -> Response:
        return await relay(request, "GET", "/runs")

    @app.get("/api/runs/{run_id}")
    async def run(request: Request, run_id: str) -> Response:
        return await relay(request, "GET", f"/runs/{run_id}")

    @app.get("/api/tips")
    async def tip_racks(request: Request) -> Response:
        return await relay(request, "GET", "/tips")

    @app.post("/api/tips/{slot}/fresh")
    async def fresh_tip_rack(request: Request, slot: str) -> Response:
        return await relay(request, "POST", f"/tips/{slot}/fresh")

    @app.delete("/api/tips/{slot}")
    async def remove_tip_rack(request: Request, slot: str) -> Response:
        return await relay(request, "DELETE", f"/tips/{slot}")

    @app.post("/api/runs/{run_id}/plate-moved")
    async def plate_moved(request: Request, run_id: str) -> Response:
        return await relay(request, "POST", f"/runs/{run_id}/plate-moved")

    @app.post("/api/runs/{run_id}/abort")
    async def abort(request: Request, run_id: str) -> Response:
        return await relay(request, "POST", f"/runs/{run_id}/abort")

    @app.get("/api/runs/{run_id}/results")
    async def results(request: Request, run_id: str) -> Response:
        return await relay(request, "GET", f"/runs/{run_id}/results")

    @app.get("/api/runs/{run_id}/results.csv")
    async def results_csv(request: Request, run_id: str) -> Response:
        return await relay(request, "GET", f"/runs/{run_id}/results.csv")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


class PasswordGate:
    """Asks for a password (HTTP Basic sign-in; any user name) before serving anything."""

    def __init__(self, app, password: str):
        self.app = app
        self.password = password.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self._allowed(dict(scope["headers"]).get(b"authorization", b"")):
            response = Response(
                "Sign in with the protocol tool password.", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Protocol tool", charset="UTF-8"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _allowed(self, header: bytes) -> bool:
        scheme, _, encoded = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            _, _, password = base64.b64decode(encoded, validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        return secrets.compare_digest(password, self.password)


async def _read_upload(upload: UploadFile, max_mb: int) -> Attachment:
    name = Path(upload.filename or "upload").name
    media_type = (upload.content_type or "").split(";")[0].strip().lower()
    if media_type not in UPLOAD_TYPES:
        media_type = TEXT_EXTENSIONS.get(Path(name).suffix.lower()) or mimetypes.guess_type(name)[0] or ""
    if media_type not in UPLOAD_TYPES:
        raise HTTPException(415, f"{name}: attach PDFs, images or text files (CSV, TXT, MD, PY, JSON)")
    data = await upload.read()
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"{name} is larger than {max_mb} MB")
    return Attachment(name=name, media_type=media_type, data=data)


def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)
