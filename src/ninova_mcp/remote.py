from __future__ import annotations

import contextlib
import os
import sys
from urllib.parse import urlparse

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import update_check
from .env import env_choice, env_switch, load_ninova_env
from .mail_send import mail_drafts_enabled, mail_send_enabled
from .remote_security import (
    RemoteSecurityMiddleware,
    api_key_required,
    build_rate_limiter_from_env,
    configured_api_key,
)
from .server import (
    LOCAL_TOOL_NAMES,
    HELP_TICKET_WRITE_TOOL_NAMES,
    MAIL_DRAFT_TOOL_NAMES,
    MAIL_SEND_TOOL_NAMES,
    MAIL_TOOL_NAMES,
    MESSAGE_BOARD_WRITE_TOOL_NAMES,
    OBS_REGISTRATION_WRITE_TOOL_NAMES,
    REMOTE_TOOL_NAMES,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    NinovaMcpApp,
    apply_server_version,
    help_ticket_writes_enabled,
    message_board_writes_enabled,
    obs_registration_writes_enabled,
    register_prompts,
    register_resources,
    register_tools,
    server_instructions,
)
from .remote_files import (
    REMOTE_FILE_INSTRUCTIONS, REMOTE_FILE_TOOLS, REMOTE_WRITE_TOOLS, RemoteNinovaMcpApp,
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv_env(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def remote_file_capabilities() -> tuple[bool, bool]:
    """Return whether remote file and write tools are enabled.

    Both default to on when their prerequisites exist (API-key authentication
    and a managed NINOVA_REMOTE_FILE_ROOT), and stay off otherwise so a basic
    setup still starts. An explicit 0 turns them off; an explicit 1 without
    the prerequisites fails at startup.
    """
    ready = bool(configured_api_key()) and bool((os.getenv("NINOVA_REMOTE_FILE_ROOT") or "").strip())
    files = env_choice("NINOVA_REMOTE_ENABLE_FILES")
    writes = env_choice("NINOVA_REMOTE_ENABLE_WRITES")
    return (ready if files is None else files, ready if writes is None else writes)


def _selected_remote_tool_names() -> list[str]:
    """Apply sensitive-tool switches and an optional server allowlist."""
    eligible = set(REMOTE_TOOL_NAMES)
    files_enabled, writes_enabled = remote_file_capabilities()
    if files_enabled:
        eligible.update(REMOTE_FILE_TOOLS)
    if writes_enabled:
        eligible.update(REMOTE_WRITE_TOOLS)
    mail_enabled = env_switch("NINOVA_REMOTE_ENABLE_MAIL")
    if mail_enabled and writes_enabled and mail_send_enabled():
        eligible.update(MAIL_SEND_TOOL_NAMES)
    if mail_enabled and writes_enabled and mail_drafts_enabled():
        eligible.update(MAIL_DRAFT_TOOL_NAMES)
    if writes_enabled and help_ticket_writes_enabled():
        eligible.update(HELP_TICKET_WRITE_TOOL_NAMES)
    if writes_enabled and os.getenv("NINOVA_COURSEHUB_WRITES", "0") == "1":
        from .course_hub_tools import COURSEHUB_WRITE_TOOLS
        eligible.update(COURSEHUB_WRITE_TOOLS)
    if writes_enabled and message_board_writes_enabled():
        eligible.update(MESSAGE_BOARD_WRITE_TOOL_NAMES)
    available = [
        name
        for name in LOCAL_TOOL_NAMES
        if name in eligible and (
            mail_enabled
            or name not in MAIL_TOOL_NAMES
        ) and (
            obs_registration_writes_enabled()
            or name not in OBS_REGISTRATION_WRITE_TOOL_NAMES
        )
    ]
    configured = _split_csv_env("NINOVA_REMOTE_ALLOWED_TOOLS")
    if not configured:
        return available

    remote_names = set(available)
    unknown = sorted(set(configured) - remote_names)
    if unknown:
        raise RuntimeError(
            "NINOVA_REMOTE_ALLOWED_TOOLS contains unknown, disabled, or "
            "remote-excluded tools: "
            + ", ".join(unknown)
        )
    allowed = set(configured)
    return [name for name in available if name in allowed]


def _normalize_mount_path(value: str | None, default: str) -> str:
    path = (value or default).strip()
    if not path:
        return default
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def _build_transport_security() -> TransportSecuritySettings | None:
    public_base_url = os.getenv("NINOVA_PUBLIC_BASE_URL")
    allowed_hosts = _split_csv_env("NINOVA_ALLOWED_HOSTS")
    allowed_origins = _split_csv_env("NINOVA_ALLOWED_ORIGINS")

    if public_base_url:
        parsed = urlparse(public_base_url)
        if parsed.netloc:
            allowed_hosts.append(parsed.netloc)
        if parsed.scheme and parsed.netloc:
            allowed_origins.append(f"{parsed.scheme}://{parsed.netloc}")

    allowed_origins.extend(["https://claude.ai", "https://claude.com"])
    allowed_hosts = sorted(set(allowed_hosts))
    allowed_origins = sorted(set(allowed_origins))

    if not allowed_hosts:
        if _env_flag("NINOVA_DISABLE_DNS_REBINDING_PROTECTION", default=False):
            return TransportSecuritySettings(enable_dns_rebinding_protection=False)
        return None

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=not _env_flag(
            "NINOVA_DISABLE_DNS_REBINDING_PROTECTION",
            default=False,
        ),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _build_fastmcp(app_logic: NinovaMcpApp, mount_path: str) -> FastMCP:
    security_settings = _build_transport_security()
    host = os.getenv("NINOVA_REMOTE_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("NINOVA_REMOTE_PORT") or "8000")
    mcp = FastMCP(
        SERVER_NAME,
        instructions=server_instructions(SERVER_INSTRUCTIONS + REMOTE_FILE_INSTRUCTIONS)
        + update_check.update_instructions(app_logic.state_dir),
        host=host,
        port=port,
        json_response=True,
        streamable_http_path="/",
        stateless_http=False,
        transport_security=security_settings,
        website_url=os.getenv("NINOVA_PUBLIC_BASE_URL"),
    )

    apply_server_version(mcp)
    register_tools(mcp, app_logic, _selected_remote_tool_names())
    # Prompts and resources are read-only text/JSON with no filesystem or
    # write access, so unlike the excluded tools they are safe to expose on
    # the hosted transport as-is.
    register_prompts(mcp)
    register_resources(mcp)

    # Keep the mounted path on the instance for observability/debug logs if needed.
    mcp.mount_path = mount_path  # type: ignore[attr-defined]
    return mcp


def build_app() -> Starlette:
    load_ninova_env()

    if _env_flag("NINOVA_REMOTE_REQUIRE_API_KEY", default=False) and not configured_api_key():
        raise RuntimeError(
            "NINOVA_REMOTE_REQUIRE_API_KEY=1 but NINOVA_REMOTE_API_KEY is not set."
        )

    extended = any(remote_file_capabilities())
    if extended and not configured_api_key():
        raise RuntimeError("Remote file/write tools require API-key authentication.")
    app_logic = RemoteNinovaMcpApp() if extended else NinovaMcpApp()
    app_logic.remote_transport = True
    app_logic.remote_writes_enabled = remote_file_capabilities()[1]
    mcp_mount_path = _normalize_mount_path(os.getenv("NINOVA_REMOTE_MCP_PATH"), "/mcp")
    mcp = _build_fastmcp(app_logic, mount_path=mcp_mount_path)
    api_key = configured_api_key()
    rate_limiter = build_rate_limiter_from_env()
    app_logic.remote_security = {"api_key_required": api_key_required(),
                                 "rate_limit_enabled": rate_limiter is not None}

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            yield

    async def root(_: Request) -> JSONResponse:
        security = mcp.settings.transport_security
        return JSONResponse(
            {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "transport": "streamable-http",
                "status": "ok",
                "mcp_path": mcp_mount_path,
                "api_key_required": api_key_required(),
                "rate_limit_enabled": rate_limiter is not None,
                "dns_rebinding_protection_enabled": bool(
                    security and security.enable_dns_rebinding_protection
                ),
            }
        )

    async def healthz(_: Request) -> JSONResponse:
        # Lightweight: do not force a full Ninova login on every probe unless asked.
        if _env_flag("NINOVA_HEALTHZ_CHECK_AUTH", default=False):
            status = app_logic.auth_status(debug=True)
            return JSONResponse(
                {
                    "ok": bool(status.get("authenticated")),
                    "credentials_present": bool(status.get("credentials_present")),
                    "authenticated": bool(status.get("authenticated")),
                    "state_dir": status.get("state_dir"),
                    "api_key_required": api_key_required(),
                },
                status_code=200 if status.get("authenticated") else 503,
            )
        credentials_present = bool(os.getenv("NINOVA_USERNAME") and os.getenv("NINOVA_PASSWORD"))
        return JSONResponse(
            {
                "ok": True,
                "credentials_present": credentials_present,
                "api_key_required": api_key_required(),
                "rate_limit_enabled": rate_limiter is not None,
            }
        )

    starlette_app = Starlette(
        routes=[
            Route("/", root),
            Route("/healthz", healthz),
            Mount(mcp_mount_path, app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    starlette_app.add_middleware(
        RemoteSecurityMiddleware,
        mcp_mount_path=mcp_mount_path,
        api_key=api_key,
        rate_limiter=rate_limiter,
        protect_all=_env_flag("NINOVA_REMOTE_PROTECT_ALL", default=False),
    )
    return starlette_app


app = build_app()


def main() -> None:
    load_ninova_env()
    if _env_flag("NINOVA_REMOTE_REQUIRE_API_KEY", default=False) and not configured_api_key():
        print(
            "error: NINOVA_REMOTE_REQUIRE_API_KEY=1 requires NINOVA_REMOTE_API_KEY",
            file=sys.stderr,
        )
        raise SystemExit(2)

    host = os.getenv("NINOVA_REMOTE_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("NINOVA_REMOTE_PORT") or "8000")
    # Off the request path; a found update is logged and shown in auth_status.
    update_check.start_background_check()
    uvicorn.run(
        "ninova_mcp.remote:app",
        host=host,
        port=port,
        reload=False,
        factory=False,
    )


if __name__ == "__main__":
    main()
