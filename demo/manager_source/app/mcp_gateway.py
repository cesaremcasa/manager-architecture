"""A narrow Streamable HTTP MCP surface for Manager Group OS.

The transport is intentionally tiny and standards-shaped: each POST returns a
JSON-RPC response, which Streamable HTTP clients may consume directly.  Tool
implementations delegate to the same Postgres-only read model used by the UI
and the AI runtime; there is no SQL or external-source tool exposed to clients.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Engine

from app.group_operations import (
    compare_units,
    data_lineage,
    group_overview,
    inventory_risks,
    invoice_exceptions,
    labor_variance,
    market_snapshot,
    sales_trend,
    supplier_price_changes,
    weather_impact,
)


TOOL_DEFINITIONS: tuple[dict[str, object], ...] = (
    {"name": "get_group_overview", "description": "Read the consolidated, source-labelled group overview.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "compare_units", "description": "Compare sales, labor, delivery, and freshness across authorized units.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_sales_trend", "description": "Read normalized daily sales trend for the group.", "inputSchema": {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 730}}, "additionalProperties": False}},
    {"name": "get_labor_variance", "description": "Read labor variance context by unit; benchmarks are context, not targets.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_inventory_risks", "description": "Read inventory counts currently below their source-provided par levels.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    {"name": "get_invoice_exceptions", "description": "Read imported invoices awaiting human approval. Does not approve or pay anything.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    {"name": "get_supplier_price_changes", "description": "Read recent normalized invoice lines for price review.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    {"name": "get_weather_impact", "description": "Read source-labelled weather observations and forecasts without causal claims.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_market_snapshot", "description": "Read dated market observations and identify whether baseline or public API supplied them.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_data_lineage", "description": "Explain normalized record origin: file, sheet, row, revision, source type and import time.", "inputSchema": {"type": "object", "properties": {"record_id": {"type": "string", "format": "uuid"}, "limit": {"type": "integer", "minimum": 1, "maximum": 250}}, "additionalProperties": False}},
)


class MCPToolError(ValueError):
    pass


def _tool_map(engine: Engine, group_id: UUID) -> dict[str, Callable[[Mapping[str, object]], dict[str, object]]]:
    return {
        "get_group_overview": lambda _args: group_overview(engine, group_id),
        "compare_units": lambda _args: compare_units(engine, group_id),
        "get_sales_trend": lambda args: sales_trend(engine, group_id, int(args.get("days", 30))),
        "get_labor_variance": lambda _args: labor_variance(engine, group_id),
        "get_inventory_risks": lambda args: inventory_risks(engine, group_id, int(args.get("limit", 30))),
        "get_invoice_exceptions": lambda args: invoice_exceptions(engine, group_id, int(args.get("limit", 30))),
        "get_supplier_price_changes": lambda args: supplier_price_changes(engine, group_id, int(args.get("limit", 30))),
        "get_weather_impact": lambda _args: weather_impact(engine, group_id),
        "get_market_snapshot": lambda _args: market_snapshot(engine, group_id),
        "get_data_lineage": lambda args: data_lineage(engine, group_id, record_id=UUID(str(args["record_id"])) if args.get("record_id") else None, limit=int(args.get("limit", 100))),
    }


def call_tool(engine: Engine, group_id: UUID, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
    handlers = _tool_map(engine, group_id)
    handler = handlers.get(name)
    if handler is None:
        raise MCPToolError(f"unknown or unauthorized tool: {name}")
    try:
        return handler(arguments or {})
    except (TypeError, ValueError) as error:
        raise MCPToolError("tool arguments are invalid") from error


def jsonrpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_jsonrpc(engine: Engine, group_id: UUID, payload: Mapping[str, Any]) -> dict[str, object]:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, Mapping):
        return jsonrpc_error(request_id, -32600, "invalid request")
    if method == "initialize":
        return jsonrpc_result(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "manager-group-os", "version": "0.1.0"}})
    if method == "tools/list":
        return jsonrpc_result(request_id, {"tools": list(TOOL_DEFINITIONS)})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return jsonrpc_error(request_id, -32602, "tools/call requires name and object arguments")
        try:
            result = call_tool(engine, group_id, name, arguments)
        except MCPToolError as error:
            return jsonrpc_result(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        return jsonrpc_result(request_id, {"content": [{"type": "text", "text": __import__("json").dumps(result, default=str)}], "structuredContent": result, "isError": False})
    if method.startswith("notifications/"):
        return {"jsonrpc": "2.0", "method": method}
    return jsonrpc_error(request_id, -32601, "method not found")

