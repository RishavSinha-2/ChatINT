"""Unified tool registry, schema conversion, and prompt hint helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    MAX_TOOL_CALLS_PER_PROMPT,
    MAX_TOOL_ROUNDS,
    WEB_SEARCH_TOOL_NAME,
    get_web_search_system_hint,
)

MAX_AGENT_ROUNDS = 25
MAX_AGENT_CALLS_PER_PROMPT = 30


@dataclass(frozen=True)
class ToolSpec:
    """Provider-agnostic tool definition and execution descriptor."""

    name: str
    description: str
    parameters_schema: Dict[str, Any]
    category: str  # "web" | "workspace_read" | "workspace_write"
    requires_workspace: bool
    mutating: bool
    handler: Callable[..., Dict[str, Any]]
    preview_fn: Optional[Callable[[Dict[str, Any], Optional[Path]], Dict[str, Any]]] = None


_REGISTRY: Dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    """Register or replace a tool definition in the global registry."""
    _REGISTRY[spec.name] = spec
    return spec


def get_tool_spec(name: str) -> Optional[ToolSpec]:
    """Look up a registered tool by exact name."""
    return _REGISTRY.get((name or "").strip())


def list_registered_tools() -> List[ToolSpec]:
    """Return all registered tools in registration order."""
    return list(_REGISTRY.values())


def get_active_tools(
    web_search_enabled: bool = False,
    agent_enabled: bool = False,
    has_workspace: bool = False,
) -> List[ToolSpec]:
    """Return the active ToolSpec list allowed for the current tab configuration."""
    active: List[ToolSpec] = []
    if web_search_enabled:
        for name in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
            spec = _REGISTRY.get(name)
            if spec:
                active.append(spec)
    if agent_enabled:
        for spec in _REGISTRY.values():
            if spec.name in (WEB_SEARCH_TOOL_NAME, IMAGE_SEARCH_TOOL_NAME):
                continue
            if spec.requires_workspace and not has_workspace:
                continue
            active.append(spec)
    return active


def to_openai_tools(specs: List[ToolSpec]) -> List[Dict[str, Any]]:
    """Convert ToolSpec entries to OpenAI Chat Completions function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters_schema,
            },
        }
        for s in specs
    ]


def to_anthropic_tools(specs: List[ToolSpec]) -> List[Dict[str, Any]]:
    """Convert ToolSpec entries to Anthropic Messages tool specs."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": s.parameters_schema,
        }
        for s in specs
    ]


def to_gemini_tools(specs: List[ToolSpec]) -> List[Any]:
    """Convert ToolSpec entries to a Google GenAI Tool containing FunctionDeclarations."""
    if not specs:
        return []
    from google.genai import types as genai_types

    declarations = [
        genai_types.FunctionDeclaration(
            name=s.name,
            description=s.description,
            parameters_json_schema=s.parameters_schema,
        )
        for s in specs
    ]
    return [genai_types.Tool(function_declarations=declarations)]


def build_tools_system_hint(
    web_search_enabled: bool = False,
    agent_enabled: bool = False,
    workspace_dir: str = "",
    agent_mode: str = "manual",
) -> str:
    """Build a concise system instruction describing active web and workspace tools."""
    parts: List[str] = []
    if web_search_enabled:
        parts.append(get_web_search_system_hint())
    if agent_enabled:
        clean_ws = (workspace_dir or "").strip()
        mode_norm = "auto" if str(agent_mode or "").strip().lower() == "auto" else "manual"
        if clean_ws:
            approval_note = (
                "Auto mode is active: read and write tools execute automatically."
                if mode_norm == "auto"
                else "Manual mode is active: read tools run automatically, while write/edit tools wait for user approval."
            )
            parts.append(
                "Agent Workspace Mode is active.\n"
                f"Workspace root: `{clean_ws}` (use relative paths inside this folder; never escape outside it).\n"
                "Available agent tools: `list_dir`, `read_file`, `search_files`, `write_file`, `edit_file`, `fetch_url`.\n"
                "Always inspect files with `list_dir`, `search_files`, or `read_file` before modifying them with `edit_file` or `write_file`.\n"
                f"{approval_note}\n"
                f"Agent step budget: up to {MAX_AGENT_CALLS_PER_PROMPT} tool calls across {MAX_AGENT_ROUNDS} rounds."
            )
        else:
            parts.append(
                "Agent Mode is active, but NO Workspace folder is currently selected.\n"
                "Only `fetch_url` (and web search if enabled) is available. "
                "If the user asks to inspect or edit local files, let them know to select a Workspace folder first."
            )
    return "\n\n".join(parts).strip()


def get_tool_budget_limits(agent_enabled: bool) -> tuple[int, int]:
    """Return (max_rounds, max_calls_per_prompt) for the active mode."""
    if agent_enabled:
        return MAX_AGENT_ROUNDS, MAX_AGENT_CALLS_PER_PROMPT
    return MAX_TOOL_ROUNDS, MAX_TOOL_CALLS_PER_PROMPT
