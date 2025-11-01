"""
Tools for Testcase Reviewer Agent

This module provides tools for analyzing and validating Testcase.
"""

from typing import Any, Dict

from google.adk.tools.tool_context import ToolContext


def exit_loop(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Terminates the current review and returns control to the parent loop.

    Args:
        tool_context: Context for tool execution

    Returns:
        Empty dictionary
    """
    print("\n----------- EXIT LOOP TRIGGERED -----------")
    print("Post review completed successfully")
    print("Loop will exit now")
    print("------------------------------------------\n")

    tool_context.actions.escalate = True
    return {}
