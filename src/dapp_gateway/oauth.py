"""Sign in to an MCP server that uses OAuth (the MCP authorization spec), as a client, for each person.

    discover(url)      the MCP server answers 401; its protected-resource metadata names an authorization server,
                       whose metadata gives the authorize, token and registration endpoints
    register(...)      Dynamic Client Registration: the gateway becomes a public client with PKCE
    authorize_url(...) where to send a person's browser (state, S256 challenge, resource)
    exchange/refresh   code -> tokens, refresh token -> tokens

Every URL the gateway fetches here goes through check (public https only), so metadata can't point the gateway at its
own network. Nothing here stores anything: the caller seals and keeps the results.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

CLIENT_NAME = "DataOS Apps gateway"
PROBE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "dapp-gateway", "version": "1"}}}


class OAuthError(RuntimeError):
    pass


class NoSignIn(OAuthError):
    """The server answered without asking anyone to sign in: it needs no OAuth."""


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _json(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400:
        raise OAuthError(f"{what}: HTTP {r.status_code} {r.text[:200]}")
    try:
        body = r.json()
    except ValueError as e:
        raise OAuthError(f"{what}: not JSON") from e
    if not isinstance(body, dict):
        raise OAuthError(f"{what}: unexpected reply")
    return body


def _well_known(url: str, name: str) -> list[str]:
    """RFC 8414 / 9728 places: /.well-known/<name><path> first, then /.well-known/<name> at the origin."""
    p = urlsplit(url)
    path = p.path.rstrip("/")
    out = [urlunsplit((p.scheme, p.netloc, f"/.well-known/{name}{path}", "", ""))] if path else []
    return [*out, urlunsplit((p.scheme, p.netloc, f"/.well-known/{name}", "", ""))]


def discover(url: str, http: httpx.Client, check) -> dict[str, Any]:
    """The OAuth settings of an MCP server, or NoSignIn when it answers without a token."""
    r = http.post(url, json=PROBE, headers={"accept": "application/json, text/event-stream"})
    if r.status_code < 400:
        raise NoSignIn("the server answered without asking to sign in")
    if r.status_code not in (401, 403):
        raise OAuthError(f"the server answered HTTP {r.status_code}, not a sign-in request")
    m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", ""))
    candidates = [m[1]] if m else _well_known(url, "oauth-protected-resource")
    prm = None
    for c in candidates:
        resp = http.get(check(c))
        if resp.status_code < 400:
            prm = _json(resp, "protected resource metadata")
            break
    if prm is None:   # older servers: the authorization server is the MCP server's own origin
        p = urlsplit(url)
        prm = {"authorization_servers": [urlunsplit((p.scheme, p.netloc, "", "", ""))], "resource": url}
    servers = prm.get("authorization_servers") or []
    if not servers:
        raise OAuthError("the server names no authorization server")
    issuer = check(servers[0])
    asm = None
    for c in [*_well_known(issuer, "oauth-authorization-server"), *_well_known(issuer, "openid-configuration")]:
        resp = http.get(check(c))
        if resp.status_code < 400:
            asm = _json(resp, "authorization server metadata")
            break
    if asm is None:
        raise OAuthError(f"no authorization server metadata at {issuer}")
    for key in ("authorization_endpoint", "token_endpoint"):
        if not asm.get(key):
            raise OAuthError(f"authorization server metadata has no {key}")
        check(asm[key])
    if asm.get("registration_endpoint"):
        check(asm["registration_endpoint"])
    methods = asm.get("code_challenge_methods_supported")
    if methods and "S256" not in methods:
        raise OAuthError("the authorization server does not support PKCE with S256")
    scopes = prm.get("scopes_supported") or asm.get("scopes_supported") or []
    return {"issuer": issuer, "resource": prm.get("resource") or url,
            "authorization_endpoint": asm["authorization_endpoint"], "token_endpoint": asm["token_endpoint"],
            "registration_endpoint": asm.get("registration_endpoint", ""),
            "scope": " ".join(scopes) if isinstance(scopes, list) else ""}


def register(meta: dict[str, Any], redirect_uri: str, http: httpx.Client) -> dict[str, Any]:
    """Dynamic Client Registration (RFC 7591) as a public client: PKCE, no secret, unless the server insists."""
    if not meta.get("registration_endpoint"):
        raise OAuthError("the server has no dynamic client registration; enter a client ID from its admin")
    body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri], "grant_types": ["authorization_code",
            "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}
    reply = _json(http.post(meta["registration_endpoint"], json=body), "client registration")
    if not reply.get("client_id"):
        raise OAuthError("client registration returned no client_id")
    return {"client_id": reply["client_id"], "client_secret": reply.get("client_secret", ""),
            "auth_method": reply.get("token_endpoint_auth_method") or ("client_secret_basic"
                                                                       if reply.get("client_secret") else "none")}


def authorize_url(meta: dict[str, Any], *, state: str, challenge: str) -> str:
    q = {"response_type": "code", "client_id": meta["client_id"], "redirect_uri": meta["redirect_uri"],
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]}
    if meta.get("scope"):
        q["scope"] = meta["scope"]
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return meta["authorization_endpoint"] + sep + urlencode(q)


def _token(meta: dict[str, Any], form: dict[str, str], http: httpx.Client) -> dict[str, Any]:
    form = {**form, "resource": meta["resource"]}
    auth = None
    if meta.get("client_secret") and meta.get("auth_method") == "client_secret_post":
        form.update(client_id=meta["client_id"], client_secret=meta["client_secret"])
    elif meta.get("client_secret"):
        auth = (meta["client_id"], meta["client_secret"])
    else:
        form["client_id"] = meta["client_id"]
    reply = _json(http.post(meta["token_endpoint"], data=form, auth=auth, headers={"accept": "application/json"}),
                  "token request")
    if not reply.get("access_token"):
        raise OAuthError("the token reply has no access_token")
    return reply


def exchange(meta: dict[str, Any], *, code: str, verifier: str, http: httpx.Client) -> dict[str, Any]:
    return _token(meta, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                         "redirect_uri": meta["redirect_uri"]}, http)


def refresh(meta: dict[str, Any], refresh_token: str, http: httpx.Client) -> dict[str, Any]:
    return _token(meta, {"grant_type": "refresh_token", "refresh_token": refresh_token}, http)
