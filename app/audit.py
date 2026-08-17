import json
import logging

logger = logging.getLogger("claude_odoo_mcp.audit")

def log_tool(tool: str, parameters=None, count=None, success=True, error=None):
    logger.info(json.dumps({
        "event": "mcp_tool_call",
        "tool": tool,
        "parameters": parameters or {},
        "record_count": count,
        "success": success,
        "error": error,
    }, default=str))
