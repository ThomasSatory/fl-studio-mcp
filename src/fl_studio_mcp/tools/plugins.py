"""Plugin control tools for FL Studio.

NOTE: These tools can only control EXISTING plugins. FL Studio's scripting API
does not support loading new plugins programmatically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_plugin_tools(mcp: FastMCP) -> None:
    """Register plugin control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_is_plugin_valid(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> bool:
        """Check if a plugin exists at the given location.

        For channel rack plugins, use just index.
        For mixer effect plugins, use index as mixer track and slot_index as effect slot.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.isValid", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return result.get("valid", False)

    @mcp.tool()
    def fl_get_plugin_name(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> str:
        """Get the name of a plugin.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.getName", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return result.get("name", "")

    @mcp.tool()
    def fl_get_plugin_param_count(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> int:
        """Get the number of parameters a plugin has.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.getParamCount", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return result.get("count", 0)

    @mcp.tool()
    def fl_get_plugin_params(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True,
        max_params: int = 50
    ) -> list[dict]:
        """Get all parameters of a plugin with their current values.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
            max_params: Maximum number of parameters to return (default 50)
        """
        result = run(get_connection(), "plugins.getParams", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
            "max_params": max_params,
        })

        return result.get("params", [])

    @mcp.tool()
    def fl_get_plugin_param_value(
        param_index: int,
        plugin_index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> dict:
        """Get the value of a specific plugin parameter.

        Args:
            param_index: Parameter index
            plugin_index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.getParamValue", {
            "param_index": param_index,
            "plugin_index": plugin_index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return {
            "index": result.get("index", param_index),
            "name": result.get("name", ""),
            "value": result.get("value", 0),
            "value_string": result.get("value_string", ""),
        }

    @mcp.tool()
    def fl_set_plugin_param_value(
        param_index: int,
        value: float,
        plugin_index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> str:
        """Set the value of a plugin parameter.

        Args:
            param_index: Parameter index
            value: New value (typically 0.0 to 1.0, but depends on parameter)
            plugin_index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.setParamValue", {
            "param_index": param_index,
            "value": value,
            "plugin_index": plugin_index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        name = result.get("name", f"Parameter {param_index}")
        new_value = result.get("value", value)
        value_str = result.get("value_string", "")
        return f"Parameter '{name}' set to {new_value:.4f} ({value_str})"

    @mcp.tool()
    def fl_get_preset_count(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> int:
        """Get the number of presets available for a plugin.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.getPresetCount", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return result.get("count", 0)

    @mcp.tool()
    def fl_next_preset(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> str:
        """Switch to the next preset for a plugin.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.nextPreset", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        plugin_name = result.get("plugin_name", "Plugin")
        return f"Switched '{plugin_name}' to next preset"

    @mcp.tool()
    def fl_prev_preset(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> str:
        """Switch to the previous preset for a plugin.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.prevPreset", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        plugin_name = result.get("plugin_name", "Plugin")
        return f"Switched '{plugin_name}' to previous preset"

    @mcp.tool()
    def fl_get_plugin_color(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True
    ) -> str:
        """Get the color of a plugin.

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "plugins.getColor", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
        })

        return result.get("color", "0x0")
