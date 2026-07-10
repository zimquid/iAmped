"""Plex connection + OAuth sign-in routes."""
from __future__ import annotations

import time
import uuid
import webbrowser

from flask import Blueprint, jsonify, request

from .. import config, plex_client, providers
from .. import server

bp = Blueprint("auth", __name__)

PLEX_OAUTH: dict[str, dict] = {}
PLEX_OAUTH_TTL = 300


def _connection_result(connection) -> dict:
    connection = providers.normalize(connection)
    info = connection.provider.server_info(connection)
    sections = connection.provider.music_sections(connection)
    if sections and not config.load()["music_section"]:
        config.save({"music_section": sections[0]["id"]})
    return {"ok": True, "provider": connection.kind, "source_id": connection.source_id,
            "server": info, "sections": sections,
            "capabilities": connection.capabilities.as_dict()}


def _store_server(plex) -> dict:
    cfg = config.load()
    plex_source = {
        "id": "plex", "kind": "plex", "name": "Plex",
        "baseurl": plex._baseurl, "token": plex._token,
        "device_id": cfg.get("plex_client_id", ""),
    }
    sources = [source for source in cfg.get("sources", [])
               if source.get("id") != "plex"] + [plex_source]
    config.save({
        "plex_baseurl": plex._baseurl,
        "plex_token": plex._token,
        "sources": sources,
        "active_source_id": "plex",
    })
    connection = providers.ProviderConnection(
        providers.provider_for("plex"), plex,
        {"id": "plex", "kind": "plex", "name": "Plex",
         "baseurl": plex._baseurl, "token": plex._token})
    server._state["server"] = connection
    server._state["source_id"] = "plex"
    return _connection_result(connection)


def _store_connection(connection: providers.ProviderConnection) -> dict:
    source = dict(connection.source)
    # Jellyfin returns a durable per-user token after password login; retain
    # that token but not the plaintext password.
    if connection.kind == "jellyfin" and source.get("token"):
        source["password"] = ""
    info = connection.provider.server_info(connection)
    source["name"] = source.get("name") or info.get("name") or connection.provider.display_name
    config.upsert_source(source, active=True)
    server._state["server"] = connection
    server._state["source_id"] = connection.source_id
    return _connection_result(connection)


def _oauth_server_view(resource) -> dict:
    return {
        "id": resource.clientIdentifier,
        "name": resource.name,
        "owned": bool(resource.owned),
        "online": bool(resource.presence),
        "platform": resource.platform or "",
    }


def _expire_oauth_logins() -> None:
    cutoff = time.time() - PLEX_OAUTH_TTL
    for login_id, state in list(PLEX_OAUTH.items()):
        if state["created"] < cutoff:
            PLEX_OAUTH.pop(login_id, None)


@bp.post("/api/connect")
def api_connect():
    """Legacy Plex manual-connect endpoint.

    New clients use /api/sources/connect and select a provider explicitly.
    """
    data = dict(request.json or {})
    data.setdefault("kind", "plex")
    return _connect_source(data)


def _connect_source(data: dict):
    kind = str(data.get("kind") or "plex").lower()
    baseurl = (data.get("baseurl") or "").strip()
    if kind == "plex" and (not baseurl or not (data.get("token") or "").strip()):
        return jsonify({"ok": False, "error": "Enter both a Plex server URL and token, or use Sign in with Plex."}), 400
    try:
        source = providers.source_from_payload(data)
        connection = providers.connect(source)
        return jsonify(_store_connection(connection))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.get("/api/sources")
def api_sources():
    cfg = config.load()
    return jsonify({"sources": [providers.public_source(
        source, source.get("id") == cfg.get("active_source_id"))
        for source in cfg.get("sources", [])], "kinds": providers.kinds(),
                    "active_source_id": cfg.get("active_source_id")})


@bp.post("/api/sources/connect")
def api_connect_source():
    return _connect_source(dict(request.json or {}))


@bp.post("/api/sources/active")
def api_active_source():
    source_id = str((request.json or {}).get("source_id") or "")
    try:
        cfg = config.load()
        source = next((item for item in cfg.get("sources", [])
                       if item.get("id") == source_id), None)
        if source is None:
            raise ValueError("Unknown media source.")
        # Verify before changing the persisted active source. A temporarily
        # unreachable NAS should not strand the app on a dead selection.
        connection = providers.connect(source)
        config.set_active_source(source_id)
        server._state["server"] = connection
        server._state["source_id"] = source_id
        return jsonify(_connection_result(connection))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.post("/api/plex/oauth/start")
def api_plex_oauth_start():
    _expire_oauth_logins()
    cfg = config.load()
    client_id = cfg["plex_client_id"] or uuid.uuid4().hex
    if not cfg["plex_client_id"]:
        config.save({"plex_client_id": client_id})
    try:
        login = plex_client.start_oauth(client_id)
        auth_url = login.oauthUrl()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Could not start Plex sign-in: {exc}"}), 502

    login_id = uuid.uuid4().hex
    PLEX_OAUTH[login_id] = {
        "created": time.time(),
        "login": login,
        "resources": None,
    }
    # The desktop webview cannot reliably create an external popup on every
    # platform, so ask the OS to open Plex in the user's normal browser.
    try:
        webbrowser.open(auth_url, new=2)
    except Exception:
        pass
    return jsonify({
        "login_id": login_id,
        "auth_url": auth_url,
        "expires_in": PLEX_OAUTH_TTL,
    })


@bp.get("/api/plex/oauth/status/<login_id>")
def api_plex_oauth_status(login_id):
    state = PLEX_OAUTH.get(login_id)
    if not state:
        return jsonify({
            "status": "expired",
            "error": "Plex sign-in expired. Start it again.",
        }), 404
    if time.time() - state["created"] >= PLEX_OAUTH_TTL:
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "expired",
            "error": "Plex sign-in expired. Start it again.",
        }), 410

    if state["resources"] is not None:
        return jsonify({
            "status": "authorized",
            "servers": [_oauth_server_view(r)
                        for r in state["resources"].values()],
        })

    login = state["login"]
    try:
        if not login.checkLogin():
            return jsonify({"status": "pending"})
        account = plex_client.oauth_account(login.token)
        resources = plex_client.account_servers(account)
    except Exception as exc:  # noqa: BLE001
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "error",
            "error": f"Plex sign-in failed: {exc}",
        }), 502

    if not resources:
        PLEX_OAUTH.pop(login_id, None)
        return jsonify({
            "status": "error",
            "error": "No Plex Media Servers are available to this account.",
        }), 404
    state["resources"] = {r.clientIdentifier: r for r in resources}
    return jsonify({
        "status": "authorized",
        "servers": [_oauth_server_view(r) for r in resources],
    })


@bp.post("/api/plex/oauth/connect")
def api_plex_oauth_connect():
    data = request.json or {}
    login_id = data.get("login_id")
    server_id = data.get("server_id")
    state = PLEX_OAUTH.get(login_id)
    if not state or state["resources"] is None:
        return jsonify({"error": "Plex sign-in is not ready or has expired."}), 400
    resource = state["resources"].get(server_id)
    if resource is None:
        return jsonify({"error": "Select an available Plex Media Server."}), 400
    try:
        plex = plex_client.connect_resource(resource)
        result = _store_server(plex)
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "error": f"Could not connect to {resource.name}: {exc}",
        }), 502
    PLEX_OAUTH.pop(login_id, None)
    return jsonify(result)


server.app.register_blueprint(bp)
