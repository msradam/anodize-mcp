"""Bearer-token authentication for the HTTP transport.

This mirrors FastMCP's resource-server model: the server is handed a token
verifier, the HTTP layer pulls ``Authorization: Bearer <token>``, and a handler
reads the result with :func:`get_access_token`. Issuing tokens is left to an
external identity provider.

Two verifiers ship here. :class:`StaticTokenVerifier` maps known token strings
to scopes and uses only the standard library. :class:`JWTVerifier` validates
JSON Web Tokens: HS256/384/512 (HMAC) need nothing beyond the standard library;
RS256/384/512 use the ``cryptography`` package if it is importable, and raise a
clear error otherwise. stdio has no network boundary, so authentication applies
to the HTTP transport only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Union, runtime_checkable


@dataclass
class AccessToken:
    """The verified identity attached to a request.

    Field names match FastMCP's ``AccessToken`` so handler code reads the same.
    """

    token: str
    client_id: Optional[str] = None
    scopes: list[str] = field(default_factory=list)
    expires_at: Optional[int] = None
    resource: Optional[str] = None
    subject: Optional[str] = None
    claims: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TokenVerifier(Protocol):
    """Anything that can turn a bearer token into an :class:`AccessToken`.

    ``required_scopes`` is read by the transport to decide between 401 (no valid
    token) and 403 (valid token, missing scope).
    """

    required_scopes: list[str]

    def verify_token(self, token: str) -> Optional[AccessToken]: ...


class _InvalidToken(Exception):
    pass


# ---------------------------------------------------------------------------
# Per-request access token
# ---------------------------------------------------------------------------

_CURRENT_TOKEN: ContextVar[Optional[AccessToken]] = ContextVar("anodize_access_token", default=None)


def get_access_token() -> Optional[AccessToken]:
    """Return the access token for the request in flight, or ``None``.

    ``None`` when the server has no verifier configured, or on stdio.
    """
    return _CURRENT_TOKEN.get()


def authorize_request(auth: Any, authorization_header: Optional[str]) -> tuple[str, Any]:
    """Evaluate a bearer token against a verifier.

    Returns one of ``("ok", access_token)``, ``("missing", None)``,
    ``("invalid", None)``, or ``("forbidden", required_scopes)``. Shared by every
    transport so they enforce auth identically.
    """
    from ._asyncrun import run_maybe_async

    if auth is None:
        return ("ok", None)
    header = authorization_header or ""
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        return ("missing", None)
    access = run_maybe_async(auth.verify_token(token))
    if access is None:
        return ("invalid", None)
    required = getattr(auth, "required_scopes", None) or []
    if required and not set(required) <= set(getattr(access, "scopes", [])):
        return ("forbidden", required)
    return ("ok", access)


# ---------------------------------------------------------------------------
# Static tokens
# ---------------------------------------------------------------------------


class StaticTokenVerifier:
    """Accept a fixed set of tokens, each mapped to metadata.

    ``tokens`` maps a token string to a dict with optional ``client_id``,
    ``scopes``, ``subject``, ``resource``, ``expires_at``, and ``claims`` keys::

        StaticTokenVerifier({"dev-token": {"client_id": "cli", "scopes": ["read"]}})
    """

    def __init__(
        self,
        tokens: dict[str, dict[str, Any]],
        *,
        required_scopes: Optional[list[str]] = None,
    ):
        self._tokens = tokens
        self.required_scopes = required_scopes or []

    def verify_token(self, token: str) -> Optional[AccessToken]:
        meta = self._tokens.get(token)
        if meta is None:
            return None
        expires_at = meta.get("expires_at")
        if expires_at is not None and time.time() >= expires_at:
            return None
        return AccessToken(
            token=token,
            client_id=meta.get("client_id"),
            scopes=list(meta.get("scopes", [])),
            expires_at=expires_at,
            resource=meta.get("resource"),
            subject=meta.get("subject"),
            claims=dict(meta.get("claims", {})),
        )


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

_HMAC_HASHES = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


class JWTVerifier:
    """Validate JSON Web Tokens against a key, a PEM public key, or a JWKS URL.

    ``public_key`` is a PEM string for RS/ES algorithms or the shared secret for
    HS algorithms (``secret`` is an explicit alias). ``jwks_uri`` fetches keys
    over HTTP with the standard-library client. ``issuer``, ``audience``, and
    ``required_scopes`` are enforced when set.
    """

    def __init__(
        self,
        *,
        public_key: Optional[Union[str, bytes]] = None,
        secret: Optional[Union[str, bytes]] = None,
        jwks_uri: Optional[str] = None,
        issuer: Optional[Union[str, list[str]]] = None,
        audience: Optional[Union[str, list[str]]] = None,
        algorithm: Optional[str] = None,
        required_scopes: Optional[list[str]] = None,
        jwks_timeout: float = 10.0,
        cache_ttl: float = 3600.0,
    ):
        self._public_key = public_key
        self._secret = secret if secret is not None else public_key
        self._jwks_uri = jwks_uri
        self._issuer = issuer
        self._audience = audience
        self._algorithm = algorithm
        self.required_scopes = required_scopes or []
        self._jwks_timeout = jwks_timeout
        self._cache_ttl = cache_ttl
        self._jwks_cache: Optional[dict[str, Any]] = None
        self._jwks_cache_time: Optional[float] = None

    def verify_token(self, token: str) -> Optional[AccessToken]:
        try:
            claims = self._decode(token)
        except (_InvalidToken, ValueError, KeyError):
            return None
        return _access_token_from_claims(token, claims)

    # -- internals --------------------------------------------------------

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            header_b64, payload_b64, signature_b64 = token.split(".")
        except ValueError as exc:
            raise _InvalidToken("malformed JWT") from exc

        header = json.loads(_b64url_decode(header_b64))
        alg = self._algorithm or header.get("alg")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = _b64url_decode(signature_b64)

        if alg in _HMAC_HASHES:
            self._verify_hmac(alg, signing_input, signature)
        elif isinstance(alg, str) and alg.startswith("RS"):
            self._verify_rsa(alg, signing_input, signature, header.get("kid"))
        else:
            raise _InvalidToken(f"unsupported algorithm: {alg!r}")

        claims = json.loads(_b64url_decode(payload_b64))
        self._check_claims(claims)
        return claims

    def _verify_hmac(self, alg: str, signing_input: bytes, signature: bytes) -> None:
        if self._secret is None:
            raise _InvalidToken("no secret configured for HMAC verification")
        secret = self._secret.encode("utf-8") if isinstance(self._secret, str) else self._secret
        expected = hmac.new(secret, signing_input, _HMAC_HASHES[alg]).digest()
        if not hmac.compare_digest(expected, signature):
            raise _InvalidToken("signature mismatch")

    def _verify_rsa(
        self, alg: str, signing_input: bytes, signature: bytes, kid: Optional[str]
    ) -> None:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise _InvalidToken(f"{alg} verification requires the 'cryptography' package") from exc

        if self._jwks_uri is not None:
            public_key = self._rsa_key_from_jwks(kid, rsa)
        elif self._public_key is not None:
            pem = (
                self._public_key.encode("utf-8")
                if isinstance(self._public_key, str)
                else self._public_key
            )
            public_key = serialization.load_pem_public_key(pem)
        else:
            raise _InvalidToken("no public key or jwks_uri configured")

        hash_cls = {"RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512}[alg]
        try:
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hash_cls())
        except InvalidSignature as exc:
            raise _InvalidToken("signature mismatch") from exc

    def _rsa_key_from_jwks(self, kid: Optional[str], rsa: Any) -> Any:
        keys = self._load_jwks().get("keys", [])
        jwk = next((k for k in keys if kid is None or k.get("kid") == kid), None)
        if jwk is None:
            raise _InvalidToken("no matching JWKS key")
        modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()

    def _load_jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._jwks_cache is not None
            and self._jwks_cache_time is not None
            and (now - self._jwks_cache_time) < self._cache_ttl
        ):
            return self._jwks_cache
        import urllib.request

        assert self._jwks_uri is not None
        with urllib.request.urlopen(self._jwks_uri, timeout=self._jwks_timeout) as response:
            self._jwks_cache = json.loads(response.read().decode("utf-8"))
        self._jwks_cache_time = now
        return self._jwks_cache

    def _check_claims(self, claims: dict[str, Any]) -> None:
        now = time.time()
        exp = claims.get("exp")
        if exp is not None and now >= exp:
            raise _InvalidToken("token expired")
        nbf = claims.get("nbf")
        if nbf is not None and now < nbf:
            raise _InvalidToken("token not yet valid")
        if self._issuer is not None:
            allowed = self._issuer if isinstance(self._issuer, list) else [self._issuer]
            if claims.get("iss") not in allowed:
                raise _InvalidToken("issuer mismatch")
        if self._audience is not None:
            aud = claims.get("aud")
            token_auds = aud if isinstance(aud, list) else [aud]
            allowed_auds = self._audience if isinstance(self._audience, list) else [self._audience]
            if not set(token_auds) & set(allowed_auds):
                raise _InvalidToken("audience mismatch")


def _access_token_from_claims(token: str, claims: dict[str, Any]) -> AccessToken:
    raw_scopes = claims.get("scope") or claims.get("scp") or []
    scopes = raw_scopes.split() if isinstance(raw_scopes, str) else list(raw_scopes)
    return AccessToken(
        token=token,
        client_id=claims.get("client_id") or claims.get("azp"),
        scopes=scopes,
        expires_at=claims.get("exp"),
        subject=claims.get("sub"),
        resource=claims.get("aud") if isinstance(claims.get("aud"), str) else None,
        claims=claims,
    )
