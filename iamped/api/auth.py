"""Plex connection + OAuth sign-in routes."""
from __future__ import annotations

import time
import uuid
import webbrowser

from flask import Blueprint, jsonify, request

from .. import config, plex_client
from .. import server

bp = Blueprint("auth", __name__)

PLEX_OAUTH: dict[str, dict] = {}
PLEX_OAUTH_TTL = 300


def _connection_result(plex) -> dict:
    info = plex_client.server_info(plex)
    sections = plex_client.music_sections(plex)
    if sections and not config.load()["music_section"]:
        config.save({"music_section": sections[0]})
    return {"ok": True, "server": info, "sections": sections}


def _store_server(plex) -> dict:
    config.save({
        "plex_baseurl": plex._baseurl,
        "plex_token": plex._token,
    })
    server._state["server"] = plex
    return _connection_result(plex)


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
    data = request.json or {}
    baseurl = (data.get("baseurl") or "").strip()
    token = (data.get("token") or "").strip()
    if not baseurl or not token:
        return jsonify({
            "ok": False,
            "error": "Enter both a server URL and token, or use Sign in with Plex.",
        }), 400
    try:
        plex = plex_client.connect(baseurl, token)
        result = _connection_result(plex)
        config.save({"plex_baseurl": plex._baseurl,
                     "plex_token": plex._token})
        server._state["server"] = plex
        return jsonify(result)
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
