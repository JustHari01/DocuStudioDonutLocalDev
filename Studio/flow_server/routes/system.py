#!/usr/bin/env python3
"""System routes for Flow Agent.

Extension transport (WebSocket / HTTP callback), model listing, credits,
token refresh, and health/liveness endpoints.
"""

import time
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Header, WebSocket

from flow_server import state
from flow_server.state import verify_api_key, get_active_bridge

# Setup logging (format configured centrally in flow_engine/__init__.py, imported above)
log = logging.getLogger("flow_engine.openai_api")

router = APIRouter()


# Extension WebSocket and Callback Endpoints
@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("Extension connecting via FastAPI WebSocket...")
    bridge = state.get_bridge()
    if bridge:
        await bridge.handle_fastapi_ws(websocket)
    else:
        log.error("Bridge not initialized")
        await websocket.close()

@router.post("/api/ext/callback")
async def http_callback(body: dict):
    bridge = state.get_bridge()
    if bridge:
        success = bridge.handle_http_callback(body)
        return {"ok": success}
    return {"ok": False}


# OpenAI Endpoints

@router.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """List available Google Flow models (OpenAI format)."""
    return {
        "object": "list",
        "data": [
            {"id": "harbor_seal", "object": "model", "created": int(time.time()), "owned_by": "google"}
        ]
    }


@router.get("/v1/credits", dependencies=[Depends(verify_api_key)])
async def get_flow_credits(x_client_id: Optional[str] = Header(None, alias="X-Client-Id")):
    """Credits for connected Chrome clients.

    - With an X-Client-Id header: returns just that client's credits.
    - Without it: queries every connected client and returns per-client
      breakdown plus the cumulative pool across all browsers.
    """
    active_bridge = await get_active_bridge()

    # Specific client requested → return only that one.
    if x_client_id:
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=x_client_id,
            )
            return res
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Otherwise fan out across all connected clients.
    client_ids = active_bridge.active_client_ids()
    if not client_ids:
        raise HTTPException(status_code=503, detail="No extensions connected")

    async def _one(cid):
        try:
            res = await active_bridge.api_request(
                "/v1/credits", body=None, captcha_action=None, method="GET",
                client_id=cid,
            )
            data = res.get("data", {}) if isinstance(res, dict) else {}
            return {
                "client_id": cid,
                "credits": int(data.get("credits", 0)),
                "tier": data.get("userPaygateTier"),
                "sku": data.get("sku"),
                "ok": res.get("status") == 200,
            }
        except Exception as e:
            return {"client_id": cid, "credits": 0, "ok": False, "error": str(e)}

    clients = await asyncio.gather(*[_one(cid) for cid in client_ids])
    total = sum(c["credits"] for c in clients if c.get("ok"))
    return {
        "total_credits": total,
        "client_count": len(clients),
        "clients": clients,
    }


@router.post("/v1/refresh-tokens", dependencies=[Depends(verify_api_key)])
async def refresh_tokens(
    force: bool = False,
    reload_page: bool = False,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Recover one browser client without multiplying Flow windows.

    Normal refresh nudges token-less clients. A forced UI recovery deliberately
    targets only one live client (preferring one that already owns a Flow tab)
    and preserves the page. ``reload_page`` is reserved for an explicit stale
    API-token repair and is never used by visible image generation.
    """
    active_bridge = await get_active_bridge()

    if x_client_id:
        targets = [x_client_id]
    elif force:
        active_clients = list(active_bridge.active_client_ids())
        targets = sorted(
            active_clients,
            key=lambda client_id: (
                int(active_bridge._flow_tab_counts.get(client_id, 0) or 0) <= 0,
                "ui_text_to_image" not in active_bridge._capabilities.get(
                    client_id, set()
                ),
            ),
        )[:1]
    else:
        targets = [cid for cid in active_bridge.active_client_ids()
                   if not active_bridge._tokens.get(cid)]

    if not targets:
        return {"nudged": 0, "clients": [], "detail": "All connected clients already have tokens"}

    async def _nudge(cid):
        try:
            if force and reload_page:
                refreshed = await active_bridge._force_refresh_client(cid)
                return {
                    "client_id": cid,
                    "ok": bool(refreshed),
                    "refreshed": bool(refreshed),
                    "page_reloaded": bool(refreshed),
                    "deferred": not bool(refreshed),
                }
            if force:
                await active_bridge._recover_ui_client(cid)
                return {
                    "client_id": cid,
                    "ok": True,
                    "refreshed": True,
                    "page_reloaded": False,
                }
            await active_bridge.send_message_to(cid, {"method": "open_flow_tab"})
            await asyncio.sleep(6)
            await active_bridge.send_message_to(cid, {"method": "refresh_flow_tab"})
            return {"client_id": cid, "ok": True}
        except Exception as e:
            return {"client_id": cid, "ok": False, "error": str(e)}

    results = await asyncio.gather(*[_nudge(cid) for cid in targets])
    return {"nudged": len(results), "clients": results}


@router.post("/v1/prepare-flow-tab", dependencies=[Depends(verify_api_key)])
async def prepare_flow_tab(
    existing_only: bool = False,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Prepare Flow through the resident extension without changing focus.

    Do not use ``get_active_bridge`` here: a client with no Flow tab is still
    exactly the client that needs this command, and the extension can create a
    background tab in an existing Edge window.
    """
    active_bridge = state.get_bridge()
    if not active_bridge:
        raise HTTPException(status_code=503, detail="Extension bridge is not initialized")
    result = await active_bridge.prepare_flow_tab(
        client_id=x_client_id,
        existing_only=existing_only,
    )
    if result.get("status") != 200:
        raise HTTPException(
            status_code=int(result.get("status") or 503),
            detail=result.get("error") or "FLOW_PREPARE_TAB_FAILED",
        )
    return result


@router.post("/v1/reload-extension", dependencies=[Depends(verify_api_key)])
async def reload_extension(
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Reload the resident Flow Agent worker in place, without a popup tab."""
    active_bridge = state.get_bridge()
    if not active_bridge:
        raise HTTPException(status_code=503, detail="Extension bridge is not initialized")
    result = await active_bridge.reload_extension(client_id=x_client_id)
    if result.get("status") != 200:
        raise HTTPException(
            status_code=int(result.get("status") or 503),
            detail=result.get("error") or "FLOW_EXTENSION_RELOAD_FAILED",
        )
    return result


@router.post("/v1/ui/inspect-configuration", dependencies=[Depends(verify_api_key)])
async def inspect_ui_configuration(
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Read and enforce the visible Flow model/count controls without a prompt."""
    active_bridge = await get_active_bridge()
    result = await active_bridge.ui_inspect_configuration(client_id=x_client_id)
    if result.get("status") != 200:
        raise HTTPException(
            status_code=int(result.get("status") or 500),
            detail=result.get("error") or "FLOW_UI_CONFIGURATION_INSPECTION_FAILED",
        )
    return result


@router.post("/v1/ui/project-snapshot", dependencies=[Depends(verify_api_key)])
async def inspect_ui_project_snapshot(
    payload: Optional[dict] = None,
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Read visible Flow project tiles for safe checkpoint reconciliation."""
    active_bridge = await get_active_bridge()
    body = payload if isinstance(payload, dict) else {}
    prompts = body.get("prompts") if isinstance(body.get("prompts"), list) else []
    debug = body.get("debug") is True
    inspect_tile_prompts = body.get("inspectTilePrompts") is True
    active_tab_only = body.get("activeTabOnly") is True
    workspace_id = str(body.get("workspaceId") or "").strip()
    flow_project_id = str(body.get("flowProjectId") or "").strip()
    result = await active_bridge.ui_project_snapshot(
        client_id=x_client_id,
        prompts=prompts,
        debug=debug,
        inspect_tile_prompts=inspect_tile_prompts,
        active_tab_only=active_tab_only,
        workspace_id=workspace_id,
        flow_project_id=flow_project_id,
    )
    if result.get("status") != 200:
        raise HTTPException(
            status_code=int(result.get("status") or 500),
            detail=result.get("error") or "FLOW_UI_PROJECT_SNAPSHOT_FAILED",
        )
    return result


@router.get("/v1/ui/status", dependencies=[Depends(verify_api_key)])
async def inspect_ui_status(
    x_client_id: Optional[str] = Header(None, alias="X-Client-Id"),
):
    """Read extension job phase and queue state without touching the Flow page."""
    active_bridge = await get_active_bridge()
    result = await active_bridge.ui_client_status(client_id=x_client_id)
    if result.get("status") not in (None, 200):
        raise HTTPException(
            status_code=int(result.get("status") or 500),
            detail=result.get("error") or "FLOW_UI_STATUS_FAILED",
        )
    return result


@router.get("/")
async def root():
    return {"status": "running", "service": "Flow Agent API"}


# Health Check
@router.get("/health")
async def health():
    bridge = state.get_bridge()
    if not bridge:
        return {"status": "starting", "connected": False}
    active_clients = bridge.active_client_ids()
    browser_open = any(
        bridge._browser_open.get(client_id, True) for client_id in active_clients
    )
    flow_tab_count = sum(
        max(0, int(bridge._flow_tab_counts.get(client_id, 0) or 0))
        for client_id in active_clients
    )
    capabilities = sorted(
        {
            capability
            for client_id in active_clients
            for capability in bridge._capabilities.get(client_id, set())
        }
    )
    active_versions = sorted(
        {
            str(bridge._extension_versions.get(client_id, "")).strip()
            for client_id in active_clients
            if str(bridge._extension_versions.get(client_id, "")).strip()
        }
    )
    return {
        "status": "healthy" if await bridge.health_check() else "unauthorized_or_disconnected",
        "extension_connected": bool(active_clients),
        "has_flow_key": any(
            bridge._tokens.get(client_id) is not None for client_id in active_clients
        ),
        "browser_open": browser_open,
        "flow_tab_count": flow_tab_count,
        "capabilities": capabilities,
        # An empty version means an older extension is still resident in Edge.
        # The desktop client can then ask for one Edge restart instead of
        # attempting every prompt with stale UI selectors.
        "extension_version": active_versions[-1] if active_versions else "",
        "clients": [
            {
                "id": client_id,
                "extension_version": bridge._extension_versions.get(client_id, ""),
                "state": bridge._states.get(client_id),
                "has_token": bridge._tokens.get(client_id) is not None,
                "browser_open": bridge._browser_open.get(client_id, True),
                "flow_tab_count": bridge._flow_tab_counts.get(client_id, 0),
                "capabilities": sorted(
                    bridge._capabilities.get(client_id, set())
                ),
                "ui_quota_blocked": (
                    bridge._ui_quota_blocked_until.get(client_id, 0.0)
                    > time.monotonic()
                ),
            }
            for client_id in active_clients
        ],
    }
