from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from .config import Settings
from .agent_workspace import build_agent_workspace
from .agents import AgentStore
from .agent_routes import register_agent_routes
from .db import connect_database, initialize_schema
from .device_capabilities import load_device_capabilities, save_device_capabilities
from .model_catalog import build_project_model_catalog
from .media_relay import register_media_relay_routes
from .notifications.web import register_email_notification_routes
from .mail.web import register_mail_routes
from .exporter import export_project_markdown
from .media_assets import (
    IMAGE_CONTENT_TYPES,
    decorate_attachment,
    media_type_for,
    thumbnail_path,
)
from .project_memory import (
    render_project_memory_markdown,
    render_runtime_project_memory_markdown,
)
from .reading import video_content_type
from .releases import list_latest_releases, release_for_platform
from .repository import Repository, TASK_PRIORITY_VALUES, TASK_STATUS_VALUES
from .server_ai import SERVER_AI_PROVIDER_LABELS, ServerAIRuntime
from .speech import speech_capabilities
from .text_safety import SuspiciousTextEncodingError

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
FORWARDED_PREFIX_RE = re.compile(
    r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$"
)
KIND_VALUES = {"message", "markdown", "link", "file", "system"}
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
REASONING_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
INLINE_IMAGE_CONTENT_TYPES = IMAGE_CONTENT_TYPES
PERMISSION_VALUES = {"read", "write", "admin"}
DEVICE_ONLINE_WINDOW_SECONDS = 10 * 60
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app(settings: Settings | None = None) -> FastAPI:
    from .notifications.log_redaction import install_email_log_redaction

    install_email_log_redaction()
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    connection = connect_database(settings.database_path)
    initialize_schema(connection)
    repo = Repository(connection, email_notifications_enabled=settings.email_enabled)
    repo.create_owner_if_missing(settings.owner_username, settings.owner_password)
    server_ai = ServerAIRuntime(settings)
    server_ai.prepare(repo)

    app = FastAPI(title="Codex Hub")
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.state.settings = settings
    app.state.repo = repo
    app.state.server_ai = server_ai
    app.router.add_event_handler("startup", server_ai.start)
    app.router.add_event_handler("shutdown", server_ai.stop)
    register_request_repository(app)
    register_security_headers(app)
    register_exception_handlers(app)
    app.mount(
        "/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="static",
    )
    register_routes(app)
    register_media_relay_routes(app)
    register_email_notification_routes(app)
    register_mail_routes(app)
    register_agent_routes(app)
    from .maintenance_routes import register_maintenance_routes
    register_maintenance_routes(app)
    from .runtime_routes import register_execution_routes
    register_execution_routes(app)
    from .delivery_routes import register_delivery_routes
    register_delivery_routes(app)
    from .model_routes import register_model_routes
    register_model_routes(app)
    return app


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    def healthz() -> dict[str, str | bool]:
        return {"ok": True, "service": "codex-hub"}

    @app.get("/api/releases")
    def releases_api(request: Request):
        return {
            "releases": release_payload(request),
            "apple": {
                "platform": "apple",
                "platform_label": "Apple",
                "status": "planned",
            },
        }

    @app.get("/api/speech/capabilities")
    def speech_capabilities_api():
        return speech_capabilities()

    @app.get("/api/mobile/notifications")
    def mobile_notifications_api(
        request: Request, after_entry_id: int = 0, limit: int = 50
    ):
        require_user(request)
        latest_entry_id, notifications = get_repo(
            request
        ).list_mobile_notifications(after_entry_id=after_entry_id, limit=limit)
        return JSONResponse(
            {
                "latest_entry_id": latest_entry_id,
                "notifications": [
                    mobile_notification_payload(item) for item in notifications
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/downloads", response_class=HTMLResponse)
    def downloads_page(request: Request):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        return render(
            request,
            "downloads.html",
            user=user,
            releases=release_payload(request),
        )

    @app.get("/downloads/{platform}")
    def download_latest_release(request: Request, platform: str):
        release = release_for_platform(
            list_latest_releases(get_settings(request).releases_dir),
            platform,
        )
        if not release:
            raise HTTPException(status_code=404, detail="release not found")
        return FileResponse(
            release.path,
            media_type=(
                "application/vnd.android.package-archive"
                if release.platform == "android"
                else "application/vnd.microsoft.portable-executable"
            ),
            filename=release.path.name,
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/")
    def home(request: Request) -> RedirectResponse:
        return redirect_to(request, "/projects")

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if current_user(request):
            return redirect_to(request, "/projects")
        return render(request, "login.html", user=None, error=None)

    @app.post("/login")
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        repo = get_repo(request)
        settings = get_settings(request)
        user = repo.authenticate_user(username.strip(), password)
        if not user:
            return render(
                request,
                "login.html",
                user=None,
                error="Invalid username or password",
            )
        session = repo.create_session(user["id"])
        response = redirect_to(request, "/projects")
        response.set_cookie(
            settings.session_cookie_name,
            session.raw,
            httponly=True,
            samesite="lax",
            secure=is_secure_request(request),
        )
        response.set_cookie(
            settings.csrf_cookie_name,
            session.csrf,
            httponly=True,
            samesite="lax",
            secure=is_secure_request(request),
        )
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("", alias="_csrf")):
        settings = get_settings(request)
        raw = request.cookies.get(settings.session_cookie_name)
        if raw:
            verify_csrf_token(request, csrf)
            get_repo(request).delete_session(raw)
        response = redirect_to(request, "/login")
        response.delete_cookie(settings.session_cookie_name)
        response.delete_cookie(settings.csrf_cookie_name)
        return response

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        projects = repo.list_projects()
        if len(projects) == 1 and request.query_params.get("list") != "1":
            return redirect_to(request, f"/projects/{projects[0]['slug']}")
        return render(
            request,
            "projects.html",
            user=user,
            projects=projects,
        )

    @app.post("/projects")
    async def create_project_web(
        request: Request,
        slug: str = Form(...),
        name: str = Form(...),
        description: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        clean_slug = validate_slug(slug)
        repo = get_repo(request)
        if repo.get_project_by_slug(clean_slug):
            raise HTTPException(status_code=409, detail="project slug already exists")
        repo.create_project(
            slug=clean_slug,
            name=name,
            description=description,
            created_by_user_id=user["id"],
        )
        return redirect_to(request, f"/projects/{clean_slug}")

    @app.get("/projects/{slug}", response_class=HTMLResponse)
    def project_detail_page(request: Request, slug: str):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        project = require_project(get_repo(request), slug)
        return render_project_detail(request, user, project)

    @app.get("/projects/{slug}/chat-updates")
    def project_chat_updates(
        request: Request,
        slug: str,
        after_entry_id: int = 0,
        limit: int = 50,
    ):
        require_user(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        latest_entry_id, next_entry_id, has_more, entries = repo.list_entries_after(
            project["id"],
            after_entry_id=after_entry_id,
            limit=limit,
        )
        attachments = repo.list_attachments_for_entries(
            project["id"], [int(entry["id"]) for entry in entries]
        )
        attachments_by_entry: dict[int, list[dict[str, Any]]] = {}
        for attachment in attachments:
            attachments_by_entry.setdefault(int(attachment["entry_id"]), []).append(
                attachment
            )
        files_root = get_settings(request).files_dir
        updates = []
        for entry in entries:
            entry_view = entry_with_attachments(
                entry,
                attachments_by_entry.get(int(entry["id"]), []),
                files_root=files_root,
            )
            updates.append(
                {
                    "id": int(entry["id"]),
                    "html": render_project_chat_entry_html(
                        request, project, entry_view
                    ),
                }
            )
        return JSONResponse(
            {
                "entries": updates,
                "latest_entry_id": latest_entry_id,
                "next_entry_id": next_entry_id,
                "has_more": has_more,
                "reset_required": bool(
                    after_entry_id > 0 and after_entry_id > latest_entry_id
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/projects/{slug}/control")
    async def update_project_control_web(
        request: Request,
        slug: str,
        goal: str = Form(""),
        non_goals: str = Form(""),
        constraints_text: str = Form(""),
        current_phase: str = Form(""),
        acceptance: str = Form(""),
        roles: str = Form(""),
        decisions: str = Form(""),
        risks: str = Form(""),
        architecture: str = Form(""),
        known_issues: str = Form(""),
        user_preferences: str = Form(""),
        key_files: str = Form(""),
        next_steps: str = Form(""),
        handoff_summary: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            repo.save_project_control(
                project_id=project["id"],
                goal=goal,
                non_goals=non_goals,
                constraints_text=constraints_text,
                current_phase=current_phase,
                acceptance=acceptance,
                roles=roles,
                decisions=decisions,
                risks=risks,
                architecture=architecture,
                known_issues=known_issues,
                user_preferences=user_preferences,
                key_files=key_files,
                next_steps=next_steps,
                handoff_summary=handoff_summary,
                created_by_user_id=user["id"],
            )
        except SuspiciousTextEncodingError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#project-memory")

    @app.post("/projects/{slug}/handoffs")
    async def create_project_handoff_web(
        request: Request,
        slug: str,
        title: str = Form(""),
        summary_markdown: str = Form(""),
        source_device_id: str = Form(""),
        target_device_id: str = Form(""),
        manifest_json: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            repo.create_project_handoff(
                project["id"],
                title=title,
                summary_markdown=summary_markdown,
                source_device_id=parse_optional_int(source_device_id),
                target_device_id=parse_optional_int(target_device_id),
                manifest=manifest_json or None,
                created_by_user_id=user["id"],
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#project-memory")

    @app.post("/projects/{slug}/handoffs/{handoff_id}/accept")
    async def accept_project_handoff_web(
        request: Request,
        slug: str,
        handoff_id: int,
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            repo.accept_project_handoff(
                project["id"], handoff_id, accepted_by_user_id=user["id"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#project-memory")

    @app.post("/projects/{slug}/tasks")
    async def create_task_web(
        request: Request,
        slug: str,
        title: str = Form(...),
        description: str = Form(""),
        priority: str = Form("normal"),
        assigned_device_id: str = Form(""),
        assigned_agent_id: str = Form(""),
        requested_group_id: str = Form(""),
        context_pack_markdown: str = Form(""),
        due_at: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            created_task = repo.create_task(
                project_id=project["id"],
                title=title,
                description=description,
                priority=validate_task_priority(priority),
                assigned_device_id=parse_optional_int(assigned_device_id),
                assigned_agent_id=parse_optional_int(assigned_agent_id),
                requested_group_id=parse_optional_int(requested_group_id),
                created_by_user_id=user["id"],
                context_pack_markdown=context_pack_markdown,
                due_at=due_at.strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if created_task.get('assigned_agent_id'):
            return redirect_to(request, f"/projects/{slug}/tasks/{created_task['id']}")
        return redirect_to(request, f"/projects/{slug}?view=archive#tasks")

    @app.post("/projects/{slug}/codex-workspaces")
    async def create_codex_workspace_web(
        request: Request,
        slug: str,
        workspace_slug: str = Form(...),
        name: str = Form(""),
        description: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        require_project(repo, slug)
        clean_slug = validate_slug(workspace_slug)
        try:
            repo.create_codex_workspace(
                slug=clean_slug,
                name=name.strip() or clean_slug,
                description=description,
                created_by_user_id=user["id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#codex-workspaces")

    @app.post("/projects/{slug}/codex-workspaces/{workspace_id}/delete")
    async def delete_codex_workspace_web(
        request: Request,
        slug: str,
        workspace_id: int,
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        require_project(repo, slug)
        workspace = repo.delete_codex_workspace(workspace_id)
        if not workspace:
            raise HTTPException(status_code=404, detail="codex workspace not found")
        return redirect_to(request, f"/projects/{slug}?view=archive#codex-workspaces")

    @app.post("/projects/{slug}/codex-workspaces/{workspace_id}/conversations")
    async def create_codex_conversation_web(
        request: Request,
        slug: str,
        workspace_id: int,
        title: str = Form(...),
        initial_prompt: str = Form(...),
        target_device_id: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        workspace = require_codex_workspace(repo, workspace_id)
        target_id = parse_optional_int(target_device_id) or default_codex_target_device_id(
            repo, project["id"]
        )
        if target_id is None:
            raise HTTPException(status_code=422, detail="no writable target device")
        if target_id is not None and not repo.can_device_write(project["id"], target_id):
            raise HTTPException(status_code=422, detail="target device is not writable")
        try:
            conversation = repo.create_codex_conversation(
                workspace_id=workspace["id"],
                hub_project_id=project["id"],
                title=title,
                initial_prompt=initial_prompt,
                target_device_id=target_id,
                created_by_user_id=user["id"],
            )
            task = repo.create_task(
                project_id=project["id"],
                title=f"Codex 对话：{workspace['name']} / {conversation['title']}",
                description=build_codex_conversation_task_description(
                    project=project,
                    workspace=workspace,
                    conversation=conversation,
                ),
                priority="normal",
                assigned_device_id=target_id,
                created_by_user_id=user["id"],
                context_pack_markdown=build_codex_conversation_context_pack(
                    workspace=workspace,
                    conversation=conversation,
                ),
            )
            repo.attach_codex_conversation_task(conversation["id"], task["id"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#codex-workspaces")

    @app.get("/projects/{slug}/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail_page(request: Request, slug: str, task_id: int):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        project = require_project(repo, slug)
        task = require_project_task(repo, project, task_id)
        return render_task_detail(request, user, project, task)

    @app.post("/projects/{slug}/tasks/{task_id}/claim")
    async def claim_task_web(
        request: Request,
        slug: str,
        task_id: int,
        device_id: int = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_project_task(repo, project, task_id)
        claimed = repo.claim_task(task_id, device_id)
        if not claimed:
            raise HTTPException(status_code=409, detail="task cannot be claimed")
        return redirect_to(request, f"/projects/{slug}/tasks/{task_id}")

    @app.post("/projects/{slug}/tasks/{task_id}/status")
    async def update_task_status_web(
        request: Request,
        slug: str,
        task_id: int,
        status: str = Form(...),
        result_summary: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_project_task(repo, project, task_id)
        try:
            repo.update_task_status(
                task_id=task_id,
                status=validate_task_status(status),
                result_summary=result_summary,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}/tasks/{task_id}")

    @app.post("/projects/{slug}/tasks/{task_id}/entries")
    async def create_task_entry_web(
        request: Request,
        slug: str,
        task_id: int,
        kind: str = Form("message"),
        body_markdown: str = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_project_task(repo, project, task_id)
        entry = repo.create_entry(
            project_id=project["id"],
            task_id=task_id,
            author_user_id=user["id"],
            source_label=user["username"],
            kind=validate_kind(kind),
            body_markdown=body_markdown,
        )
        get_server_ai_runtime(request).enqueue_entry(repo, entry)
        return redirect_to(request, f"/projects/{slug}/tasks/{task_id}")

    @app.get("/projects/{slug}/tasks/{task_id}/prompt.md", response_class=PlainTextResponse)
    def task_prompt_web(request: Request, slug: str, task_id: int):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_project_task(repo, project, task_id)
        return PlainTextResponse(
            repo.build_task_prompt_markdown(slug, task_id),
            media_type="text/markdown",
        )

    @app.post("/projects/{slug}/entries")
    async def create_entry_web(
        request: Request,
        slug: str,
        kind: str = Form("markdown"),
        body_markdown: str = Form(""),
        codex_model: str = Form(""),
        codex_reasoning_effort: str = Form(""),
        hermes_model: str = Form(""),
        hermes_reasoning_effort: str = Form(""),
        delivery_mode: str = Form(""),
        target_device_ids: list[int] = Form([]),
        files: list[UploadFile] | None = File(None),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        uploads = [file for file in (files or []) if file.filename]
        if not body_markdown.strip() and not uploads:
            raise HTTPException(status_code=422, detail="message or attachment required")
        stored_attachments: list[dict[str, Any]] = []
        execution_metadata = build_execution_metadata(
            codex_model=codex_model,
            codex_reasoning_effort=codex_reasoning_effort,
            hermes_model=hermes_model,
            hermes_reasoning_effort=hermes_reasoning_effort,
        )
        # Old clients omit these fields; an explicit empty selection must never
        # fall back to an @mention or another device chosen by the server.
        if delivery_mode or target_device_ids:
            execution_metadata["routing"] = validate_routing_payload(
                {"mode": delivery_mode or "selected", "device_ids": target_device_ids}
            )
        try:
            for file in uploads:
                stored_attachments.append(
                    await store_attachment_file(
                        request=request,
                        project=project,
                        file=file,
                    )
                )
            entry, attachments = repo.create_entry_with_attachments(
                project_id=project["id"],
                author_user_id=user["id"],
                source_label=user["username"],
                kind=validate_kind(kind),
                body_markdown=body_markdown,
                attachments=stored_attachments,
                metadata_json=json.dumps(execution_metadata, ensure_ascii=False),
            )
        except Exception as exc:
            for stored in stored_attachments:
                remove_stored_attachment(request, stored)
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        get_server_ai_runtime(request).enqueue_entry(repo, entry)
        if request.headers.get("x-requested-with") == "codex-hub-composer":
            entry_view = entry_with_attachments(
                entry,
                attachments,
                files_root=get_settings(request).files_dir,
            )
            entry_html = render_project_chat_entry_html(request, project, entry_view)
            return JSONResponse(
                {
                    "ok": True,
                    "entry": entry,
                    "attachments": attachments,
                    "entry_html": entry_html,
                },
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        return redirect_to(request, f"/projects/{slug}?view=archive")

    @app.post("/projects/{slug}/attachments")
    async def upload_attachment_web(
        request: Request,
        slug: str,
        file: UploadFile = File(...),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        stored = await store_attachment_file(
            request=request,
            project=project,
            file=file,
        )
        try:
            repo.create_attachment_entry(
                project_id=project["id"],
                source_label=user["username"],
                created_by_user_id=user["id"],
                **stored,
            )
        except Exception:
            remove_stored_attachment(request, stored)
            raise
        return redirect_to(request, f"/projects/{slug}?view=archive#files")

    @app.get("/projects/{slug}/attachments/{attachment_id}")
    def download_attachment_web(
        request: Request,
        slug: str,
        attachment_id: int,
        inline: bool = False,
        size: str = "",
    ):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        project = require_project(repo, slug)
        attachment = require_project_attachment(repo, project, attachment_id)
        return attachment_file_response(request, attachment, inline=inline, size=size)

    @app.post("/projects/{slug}/members")
    async def add_member_web(
        request: Request,
        slug: str,
        device_id: int = Form(...),
        permission: str = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        if permission not in PERMISSION_VALUES:
            raise HTTPException(status_code=422, detail="invalid permission")
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device(repo, device_id)
        repo.add_project_member(
            project_id=project["id"],
            device_id=device_id,
            permission=permission,
            added_by_user_id=user["id"],
        )
        return redirect_to(request, f"/projects/{slug}?view=archive#members")

    @app.post("/projects/{slug}/primary-device")
    async def set_primary_device_web(
        request: Request,
        slug: str,
        primary_device_id: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        previous = repo.get_project_primary_device(project["id"])
        try:
            repo.set_project_primary_device(
                project["id"], parse_optional_int(primary_device_id)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current = repo.get_project_primary_device(project["id"])
        if (previous or {}).get("id") != (current or {}).get("id"):
            previous_label = (previous or {}).get("label") or "未设置"
            current_label = (current or {}).get("label") or "未设置"
            repo.create_entry(
                project_id=project["id"],
                author_user_id=user["id"],
                source_label="Codex Hub",
                kind="system",
                body_markdown=(
                    f"主设备已从 `{previous_label}` 切换为 `{current_label}`。"
                    "请在继续重要任务前生成或确认项目交接，并检查未同步记录、文件差异与大文件依赖。"
                ),
            )
        return redirect_to(request, f"/projects/{slug}?view=archive#project-memory")

    @app.post("/projects/{slug}/chat-roles")
    async def add_project_chat_role_web(
        request: Request,
        slug: str,
        role_name: str = Form(...),
        role_device_id: int = Form(...),
        role_instructions: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            repo.add_project_chat_role(
                project_id=project["id"],
                name=role_name,
                device_id=role_device_id,
                instructions=role_instructions,
                added_by_user_id=user["id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#members")

    @app.post("/projects/{slug}/chat-roles/{role_id}/remove")
    async def remove_project_chat_role_web(
        request: Request,
        slug: str,
        role_id: int,
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        repo.remove_project_chat_role(project["id"], role_id)
        return redirect_to(request, f"/projects/{slug}?view=archive#members")

    @app.post("/projects/{slug}/collaboration-mode")
    async def set_collaboration_mode_web(
        request: Request,
        slug: str,
        enabled: str = Form("0"),
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        try:
            repo.set_project_collaboration_mode(
                project["id"], enabled.strip().casefold() in {"1", "true", "on"}
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redirect_to(request, f"/projects/{slug}?view=archive#conversation")

    @app.post("/projects/{slug}/members/{device_id}/remove")
    async def remove_member_web(
        request: Request,
        slug: str,
        device_id: int,
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        project = require_project(repo, slug)
        repo.remove_project_member(project["id"], device_id)
        return redirect_to(request, f"/projects/{slug}?view=archive#members")

    @app.post("/projects/{slug}/server-ai/{provider}")
    async def configure_project_server_ai_web(
        request: Request,
        slug: str,
        provider: str,
        enabled: str = Form("0"),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        runtime = get_server_ai_runtime(request)
        clean_provider = provider.strip().casefold()
        if clean_provider not in SERVER_AI_PROVIDER_LABELS:
            raise HTTPException(status_code=404, detail="server AI provider not found")
        if not get_settings(request).server_ai_enabled:
            raise HTTPException(status_code=409, detail="server AI is disabled")
        repo = get_repo(request)
        project = require_project(repo, slug)
        device = repo.get_server_ai_device(clean_provider)
        if device is None:
            runtime.prepare(repo)
            device = repo.get_server_ai_device(clean_provider)
        if device is None:
            raise HTTPException(status_code=409, detail="server AI device is unavailable")
        should_enable = enabled.strip().casefold() in {"1", "true", "on"}
        if should_enable:
            repo.add_project_member(
                project_id=project["id"],
                device_id=device["id"],
                permission="write",
                added_by_user_id=user["id"],
            )
        else:
            repo.remove_project_member(project["id"], device["id"])
        return redirect_to(request, f"/projects/{slug}?view=archive#members")

    @app.get("/projects/{slug}/export.md", response_class=PlainTextResponse)
    def export_project_web(request: Request, slug: str):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        project = require_project(repo, slug)
        return PlainTextResponse(build_export(repo, project), media_type="text/markdown")

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        devices = repo.list_devices()
        selected_device = devices[0] if devices else None
        return render(
            request,
            "inbox.html",
            user=user,
            devices=devices,
            selected_device=selected_device,
            inbox_tasks=(
                repo.list_inbox_tasks_for_device(selected_device["id"])
                if selected_device
                else []
            ),
        )

    @app.get("/devices/{device_id}/inbox", response_class=HTMLResponse)
    def device_inbox_page(request: Request, device_id: int):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        repo = get_repo(request)
        device = require_device(repo, device_id)
        return render(
            request,
            "inbox.html",
            user=user,
            devices=repo.list_devices(),
            selected_device=device,
            inbox_tasks=repo.list_inbox_tasks_for_device(device_id),
        )

    @app.get("/admin/devices", response_class=HTMLResponse)
    @app.get("/devices", response_class=HTMLResponse)
    def admin_devices_page(request: Request):
        user = current_user(request)
        if not user:
            return redirect_to(request, "/login")
        return render_admin_devices(request, user=user, created_token=None)

    @app.post("/admin/groups")
    @app.post("/groups")
    async def create_group_web(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        get_repo(request).create_group(name=name, description=description)
        return redirect_to(request, "/admin/devices")

    @app.post("/admin/devices")
    @app.post("/devices")
    async def create_device_web(
        request: Request,
        label: str = Form(...),
        group_id: str = Form(""),
        note: str = Form(""),
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        parsed_group_id = parse_optional_int(group_id)
        repo = get_repo(request)
        if parsed_group_id is not None and not repo.get_group(parsed_group_id):
            raise HTTPException(status_code=422, detail="device group not found")
        repo.create_device(label=label, group_id=parsed_group_id, note=note)
        return redirect_to(request, "/admin/devices")

    @app.post("/admin/tokens")
    async def create_token_web(
        request: Request,
        device_id: int = Form(...),
        name: str = Form(...),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        require_device(repo, device_id)
        token = repo.create_api_token(device_id=device_id, name=name)
        return render_admin_devices(request, user=user, created_token=token.raw)

    @app.post("/devices/{device_id}/tokens", response_class=HTMLResponse)
    async def create_legacy_token_web(
        request: Request,
        device_id: int,
        name: str = Form("main"),
        csrf: str = Form("", alias="_csrf"),
    ):
        user = require_user(request)
        verify_csrf_token(request, csrf)
        repo = get_repo(request)
        require_device(repo, device_id)
        token = repo.create_api_token(device_id=device_id, name=name)
        return render_admin_devices(request, user=user, created_token=token.raw)

    @app.post("/devices/{device_id}/heartbeat")
    async def device_heartbeat_web(
        request: Request,
        device_id: int,
        csrf: str = Form("", alias="_csrf"),
    ):
        require_user(request)
        verify_csrf_token(request, csrf)
        device = get_repo(request).touch_device(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="device not found")
        return {"ok": True, "device": {"id": device["id"], "label": device["label"]}}

    @app.get("/api/projects")
    def list_projects_api(request: Request):
        device = require_api_device(request)
        return {"projects": get_repo(request).list_projects_for_device(device["id"])}

    @app.post("/api/device/heartbeat")
    def device_heartbeat_api(request: Request):
        # Authentication updates only this connection's agent heartbeat. No
        # inbox claim, task mutation, capabilities probe or model call is needed.
        device = require_api_device(request)
        return {"ok": True, "device_id": device["id"]}

    @app.post("/api/device/capabilities")
    async def device_capabilities_api(request: Request):
        device = require_api_device(request)
        payload = await json_object(request)
        if len(json.dumps(payload, ensure_ascii=False)) > 256_000:
            raise HTTPException(status_code=422, detail="device capabilities too large")
        try:
            capabilities = save_device_capabilities(
                get_settings(request).data_dir, device["id"], payload
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="invalid device capabilities") from exc
        return {"ok": True, "device_id": device["id"], "capabilities": capabilities}

    @app.get("/api/projects/{slug}/models")
    def project_models_api(request: Request, slug: str):
        repo = get_repo(request)
        project = require_project(repo, slug)
        if not current_user(request):
            device = require_api_device(request)
            require_device_read(repo, project, device)
        memberships = repo.list_project_memberships(project["id"])
        capabilities = load_device_capabilities(
            get_settings(request).data_dir, (item["device_id"] for item in memberships)
        )
        return JSONResponse(
            build_project_model_catalog(memberships, capabilities),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/projects", status_code=201)
    async def create_project_api(request: Request):
        device = require_api_device(request)
        payload = await json_object(request)
        slug = validate_slug(str(payload.get("slug", "")))
        repo = get_repo(request)
        if repo.get_project_by_slug(slug):
            raise HTTPException(status_code=409, detail="project slug already exists")
        project = repo.create_project(
            slug=slug,
            name=str(payload.get("name") or slug),
            description=str(payload.get("description", "")),
            created_by_device_id=device["id"],
        )
        return {"project": project}

    @app.get("/api/projects/{slug}")
    def project_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return {"project": project}

    @app.get("/api/inbox")
    def inbox_api(request: Request):
        device = require_api_device(request)
        repo = get_repo(request)
        return {
            "device": {
                "id": device["id"],
                "label": device["label"],
                "group_id": device["group_id"],
                "group_name": device.get("group_name"),
            },
            "tasks": repo.list_inbox_tasks_for_device(device["id"]),
            "mentions": repo.list_mentions_for_device(device["id"]),
        }

    @app.post("/api/projects/{slug}/mentions/{entry_id}/claim")
    def claim_mention_api(request: Request, slug: str, entry_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        mention = repo.get_mention_for_device(
            entry_id, device["id"], include_handled=True
        )
        if not mention or mention["project_id"] != project["id"]:
            raise HTTPException(status_code=409, detail="mention cannot be claimed")
        receipt = repo.claim_mention(entry_id, device["id"])
        if not receipt:
            raise HTTPException(status_code=409, detail="mention cannot be claimed")
        return {"mention": mention, "receipt": receipt}

    @app.post("/api/projects/{slug}/mentions/{entry_id}/dispatching")
    def dispatch_mention_api(request: Request, slug: str, entry_id: int):
        device, repo, mention = require_api_mention(request, slug, entry_id)
        receipt = repo.mark_mention_dispatching(entry_id, device["id"])
        if not receipt:
            raise HTTPException(status_code=409, detail="mention cannot be dispatched")
        return {"mention": mention, "receipt": receipt}

    @app.post("/api/projects/{slug}/mentions/{entry_id}/submitted")
    def submit_mention_api(request: Request, slug: str, entry_id: int):
        device, repo, mention = require_api_mention(request, slug, entry_id)
        receipt = repo.mark_mention_submitted(entry_id, device["id"])
        if not receipt:
            raise HTTPException(status_code=409, detail="mention cannot be submitted")
        return {"mention": mention, "receipt": receipt}

    @app.post("/api/projects/{slug}/mentions/{entry_id}/release")
    def release_mention_api(request: Request, slug: str, entry_id: int):
        device, repo, mention = require_api_mention(request, slug, entry_id)
        receipt = repo.release_mention_dispatch(entry_id, device["id"])
        if not receipt:
            raise HTTPException(status_code=409, detail="mention cannot be released")
        return {"mention": mention, "receipt": receipt}

    @app.post("/api/projects/{slug}/mentions/{entry_id}/handled")
    def handle_mention_api(request: Request, slug: str, entry_id: int):
        device, repo, mention = require_api_mention(request, slug, entry_id)
        receipt = repo.mark_mention_handled(entry_id, device["id"])
        if not receipt:
            raise HTTPException(status_code=409, detail="mention cannot be handled")
        return {"mention": mention, "receipt": receipt}

    @app.get("/api/projects/{slug}/control.md")
    def project_control_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        control = repo.get_latest_project_control(project["id"])
        return PlainTextResponse(
            build_project_control_markdown(project, control),
            media_type="text/markdown",
        )

    @app.get("/api/projects/{slug}/memory.md")
    def project_memory_markdown_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return PlainTextResponse(
            render_project_memory_markdown(
                project,
                repo.get_latest_project_control(project["id"]),
                repo.list_project_handoffs(project["id"], limit=5),
            ),
            media_type="text/markdown",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/projects/{slug}/memory/runtime.md")
    def project_runtime_memory_markdown_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return PlainTextResponse(
            render_runtime_project_memory_markdown(
                project,
                repo.get_latest_project_control(project["id"]),
            ),
            media_type="text/markdown",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/projects/{slug}/memory")
    def project_memory_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return {
            "project": project,
            "memory": repo.get_latest_project_control(project["id"]),
            "handoffs": repo.list_project_handoffs(project["id"]),
            "sync_snapshots": repo.list_latest_project_sync_snapshots(project["id"]),
        }

    @app.put("/api/projects/{slug}/memory")
    async def update_project_memory_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_primary_device_write(repo, project, device)
        payload = await json_object(request)
        try:
            memory = repo.save_project_control(
                project_id=project["id"],
                goal=str(payload.get("goal", "")),
                non_goals=str(payload.get("non_goals", "")),
                constraints_text=str(payload.get("constraints_text", "")),
                current_phase=str(payload.get("current_phase", "")),
                acceptance=str(payload.get("acceptance", "")),
                roles=str(payload.get("roles", "")),
                architecture=str(payload.get("architecture", "")),
                decisions=str(payload.get("decisions", "")),
                known_issues=str(payload.get("known_issues", "")),
                risks=str(payload.get("risks", "")),
                user_preferences=str(payload.get("user_preferences", "")),
                key_files=str(payload.get("key_files", "")),
                next_steps=str(payload.get("next_steps", "")),
                handoff_summary=str(payload.get("handoff_summary", "")),
                created_by_device_id=device["id"],
            )
        except SuspiciousTextEncodingError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"memory": memory}

    @app.get("/api/projects/{slug}/handoffs")
    def list_project_handoffs_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return {"handoffs": repo.list_project_handoffs(project["id"])}

    @app.post("/api/projects/{slug}/handoffs", status_code=201)
    async def create_project_handoff_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        payload = await json_object(request)
        try:
            handoff = repo.create_project_handoff(
                project["id"],
                title=str(payload.get("title", "")),
                summary_markdown=str(payload.get("summary_markdown", "")),
                status=str(payload.get("status", "ready")),
                base_memory_version=payload.get("base_memory_version"),
                source_device_id=device["id"],
                target_device_id=payload.get("target_device_id"),
                manifest=payload.get("manifest"),
                created_by_device_id=device["id"],
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"handoff": handoff}

    @app.post("/api/projects/{slug}/handoffs/{handoff_id}/accept")
    def accept_project_handoff_api(
        request: Request, slug: str, handoff_id: int
    ):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_primary_device_write(repo, project, device)
        try:
            handoff = repo.accept_project_handoff(
                project["id"], handoff_id, accepted_by_device_id=device["id"]
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"handoff": handoff}

    @app.post("/api/projects/{slug}/sync-snapshots", status_code=201)
    async def create_project_sync_snapshot_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        payload = await json_object(request)
        try:
            snapshot = repo.save_project_sync_snapshot(
                project["id"],
                device["id"],
                memory_version=int(payload.get("memory_version") or 0),
                git_commit=str(payload.get("git_commit") or ""),
                manifest=payload.get("manifest"),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"snapshot": snapshot}

    @app.get("/api/projects/{slug}/sync-diff")
    def project_sync_diff_api(
        request: Request,
        slug: str,
        source_device_id: int = 0,
        target_device_id: int = 0,
    ):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        source_id = source_device_id or int(project.get("primary_device_id") or 0)
        target_id = target_device_id or int(device["id"])
        if not source_id:
            raise HTTPException(
                status_code=422, detail="project primary device is not set"
            )
        if not repo.get_project_membership(
            project["id"], source_id
        ) or not repo.get_project_membership(project["id"], target_id):
            raise HTTPException(
                status_code=403, detail="sync devices must be project members"
            )
        try:
            comparison = repo.compare_project_devices(
                project["id"], source_id, target_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"comparison": comparison}

    @app.put("/api/projects/{slug}/control")
    async def update_project_control_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        payload = await json_object(request)
        control = repo.save_project_control(
            project_id=project["id"],
            goal=str(payload.get("goal", "")),
            non_goals=str(payload.get("non_goals", "")),
            constraints_text=str(payload.get("constraints_text", "")),
            current_phase=str(payload.get("current_phase", "")),
            acceptance=str(payload.get("acceptance", "")),
            roles=str(payload.get("roles", "")),
            decisions=str(payload.get("decisions", "")),
            risks=str(payload.get("risks", "")),
            architecture=str(payload.get("architecture", "")),
            known_issues=str(payload.get("known_issues", "")),
            user_preferences=str(payload.get("user_preferences", "")),
            key_files=str(payload.get("key_files", "")),
            next_steps=str(payload.get("next_steps", "")),
            handoff_summary=str(payload.get("handoff_summary", "")),
            created_by_device_id=device["id"],
        )
        return {"control": control}

    @app.get("/api/projects/{slug}/tasks")
    def list_tasks_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return {"project": project, "tasks": repo.list_tasks(project["id"])}

    @app.post("/api/projects/{slug}/tasks", status_code=201)
    async def create_task_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        payload = await json_object(request)
        try:
            task = repo.create_task(
                project_id=project["id"],
                title=str(payload.get("title", "")),
                description=str(payload.get("description", "")),
                priority=validate_task_priority(str(payload.get("priority", "normal"))),
                assigned_device_id=payload.get("assigned_device_id"),
                assigned_agent_id=parse_optional_int(payload.get("assigned_agent_id")),
                requested_group_id=payload.get("requested_group_id"),
                created_by_device_id=device["id"],
                context_pack_markdown=str(payload.get("context_pack_markdown", "")),
                due_at=payload.get("due_at"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"task": task}

    @app.get("/api/projects/{slug}/tasks/{task_id}")
    def task_api(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        task = require_project_task(repo, project, task_id)
        return {
            "project": project,
            "task": task,
            "entries": repo.list_entries(project["id"], task_id=task_id),
        }

    @app.post("/api/projects/{slug}/tasks/{task_id}/claim")
    def claim_task_api(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        require_project_task(repo, project, task_id)
        claimed = repo.claim_task(task_id, device["id"])
        if not claimed:
            raise HTTPException(status_code=409, detail="task cannot be claimed")
        return {"task": claimed}

    @app.post("/api/projects/{slug}/tasks/{task_id}/status")
    async def update_task_status_api(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        original_task = require_project_task(repo, project, task_id)
        if original_task.get("assigned_agent_id") and device["id"] not in {
            original_task["assigned_device_id"], project.get("primary_device_id")
        }:
            raise HTTPException(status_code=403, detail="task belongs to another executor")
        payload = await json_object(request)
        try:
            task = repo.update_task_status(
                task_id=task_id,
                status=validate_task_status(str(payload.get("status", ""))),
                result_summary=str(payload.get("result_summary", "")),
                reflection=payload.get("reflection"),
                actor_device_id=device["id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"task": task}

    @app.post("/api/projects/{slug}/tasks/{task_id}/entries", status_code=201)
    async def create_task_entry_api(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        require_project_task(repo, project, task_id)
        payload = await json_object(request)
        metadata = entry_api_metadata(payload)
        try:
            entry = repo.create_entry(
                project_id=project["id"],
                task_id=task_id,
                author_device_id=device["id"],
                source_label=device["label"],
                kind=validate_kind(str(payload.get("kind", "message"))),
                body_markdown=str(payload.get("body_markdown", "")),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"entry": entry}

    @app.get("/api/projects/{slug}/tasks/{task_id}/prompt.md")
    def task_prompt_api(request: Request, slug: str, task_id: int):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        task = require_project_task(repo, project, task_id)
        if task.get("assigned_agent_id"):
            membership = repo.get_project_membership(project["id"], device["id"])
            if device["id"] not in {task["assigned_device_id"], project.get("primary_device_id")} and membership["permission"] != "admin":
                raise HTTPException(status_code=403, detail="agent experience belongs to another executor")
        return PlainTextResponse(
            repo.build_task_prompt_markdown(slug, task_id),
            media_type="text/markdown",
        )

    @app.get("/api/projects/{slug}/entries")
    def list_entries_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        entries = repo.list_project_entries(project["id"])
        return {
            "project": project,
            "entries": entries,
            "attachments": repo.list_attachments_for_entries(
                project["id"], [int(entry["id"]) for entry in entries]
            ),
        }

    @app.post("/api/projects/{slug}/entries", status_code=201)
    async def create_entry_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        payload = await json_object(request)
        metadata = entry_api_metadata(payload)
        try:
            entry = repo.create_entry(
                project_id=project["id"],
                author_device_id=device["id"],
                source_label=device["label"],
                kind=validate_kind(str(payload.get("kind", "message"))),
                body_markdown=str(payload.get("body_markdown", "")),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"entry": entry}

    @app.post("/api/projects/{slug}/attachments", status_code=201)
    async def upload_attachment_api(
        request: Request,
        slug: str,
        file: UploadFile = File(...),
    ):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_write(repo, project, device)
        stored = await store_attachment_file(
            request=request,
            project=project,
            file=file,
        )
        try:
            entry, attachment = repo.create_attachment_entry(
                project_id=project["id"],
                source_label=device["label"],
                created_by_device_id=device["id"],
                **stored,
            )
        except Exception:
            remove_stored_attachment(request, stored)
            raise
        return {"entry": entry, "attachment": attachment}

    @app.get("/api/projects/{slug}/attachments/{attachment_id}")
    def download_attachment_api(
        request: Request,
        slug: str,
        attachment_id: int,
        inline: bool = False,
        size: str = "",
    ):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        attachment = require_project_attachment(repo, project, attachment_id)
        return attachment_file_response(request, attachment, inline=inline, size=size)

    @app.get("/api/projects/{slug}/export.md")
    def export_project_api(request: Request, slug: str):
        device = require_api_device(request)
        repo = get_repo(request)
        project = require_project(repo, slug)
        require_device_read(repo, project, device)
        return PlainTextResponse(build_export(repo, project), media_type="text/markdown")

    @app.get("/api/server-ai/status")
    def server_ai_status_api(request: Request):
        require_user(request)
        runtime = get_server_ai_runtime(request)
        return {
            "enabled": get_settings(request).server_ai_enabled,
            "providers": runtime.statuses(get_repo(request)),
        }


def get_repo(request: Request) -> Repository:
    repo = getattr(request.state, "repo", None)
    if repo is None:
        connection = connect_database(get_settings(request).database_path)
        repo = Repository(connection, email_notifications_enabled=get_settings(request).email_enabled)
        request.state.repo = repo
    return repo


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_server_ai_runtime(request: Request) -> ServerAIRuntime:
    return request.app.state.server_ai


def register_request_repository(app: FastAPI) -> None:
    @app.middleware("http")
    async def close_request_repository(request: Request, call_next):
        request.state.repo = None
        try:
            return await call_next(request)
        finally:
            repo = getattr(request.state, "repo", None)
            if repo is not None:
                repo.connection.close()


def render(
    request: Request,
    template: str,
    response_status_code: int = 200,
    **context: Any,
):
    csrf = csrf_token(request)
    new_csrf: str | None = None
    raw_session = request.cookies.get(get_settings(request).session_cookie_name, "")
    if raw_session and current_user(request):
        if not csrf or not get_repo(request).session_matches_csrf(raw_session, csrf):
            new_csrf = get_repo(request).rotate_session_csrf(raw_session)
            csrf = new_csrf or ""
    context.setdefault("app_path", lambda path: app_path(request, path))
    context.setdefault("inbox_url", get_settings(request).inbox_url)
    context.setdefault("csrf_token", csrf)
    from . import __version__
    context.setdefault("app_version", __version__)
    response = templates.TemplateResponse(
        request,
        template,
        context,
        status_code=response_status_code,
    )
    if new_csrf:
        response.set_cookie(
            get_settings(request).csrf_cookie_name,
            new_csrf,
            httponly=True,
            samesite="lax",
            secure=is_secure_request(request),
        )
    return response


def current_user(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(get_settings(request).session_cookie_name)
    if not raw:
        return None
    return get_repo(request).get_session_user(raw)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


def register_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if "/static/" not in request.url.path:
            response.headers.setdefault("Cache-Control", "no-store")
        if (
            request.method == "GET"
            and "/static/" in request.url.path
            and request.query_params.get("v")
        ):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        return response


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SuspiciousTextEncodingError)
    async def suspicious_text_encoding_error(
        request: Request, exc: SuspiciousTextEncodingError
    ):
        if wants_html(request):
            return render(
                request,
                "error.html",
                user=current_user(request),
                status_code=422,
                detail=str(exc),
                status_label=http_status_label(422),
                response_status_code=422,
            )
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if wants_html(request):
            return render(
                request,
                "error.html",
                user=current_user(request),
                status_code=exc.status_code,
                detail=str(exc.detail),
                status_label=http_status_label(exc.status_code),
                response_status_code=exc.status_code,
            )
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )


def app_path(request: Request, path: str) -> str:
    prefix = forwarded_prefix(request)
    normalized = f"/{path.lstrip('/')}"
    return f"{prefix}{normalized}" if prefix else normalized


def forwarded_prefix(request: Request) -> str:
    prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if not prefix or not FORWARDED_PREFIX_RE.fullmatch(prefix):
        return ""
    if any(part in {".", ".."} for part in prefix.split("/")):
        return ""
    return prefix


def release_payload(request: Request) -> list[dict[str, str | int]]:
    return [
        release.as_dict(app_path(request, f"/downloads/{release.platform}"))
        for release in list_latest_releases(get_settings(request).releases_dir)
    ]


def mobile_notification_payload(entry: dict[str, Any]) -> dict[str, Any]:
    body = re.sub(r"\s+", " ", str(entry.get("body_markdown") or "")).strip()
    if len(body) > 180:
        body = f"{body[:177].rstrip()}..."
    return {
        "entry_id": int(entry["id"]),
        "project_slug": entry["project_slug"],
        "project_name": entry["project_name"],
        "source_label": entry["source_label"],
        "kind": entry["kind"],
        "body": body or "收到一条新回复",
        "created_at": entry["created_at"],
        "task_id": entry.get("task_id"),
    }


def wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def http_status_label(status_code: int) -> str:
    labels = {
        400: "请求有误",
        401: "需要登录",
        403: "没有权限",
        404: "未找到",
        409: "内容冲突",
        413: "文件过大",
        422: "参数无效",
    }
    return labels.get(status_code, "请求失败")


def redirect_to(
    request: Request, path: str, status_code: int = 303
) -> RedirectResponse:
    return RedirectResponse(app_path(request, path), status_code=status_code)


def csrf_token(request: Request) -> str:
    return request.cookies.get(get_settings(request).csrf_cookie_name, "")


def verify_csrf_token(request: Request, submitted: str) -> None:
    expected = csrf_token(request)
    raw_session = request.cookies.get(get_settings(request).session_cookie_name, "")
    if (
        not expected
        or not submitted
        or not raw_session
        or not hmac.compare_digest(expected, submitted)
        or not get_repo(request).session_matches_csrf(raw_session, expected)
    ):
        raise HTTPException(status_code=403, detail="invalid csrf token")


def is_secure_request(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def require_api_device(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    device = get_repo(request).authenticate_api_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return device


def validate_slug(slug: str) -> str:
    clean = (slug or "").strip().lower()
    if not SLUG_RE.fullmatch(clean):
        raise HTTPException(status_code=422, detail="invalid project slug")
    return clean


def validate_kind(kind: str) -> str:
    if kind not in KIND_VALUES:
        raise HTTPException(status_code=422, detail="invalid entry kind")
    return kind


def entry_api_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    execution = payload.get("execution")
    metadata: dict[str, Any] = {}
    if isinstance(execution, dict):
        metadata.update(normalize_execution_metadata(execution))
    if "routing" in payload:
        metadata["routing"] = validate_routing_payload(payload["routing"])
    if "reply_to_entry_id" in payload:
        reply_id = payload["reply_to_entry_id"]
        if isinstance(reply_id, bool) or not isinstance(reply_id, int) or reply_id < 1:
            raise HTTPException(status_code=422, detail="invalid reply entry id")
        metadata["reply_to_entry_id"] = reply_id
    return metadata


def validate_routing_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="请选择本次执行成员")
    mode = value.get("mode", "selected")
    device_ids = value.get("device_ids", [])
    if mode not in ("selected", "group", "none"):
        raise HTTPException(status_code=422, detail="无效的执行范围")
    if not isinstance(device_ids, list) or len(device_ids) > 100:
        raise HTTPException(status_code=422, detail="无效的成员列表")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in device_ids):
        raise HTTPException(status_code=422, detail="无效的成员编号")
    return {"mode": mode, "device_ids": list(dict.fromkeys(device_ids))}


def validate_model_id(value: Any) -> str:
    clean = str(value or "").strip()
    if clean and not MODEL_ID_RE.fullmatch(clean):
        raise HTTPException(status_code=422, detail="invalid model id")
    return clean


def validate_reasoning_effort(value: Any) -> str:
    clean = str(value or "").strip().lower()
    if clean and clean not in REASONING_EFFORT_VALUES:
        raise HTTPException(status_code=422, detail="invalid reasoning effort")
    return clean


def normalize_execution_metadata(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"execution": {"source": "ui"}}
    for backend in ("codex", "hermes"):
        raw = value.get(backend)
        if not isinstance(raw, dict):
            continue
        model = validate_model_id(raw.get("model"))
        effort = validate_reasoning_effort(raw.get("reasoning_effort"))
        settings = {
            key: item
            for key, item in (("model", model), ("reasoning_effort", effort))
            if item
        }
        if settings:
            result["execution"][backend] = settings
    if len(result["execution"]) == 1:
        return {}
    return result


def build_execution_metadata(
    *,
    codex_model: Any = "",
    codex_reasoning_effort: Any = "",
    hermes_model: Any = "",
    hermes_reasoning_effort: Any = "",
) -> dict[str, Any]:
    return normalize_execution_metadata(
        {
            "codex": {
                "model": codex_model,
                "reasoning_effort": codex_reasoning_effort,
            },
            "hermes": {
                "model": hermes_model,
                "reasoning_effort": hermes_reasoning_effort,
            },
        }
    )


def validate_task_status(status: str) -> str:
    if status not in TASK_STATUS_VALUES:
        raise HTTPException(status_code=422, detail="invalid task status")
    return status


def validate_task_priority(priority: str) -> str:
    if priority not in TASK_PRIORITY_VALUES:
        raise HTTPException(status_code=422, detail="invalid task priority")
    return priority


def parse_optional_int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="value must be an integer") from exc


async def json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return payload


def require_project(repo: Repository, slug: str) -> dict[str, Any]:
    project = repo.get_project_by_slug(slug)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def require_device(repo: Repository, device_id: int) -> dict[str, Any]:
    device = repo.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    return device


def require_api_mention(
    request: Request, slug: str, entry_id: int
) -> tuple[dict[str, Any], Repository, dict[str, Any]]:
    device = require_api_device(request)
    repo = get_repo(request)
    project = require_project(repo, slug)
    require_device_read(repo, project, device)
    mention = repo.get_mention_for_device(
        entry_id, device["id"], include_handled=True
    )
    if not mention or mention["project_id"] != project["id"]:
        raise HTTPException(status_code=409, detail="mention is not available")
    return device, repo, mention


def require_project_task(
    repo: Repository, project: dict[str, Any], task_id: int
) -> dict[str, Any]:
    task = repo.get_task(task_id)
    if not task or task["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def require_project_attachment(
    repo: Repository, project: dict[str, Any], attachment_id: int
) -> dict[str, Any]:
    attachment = repo.get_attachment(attachment_id)
    if not attachment or attachment["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="attachment not found")
    return attachment


def require_codex_workspace(repo: Repository, workspace_id: int) -> dict[str, Any]:
    workspace = repo.get_codex_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="codex workspace not found")
    return workspace


def default_codex_target_device_id(repo: Repository, project_id: int) -> int | None:
    memberships = repo.list_project_memberships(project_id)
    writable = [
        member
        for member in memberships
        if member.get("permission") in {"write", "admin"}
        and not member.get("server_ai_provider")
        and not member.get("agent_tasks_only")
    ]
    if not writable:
        local_members = [
            member for member in memberships if not member.get("server_ai_provider")
        ]
        return local_members[0]["device_id"] if local_members else None
    agent_online = [member for member in writable if member.get("last_agent_seen_at")]
    if agent_online:
        return agent_online[0]["device_id"]
    return writable[0]["device_id"]


def build_codex_conversation_task_description(
    *,
    project: dict[str, Any],
    workspace: dict[str, Any],
    conversation: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "请在目标 Codex 项目中开启一个新对话，并直接执行用户请求。",
            "",
            f"Hub project slug: {project['slug']}",
            f"Codex target project slug: {workspace['slug']}",
            f"Codex target project name: {workspace['name']}",
            f"Codex conversation id: {conversation['id']}",
            "",
            "如果本机没有这个 Codex 项目，Bridge 会在可信项目根目录下创建同名文件夹。",
        ]
    )


def build_codex_conversation_context_pack(
    *, workspace: dict[str, Any], conversation: dict[str, Any]
) -> str:
    return "\n".join(
        [
            "## Codex Workspace Target",
            f"- Codex target project slug: {workspace['slug']}",
            f"- Codex target project name: {workspace['name']}",
            "",
            "## Conversation Request",
            f"- Title: {conversation['title']}",
            "",
            conversation["initial_prompt"],
            "",
            "## Return",
            "完成后把简洁结果写回这个 Hub 任务，必要时同步写一条项目聊天消息。",
        ]
    )


def attach_codex_conversations_to_workspaces(
    workspaces: list[dict[str, Any]], conversations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_workspace: dict[int, list[dict[str, Any]]] = {}
    for conversation in conversations:
        by_workspace.setdefault(int(conversation["workspace_id"]), []).append(
            conversation
        )
    annotated = []
    for workspace in workspaces:
        copy = dict(workspace)
        copy["conversations"] = by_workspace.get(int(workspace["id"]), [])[:5]
        annotated.append(copy)
    return annotated


def require_device_read(
    repo: Repository, project: dict[str, Any], device: dict[str, Any]
) -> None:
    if not repo.can_device_read(project["id"], device["id"]):
        raise HTTPException(status_code=403, detail="device is not on project allowlist")


def require_device_write(
    repo: Repository, project: dict[str, Any], device: dict[str, Any]
) -> None:
    if not repo.can_device_write(project["id"], device["id"]):
        raise HTTPException(status_code=403, detail="device cannot write to project")


def require_primary_device_write(
    repo: Repository, project: dict[str, Any], device: dict[str, Any]
) -> None:
    require_device_write(repo, project, device)
    primary_device_id = project.get("primary_device_id")
    if primary_device_id and int(primary_device_id) != int(device["id"]):
        raise HTTPException(
            status_code=403,
            detail="only the project primary device can publish canonical memory",
        )


async def store_attachment_file(
    request: Request,
    project: dict[str, Any],
    file: UploadFile,
) -> dict[str, Any]:
    settings = get_settings(request)
    original = safe_upload_filename(file.filename)
    suffix = PurePath(original).suffix[:16]
    relpath = f"{project['slug']}/{uuid.uuid4().hex}{suffix}"
    target = settings.files_dir / relpath
    resolved = target.resolve()
    files_root = settings.files_dir.resolve()
    if files_root not in resolved.parents:
        raise HTTPException(status_code=400, detail="invalid file path")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with temporary.open("xb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                byte_size += len(chunk)
                if byte_size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="file too large")
                digest.update(chunk)
                handle.write(chunk)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        try:
            await file.close()
        except BaseException:
            # A cancellation while closing a spooled upload must not leave a
            # completed file that the caller has never received for cleanup.
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
    return {
        "original_filename": original,
        "stored_relpath": relpath,
        "content_type": safe_content_type(file.content_type),
        "byte_size": byte_size,
        "sha256": digest.hexdigest(),
    }


def safe_upload_filename(value: str | None) -> str:
    portable_value = str(value or "upload.bin").replace("\\", "/")
    name = unicodedata.normalize("NFC", portable_value.rsplit("/", 1)[-1])
    name = "".join(character for character in name if character not in "\x00\r\n").strip()
    return name[:240] or "upload.bin"


def safe_content_type(value: str | None) -> str:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type):
        return "application/octet-stream"
    return content_type


def remove_stored_attachment(request: Request, stored: dict[str, Any]) -> None:
    path = (get_settings(request).files_dir / stored["stored_relpath"]).resolve()
    files_root = get_settings(request).files_dir.resolve()
    if files_root in path.parents and path.is_file():
        path.unlink()


def attachment_file_response(
    request: Request,
    attachment: dict[str, Any],
    inline: bool = False,
    size: str = "",
) -> FileResponse:
    files_root = get_settings(request).files_dir.resolve()
    path = (files_root / attachment["stored_relpath"]).resolve()
    if files_root not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="attachment file not found")
    attachment_media_type = media_type_for(attachment)
    media_type = (
        video_content_type(attachment)
        if attachment_media_type == "video"
        else attachment["content_type"]
    ) or attachment["content_type"]
    filename = attachment["original_filename"]
    if inline and size in {"thumb", "preview"}:
        derived = thumbnail_path(files_root, attachment, size)
        if derived:
            path = derived
            media_type = "image/webp"
            filename = f"{Path(filename).stem}-{size}.webp"
    allow_inline = attachment_media_type in {"image", "video"}
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline" if inline and allow_inline else "attachment",
        headers={"Cache-Control": "private, max-age=86400"} if inline else None,
    )


def is_inline_image(attachment: dict[str, Any]) -> bool:
    return media_type_for(attachment) == "image"


def entries_with_attachments(
    repo: Repository,
    project_id: int,
    files_root: Path,
    task_id: int | None = None,
) -> list[dict[str, Any]]:
    entries = [
        dict(entry)
        for entry in (
            repo.list_project_entries(project_id)
            if task_id is None
            else repo.list_entries(project_id, task_id=task_id)
        )
    ]
    entry_ids = {entry["id"] for entry in entries}
    attachments_by_entry: dict[int, list[dict[str, Any]]] = {}
    for attachment in repo.list_attachments(project_id):
        entry_id = attachment.get("entry_id")
        if entry_id not in entry_ids:
            continue
        attachments_by_entry.setdefault(entry_id, []).append(attachment)
    return [
        entry_with_attachments(
            entry,
            attachments_by_entry.get(entry["id"], []),
            files_root=files_root,
        )
        for entry in entries
    ]


def entry_with_attachments(
    entry: dict[str, Any],
    attachments: list[dict[str, Any]],
    *,
    files_root: Path,
) -> dict[str, Any]:
    decorated_entry = dict(entry)
    try:
        metadata = json.loads(str(decorated_entry.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    server_ai_metadata = (
        metadata.get("server_ai")
        if isinstance(metadata, dict) and isinstance(metadata.get("server_ai"), dict)
        else {}
    )
    decorated_entry["server_ai_metadata"] = server_ai_metadata
    decorated_entry["server_ai_peak_call"] = bool(
        server_ai_metadata.get("provider") == "deepseek"
        and server_ai_metadata.get("peak_call") is True
    )
    decorated_attachments = [
        decorate_attachment(
            attachment,
            entry=decorated_entry,
            files_root=files_root,
        )
        for attachment in attachments
    ]
    decorated_entry["attachments"] = decorated_attachments
    decorated_entry["images"] = [
        item for item in decorated_attachments if item["media_type"] == "image"
    ]
    decorated_entry["videos"] = [
        item for item in decorated_attachments if item["media_type"] == "video"
    ]
    decorated_entry["files"] = [
        item for item in decorated_attachments if item["media_type"] == "document"
    ]
    return decorated_entry


def render_project_chat_entry_html(
    request: Request,
    project: dict[str, Any],
    entry: dict[str, Any],
) -> str:
    return templates.env.get_template("partials/project_chat_entry.html").render(
        entry=entry,
        project=project,
        app_path=lambda path: app_path(request, path),
    )


def build_media_library(
    repo: Repository,
    project_id: int,
    files_root: Path,
) -> list[dict[str, Any]]:
    entries = {entry["id"]: entry for entry in repo.list_all_entries(project_id)}
    return [
        decorate_attachment(
            attachment,
            entry=entries.get(attachment.get("entry_id")),
            files_root=files_root,
        )
        for attachment in repo.list_attachments(project_id)
    ]


def build_export(repo: Repository, project: dict[str, Any]) -> str:
    entries = repo.list_project_entries(project["id"])
    return export_project_markdown(
        project=project,
        entries=entries,
        memberships=repo.list_project_memberships(project["id"]),
        attachments=repo.list_attachments_for_entries(
            project["id"], [int(entry["id"]) for entry in entries]
        ),
    )


def build_project_control_markdown(
    project: dict[str, Any], control: dict[str, Any] | None
) -> str:
    active = control or {}
    lines = [
        f"# Project Control: {project['name']}",
        "",
        f"Slug: `{project['slug']}`",
        f"Version: {active.get('version') or 0}",
        "",
        "## Goal",
        active.get("goal") or "(not set)",
        "",
        "## Non-goals",
        active.get("non_goals") or "(not set)",
        "",
        "## Constraints",
        active.get("constraints_text") or "(not set)",
        "",
        "## Current Phase",
        active.get("current_phase") or "(not set)",
        "",
        "## Acceptance",
        active.get("acceptance") or "(not set)",
        "",
        "## Roles",
        active.get("roles") or "(not set)",
        "",
        "## Architecture",
        active.get("architecture") or "(not set)",
        "",
        "## Decisions",
        active.get("decisions") or "(not set)",
        "",
        "## Known Issues",
        active.get("known_issues") or "(not set)",
        "",
        "## Risks",
        active.get("risks") or "(not set)",
        "",
        "## User Preferences",
        active.get("user_preferences") or "(not set)",
        "",
        "## Key Files",
        active.get("key_files") or "(not set)",
        "",
        "## Next Steps",
        active.get("next_steps") or "(not set)",
        "",
        "## Handoff Summary",
        active.get("handoff_summary") or "(not set)",
        "",
    ]
    return "\n".join(lines)


def render_project_detail(request: Request, user: dict[str, Any], project: dict[str, Any]):
    if request.query_params.get('view') != 'archive':
        from .workspace import render_workspace
        return render_workspace(request, user, project)
    repo = get_repo(request)
    files_root = get_settings(request).files_dir
    memory = repo.get_latest_project_control(project["id"])
    handoffs = repo.list_project_handoffs(project["id"])
    sync_snapshots = repo.list_latest_project_sync_snapshots(project["id"])
    memory_version = int((memory or {}).get("version") or 0)
    memory_sync_state = (
        "未同步"
        if not sync_snapshots
        else "已同步"
        if all(
            int(snapshot.get("memory_version") or 0) == memory_version
            for snapshot in sync_snapshots
        )
        else "有差异"
    )
    server_ai_statuses = get_server_ai_runtime(request).statuses(repo, project["id"])
    server_ai_status_by_provider = {
        item["provider"]: item for item in server_ai_statuses
    }
    memberships = annotate_membership_presence(
        repo.list_project_memberships(project["id"]),
        server_ai_status_by_provider=server_ai_status_by_provider,
    )
    chat_roles = repo.list_project_chat_roles(project["id"])
    primary_device = repo.get_project_primary_device(project["id"])
    primary_device_id = primary_device["id"] if primary_device else None
    for member in memberships:
        member["is_primary_device"] = member["device_id"] == primary_device_id
    member_device_ids = {member["device_id"] for member in memberships}
    available_devices = [
        device for device in repo.list_devices() if device["id"] not in member_device_ids
    ]
    for device in available_devices:
        provider = str(device.get("server_ai_provider") or "")
        if provider:
            device["server_ai_status"] = server_ai_status_by_provider.get(provider, {})
    codex_workspaces = attach_codex_conversations_to_workspaces(
        repo.list_codex_workspaces(),
        repo.list_codex_conversations(hub_project_id=project["id"]),
    )
    media_library = build_media_library(repo, project["id"], files_root)
    tasks = repo.list_tasks(project["id"])
    return render(
        request,
        "project_detail.html",
        user=user,
        project=project,
        control=memory,
        memory=memory,
        memory_version=memory_version,
        memory_sync_state=memory_sync_state,
        handoffs=handoffs,
        sync_snapshots=sync_snapshots,
        tasks=tasks,
        agent_workspace=build_agent_workspace(memberships, chat_roles, tasks),
        durable_agents=AgentStore(repo.connection).list(project["id"]),
        agent_conversations=AgentStore(repo.connection).conversations(project["id"]),
        dispatch_key=uuid.uuid4().hex,
        groups=repo.list_groups(),
        projects=repo.list_projects(),
        entries=entries_with_attachments(repo, project["id"], files_root),
        attachments=media_library,
        media_library=media_library,
        media_devices=sorted(
            {item["source_label"] for item in media_library if item["source_label"]}
        ),
        memberships=memberships,
        chat_roles=chat_roles,
        devices=available_devices,
        member_device_ids=member_device_ids,
        primary_device=primary_device,
        codex_workspaces=codex_workspaces,
        default_codex_device_id=default_codex_target_device_id(repo, project["id"]),
        server_ai_statuses=server_ai_statuses,
        server_ai_jobs=repo.list_server_ai_jobs(project["id"], limit=12),
    )


def render_task_detail(
    request: Request,
    user: dict[str, Any],
    project: dict[str, Any],
    task: dict[str, Any],
):
    repo = get_repo(request)
    files_root = get_settings(request).files_dir
    entries = entries_with_attachments(
        repo, project["id"], files_root, task_id=task["id"]
    )
    return render(
        request,
        "task_detail.html",
        user=user,
        project=project,
        task=task,
        assigned_agent=(AgentStore(repo.connection).get(project["id"], task["assigned_agent_id"])
                        if task.get("assigned_agent_id") else None),
        control=repo.get_latest_project_control(project["id"]),
        entries=entries,
        attachments=repo.list_attachments_for_entries(
            project["id"], [int(entry["id"]) for entry in entries]
        ),
        memberships=repo.list_project_memberships(project["id"]),
        prompt_markdown=repo.build_task_prompt_markdown(project["slug"], task["id"]),
        statuses=sorted(TASK_STATUS_VALUES),
    )


def annotate_membership_presence(
    memberships: list[dict[str, Any]],
    server_ai_status_by_provider: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    annotated: list[dict[str, Any]] = []
    for member in memberships:
        copy = dict(member)
        server_ai_provider = str(copy.get("server_ai_provider") or "")
        if server_ai_provider:
            status = (server_ai_status_by_provider or {}).get(server_ai_provider, {})
            state = str(status.get("state") or "disabled")
            labels = {
                "ready": "服务器直连已就绪",
                "awaiting_key": "等待 API Key",
                "configuration_error": "服务器配置错误",
                "quota_exhausted": "今日调用额度已用完",
                "disabled": "服务器直连未启用",
            }
            ready = state == "ready"
            copy["is_online"] = ready
            copy["is_web_online"] = False
            copy["is_agent_online"] = ready
            copy["presence_mode"] = f"server-ai-{state}"
            copy["presence_label"] = labels.get(state, "服务器 AI 不可用")
            copy["server_ai_state"] = state
            annotated.append(copy)
            continue
        seen_at = parse_iso_datetime(str(copy.get("last_seen_at") or ""))
        web_seen_at = parse_iso_datetime(str(copy.get("last_web_seen_at") or ""))
        agent_seen_at = parse_iso_datetime(str(copy.get("last_agent_seen_at") or ""))
        seconds_since_seen = seconds_since(now, seen_at)
        seconds_since_web = seconds_since(now, web_seen_at)
        seconds_since_agent = seconds_since(now, agent_seen_at)
        is_web_online = is_recent(seconds_since_web)
        is_agent_online = is_recent(seconds_since_agent)
        copy["is_online"] = is_recent(seconds_since_seen)
        copy["is_web_online"] = is_web_online
        copy["is_agent_online"] = is_agent_online
        copy["presence_mode"] = (
            "agent" if is_agent_online else "web" if is_web_online else "offline"
        )
        copy["presence_label"] = presence_label(
            device_label=str(copy.get("device_label") or ""),
            seconds_since_agent=seconds_since_agent,
            seconds_since_web=seconds_since_web,
        )
        annotated.append(copy)
    return annotated


def seconds_since(now: datetime, seen_at: datetime | None) -> float | None:
    return (now - seen_at).total_seconds() if seen_at else None


def is_recent(seconds: float | None) -> bool:
    return seconds is not None and 0 <= seconds <= DEVICE_ONLINE_WINDOW_SECONDS


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def presence_label(
    device_label: str,
    seconds_since_agent: float | None,
    seconds_since_web: float | None,
) -> str:
    agent_name = agent_display_name(device_label)
    if is_recent(seconds_since_agent):
        return f"{agent_name}已接入"
    if is_recent(seconds_since_web):
        return f"网页登录在线，{agent_name}未接入"
    latest = min(
        [value for value in [seconds_since_agent, seconds_since_web] if value is not None],
        default=None,
    )
    if latest is None:
        return "未连接"
    if 0 <= latest <= DEVICE_ONLINE_WINDOW_SECONDS:
        minutes = max(1, int(latest // 60))
        return f"{minutes} 分钟前在线"
    return "离线"


def agent_display_name(device_label: str) -> str:
    label = device_label.strip().casefold()
    if label.startswith("hermes"):
        return "Hermes"
    return "Codex"


def render_admin_devices(
    request: Request,
    user: dict[str, Any],
    created_token: str | None,
):
    repo = get_repo(request)
    return render(
        request,
        "admin_devices.html",
        user=user,
        groups=repo.list_groups(),
        devices=repo.list_devices(),
        tokens=repo.list_tokens(),
        created_token=created_token,
    )


app = create_app()
