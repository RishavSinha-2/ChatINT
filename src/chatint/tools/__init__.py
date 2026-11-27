"""Unified tool registry and built-in web/workspace tools for ChatInt."""
from __future__ import annotations

from chatint.tools.registry import (
    MAX_AGENT_CALLS_PER_PROMPT,
    MAX_AGENT_ROUNDS,
    ToolSpec,
    build_tools_system_hint,
    get_active_tools,
    get_tool_budget_limits,
    get_tool_spec,
    list_registered_tools,
    register_tool,
    to_anthropic_tools,
    to_gemini_tools,
    to_openai_tools,
)
from chatint.tools.web_tools import FETCH_URL_TOOL_NAME, fetch_url, register_web_tools
from chatint.tools.workspace_tools import (
    register_workspace_tools,
    resolve_workspace_path,
    tool_edit_file,
    tool_list_dir,
    tool_read_file,
    tool_search_files,
    tool_write_file,
)

from chatint.web_search_tool import IMAGE_SEARCH_TOOL_NAME, WEB_SEARCH_TOOL_NAME

register_web_tools()
register_workspace_tools()

__all__ = [
    "FETCH_URL_TOOL_NAME",
    "IMAGE_SEARCH_TOOL_NAME",
    "MAX_AGENT_CALLS_PER_PROMPT",
    "MAX_AGENT_ROUNDS",
    "ToolSpec",
    "WEB_SEARCH_TOOL_NAME",
    "build_tools_system_hint",
    "fetch_url",
    "get_active_tools",
    "get_tool_budget_limits",
    "get_tool_spec",
    "list_registered_tools",
    "register_tool",
    "resolve_workspace_path",
    "to_anthropic_tools",
    "to_gemini_tools",
    "to_openai_tools",
    "tool_edit_file",
    "tool_list_dir",
    "tool_read_file",
    "tool_search_files",
    "tool_write_file",
]
