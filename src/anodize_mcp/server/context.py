"""The ``Context`` object handlers can request to talk back to the client.

A tool, resource, or prompt handler receives a context by declaring a parameter
annotated as :class:`Context`. It is never part of the tool's input schema; the
server injects it at call time.

Every method returns a :class:`~anodize_mcp._deferred.Deferred`, so the same handler
source runs whether written synchronously (``ctx.info(...)``) or in FastMCP's
async style (``await ctx.info(...)``). See :mod:`anodize_mcp._deferred`.
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING, Any, Optional, Union

from .._deferred import defer
from ..clientfeatures import (
    CreateMessageResult,
    ElicitResult,
    Root,
    elicitation_schema,
    normalize_sampling_messages,
)
from ..exceptions import INVALID_REQUEST, McpError
from ..protocol import make_notification
from ..session import Session

if TYPE_CHECKING:
    from .server import AnodizeMCP


@dataclasses.dataclass
class RequestContext:
    """The per-request state FastMCP exposes via ``ctx.request_context``."""

    lifespan_context: Any = None
    request_id: Any = None
    session: Any = None


class Context:
    def __init__(
        self,
        session: Session,
        server: AnodizeMCP,
        request_id: Any = None,
        progress_token: Any = None,
    ):
        self._session = session
        self._server = server
        self._request_id = request_id
        self._progress_token = progress_token

    @property
    def session(self) -> Session:
        return self._session

    @property
    def request_id(self) -> Any:
        return self._request_id

    @property
    def client_id(self) -> Optional[str]:
        return self._session.session_id

    @property
    def session_id(self) -> Optional[str]:
        return self._session.session_id

    @property
    def client_info(self) -> dict[str, Any]:
        return self._session.client_info

    @property
    def access_token(self) -> Any:
        """The verified access token for this request, or ``None``."""
        from ..auth import get_access_token

        return get_access_token()

    @property
    def fastmcp(self) -> AnodizeMCP:
        return self._server

    @property
    def server(self) -> AnodizeMCP:
        return self._server

    @property
    def transport(self) -> Optional[str]:
        return getattr(self._session, "transport", None)

    @property
    def lifespan_context(self) -> Any:
        return self._server._lifespan_state

    @property
    def request_context(self) -> RequestContext:
        return RequestContext(
            lifespan_context=self._server._lifespan_state,
            request_id=self._request_id,
            session=self._session,
        )

    def client_supports_extension(self, name: str) -> bool:
        return name in self._session.client_capabilities

    def send_notification(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Send an arbitrary JSON-RPC notification to the client."""
        self._session.send_message(make_notification(method, params))
        return defer(None)

    # -- logging ----------------------------------------------------------

    def log(
        self,
        message: Any,
        level: str = "info",
        *,
        logger_name: Optional[str] = None,
        logger: Optional[str] = None,
        data: Optional[Any] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Send a ``notifications/message`` log entry.

        Argument order matches FastMCP: ``ctx.log(message, level=...)``. The
        wire payload is FastMCP's LogData shape, ``{"msg": ..., "extra": ...}``.
        """
        if self._session.should_log(level):
            payload: dict[str, Any] = {"level": level}
            name = logger or logger_name
            if name is not None:
                payload["logger"] = name
            if data is not None:
                payload["data"] = data
            else:
                msg = message if isinstance(message, (dict, list)) else str(message)
                payload["data"] = {"msg": msg, "extra": extra}
            self._session.send_message(make_notification("notifications/message", payload))
        return defer(None)

    def debug(self, message: Any, **kwargs: Any) -> Any:
        return self.log(message, "debug", **kwargs)

    def info(self, message: Any, **kwargs: Any) -> Any:
        return self.log(message, "info", **kwargs)

    def notice(self, message: Any, **kwargs: Any) -> Any:
        return self.log(message, "notice", **kwargs)

    def warning(self, message: Any, **kwargs: Any) -> Any:
        return self.log(message, "warning", **kwargs)

    def error(self, message: Any, **kwargs: Any) -> Any:
        return self.log(message, "error", **kwargs)

    # -- progress ---------------------------------------------------------

    def report_progress(
        self,
        progress: float,
        total: Optional[float] = None,
        message: Optional[str] = None,
    ) -> Any:
        """Send a progress notification, if the client supplied a token."""
        if self._progress_token is not None:
            params: dict[str, Any] = {"progressToken": self._progress_token, "progress": progress}
            if total is not None:
                params["total"] = total
            if message is not None:
                params["message"] = message
            self._session.send_message(make_notification("notifications/progress", params))
        return defer(None)

    # -- per-request/session state ----------------------------------------

    def set_state(self, key: str, value: Any) -> Any:
        self._session.state[key] = value
        return defer(None)

    def get_state(self, key: str, default: Any = None) -> Any:
        return defer(self._session.state.get(key, default))

    def delete_state(self, key: str) -> Any:
        self._session.state.pop(key, None)
        return defer(None)

    # -- resource access --------------------------------------------------

    def read_resource(self, uri: str) -> Any:
        """Read another resource registered on this server and return its contents."""
        return defer(self._server.read_resource(uri, self._session))

    def list_resources(self) -> Any:
        """List the server's registered (static) resources."""
        return defer([r.describe() for r in self._server._resources.values()])

    def list_prompts(self) -> Any:
        """List the server's registered prompts."""
        return defer([p.describe() for p in self._server._prompts.values()])

    def get_prompt(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """Render one of the server's prompts."""
        params = {"name": name, "arguments": arguments or {}}
        return defer(self._server._handle_prompt_get(params, self._session, self._request_id))

    # -- server-initiated requests to the client --------------------------

    def _require_client_capability(self, name: str) -> None:
        if name not in self._session.client_capabilities:
            raise McpError(f"client does not support the {name!r} capability", code=INVALID_REQUEST)

    def sample(
        self,
        messages: Any,
        *,
        system_prompt: Optional[str] = None,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        stop_sequences: Optional[list[str]] = None,
        model_preferences: Optional[Any] = None,
        model: Optional[Any] = None,
        include_context: Optional[str] = None,
        result_type: Optional[Any] = None,
        tools: Optional[Any] = None,
        timeout: float = 60.0,
    ) -> CreateMessageResult:
        """Ask the client's LLM to generate a message (``sampling/createMessage``).

        ``messages`` may be a string, a single message dict, or a list of either.
        FastMCP's ``model=``, ``result_type=``, and ``tools=`` are accepted; the
        result exposes ``.text`` for the caller to parse (anodize does not run the
        structured parsing itself, which would need pydantic).
        """
        self._require_client_capability("sampling")
        params: dict[str, Any] = {
            "messages": normalize_sampling_messages(messages),
            "maxTokens": max_tokens,
        }
        if system_prompt is not None:
            params["systemPrompt"] = system_prompt
        if temperature is not None:
            params["temperature"] = temperature
        if stop_sequences is not None:
            params["stopSequences"] = stop_sequences
        prefs = model_preferences if model_preferences is not None else model
        if prefs is not None:
            params["modelPreferences"] = _normalize_model_preferences(prefs)
        if include_context is not None:
            params["includeContext"] = include_context
        if tools is not None:
            params["tools"] = tools
        result = self._session.send_request("sampling/createMessage", params, timeout=timeout)
        return defer(CreateMessageResult.from_dict(result or {}))

    def elicit(
        self,
        message: str,
        schema: Union[dict[str, Any], type, None] = None,
        *,
        response_type: Union[dict[str, Any], type, None] = None,
        timeout: float = 60.0,
    ) -> ElicitResult:
        """Ask the user for structured input via the client (``elicitation/create``).

        ``schema`` (or FastMCP's ``response_type=``) is a JSON Schema dict, a
        dataclass, a pydantic model, or a scalar type. On accept, ``result.data``
        is an instance of a dataclass schema, the unwrapped scalar, or the raw dict.
        """
        schema = response_type if schema is None else schema
        if schema is None:
            raise TypeError("elicit() requires a schema or response_type")
        self._require_client_capability("elicitation")
        params = {"message": message, "requestedSchema": elicitation_schema(schema)}
        result = self._session.send_request("elicitation/create", params, timeout=timeout) or {}
        action = result.get("action", "cancel")
        content = result.get("content")
        data: Any = content
        if action == "accept" and isinstance(content, dict):
            if dataclasses.is_dataclass(schema) and isinstance(schema, type):
                with contextlib.suppress(TypeError):
                    data = schema(**content)
            elif schema in (str, int, float, bool) and "value" in content:
                data = content["value"]
        return defer(ElicitResult(action=action, data=data))

    def list_roots(self, *, timeout: float = 30.0) -> list[Root]:
        """Ask the client for its filesystem roots (``roots/list``)."""
        self._require_client_capability("roots")
        result = self._session.send_request("roots/list", {}, timeout=timeout) or {}
        return defer([Root(uri=r["uri"], name=r.get("name")) for r in result.get("roots", [])])


def _normalize_model_preferences(prefs: Any) -> Any:
    """Accept a model name, a list of names, or a preferences dict (as FastMCP does)."""
    if isinstance(prefs, str):
        return {"hints": [{"name": prefs}]}
    if isinstance(prefs, (list, tuple)):
        return {"hints": [{"name": p} if isinstance(p, str) else p for p in prefs]}
    return prefs
