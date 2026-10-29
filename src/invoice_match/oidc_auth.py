"""OIDC Authorization Code + PKCE login and signed reviewer browser sessions.

Supported ID-token signing is RS256. OIDC roles are granted only through the explicit
OIDC_ROLE_MAP; the service never treats arbitrary identity-provider groups as admin.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException, Request

_COOKIE_NAME = "invoice_match_session"
_STATE_COOKIE = "invoice_match_oidc_state"
_ALLOWED_ROLES = {"reviewer", "approver", "admin"}


def _config() -> dict:
    issuer = os.getenv("OIDC_ISSUER", "").strip().rstrip("/")
    client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("OIDC_REDIRECT_URI", "").strip()
    if not issuer or not client_id or not redirect_uri:
        raise HTTPException(status_code=503, detail="OIDC reviewer login is not configured")
    parsed_issuer, parsed_redirect = urlparse(issuer), urlparse(redirect_uri)
    if parsed_issuer.scheme != "https" and parsed_issuer.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=503, detail="OIDC_ISSUER must use HTTPS outside localhost")
    if parsed_redirect.scheme != "https" and parsed_redirect.hostname not in {"localhost", "127.0.0.1", "testserver"}:
        raise HTTPException(status_code=503, detail="OIDC_REDIRECT_URI must use HTTPS outside localhost")
    try:
        secret = os.environ["OIDC_SESSION_SECRET"].encode()
    except KeyError as exc:
        raise HTTPException(status_code=503, detail="OIDC_SESSION_SECRET is required") from exc
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="OIDC_SESSION_SECRET must be at least 32 bytes")
    return {"issuer": issuer, "client_id": client_id, "client_secret": os.getenv("OIDC_CLIENT_SECRET", ""),
            "redirect_uri": redirect_uri, "secret": secret,
            "scopes": os.getenv("OIDC_SCOPES", "openid profile email").strip()}


def oidc_enabled() -> bool:
    return bool(os.getenv("OIDC_ISSUER") and os.getenv("OIDC_CLIENT_ID") and os.getenv("OIDC_REDIRECT_URI"))


def session_ttl() -> int:
    try:
        value = int(os.getenv("OIDC_SESSION_TTL_SECONDS", "3600"))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="OIDC_SESSION_TTL_SECONDS must be an integer") from exc
    if not 60 <= value <= 86400:
        raise HTTPException(status_code=503, detail="OIDC session lifetime must be between 60 and 86400 seconds")
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: dict, *, purpose: str) -> str:
    config = _config()
    body = _b64(json.dumps({**payload, "purpose": purpose}, separators=(",", ":")).encode())
    signature = hmac.new(config["secret"], body.encode(), hashlib.sha256).digest()
    return body + "." + _b64(signature)


def _unsign(token: str | None, *, purpose: str, max_age: int) -> dict:
    if not token or "." not in token:
        raise ValueError("Missing or malformed signed session")
    body, signature = token.rsplit(".", 1)
    config = _config()
    expected = hmac.new(config["secret"], body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(signature)):
        raise ValueError("Invalid session signature")
    payload = json.loads(_unb64(body))
    if payload.get("purpose") != purpose or int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Expired or invalid session")
    if int(payload.get("exp", 0)) - int(payload.get("iat", 0)) > max_age:
        raise ValueError("Invalid session lifetime")
    return payload


def _json_url(url: str) -> dict:
    try:
        with urlopen(url, timeout=10) as response:
            raw = response.read(1_000_000)
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"OIDC provider request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("OIDC provider returned invalid JSON")
    return data


def _discovery() -> dict:
    config = _config()
    data = _json_url(config["issuer"] + "/.well-known/openid-configuration")
    if data.get("issuer", "").rstrip("/") != config["issuer"]:
        raise ValueError("OIDC discovery issuer does not match OIDC_ISSUER")
    if not data.get("authorization_endpoint") or not data.get("token_endpoint") or not data.get("jwks_uri"):
        raise ValueError("OIDC discovery document is missing required endpoints")
    return data


def begin_login(return_to: str = "/reviewer") -> tuple[str, str]:
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/reviewer"
    config, discovery = _config(), _discovery()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    state_token = _sign({"state": state, "nonce": nonce, "verifier": verifier,
                         "return_to": return_to, "iat": int(time.time()), "exp": int(time.time()) + 600},
                        purpose="oidc-state")
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"client_id": config["client_id"], "redirect_uri": config["redirect_uri"],
              "response_type": "code", "scope": config["scopes"], "state": state,
              "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    return discovery["authorization_endpoint"] + "?" + urlencode(params), state_token


def _exchange(code: str, verifier: str, discovery: dict, config: dict) -> dict:
    from urllib.parse import urlencode as form_encode
    from urllib.request import Request

    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": config["redirect_uri"],
            "client_id": config["client_id"], "code_verifier": verifier}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if config["client_secret"]:
        import base64 as b64mod
        value = b64mod.b64encode(f"{config['client_id']}:{config['client_secret']}".encode()).decode()
        headers["Authorization"] = f"Basic {value}"
        body.pop("client_id")
    request = Request(discovery["token_endpoint"], data=form_encode(body).encode(), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            token_data = json.loads(response.read(1_000_000))
    except Exception as exc:
        raise ValueError(f"OIDC token exchange failed: {exc}") from exc
    if not isinstance(token_data, dict) or not isinstance(token_data.get("id_token"), str):
        raise ValueError("OIDC token response did not contain an ID token")
    return token_data


def _verify_id_token(id_token: str, nonce: str, discovery: dict, config: dict) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("Provider returned a malformed ID token")
    try:
        header, claims = json.loads(_unb64(parts[0])), json.loads(_unb64(parts[1]))
    except Exception as exc:
        raise ValueError("Provider returned an unreadable ID token") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise ValueError("Only RS256 ID tokens with a key ID are accepted")
    keys = _json_url(discovery["jwks_uri"]).get("keys", [])
    key = next((item for item in keys if item.get("kid") == header["kid"] and item.get("kty") == "RSA"), None)
    if key is None:
        raise ValueError("ID token signing key is not present in provider JWKS")
    modulus, exponent = int.from_bytes(_unb64(key["n"]), "big"), int.from_bytes(_unb64(key["e"]), "big")
    public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    try:
        public_key.verify(_unb64(parts[2]), f"{parts[0]}.{parts[1]}".encode(),
                          padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise ValueError("ID token signature verification failed") from exc
    now = int(time.time())
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    if claims.get("iss", "").rstrip("/") != config["issuer"] or config["client_id"] not in audiences:
        raise ValueError("ID token issuer or audience is invalid")
    if int(claims.get("exp", 0)) <= now or int(claims.get("iat", 0)) > now + 60:
        raise ValueError("ID token is expired or has an invalid issue time")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce) or not claims.get("sub"):
        raise ValueError("ID token nonce or subject is invalid")
    return claims


def _mapped_principal(claims: dict) -> dict:
    try:
        role_map = json.loads(os.getenv("OIDC_ROLE_MAP", "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("OIDC_ROLE_MAP must be valid JSON") from exc
    if not isinstance(role_map, dict):
        raise ValueError("OIDC_ROLE_MAP must be an object")
    claim_name = os.getenv("OIDC_ROLE_CLAIM", "groups")
    values = claims.get(claim_name, [])
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, list):
        raise ValueError("OIDC role claim must be a string or list")
    granted = set()
    for group in values:
        mapped = role_map.get(str(group), [])
        mapped = [mapped] if isinstance(mapped, str) else mapped
        if not isinstance(mapped, list) or any(role not in _ALLOWED_ROLES for role in mapped):
            raise ValueError("OIDC_ROLE_MAP contains an invalid role mapping")
        granted.update(mapped)
    if not granted:
        raise ValueError("No reviewer roles are mapped to this identity")
    company_claim = os.getenv("OIDC_COMPANY_CLAIM", "company_id")
    company = claims.get(company_claim, "default")
    if not isinstance(company, str) or not company.strip() or len(company) > 100:
        raise ValueError("OIDC company claim must contain a valid company identifier")
    name = claims.get("preferred_username") or claims.get("email") or claims.get("name") or claims["sub"]
    return {"sub": str(claims["sub"]), "name": str(name)[:255], "roles": sorted(granted),
            "company_id": company.strip()}


def finish_login(code: str, state: str, state_cookie: str | None) -> tuple[str, str]:
    try:
        config = _config()
        saved = _unsign(state_cookie, purpose="oidc-state", max_age=600)
        if not hmac.compare_digest(str(saved.get("state", "")), state):
            raise ValueError("OIDC state does not match")
        discovery = _discovery()
        token_data = _exchange(code, saved["verifier"], discovery, config)
        claims = _verify_id_token(token_data["id_token"], saved["nonce"], discovery, config)
        principal = _mapped_principal(claims)
        now = int(time.time())
        session_token = _sign({**principal, "iat": now, "exp": now + session_ttl()},
                              purpose="reviewer-session")
        return session_token, saved.get("return_to", "/reviewer")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"OIDC login failed: {exc}") from exc


def principal_from_cookie(request: Request) -> dict | None:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    try:
        return _unsign(token, purpose="reviewer-session", max_age=session_ttl())
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def cookie_options() -> dict:
    return {"httponly": True, "secure": os.getenv("OIDC_COOKIE_SECURE", "false").casefold() == "true",
            "samesite": "lax", "path": "/"}


def cookie_name() -> str:
    return _COOKIE_NAME


def state_cookie_name() -> str:
    return _STATE_COOKIE
