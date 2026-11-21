"""Interface adapters with identical semantics (``nl-dsl-llm`` §3 Universal Interface Parity).

* REST  – stdlib ``http.server`` (loopback by default): ``/dsl``, ``/query``, ``/suggest``, ``/schema``,
  ``/history``, ``/health``
* MCP   – JSON-RPC 2.0 over stdio: tools ``nl_ask``, ``execute_dsl``, ``suggest_options``, ``list_actions``

Both call the same :class:`~bottlenecks.engine.Engine`; NL/REST/MCP requests are
never auto-confirmed, so actions with ``process``/``write`` effects need
``confirm: true`` explicitly.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import IO, Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .dsl import DSLResult

MAX_BODY = 1_000_000
HTTP_STATUS = {"OK": 200, "NOT_FOUND": 404, "VALIDATION_ERROR": 422, "CLARIFY": 422, "UNAUTHORIZED": 403,
               "EXECUTION_ERROR": 500}
SCHEMA_FILES = ("dsl-command", "dsl-result", "nl-query", "urtlog-record")


def load_schemas() -> Dict[str, Any]:
    out = {}
    for name in SCHEMA_FILES:
        text = resources.files("bottlenecks").joinpath("schemas", f"{name}.schema.json").read_text(encoding="utf-8")
        out[name] = json.loads(text)
    return out


def schema_document(engine) -> Dict[str, Any]:
    return {"registry": engine.registry.manifest(), "schemas": load_schemas()}


# ---------------------------------------------------------------------- REST
def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"bottlenecks/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep stdout clean
            sys.stderr.write("rest: " + fmt % args + "\n")

        def _send(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _result(self, res: DSLResult) -> None:
            self._send(HTTP_STATUS.get(res.status, 400), res.to_dict())

        def _body(self) -> Optional[Dict[str, Any]]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"success": False, "errors": [{"code": "BODY_TOO_LARGE", "message": "max 1MB"}]})
                return None
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"success": False, "errors": [{"code": "INVALID_JSON", "message": "body must be JSON"}]})
                return None
            if not isinstance(data, dict):
                self._send(400, {"success": False, "errors": [{"code": "INVALID_JSON", "message": "body must be an object"}]})
                return None
            return data

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/health":
                self._send(200, {"status": "ok", "version": __version__, "ledger": engine.ledger is not None,
                                 "llm": engine.translator.enabled})
            elif url.path == "/schema":
                self._send(200, schema_document(engine))
            elif url.path == "/suggest":
                self._send(200, engine.suggest(q.get("q", ""), max_suggestions=int(q.get("max", 5))))
            elif url.path == "/history":
                self._result(engine.execute(f"history.list limit={int(q.get('limit', 20))}", confirmed=False))
            else:
                self._send(404, {"success": False, "errors": [{"code": "NO_ROUTE", "message": url.path}]})

        def do_POST(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            data = self._body()
            if data is None:
                return
            confirm = data.get("confirm") is True
            if url.path == "/dsl":
                command = data.get("dsl") if isinstance(data.get("dsl"), str) else data.get("command", data)
                if isinstance(command, dict):
                    command = {k: v for k, v in command.items() if k != "confirm"}
                self._result(engine.execute(command, source_layer="direct_dsl", confirmed=confirm))
            elif url.path == "/query":
                allow = data.get("allowLlmFallback", True)
                self._result(engine.ask(str(data.get("query", "")), allow_llm_fallback=allow is not False,
                                        confirmed=confirm))
            else:
                self._send(404, {"success": False, "errors": [{"code": "NO_ROUTE", "message": url.path}]})

    return Handler


def make_server(engine, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(engine))


def serve_rest(engine, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(engine, host, port)
    sys.stderr.write(f"bottlenecks REST on http://{host}:{server.server_address[1]} (/dsl /query /suggest /schema)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()


# ----------------------------------------------------------------------- MCP
MCP_PROTOCOL = "2024-11-05"


def mcp_tools() -> list:
    return [
        {"name": "nl_ask", "description": "Ask in Polish or English about bottlenecks in code, runtime, pipelines or "
                                          "the host; compiled to canonical DSL and executed safely.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string", "description": "e.g. 'znajdź wąskie gardła w kodzie src'"},
             "allow_llm_fallback": {"type": "boolean", "default": True},
             "confirm": {"type": "boolean", "default": False,
                         "description": "required for actions with process/write effects"}}}},
        {"name": "execute_dsl", "description": "Execute a canonical bottlenecks DSL statement, e.g. "
                                               "'code.scan path=src top=10 min_severity=high'.",
         "inputSchema": {"type": "object", "required": ["dsl_command"], "properties": {
             "dsl_command": {"type": "string"}, "confirm": {"type": "boolean", "default": False}}}},
        {"name": "suggest_options", "description": "Autocomplete / option network for a partial query.",
         "inputSchema": {"type": "object", "required": ["partial_query"], "properties": {
             "partial_query": {"type": "string"}, "max_suggestions": {"type": "integer", "default": 5}}}},
        {"name": "list_actions", "description": "Action registry: every DSL action with its Action URI, params "
                                                "and effects.", "inputSchema": {"type": "object", "properties": {}}},
    ]


def _tool_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2, default=str)}],
            "isError": is_error}


def mcp_call_tool(engine, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    confirm = args.get("confirm") is True
    if name == "nl_ask":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("nl_ask requires a non-empty 'query'")
        res = engine.ask(query, allow_llm_fallback=args.get("allow_llm_fallback", True) is not False, confirmed=confirm)
        return _tool_result(res.to_dict(), not res.success)
    if name == "execute_dsl":
        dsl = args.get("dsl_command")
        if not isinstance(dsl, str) or not dsl.strip():
            raise ValueError("execute_dsl requires a non-empty 'dsl_command'")
        res = engine.execute(dsl, source_layer="direct_dsl", confirmed=confirm)
        return _tool_result(res.to_dict(), not res.success)
    if name == "suggest_options":
        return _tool_result(engine.suggest(str(args.get("partial_query", "")),
                                           max_suggestions=int(args.get("max_suggestions", 5))))
    if name == "list_actions":
        return _tool_result(engine.registry.manifest())
    raise LookupError(f"unknown tool: {name}")


def mcp_handle(engine, msg: Any) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message; returns the response (``None`` for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result: Any = {"protocolVersion": MCP_PROTOCOL, "capabilities": {"tools": {}},
                           "serverInfo": {"name": "bottlenecks", "version": __version__}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": mcp_tools()}
        elif method == "tools/call":
            result = mcp_call_tool(engine, params.get("name", ""), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except (ValueError, LookupError) as exc:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_mcp(engine, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: Optional[Dict[str, Any]] = {"jsonrpc": "2.0", "id": None,
                                              "error": {"code": -32700, "message": "Parse error"}}
        else:
            resp = mcp_handle(engine, msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            stdout.flush()
