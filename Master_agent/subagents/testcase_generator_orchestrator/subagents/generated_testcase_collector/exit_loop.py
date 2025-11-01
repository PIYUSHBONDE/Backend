"""
Tools for Testcase Collector Agent

This module provides tools for analyzing and validating Testcase.
"""

from typing import Any, Dict

from google.adk.tools.tool_context import ToolContext


def exit_loop(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Call this function ONLY when the features_to_process list is empty,
    signaling the iterative process should end.

    Args:
        tool_context: Context for tool execution

    Returns:
        Empty dictionary
    """
    print("\n----------- EXIT LOOP TRIGGERED -----------")
    print("All features have been processed.")
    print("Loop will exit now")
    print("------------------------------------------\n")

    tool_context.actions.escalate = True
    return {}
