"""The :class:`AnodizeMCP` server: decorators, a registry, and a JSON-RPC dispatcher.

This is the FastMCP-equivalent entry point. Register tools, resources, and
prompts with decorators, then call :meth:`AnodizeMCP.run`.
"""

from __future__ import annotations

import contextlib
import inspect
import threading
import warnings
import weakref
from typing import Any, Callable, Optional, TypeVar

from .. import _compat
from .._asyncrun import run_coro, run_maybe_async
from .._deferred import defer
from ..attrdict import wrap as attr_wrap
from ..clientfeatures import CompletionResult
from ..content import (
    is_content_value,
    normalize_resource_result,
    normalize_tool_result,
    to_jsonable,
)
from ..exceptions import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    InvalidParams,
    McpError,
    NotFoundError,
    ResourceError,
    ToolError,
)
from ..pagination import paginate
from ..prompts.prompt import PromptArgument, PromptDef, normalize_prompt_result
from ..protocol import (
    LATEST_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    make_error,
    make_notification,
    make_response,
)
from ..resources.resource import ResourceDef
from ..resources.template import ResourceTemplateDef, compile_uri_template
from ..schema import (
    build_input_schema,
    build_params,
    coerce_arguments,
    doc_summary,
    output_schema_for,
    parse_param_docs,
)
from ..session import LOG_LEVELS, Session
from ..tools.tool import ToolDef, ToolResult
from .context import Context

F = TypeVar("F", bound=Callable[..., Any])

_MAX_COMPLETION_VALUES = 100


def _package_version() -> str:
    from .. import __version__

    return __version__


def _has_middleware_hooks(mw: Any) -> bool:
    from .middleware.middleware import OPERATION_HOOKS, Middleware

    if isinstance(mw, Middleware):
        return True
    hooks = ("on_message", "on_request", "on_notification", *OPERATION_HOOKS.values())
    return any(callable(getattr(mw, hook, None)) for hook in hooks)


def _wrap_hook(handler: Callable[..., Any], next_call: Callable[..., Any]) -> Callable[..., Any]:
    async def call(ctx: Any) -> Any:
        out = handler(ctx, next_call)
        if inspect.iscoroutine(out):
            out = await out
        return out

    return call


class AnodizeMCP:
    def __init__(
        self,
        name: str = "AnodizeMCP",
        version: Optional[str] = None,
        *,
        instructions: Optional[str] = None,
        title: Optional[str] = None,
        page_size: Optional[int] = None,
        list_page_size: Optional[int] = None,
        auth: Any = None,
        lifespan: Any = None,
        icons: Optional[list[dict[str, Any]]] = None,
        website_url: Optional[str] = None,
        on_duplicate: str = "warn",
        mask_error_details: bool = False,
        strict_input_validation: bool = False,
    ):
        self.name = name
        # FastMCP defaults serverInfo.version to its own package version.
        self.version = version if version is not None else _package_version()
        self.title = title
        self.instructions = instructions
        # FastMCP names this list_page_size; accept either, preferring the
        # explicit one. None (the FastMCP default) disables pagination.
        self.page_size = list_page_size if list_page_size is not None else page_size
        # A token verifier (object with verify_token); enforced by the HTTP
        # transport only. stdio has no network boundary, so it is ignored there.
        self.auth = auth
        self.lifespan = lifespan
        self.icons = icons
        self.website_url = website_url
        # "warn" | "error" | "replace": what to do when a name is reused.
        self.on_duplicate = on_duplicate
        self.mask_error_details = mask_error_details
        self.strict_input_validation = strict_input_validation
        self._tools: dict[str, ToolDef] = {}
        self._resources: dict[str, ResourceDef] = {}
        self._templates: list[ResourceTemplateDef] = []
        self._prompts: dict[str, PromptDef] = {}
        self._disabled: set[str] = set()
        # key is ("prompt", name) or ("resource", uri_template)
        self._completers: dict[tuple[str, str], Callable[..., Any]] = {}
        self._middleware: list[Any] = []
        # (methods, path) -> handler, for custom_route
        self._routes: dict[tuple[str, str], Callable[..., Any]] = {}
        self._sessions: weakref.WeakSet[Session] = weakref.WeakSet()
        self._lifespan_state: Any = None
        self._lifespan_cm: Any = None
        self._lifespan_async = False
        self._lifespan_refs = 0
        self._lifespan_lock = threading.Lock()

    def _check_duplicate(self, kind: str, name: str, registry: Any) -> None:
        if name not in registry:
            return
        if self.on_duplicate == "error":
            raise ValueError(f"{kind} {name!r} is already registered")
        if self.on_duplicate == "warn":
            warnings.warn(f"{kind} {name!r} is already registered; replacing", stacklevel=3)

    # ------------------------------------------------------------------
    # Decorators
    # ------------------------------------------------------------------

    def tool(
        self,
        name_or_func: Any = None,
        *,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        annotations: Optional[dict[str, Any]] = None,
        tags: Any = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Register a function as a tool.

        Usable bare (``@mcp.tool``) or called (``@mcp.tool(name="x")``). ``tags``
        is accepted for FastMCP source compatibility but not used for filtering.
        """

        def decorator(func: F) -> F:
            context_param = _find_context_param(func)
            specs = build_params(func, skip=(context_param,) if context_param else ())
            tool_name: str = name if name is not None else getattr(func, "__name__", "tool")
            self._check_duplicate("tool", tool_name, self._tools)
            out_schema, wrap = output_schema_for(_return_annotation(func))
            self._tools[tool_name] = ToolDef(
                name=tool_name,
                handler=func,
                param_specs=specs,
                input_schema=build_input_schema(specs),
                title=title,
                description=description or _docstring(func),
                output_schema=out_schema,
                wrap_output=wrap,
                annotations=_normalize_annotations(annotations),
                context_param=context_param,
                tags=tags,
                meta=meta,
            )
            return func

        if callable(name_or_func):
            return decorator(name_or_func)
        if isinstance(name_or_func, str) and name is None:
            name = name_or_func
        return decorator

    def resource(
        self,
        uri: str,
        *,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        mime_type: Optional[str] = None,
        size: Optional[int] = None,
        annotations: Optional[dict[str, Any]] = None,
        tags: Any = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> Callable[[F], F]:
        """Register a function as a resource or, if ``uri`` has ``{vars}``, a template."""
        if callable(uri):
            # Bare @mcp.resource swallows the function silently; FastMCP raises.
            raise TypeError(
                "The @resource decorator requires a URI: use @mcp.resource('uri://...')"
            )

        def decorator(func: F) -> F:
            context_param = _find_context_param(func)
            res_name: str = name if name is not None else getattr(func, "__name__", "resource")
            if "{" in uri:
                # Templates have no per-parameter description slot, so FastMCP
                # keeps the whole docstring (Args section included).
                desc = description or inspect.getdoc(func)
                pattern, var_names = compile_uri_template(uri)
                self._templates.append(
                    ResourceTemplateDef(
                        uri_template=uri,
                        handler=func,
                        name=res_name,
                        param_names=var_names,
                        pattern=pattern,
                        title=title,
                        description=desc,
                        mime_type=mime_type,
                        context_param=context_param,
                        tags=tags,
                        meta=meta,
                        param_specs=build_params(
                            func, skip=(context_param,) if context_param else ()
                        ),
                    )
                )
            else:
                self._check_duplicate("resource", uri, self._resources)
                self._resources[uri] = ResourceDef(
                    uri=uri,
                    handler=func,
                    name=res_name,
                    title=title,
                    description=description or _docstring(func),
                    mime_type=mime_type,
                    size=size,
                    annotations=_normalize_annotations(annotations),
                    context_param=context_param,
                    tags=tags,
                    meta=meta,
                )
            return func

        return decorator

    def prompt(
        self,
        name_or_func: Any = None,
        *,
        name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Any = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Register a function as a prompt."""

        def decorator(func: F) -> F:
            context_param = _find_context_param(func)
            specs = build_params(func, skip=(context_param,) if context_param else ())
            arguments = [
                PromptArgument(
                    name=spec.name,
                    description=spec.field.description,
                    required=spec.required,
                )
                for spec in specs
            ]
            prompt_name: str = name if name is not None else getattr(func, "__name__", "prompt")
            self._check_duplicate("prompt", prompt_name, self._prompts)
            self._prompts[prompt_name] = PromptDef(
                name=prompt_name,
                handler=func,
                arguments=arguments,
                param_specs=specs,
                title=title,
                description=description or _docstring(func),
                context_param=context_param,
                tags=tags,
                meta=meta,
            )
            return func

        if callable(name_or_func):
            return decorator(name_or_func)
        if isinstance(name_or_func, str) and name is None:
            name = name_or_func
        return decorator

    def complete_prompt(self, prompt_name: str) -> Callable[[F], F]:
        """Register an argument-completion handler for a prompt.

        The handler is called ``handler(argument, value[, context])`` and returns
        a list of strings or a :class:`CompletionResult`.
        """

        def decorator(func: F) -> F:
            self._completers[("prompt", prompt_name)] = func
            return func

        return decorator

    def complete_resource(self, uri_template: str) -> Callable[[F], F]:
        """Register an argument-completion handler for a resource template."""

        def decorator(func: F) -> F:
            self._completers[("resource", uri_template)] = func
            return func

        return decorator

    # Programmatic registration (FastMCP source compatibility); the decorators
    # above already work when called directly, these just read more naturally.

    def add_tool(self, fn: Callable[..., Any], **kwargs: Any) -> Callable[..., Any]:
        self.tool(**kwargs)(fn)
        return fn

    def add_prompt(self, fn: Callable[..., Any], **kwargs: Any) -> Callable[..., Any]:
        self.prompt(**kwargs)(fn)
        return fn

    def add_resource(self, uri: str, fn: Callable[..., Any], **kwargs: Any) -> Callable[..., Any]:
        self.resource(uri, **kwargs)(fn)
        return fn

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: Optional[str] = None,
        include_in_schema: bool = True,
    ) -> Callable[[F], F]:
        """Register a handler at an arbitrary HTTP path (health checks, callbacks).

        The handler receives an :class:`~anodize_mcp.routes.Request` and returns a
        :class:`~anodize_mcp.routes.Response`, a ``(status, body)`` tuple, a
        ``dict``/``list`` (JSON), a ``str``, or ``bytes``. Custom routes bypass
        the MCP auth and Origin checks. HTTP transport only.
        """

        def decorator(func: F) -> F:
            for method in methods:
                self._routes[(method.upper(), path)] = func
            return func

        return decorator

    def find_route(self, method: str, path: str) -> Optional[Callable[..., Any]]:
        return self._routes.get((method.upper(), path))

    def add_middleware(self, middleware: Any) -> None:
        self._middleware.append(middleware)

    # ------------------------------------------------------------------
    # Introspection and management
    # ------------------------------------------------------------------

    def list_tools(self) -> Any:
        return defer(
            [attr_wrap(t.describe()) for t in self._tools.values() if t.name not in self._disabled]
        )

    def list_resources(self) -> Any:
        return defer([attr_wrap(r.describe()) for r in self._resources.values()])

    def list_resource_templates(self) -> Any:
        return defer([attr_wrap(t.describe()) for t in self._templates])

    def list_prompts(self) -> Any:
        return defer([attr_wrap(p.describe()) for p in self._prompts.values()])

    async def _list_tools_mcp(self, request: Any = None) -> Any:
        tools = [t.describe() for t in self._tools.values() if t.name not in self._disabled]
        return attr_wrap({"tools": tools})

    def get_tool(self, name: str) -> Optional[ToolDef]:
        return self._tools.get(name)

    def get_prompt(self, name: str) -> Optional[PromptDef]:
        return self._prompts.get(name)

    def get_resource(self, uri: str) -> Optional[ResourceDef]:
        return self._resources.get(uri)

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """Invoke a registered tool in-process and return its result dict."""
        session = self.new_session()
        return defer(
            self._handle_tool_call({"name": name, "arguments": arguments or {}}, session, None)
        )

    def render_prompt(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        """Render a registered prompt in-process and return its messages."""
        session = self.new_session()
        return defer(
            self._handle_prompt_get({"name": name, "arguments": arguments or {}}, session, None)
        )

    def disable_tool(self, name: str) -> None:
        self._disabled.add(name)
        self.notify_tools_changed()

    def enable_tool(self, name: str) -> None:
        if name in self._disabled:
            self._disabled.discard(name)
            self.notify_tools_changed()

    # ------------------------------------------------------------------
    # Dynamic registration / notifications
    # ------------------------------------------------------------------

    def remove_tool(self, name: str) -> None:
        if self._tools.pop(name, None) is not None:
            self.notify_tools_changed()

    def remove_prompt(self, name: str) -> None:
        if self._prompts.pop(name, None) is not None:
            self.notify_prompts_changed()

    def remove_resource(self, uri: str) -> None:
        if self._resources.pop(uri, None) is not None:
            self.notify_resources_changed()

    def notify_tools_changed(self) -> None:
        self._broadcast(make_notification("notifications/tools/list_changed"))

    def notify_prompts_changed(self) -> None:
        self._broadcast(make_notification("notifications/prompts/list_changed"))

    def notify_resources_changed(self) -> None:
        self._broadcast(make_notification("notifications/resources/list_changed"))

    def notify_resource_updated(self, uri: str) -> None:
        """Notify every session subscribed to ``uri`` that it changed."""
        message = make_notification("notifications/resources/updated", {"uri": uri})
        self._broadcast(message, predicate=lambda s: uri in s.subscriptions)

    def _broadcast(
        self, message: dict[str, Any], predicate: Optional[Callable[[Session], bool]] = None
    ) -> None:
        for session in list(self._sessions):
            if predicate is not None and not predicate(session):
                continue
            # One dead connection must not break delivery to the others.
            with contextlib.suppress(Exception):
                session.send_message(message)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def new_session(self, send: Optional[Callable[[dict[str, Any]], None]] = None) -> Session:
        session = Session(send=send)
        self._sessions.add(session)
        return session

    def handle_message(self, message: Any, session: Session) -> Optional[dict[str, Any]]:
        """Process one parsed JSON-RPC message; return a response dict or ``None``.

        ``None`` means there is nothing to send back: the message was a
        notification, or a client response to a server-initiated request (which
        is routed to whichever handler is waiting on it).
        """
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            return make_error(request_id, INVALID_PARAMS, "invalid JSON-RPC message")

        method = message.get("method")
        if method is None:
            session.resolve_response(message)
            return None

        is_notification = "id" not in message
        request_id = message.get("id")
        params = message.get("params") or {}

        try:
            if self._middleware:
                result = self._dispatch_with_middleware(
                    method, params, session, request_id, is_notification
                )
            else:
                result = self._route(method, params, session, request_id)
        except McpError as exc:
            return (
                None if is_notification else make_error(request_id, exc.code, exc.message, exc.data)
            )
        except Exception as exc:  # noqa: BLE001
            return (
                None
                if is_notification
                else make_error(request_id, -32603, f"{type(exc).__name__}: {exc}")
            )

        if is_notification:
            return None
        return make_response(request_id, result)

    def _dispatch_with_middleware(
        self,
        method: str,
        params: dict[str, Any],
        session: Session,
        request_id: Any,
        is_notification: bool,
    ) -> Any:
        from .middleware.middleware import OPERATION_HOOKS, MiddlewareContext

        mw_context: MiddlewareContext = MiddlewareContext(
            message=attr_wrap(params),
            method=method,
            source="client",
            type="notification" if is_notification else "request",
            fastmcp_context=Context(session, self, request_id),
        )

        domain_terminal, rewrap = self._middleware_shapes(method, params, session, request_id)

        async def terminal(ctx: MiddlewareContext) -> Any:
            # Honor context.copy(message=...): the terminal reads the message
            # that reached it, not the original params.
            effective = dict(ctx.message) if isinstance(ctx.message, dict) else params
            return domain_terminal(effective)

        stages = ["on_message", "on_notification" if is_notification else "on_request"]
        operation_hook = OPERATION_HOOKS.get(method)
        if operation_hook is not None:
            stages.append(operation_hook)

        # Compose per middleware, as FastMCP does: every hook of the first
        # middleware wraps every hook of the second, and so on. A plain
        # callable (FastMCP invokes middleware via __call__) wraps the whole
        # chain in one step.
        call: Any = terminal
        for mw in reversed(self._middleware):
            if not _has_middleware_hooks(mw) and callable(mw):
                call = _wrap_hook(mw, call)
                continue
            for stage in reversed(stages):
                handler = getattr(mw, stage, None)
                if handler is not None:
                    call = _wrap_hook(handler, call)
        if method == "tools/call":
            # FastMCP converts any tools/call pipeline failure (middleware
            # included) into a tool-level isError result.
            try:
                return rewrap(run_maybe_async(call(mw_context)))
            except McpError as exc:
                return self._tool_error(exc.message)
            except Exception as exc:  # noqa: BLE001
                return self._tool_error(str(exc))
        return rewrap(run_maybe_async(call(mw_context)))

    def _middleware_shapes(
        self, method: str, params: dict[str, Any], session: Session, request_id: Any
    ) -> tuple[Callable[[dict[str, Any]], Any], Callable[[Any], Any]]:
        """The terminal and re-enveloping pair for one middleware dispatch.

        Operation hooks see FastMCP's domain shapes (a ToolResult from
        tools/call, component lists from the list methods, the contents list
        from resources/read); the wire envelope and pagination are applied
        after the chain returns. A hook that returns the wire dict directly
        (the pre-0.7 anodize convention) still works.
        """
        list_keys = {
            "tools/list": "tools",
            "resources/list": "resources",
            "resources/templates/list": "resourceTemplates",
            "prompts/list": "prompts",
        }
        if method in list_keys:
            key = list_keys[method]

            def list_terminal(p: dict[str, Any]) -> Any:
                return self._list_components(method)

            def list_rewrap(result: Any) -> Any:
                if isinstance(result, dict):
                    return result
                return self._paged(key, list(result), params)

            return list_terminal, list_rewrap

        if method == "tools/call":

            def call_terminal(p: dict[str, Any]) -> Any:
                wire = self._handle_tool_call(p, session, request_id)
                result = ToolResult(
                    content=attr_wrap(wire.get("content", [])),
                    structured_content=wire.get("structuredContent"),
                    meta=wire.get("_meta"),
                    is_error=wire.get("isError", False),
                )
                return result

            def call_rewrap(result: Any) -> Any:
                if isinstance(result, ToolResult):
                    return self._tool_result_to_wire(result)
                return result

            return call_terminal, call_rewrap

        if method == "resources/read":

            def read_terminal(p: dict[str, Any]) -> Any:
                # Attribute-wrapped so middleware reading result.contents
                # (FastMCP 3.x's ResourceResult shape) works.
                return attr_wrap(self._route(method, p, session, request_id))

            def read_rewrap(result: Any) -> Any:
                if isinstance(result, dict):
                    return result
                return {"contents": list(result)}

            return read_terminal, read_rewrap

        def default_terminal(p: dict[str, Any]) -> Any:
            result = self._route(method, p, session, request_id)
            return attr_wrap(result) if isinstance(result, dict) else result

        return default_terminal, lambda result: result

    def _list_components(self, method: str) -> list[Any]:
        if method == "tools/list":
            return [t for t in self._tools.values() if t.name not in self._disabled]
        if method == "resources/list":
            return list(self._resources.values())
        if method == "resources/templates/list":
            return list(self._templates)
        return list(self._prompts.values())

    def _route(self, method: str, params: dict[str, Any], session: Session, request_id: Any) -> Any:
        if method == "initialize":
            return self._handle_initialize(params, session)
        if method == "notifications/initialized":
            session.initialized = True
            return None
        if method == "ping":
            return {}
        if method == "logging/setLevel":
            level = params.get("level")
            if level in LOG_LEVELS:
                session.log_level = level
            return {}
        if method == "tools/list":
            active = [t for t in self._tools.values() if t.name not in self._disabled]
            return self._paged("tools", active, params)
        if method == "tools/call":
            return self._handle_tool_call(params, session, request_id)
        if method == "resources/list":
            return self._paged("resources", list(self._resources.values()), params)
        if method == "resources/templates/list":
            return self._paged("resourceTemplates", list(self._templates), params)
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                raise InvalidParams("resources/read requires a string 'uri'")
            return {"contents": self.read_resource(uri, session, request_id)}
        if method == "resources/subscribe":
            uri = params.get("uri")
            if isinstance(uri, str):
                session.subscriptions.add(uri)
            return {}
        if method == "resources/unsubscribe":
            session.subscriptions.discard(params.get("uri"))
            return {}
        if method == "prompts/list":
            return self._paged("prompts", list(self._prompts.values()), params)
        if method == "prompts/get":
            return self._handle_prompt_get(params, session, request_id)
        if method == "completion/complete":
            return self._handle_completion(params)
        if method.startswith("notifications/"):
            return None  # cancellation, roots list changed, etc.: accept silently
        raise McpError(f"method not found: {method}", code=METHOD_NOT_FOUND)

    def _paged(self, key: str, items: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        if self.page_size is None:
            return {key: [item.describe() for item in items]}
        page, next_cursor = paginate(items, params.get("cursor"), self.page_size)
        result: dict[str, Any] = {key: [item.describe() for item in page]}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    def _handle_initialize(self, params: dict[str, Any], session: Session) -> dict[str, Any]:
        session.client_info = params.get("clientInfo", {}) or {}
        session.client_capabilities = params.get("capabilities", {}) or {}
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            session.protocol_version = requested
        else:
            session.protocol_version = LATEST_PROTOCOL_VERSION

        server_info: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title is not None:
            server_info["title"] = self.title
        if self.icons is not None:
            server_info["icons"] = self.icons
        if self.website_url is not None:
            server_info["websiteUrl"] = self.website_url

        result: dict[str, Any] = {
            "protocolVersion": session.protocol_version,
            "capabilities": self._capabilities(),
            "serverInfo": server_info,
            "instructions": self.instructions,
        }
        return result

    def _capabilities(self) -> dict[str, Any]:
        # Advertised unconditionally, as FastMCP does: components may be
        # registered after initialize, and capability-gated clients would
        # otherwise never look.
        return {
            "logging": {},
            "tools": {"listChanged": True},
            "resources": {"subscribe": True, "listChanged": True},
            "prompts": {"listChanged": True},
            "completions": {},
        }

    def _handle_tool_call(
        self, params: dict[str, Any], session: Session, request_id: Any
    ) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None or name in self._disabled:
            # FastMCP reports this as a tool-level error result, not a protocol
            # error, so clients using raise_on_error=False can handle it.
            return self._tool_error(f"Unknown tool: {name!r}")

        arguments = params.get("arguments") or {}
        request_meta = params.get("_meta") or {}
        progress_token = request_meta.get("progressToken")

        try:
            coerced = coerce_arguments(
                tool.param_specs, arguments, strict=self.strict_input_validation
            )
        except McpError as exc:
            # FastMCP reports input validation failures as isError results.
            return self._tool_error(exc.message)

        try:
            if tool.context_param:
                coerced[tool.context_param] = Context(
                    session,
                    self,
                    request_id,
                    progress_token=progress_token,
                    meta=request_meta or None,
                )
            value = run_maybe_async(tool.handler(**coerced))
        except ToolError as exc:
            return self._tool_error(exc.message)
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            # FastMCP's exact masked and unmasked error texts.
            detail = (
                f"Error calling tool {name!r}"
                if self.mask_error_details
                else f"Error calling tool {name!r}: {exc}"
            )
            return self._tool_error(detail)

        try:
            return self._tool_value_to_wire(tool, value)
        except (TypeError, ValueError) as exc:
            # An unserializable result (non-finite floats and the like) is a
            # tool-level error, as FastMCP reports it.
            return self._tool_error(f"Error calling tool {name!r}: {exc}")

    def _tool_value_to_wire(self, tool: ToolDef, value: Any) -> dict[str, Any]:
        if isinstance(value, ToolResult):
            return self._tool_result_to_wire(value)

        content, auto_structured = normalize_tool_result(value)
        result: dict[str, Any] = {"content": content, "isError": False}
        if tool.output_schema is not None and value is not None and not is_content_value(value):
            payload = to_jsonable(value)
            if tool.wrap_output:
                result["structuredContent"] = {"result": payload}
                # Mark the synthetic wrapper so the client unwraps .data back to
                # the original value, matching FastMCP.
                result["_meta"] = {"fastmcp": {"wrap_result": True}}
            else:
                result["structuredContent"] = payload
        elif auto_structured is not None:
            # An object-shaped value (dict, dataclass, model) is structured
            # output even without a return annotation, as on FastMCP.
            result["structuredContent"] = to_jsonable(auto_structured)
        return result

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _tool_result_to_wire(self, value: ToolResult) -> dict[str, Any]:
        content = value.content
        if isinstance(content, list) and all(isinstance(b, dict) and "type" in b for b in content):
            blocks: list[dict[str, Any]] = [dict(b) for b in content]
        else:
            blocks = normalize_tool_result(content)[0] if content is not None else []
        out: dict[str, Any] = {"content": blocks, "isError": value.is_error}
        if value.structured_content is not None:
            out["structuredContent"] = to_jsonable(value.structured_content)
        if value.meta is not None:
            out["_meta"] = value.meta
        return out

    def read_resource(
        self, uri: str, session: Session, request_id: Any = None
    ) -> list[dict[str, Any]]:
        resource = self._resources.get(uri)
        if resource is not None:
            value = self._invoke_resource(
                resource.handler, {}, resource.context_param, session, request_id
            )
            return normalize_resource_result(uri, value, resource.mime_type)

        for template in self._templates:
            variables = template.match(uri)
            if variables is None:
                continue
            if template.param_specs is not None:
                # Path variables arrive as strings; coerce to the annotated
                # types, as FastMCP's pydantic validation does.
                variables = coerce_arguments(template.param_specs, variables)
            value = self._invoke_resource(
                template.handler, variables, template.context_param, session, request_id
            )
            # FastMCP serves JSON-shaped template reads as application/json
            # (static resources default to text/plain).
            return normalize_resource_result(
                uri, value, template.mime_type, json_mime_default="application/json"
            )

        raise NotFoundError(f"Unknown resource: {uri!r}")

    def _invoke_resource(
        self,
        handler: Callable[..., Any],
        variables: dict[str, Any],
        context_param: Optional[str],
        session: Session,
        request_id: Any,
    ) -> Any:
        kwargs = dict(variables)
        if context_param:
            kwargs[context_param] = Context(session, self, request_id)
        try:
            return run_maybe_async(handler(**kwargs))
        except ResourceError as exc:
            raise McpError(exc.message, code=RESOURCE_NOT_FOUND) from exc

    def _handle_prompt_get(
        self, params: dict[str, Any], session: Session, request_id: Any
    ) -> dict[str, Any]:
        name = params.get("name")
        prompt = self._prompts.get(name) if isinstance(name, str) else None
        if prompt is None:
            raise NotFoundError(f"Unknown prompt: {name!r}")

        arguments = params.get("arguments") or {}
        coerced = coerce_arguments(prompt.param_specs, arguments)
        if prompt.context_param:
            coerced[prompt.context_param] = Context(session, self, request_id)
        value = run_maybe_async(prompt.handler(**coerced))
        result = normalize_prompt_result(value)
        if "description" not in result and prompt.description is not None:
            result["description"] = prompt.description
        return result

    def _handle_completion(self, params: dict[str, Any]) -> dict[str, Any]:
        ref = params.get("ref") or {}
        argument = params.get("argument") or {}
        arg_name = argument.get("name", "")
        value = argument.get("value", "")
        context_args = (params.get("context") or {}).get("arguments") or {}

        ref_type = ref.get("type")
        ref_name = ref.get("name") if ref_type == "ref/prompt" else ref.get("uri")
        key: Optional[tuple[str, str]] = None
        if ref_type == "ref/prompt" and isinstance(ref_name, str):
            key = ("prompt", ref_name)
        elif ref_type == "ref/resource" and isinstance(ref_name, str):
            key = ("resource", ref_name)

        handler = self._completers.get(key) if key else None
        if handler is None:
            return {"completion": {"values": [], "hasMore": False}}

        if len(inspect.signature(handler).parameters) >= 3:
            raw = run_maybe_async(handler(arg_name, value, context_args))
        else:
            raw = run_maybe_async(handler(arg_name, value))

        if isinstance(raw, CompletionResult):
            values = list(raw.values)
            total = raw.total
            has_more = raw.has_more
        else:
            values = list(raw)
            total = None
            has_more = None

        truncated = len(values) > _MAX_COMPLETION_VALUES
        if total is None and truncated:
            total = len(values)
        completion: dict[str, Any] = {"values": values[:_MAX_COMPLETION_VALUES]}
        if total is not None:
            completion["total"] = total
        completion["hasMore"] = has_more if has_more is not None else truncated
        return {"completion": completion}

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run(self, transport: str = "stdio", **kwargs: Any) -> None:
        """Start serving. ``transport`` is ``"stdio"`` (default) or ``"http"``.

        Transport names match FastMCP's. ``"sse"`` (the deprecated FastMCP
        transport) is not implemented; use ``"http"`` (Streamable HTTP).
        """
        if transport == "stdio":
            self.run_stdio(**kwargs)
        elif transport in ("http", "streamable-http"):
            self.run_http(**kwargs)
        elif transport == "sse":
            raise ValueError("the legacy 'sse' transport is not implemented; use transport='http'")
        else:
            raise ValueError(f"unknown transport: {transport!r}")

    def run_stdio(self, **kwargs: Any) -> None:
        from ..transports.stdio import serve_stdio

        self._enter_lifespan()
        try:
            serve_stdio(self, **kwargs)
        finally:
            self._exit_lifespan()

    async def run_async(
        self, transport: str = "stdio", *, show_banner: Optional[bool] = None, **kwargs: Any
    ) -> None:
        """Async counterpart of :meth:`run`. anodize prints no banner, but
        ``show_banner`` is threaded through for FastMCP parity."""
        banner = True if show_banner is None else show_banner
        if transport == "stdio":
            await self.run_stdio_async(show_banner=banner, **kwargs)
        elif transport in ("http", "streamable-http"):
            await self.run_http_async(show_banner=banner, **kwargs)
        elif transport == "sse":
            raise ValueError("the legacy 'sse' transport is not implemented; use transport='http'")
        else:
            raise ValueError(f"unknown transport: {transport!r}")

    async def run_stdio_async(self, *, show_banner: bool = True, **kwargs: Any) -> None:
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.run_stdio(**kwargs))

    async def run_http_async(self, *, show_banner: bool = True, **kwargs: Any) -> None:
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.run_http(**kwargs))

    def asgi_app(
        self,
        *,
        path: str = "/mcp",
        allowed_origins: Optional[set[str]] = None,
        stateless: bool = False,
    ) -> Any:
        """Return the ASGI application, to run under uvicorn/gunicorn/hypercorn."""
        from ..transports.asgi import make_asgi_app

        return make_asgi_app(
            self, endpoint=path, allowed_origins=allowed_origins, stateless=stateless
        )

    def run_http(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        *,
        path: str = "/mcp",
        log_level: Optional[str] = None,
        allowed_origins: Optional[set[str]] = None,
        stateless: bool = False,
        uvicorn_config: Optional[dict[str, Any]] = None,
        sockets: Any = None,
    ) -> None:
        """Serve over Streamable HTTP, under uvicorn when it is importable.

        The uvicorn config matches FastMCP's defaults (``timeout_graceful_shutdown=2``,
        ``lifespan="on"``); ``uvicorn_config`` is merged in for any other settings.
        Falls back to the standard-library server when uvicorn is not installed.
        """
        import importlib.util

        if importlib.util.find_spec("uvicorn") is not None:
            import uvicorn

            app = self.asgi_app(path=path, allowed_origins=allowed_origins, stateless=stateless)
            config_kwargs: dict[str, Any] = {"timeout_graceful_shutdown": 2, "lifespan": "on"}
            if log_level is not None:
                config_kwargs["log_level"] = log_level.lower()
            config_kwargs.update(uvicorn_config or {})
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, **config_kwargs))
            if sockets is not None:
                import asyncio

                asyncio.run(server.serve(sockets=sockets))
            else:
                server.run()
            return

        from ..transports.http import serve_http

        self._enter_lifespan()
        try:
            serve_http(
                self,
                host=host,
                port=port,
                endpoint=path,
                allowed_origins=allowed_origins,
                stateless=stateless,
            )
        finally:
            self._exit_lifespan()

    def _enter_lifespan(self) -> Any:
        """Run the lifespan startup and store the yielded value.

        Accepts a sync or async context manager (``@contextmanager`` or
        ``@asynccontextmanager``). Synchronous resources work cleanly; an async
        resource bound to an event loop carries the usual cross-loop caveat,
        since each handler runs on its own loop.
        """
        if self.lifespan is None:
            return None
        manager = self.lifespan(self)
        if hasattr(manager, "__aenter__"):
            self._lifespan_cm = manager
            self._lifespan_async = True
            self._lifespan_state = run_coro(manager.__aenter__())
        elif hasattr(manager, "__enter__"):
            self._lifespan_cm = manager
            self._lifespan_async = False
            self._lifespan_state = manager.__enter__()
        else:
            self._lifespan_state = manager
        return self._lifespan_state

    def _acquire_lifespan(self) -> bool:
        """Refcounted lifespan entry for in-memory connections.

        Returns True when this caller owns a reference and must release it.
        A lifespan already entered by a transport runner (stdio, ASGI) is
        left alone.
        """
        if self.lifespan is None:
            return False
        with self._lifespan_lock:
            if self._lifespan_refs == 0 and (
                self._lifespan_cm is not None or self._lifespan_state is not None
            ):
                return False
            self._lifespan_refs += 1
            if self._lifespan_refs == 1:
                self._enter_lifespan()
            return True

    def _release_lifespan(self) -> None:
        with self._lifespan_lock:
            if self._lifespan_refs > 0:
                self._lifespan_refs -= 1
                if self._lifespan_refs == 0:
                    self._exit_lifespan()

    def _exit_lifespan(self) -> None:
        manager = self._lifespan_cm
        self._lifespan_cm = None
        if manager is None:
            return
        if self._lifespan_async:
            run_coro(manager.__aexit__(None, None, None))
        else:
            manager.__exit__(None, None, None)


# `Anodize` is a short alias for the canonical class name.
Anodize = AnodizeMCP

# Source-compatibility alias: code can `from anodize_mcp import FastMCP` today and
# switch to `from fastmcp import FastMCP` once a Rust toolchain is available,
# changing only the import line.
FastMCP = AnodizeMCP


def _normalize_annotations(annotations: Any) -> Optional[dict[str, Any]]:
    """Coerce annotations (a dict or any model-like object) to a plain dict.

    FastMCP callers may pass an ``mcp.types.ToolAnnotations``; reduce it to a dict
    so it serializes over the wire without the SDK.
    """
    if annotations is None or isinstance(annotations, dict):
        return annotations
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump(exclude_none=True)
    if hasattr(annotations, "__dict__"):
        return {k: v for k, v in vars(annotations).items() if v is not None}
    return dict(annotations)


def _find_context_param(func: Callable[..., Any]) -> Optional[str]:
    hints = _compat.get_type_hints(func)
    for param_name, param in inspect.signature(func).parameters.items():
        annotation = hints.get(param_name, param.annotation)
        annotation, _ = _compat.unwrap_annotated(annotation)
        if annotation is Context:
            return param_name
    return None


def _docstring(func: Callable[..., Any]) -> Optional[str]:
    # FastMCP keeps the whole docstring unless parameter descriptions were
    # actually extracted from it; only then is the summary used alone.
    doc = inspect.getdoc(func)
    if doc and parse_param_docs(doc):
        return doc_summary(doc)
    return doc


def _return_annotation(func: Callable[..., Any]) -> Any:
    hints = _compat.get_type_hints(func)
    if "return" in hints:
        return hints["return"]
    return inspect.signature(func).return_annotation
