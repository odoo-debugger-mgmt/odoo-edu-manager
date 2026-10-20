"""MCP server exposing the Odoo Edu Manager to an agent.

Wraps `docker_manager` — the module that holds the actual domain logic
(docker orchestration, `odoo shell`, psql) — rather than the Flask app.
The HTTP layer exists to make the manager usable by a human in a browser;
none of that is useful to an agent.

Deliberately omitted: remove_instance, delete_database, _drop_database
and the restore path. Those destroy databases, there is no agent workflow
that needs them, and the dashboard is already the right interface for a
human to do it deliberately. Everything exposed here is read-only.

Run:  ./venv/bin/python -m odoo_edu_manager.mcp_server
"""

from . import docker_manager as dm
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="odoo-edu-manager",
    instructions=(
        "Inspect the Docker-based Odoo instances this manager runs. "
        "list_instances() says which exist, their Odoo version, database "
        "and whether they are running."
    ),
)


# --- read ---------------------------------------------------------------


@mcp.tool(description="List the managed Odoo instances, with version, db name and running state.")
def list_instances() -> list[dict]:
    return [
        {"name": i["name"], "version": i["version"], "db_name": i["db_name"], "status": i["status"]}
        for i in dm.list_instances()
    ]


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
