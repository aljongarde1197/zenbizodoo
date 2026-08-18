import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from app.audit import log_tool
from app.config import Settings
from app.odoo_client import OdooAPIError, OdooClient
from app.oauth import JWKSJWTTokenVerifier
from app.security import (
    ALLOWED_PICKING_STATES,
    ALLOWED_SALES_STATES,
    clamp_limit,
    clean_search,
    positive_id,
    choice,
)
from mcp.server.transport_security import TransportSecuritySettings


settings = Settings.from_env()

logging.basicConfig(
    level=getattr(
        logging,
        settings.log_level,
        logging.INFO,
    )
)


# ---------------------------------------------------------------------------
# MCP configuration
# ---------------------------------------------------------------------------

# mcp_kwargs: dict[str, Any] = {
#     "stateless_http": True,
#     "json_response": True,
# }

mcp_kwargs: dict[str, Any] = {
    "stateless_http": True,
    "json_response": True,
    "transport_security": TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "zenbizodoo-production.up.railway.app",
            "zenbizodoo-production.up.railway.app:*",
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
        ],
        allowed_origins=[
            "https://zenbizodoo-production.up.railway.app",
            "https://zenbizodoo-production.up.railway.app:*",
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
    ),
}


if settings.auth_enabled:
    mcp_kwargs["token_verifier"] = JWKSJWTTokenVerifier(
        issuer=settings.auth_issuer_url,
        audience=settings.auth_audience,
        jwks_url=settings.auth_jwks_url,
        required_scopes=settings.auth_required_scopes,
        algorithms=settings.auth_algorithms,
    )

    mcp_kwargs["auth"] = AuthSettings(
        issuer_url=AnyHttpUrl(
            settings.auth_issuer_url
        ),
        resource_server_url=AnyHttpUrl(
            settings.auth_resource_server_url
        ),
        required_scopes=settings.auth_required_scopes,
    )


mcp = FastMCP(
    "Claude Odoo OAuth",
    **mcp_kwargs,
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mcp.custom_route(
    "/health",
    methods=["GET"],
)
async def health(request):
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "status": "ok",
            "oauth_enabled": settings.auth_enabled,
            "transport": settings.transport,
        }
    )


# ---------------------------------------------------------------------------
# Odoo client
# ---------------------------------------------------------------------------

odoo = OdooClient(
    base_url=settings.odoo_url,
    database=settings.odoo_database,
    api_key=settings.odoo_api_key,
    timeout_seconds=settings.request_timeout_seconds,
)


# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------

def failed(
    tool: str,
    exc: Exception,
    params: dict[str, Any],
):
    log_tool(
        tool,
        params,
        success=False,
        error=str(exc),
    )

    return {
        "success": False,
        "error": str(exc),
    }


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@mcp.tool()
async def test_odoo_connection():
    """
    Verify Odoo JSON-2 authentication without changing data.
    """

    tool = "test_odoo_connection"

    try:
        await odoo.search_read(
            model="res.users",
            domain=[
                ["id", "=", 0],
            ],
            fields=[
                "id",
            ],
            limit=1,
        )

        log_tool(tool)

        return {
            "success": True,
            "message": "Odoo authentication succeeded.",
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            {},
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_users(
    search: str = "",
    active_only: bool = True,
    limit: int = 20,
):
    """
    Search Odoo users.

    Read-only.

    Searches by user name or login/email.

    Parameters:
    - search: partial user name or login/email
    - active_only: when true, only active users are returned
    - limit: maximum number of records to return
    """

    tool = "search_users"

    params = {
        "search": clean_search(search),
        "active_only": active_only,
        "limit": limit,
    }

    try:
        safe_limit = clamp_limit(
            limit,
            settings.max_results,
        )

        domain: list[Any] = []

        if active_only:
            domain.append(
                ["active", "=", True]
            )

        if params["search"]:
            domain.extend(
                [
                    "|",
                    [
                        "name",
                        "ilike",
                        params["search"],
                    ],
                    [
                        "login",
                        "ilike",
                        params["search"],
                    ],
                ]
            )

        rows = await odoo.search_read(
            model="res.users",
            domain=domain,
            fields=[
                "id",
                "name",
                "login",
                "active",
                "partner_id",
                "company_id",
                "company_ids",
                "create_date",
            ],
            limit=safe_limit,
            order="name asc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "users": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Projects / Tasks
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_user_projects_tasks(
    search: str,
    active_only: bool = True,
    limit: int = 20,
):
    """
    Search for Odoo users and return projects that contain tasks
    assigned to the matched user(s).

    Read-only.

    Searches users by name or login/email, then finds project tasks
    where any matched user is included in the task assignees.

    Parameters:
    - search: partial user name or login/email
    - active_only: when true, only active users and active tasks are returned
    - limit: maximum number of task records to return
    """

    tool = "search_user_projects_tasks"

    params = {
        "search": clean_search(search),
        "active_only": active_only,
        "limit": limit,
    }

    try:
        if not params["search"]:
            raise ValueError(
                "Provide a user name or login/email."
            )

        safe_limit = clamp_limit(
            limit,
            settings.max_results,
        )

        user_domain: list[Any] = []

        if active_only:
            user_domain.append(
                ["active", "=", True]
            )

        user_domain.extend(
            [
                "|",
                [
                    "name",
                    "ilike",
                    params["search"],
                ],
                [
                    "login",
                    "ilike",
                    params["search"],
                ],
            ]
        )

        users = await odoo.search_read(
            model="res.users",
            domain=user_domain,
            fields=[
                "id",
                "name",
                "login",
                "active",
            ],
            limit=safe_limit,
            order="name asc",
        )

        if not users:
            log_tool(
                tool,
                params,
                0,
            )

            return {
                "success": True,
                "count": 0,
                "matched_users": [],
                "projects": [],
                "message": "No matching user found.",
            }

        user_ids = [
            user["id"]
            for user in users
        ]

        task_domain: list[Any] = [
            [
                "user_ids",
                "in",
                user_ids,
            ],
            [
                "project_id",
                "!=",
                False,
            ],
        ]

        if active_only:
            task_domain.append(
                ["active", "=", True]
            )

        tasks = await odoo.search_read(
            model="project.task",
            domain=task_domain,
            fields=[
                "id",
                "name",
                "project_id",
                "user_ids",
                "stage_id",
                "date_deadline",
                "priority",
                "create_date",
                "write_date",
            ],
            limit=safe_limit,
            order="project_id asc, id asc",
        )

        projects_by_id: dict[int, dict[str, Any]] = {}

        for task in tasks:
            project = task.get("project_id")

            if not project:
                continue

            project_id = project[0]
            project_name = (
                project[1]
                if len(project) > 1
                else ""
            )

            if project_id not in projects_by_id:
                projects_by_id[project_id] = {
                    "id": project_id,
                    "name": project_name,
                    "task_count": 0,
                    "tasks": [],
                }

            projects_by_id[project_id]["tasks"].append(
                task
            )
            projects_by_id[project_id]["task_count"] += 1

        projects = list(
            projects_by_id.values()
        )

        log_tool(
            tool,
            params,
            len(tasks),
        )

        return {
            "success": True,
            "count": len(projects),
            "task_count": len(tasks),
            "matched_users": users,
            "projects": projects,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------

async def _odoo_json2_call(
    model: str,
    method: str,
    payload: dict[str, Any],
):
    """
    Call an Odoo JSON-2 model method.

    Used only for controlled write operations that are intentionally
    exposed as MCP tools.
    """

    url = (
        f"{settings.odoo_url.rstrip('/')}"
        f"/json/2/{model}/{method}"
    )

    headers = {
        "Authorization": f"bearer {settings.odoo_api_key}",
        "X-Odoo-Database": settings.odoo_database,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
    ) as client:
        response = await client.post(
            url,
            headers=headers,
            json=payload,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Odoo JSON-2 {model}.{method} failed "
            f"with HTTP {response.status_code}: "
            f"{response.text}"
        )

    return response.json()


async def _disable_project_email_alias(
    project_id: int,
):
    """
    Disable the automatic Project email alias created by Odoo.

    Odoo Project may automatically create/link a mail.alias when a
    project.project record is created so incoming email can create tasks.

    We intentionally DO NOT unlink/delete the mail.alias record because
    project.project may depend on its alias_id relationship internally.
    Instead, we clear alias_name, which prevents the alias from being a
    usable incoming email address while preserving Odoo's internal link.

    project.task creation itself does not require an email alias.
    """

    positive_id(
        project_id,
        "project_id",
    )

    projects = await odoo.read(
        model="project.project",
        record_ids=[
            project_id,
        ],
        fields=[
            "id",
            "name",
            "alias_id",
        ],
    )

    if not projects:
        raise ValueError(
            "Created project could not be read while disabling its alias."
        )

    project = projects[0]
    alias = project.get(
        "alias_id"
    )

    if not alias:
        return {
            "disabled": False,
            "alias_id": None,
            "message": "No project email alias was created.",
        }

    alias_id = (
        alias[0]
        if isinstance(alias, (list, tuple))
        else alias
    )

    if not isinstance(alias_id, int):
        return {
            "disabled": False,
            "alias_id": None,
            "message": "Project alias could not be resolved.",
        }

    await _odoo_json2_call(
        model="mail.alias",
        method="write",
        payload={
            "ids": [
                alias_id
            ],
            "vals": {
                "alias_name": False,
            },
        },
    )

    return {
        "disabled": True,
        "alias_id": alias_id,
        "message": (
            "Automatic project email alias was disabled "
            "by clearing mail.alias.alias_name."
        ),
    }


async def _resolve_existing_user(
    search: str,
):
    """
    Resolve one active existing Odoo user by name or login/email.

    Exact case-insensitive matches are preferred. If no exact match
    exists, a partial search is attempted. Ambiguous matches are rejected
    so a task is not assigned to the wrong user.
    """

    search = clean_search(search)

    if not search:
        raise ValueError(
            "Assignee name or login/email cannot be empty."
        )

    exact_users = await odoo.search_read(
        model="res.users",
        domain=[
            ["active", "=", True],
            "|",
            [
                "name",
                "=ilike",
                search,
            ],
            [
                "login",
                "=ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
            "login",
        ],
        limit=2,
        order="name asc",
    )

    if len(exact_users) == 1:
        return exact_users[0]

    if len(exact_users) > 1:
        raise ValueError(
            f'More than one active user exactly matches "{search}". '
            "Use the user's login/email instead."
        )

    partial_users = await odoo.search_read(
        model="res.users",
        domain=[
            ["active", "=", True],
            "|",
            [
                "name",
                "ilike",
                search,
            ],
            [
                "login",
                "ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
            "login",
        ],
        limit=2,
        order="name asc",
    )

    if not partial_users:
        raise ValueError(
            f'No active Odoo user found for "{search}".'
        )

    if len(partial_users) > 1:
        matches = ", ".join(
            f'{user.get("name")} ({user.get("login")})'
            for user in partial_users
        )

        raise ValueError(
            f'User search "{search}" is ambiguous. '
            f"Matching users include: {matches}. "
            "Use the exact name or login/email."
        )

    return partial_users[0]


@mcp.tool()
async def create_project(
    project_name: str,
    description: str = "",
    tasks: list[dict[str, Any]] | None = None,
):
    """
    Create an Odoo project, optionally with tasks and existing-user
    assignments.

    Write operation.

    The project can be created with no tasks by omitting `tasks` or
    passing an empty list.

    Each task object supports:
    - name: required task name
    - description: optional task description
    - deadline: optional deadline in YYYY-MM-DD format
    - assignees: optional list of existing Odoo user names or login/emails

    Examples:

    Create a project only:
    {
        "project_name": "Website Redesign"
    }

    Create a project with tasks:
    {
        "project_name": "Website Redesign",
        "description": "Redesign the company website.",
        "tasks": [
            {
                "name": "Requirements Gathering",
                "assignees": ["Jenny Santos"]
            },
            {
                "name": "Frontend Development",
                "description": "Implement approved website UI.",
                "deadline": "2026-08-30",
                "assignees": [
                    "Aljon Garde",
                    "jenny@example.com"
                ]
            }
        ]
    }

    Important:
    - Assignees must already exist as active Odoo users.
    - Ambiguous user searches are rejected.
    - Users are resolved before the project is created.
    - Any automatic email alias created by Odoo for the new project is
      immediately disabled by clearing mail.alias.alias_name.
    - Creating project.task records does not create/enable aliases here.
    """

    tool = "create_project"

    project_name = clean_search(project_name)
    description = (
        description.strip()
        if isinstance(description, str)
        else ""
    )
    tasks = tasks or []

    params = {
        "project_name": project_name,
        "description": description,
        "tasks": tasks,
    }

    try:
        if not project_name:
            raise ValueError(
                "Provide a project name."
            )

        if not isinstance(tasks, list):
            raise ValueError(
                "tasks must be a list."
            )

        prepared_tasks: list[dict[str, Any]] = []

        # Validate every task and resolve every assignee first.
        # This prevents creating the project when a requested user
        # cannot be found or is ambiguous.
        for index, task_data in enumerate(
            tasks,
            start=1,
        ):
            if not isinstance(task_data, dict):
                raise ValueError(
                    f"Task #{index} must be an object."
                )

            task_name = clean_search(
                str(
                    task_data.get("name") or ""
                )
            )

            if not task_name:
                raise ValueError(
                    f"Task #{index} requires a name."
                )

            task_description = task_data.get(
                "description"
            ) or ""

            deadline = task_data.get(
                "deadline"
            )

            assignees = task_data.get(
                "assignees"
            ) or []

            if isinstance(assignees, str):
                assignees = [
                    assignees
                ]

            if not isinstance(assignees, list):
                raise ValueError(
                    f'Assignees for task "{task_name}" '
                    "must be a list."
                )

            resolved_users = []

            for assignee in assignees:
                user = await _resolve_existing_user(
                    str(assignee)
                )

                if user["id"] not in [
                    item["id"]
                    for item in resolved_users
                ]:
                    resolved_users.append(
                        user
                    )

            prepared_tasks.append(
                {
                    "name": task_name,
                    "description": str(
                        task_description
                    ),
                    "deadline": (
                        str(deadline)
                        if deadline
                        else None
                    ),
                    "users": resolved_users,
                }
            )

        project_values: dict[str, Any] = {
            "name": project_name,
        }

        if description:
            project_values["description"] = (
                description
            )

        project_result = await _odoo_json2_call(
            model="project.project",
            method="create",
            payload={
                "vals_list": [
                    project_values
                ],
            },
        )

        # Odoo JSON-2 create responses can be represented as a single
        # ID or a list depending on the exposed method/serialization.
        if isinstance(project_result, list):
            if not project_result:
                raise RuntimeError(
                    "Odoo did not return the created project ID."
                )

            first_project = project_result[0]

            if isinstance(first_project, dict):
                project_id = first_project.get("id")
            else:
                project_id = first_project

        elif isinstance(project_result, dict):
            project_id = project_result.get(
                "id"
            )
        else:
            project_id = project_result

        if not isinstance(project_id, int):
            raise RuntimeError(
                "Unable to determine the created project ID "
                f"from Odoo response: {project_result!r}"
            )

        project_alias = await _disable_project_email_alias(
            project_id
        )

        created_tasks = []

        for prepared_task in prepared_tasks:
            task_values: dict[str, Any] = {
                "name": prepared_task["name"],
                "project_id": project_id,
            }

            if prepared_task["description"]:
                task_values["description"] = (
                    prepared_task["description"]
                )

            if prepared_task["deadline"]:
                task_values["date_deadline"] = (
                    prepared_task["deadline"]
                )

            user_ids = [
                user["id"]
                for user in prepared_task["users"]
            ]

            if user_ids:
                task_values["user_ids"] = [
                    [
                        6,
                        0,
                        user_ids,
                    ]
                ]

            task_result = await _odoo_json2_call(
                model="project.task",
                method="create",
                payload={
                    "vals_list": [
                        task_values
                    ],
                },
            )

            if isinstance(task_result, list):
                first_task = (
                    task_result[0]
                    if task_result
                    else None
                )

                if isinstance(first_task, dict):
                    task_id = first_task.get(
                        "id"
                    )
                else:
                    task_id = first_task

            elif isinstance(task_result, dict):
                task_id = task_result.get(
                    "id"
                )
            else:
                task_id = task_result

            created_tasks.append(
                {
                    "id": task_id,
                    "name": prepared_task["name"],
                    "deadline": prepared_task[
                        "deadline"
                    ],
                    "assignees": [
                        {
                            "id": user["id"],
                            "name": user["name"],
                            "login": user["login"],
                        }
                        for user
                        in prepared_task["users"]
                    ],
                }
            )

        log_tool(
            tool,
            params,
            1 + len(created_tasks),
        )

        return {
            "success": True,
            "message": (
                f'Project "{project_name}" created successfully.'
            ),
            "project": {
                "id": project_id,
                "name": project_name,
                "description": description,
                "email_alias": project_alias,
            },
            "task_count": len(
                created_tasks
            ),
            "tasks": created_tasks,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Attendance / Timesheets
# ---------------------------------------------------------------------------

async def _resolve_employee_from_user(
    search: str,
):
    """
    Resolve one active res.users record and its linked active hr.employee.

    Attendances and Timesheets operate on employees, while MCP prompts are
    more natural when users are searched by name/login/email.
    """

    user = await _resolve_existing_user(
        search
    )

    employees = await odoo.search_read(
        model="hr.employee",
        domain=[
            [
                "user_id",
                "=",
                user["id"],
            ],
            [
                "active",
                "=",
                True,
            ],
        ],
        fields=[
            "id",
            "name",
            "user_id",
            "company_id",
        ],
        limit=2,
        order="name asc",
    )

    if not employees:
        raise ValueError(
            f'User "{user["name"]}" is not linked to an active '
            "hr.employee record."
        )

    if len(employees) > 1:
        raise ValueError(
            f'User "{user["name"]}" is linked to more than one active '
            "employee record. Resolve the employee mapping in Odoo first."
        )

    return {
        "user": user,
        "employee": employees[0],
    }


async def _resolve_existing_project(
    search: str,
):
    """
    Resolve one existing project by name.

    Exact case-insensitive matches are preferred. Ambiguous partial matches
    are rejected.
    """

    search = clean_search(search)

    if not search:
        raise ValueError(
            "Project name cannot be empty."
        )

    exact_projects = await odoo.search_read(
        model="project.project",
        domain=[
            [
                "name",
                "=ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
        ],
        limit=2,
        order="name asc",
    )

    if len(exact_projects) == 1:
        return exact_projects[0]

    if len(exact_projects) > 1:
        raise ValueError(
            f'More than one project exactly matches "{search}". '
            "Use a more specific project name."
        )

    partial_projects = await odoo.search_read(
        model="project.project",
        domain=[
            [
                "name",
                "ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
        ],
        limit=2,
        order="name asc",
    )

    if not partial_projects:
        raise ValueError(
            f'No project found for "{search}".'
        )

    if len(partial_projects) > 1:
        matches = ", ".join(
            project.get("name") or ""
            for project in partial_projects
        )

        raise ValueError(
            f'Project search "{search}" is ambiguous. '
            f"Matching projects include: {matches}. "
            "Use the exact project name."
        )

    return partial_projects[0]


async def _resolve_existing_task(
    search: str,
    project_id: int | None = None,
):
    """
    Resolve one existing active project task by name.

    When project_id is supplied, the task search is restricted to that
    project.
    """

    search = clean_search(search)

    if not search:
        raise ValueError(
            "Task name cannot be empty."
        )

    base_domain: list[Any] = [
        [
            "active",
            "=",
            True,
        ],
    ]

    if project_id:
        base_domain.append(
            [
                "project_id",
                "=",
                project_id,
            ]
        )

    exact_domain = list(
        base_domain
    )
    exact_domain.append(
        [
            "name",
            "=ilike",
            search,
        ]
    )

    exact_tasks = await odoo.search_read(
        model="project.task",
        domain=exact_domain,
        fields=[
            "id",
            "name",
            "project_id",
            "user_ids",
            "stage_id",
        ],
        limit=2,
        order="name asc",
    )

    if len(exact_tasks) == 1:
        return exact_tasks[0]

    if len(exact_tasks) > 1:
        raise ValueError(
            f'More than one task exactly matches "{search}". '
            "Provide a more specific task/project."
        )

    partial_domain = list(
        base_domain
    )
    partial_domain.append(
        [
            "name",
            "ilike",
            search,
        ]
    )

    partial_tasks = await odoo.search_read(
        model="project.task",
        domain=partial_domain,
        fields=[
            "id",
            "name",
            "project_id",
            "user_ids",
            "stage_id",
        ],
        limit=2,
        order="name asc",
    )

    if not partial_tasks:
        raise ValueError(
            f'No task found for "{search}".'
        )

    if len(partial_tasks) > 1:
        matches = ", ".join(
            task.get("name") or ""
            for task in partial_tasks
        )

        raise ValueError(
            f'Task search "{search}" is ambiguous. '
            f"Matching tasks include: {matches}. "
            "Provide the exact task and project name."
        )

    return partial_tasks[0]


@mcp.tool()
async def attendance_time_in(
    user: str,
):
    """
    Check an existing employee in using Odoo Attendances.

    Write operation.

    Parameters:
    - user: existing Odoo user name or login/email

    This creates an hr.attendance record with check_in set to the current
    UTC time. It refuses to create another open attendance when the employee
    is already checked in.
    """

    tool = "attendance_time_in"

    params = {
        "user": clean_search(user),
    }

    try:
        if not params["user"]:
            raise ValueError(
                "Provide a user name or login/email."
            )

        employee_data = await _resolve_employee_from_user(
            params["user"]
        )

        employee = employee_data[
            "employee"
        ]

        open_attendances = await odoo.search_read(
            model="hr.attendance",
            domain=[
                [
                    "employee_id",
                    "=",
                    employee["id"],
                ],
                [
                    "check_out",
                    "=",
                    False,
                ],
            ],
            fields=[
                "id",
                "employee_id",
                "check_in",
                "check_out",
            ],
            limit=1,
            order="check_in desc, id desc",
        )

        if open_attendances:
            attendance = open_attendances[0]

            raise ValueError(
                f'{employee["name"]} is already checked in '
                f'since {attendance.get("check_in")}.'
            )

        check_in = datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        attendance_result = await _odoo_json2_call(
            model="hr.attendance",
            method="create",
            payload={
                "vals_list": [
                    {
                        "employee_id": employee["id"],
                        "check_in": check_in,
                    }
                ],
            },
        )

        if isinstance(attendance_result, list):
            first_attendance = (
                attendance_result[0]
                if attendance_result
                else None
            )

            if isinstance(first_attendance, dict):
                attendance_id = first_attendance.get(
                    "id"
                )
            else:
                attendance_id = first_attendance

        elif isinstance(attendance_result, dict):
            attendance_id = attendance_result.get(
                "id"
            )
        else:
            attendance_id = attendance_result

        if not isinstance(attendance_id, int):
            raise RuntimeError(
                "Unable to determine the created attendance ID "
                f"from Odoo response: {attendance_result!r}"
            )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "message": (
                f'{employee["name"]} checked in successfully.'
            ),
            "attendance": {
                "id": attendance_id,
                "employee": employee,
                "check_in": check_in,
                "check_out": None,
            },
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def attendance_time_out(
    user: str,
):
    """
    Check an existing employee out using Odoo Attendances.

    Write operation.

    Parameters:
    - user: existing Odoo user name or login/email

    The tool finds the employee's latest open hr.attendance record and writes
    the current UTC time to check_out.
    """

    tool = "attendance_time_out"

    params = {
        "user": clean_search(user),
    }

    try:
        if not params["user"]:
            raise ValueError(
                "Provide a user name or login/email."
            )

        employee_data = await _resolve_employee_from_user(
            params["user"]
        )

        employee = employee_data[
            "employee"
        ]

        open_attendances = await odoo.search_read(
            model="hr.attendance",
            domain=[
                [
                    "employee_id",
                    "=",
                    employee["id"],
                ],
                [
                    "check_out",
                    "=",
                    False,
                ],
            ],
            fields=[
                "id",
                "employee_id",
                "check_in",
                "check_out",
            ],
            limit=1,
            order="check_in desc, id desc",
        )

        if not open_attendances:
            raise ValueError(
                f'{employee["name"]} has no open attendance record '
                "to check out."
            )

        attendance = open_attendances[0]

        check_out = datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        await _odoo_json2_call(
            model="hr.attendance",
            method="write",
            payload={
                "ids": [
                    attendance["id"]
                ],
                "vals": {
                    "check_out": check_out,
                },
            },
        )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "message": (
                f'{employee["name"]} checked out successfully.'
            ),
            "attendance": {
                "id": attendance["id"],
                "employee": employee,
                "check_in": attendance.get(
                    "check_in"
                ),
                "check_out": check_out,
            },
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def create_project_task(
    project: str,
    task_name: str,
    description: str = "",
    assignees: list[str] | None = None,
    deadline: str = "",
):
    """
    Create a task under an existing Odoo project.

    Write operation.

    Parameters:
    - project: existing project name
    - task_name: required new task title
    - description: optional task description
    - assignees: optional existing Odoo user names or login/emails
    - deadline: optional YYYY-MM-DD deadline
    """

    tool = "create_project_task"

    project = clean_search(project)
    task_name = clean_search(task_name)
    description = (
        description.strip()
        if isinstance(description, str)
        else ""
    )
    assignees = assignees or []
    deadline = str(
        deadline
    ).strip()

    params = {
        "project": project,
        "task_name": task_name,
        "description": description,
        "assignees": assignees,
        "deadline": deadline,
    }

    try:
        if not project:
            raise ValueError(
                "Provide an existing project name."
            )

        if not task_name:
            raise ValueError(
                "Provide a task name."
            )

        if isinstance(assignees, str):
            assignees = [
                assignees
            ]

        if not isinstance(assignees, list):
            raise ValueError(
                "assignees must be a list."
            )

        resolved_project = await _resolve_existing_project(
            project
        )

        resolved_users = []

        for assignee in assignees:
            user = await _resolve_existing_user(
                str(assignee)
            )

            if user["id"] not in [
                existing["id"]
                for existing in resolved_users
            ]:
                resolved_users.append(
                    user
                )

        task_values: dict[str, Any] = {
            "name": task_name,
            "project_id": resolved_project["id"],
        }

        if description:
            task_values["description"] = (
                description
            )

        if deadline:
            task_values["date_deadline"] = (
                deadline
            )

        user_ids = [
            user["id"]
            for user in resolved_users
        ]

        if user_ids:
            task_values["user_ids"] = [
                [
                    6,
                    0,
                    user_ids,
                ]
            ]

        task_result = await _odoo_json2_call(
            model="project.task",
            method="create",
            payload={
                "vals_list": [
                    task_values
                ],
            },
        )

        if isinstance(task_result, list):
            first_task = (
                task_result[0]
                if task_result
                else None
            )

            if isinstance(first_task, dict):
                task_id = first_task.get(
                    "id"
                )
            else:
                task_id = first_task

        elif isinstance(task_result, dict):
            task_id = task_result.get(
                "id"
            )
        else:
            task_id = task_result

        if not isinstance(task_id, int):
            raise RuntimeError(
                "Unable to determine the created task ID "
                f"from Odoo response: {task_result!r}"
            )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "message": (
                f'Task "{task_name}" created under '
                f'project "{resolved_project["name"]}".'
            ),
            "task": {
                "id": task_id,
                "name": task_name,
                "project": resolved_project,
                "description": description,
                "deadline": (
                    deadline
                    if deadline
                    else None
                ),
                "assignees": resolved_users,
            },
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def create_timesheet_entry(
    user: str,
    project: str,
    description: str,
    hours: float,
    task: str = "",
    date: str = "",
):
    """
    Create a manual Odoo Timesheets entry.

    Write operation.

    Parameters:
    - user: existing Odoo user name or login/email
    - project: existing project name
    - description: required description of work performed
    - hours: time spent in hours, greater than 0
    - task: optional existing task name within the project
    - date: optional YYYY-MM-DD date; when omitted Odoo uses its default

    Notes:
    - The Odoo user must be linked to an employee.
    - The project must permit timesheets in Odoo.
    - When a task is provided, it must belong to the selected project.
    """

    tool = "create_timesheet_entry"

    params = {
        "user": clean_search(user),
        "project": clean_search(project),
        "description": (
            description.strip()
            if isinstance(description, str)
            else ""
        ),
        "hours": hours,
        "task": clean_search(task),
        "date": str(
            date
        ).strip(),
    }

    try:
        if not params["user"]:
            raise ValueError(
                "Provide a user name or login/email."
            )

        if not params["project"]:
            raise ValueError(
                "Provide an existing project name."
            )

        if not params["description"]:
            raise ValueError(
                "Provide a timesheet description."
            )

        try:
            unit_amount = float(
                params["hours"]
            )
        except (
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                "hours must be a valid number."
            ) from exc

        if unit_amount <= 0:
            raise ValueError(
                "hours must be greater than 0."
            )

        employee_data = await _resolve_employee_from_user(
            params["user"]
        )

        resolved_project = await _resolve_existing_project(
            params["project"]
        )

        resolved_task = None

        if params["task"]:
            resolved_task = await _resolve_existing_task(
                params["task"],
                resolved_project["id"],
            )

            task_project = resolved_task.get(
                "project_id"
            )

            if (
                task_project
                and task_project[0]
                != resolved_project["id"]
            ):
                raise ValueError(
                    "The selected task does not belong to the "
                    "selected project."
                )

        timesheet_values: dict[str, Any] = {
            "name": params["description"],
            "employee_id": employee_data[
                "employee"
            ]["id"],
            "project_id": resolved_project["id"],
            "unit_amount": unit_amount,
        }

        if resolved_task:
            timesheet_values["task_id"] = (
                resolved_task["id"]
            )

        if params["date"]:
            timesheet_values["date"] = (
                params["date"]
            )

        timesheet_result = await _odoo_json2_call(
            model="account.analytic.line",
            method="create",
            payload={
                "vals_list": [
                    timesheet_values
                ],
            },
        )

        if isinstance(timesheet_result, list):
            first_timesheet = (
                timesheet_result[0]
                if timesheet_result
                else None
            )

            if isinstance(first_timesheet, dict):
                timesheet_id = first_timesheet.get(
                    "id"
                )
            else:
                timesheet_id = first_timesheet

        elif isinstance(timesheet_result, dict):
            timesheet_id = timesheet_result.get(
                "id"
            )
        else:
            timesheet_id = timesheet_result

        if not isinstance(timesheet_id, int):
            raise RuntimeError(
                "Unable to determine the created timesheet ID "
                f"from Odoo response: {timesheet_result!r}"
            )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "message": (
                f'{unit_amount:g} hour(s) logged for '
                f'{employee_data["employee"]["name"]}.'
            ),
            "timesheet": {
                "id": timesheet_id,
                "employee": employee_data[
                    "employee"
                ],
                "project": resolved_project,
                "task": resolved_task,
                "description": params[
                    "description"
                ],
                "hours": unit_amount,
                "date": (
                    params["date"]
                    if params["date"]
                    else None
                ),
            },
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# SOD / EOD Shift Processing
# ---------------------------------------------------------------------------

def _parse_shift_update_message(
    message: str,
):
    """
    Parse a strict SOD/EOD message.

    Rules:
    - The first non-empty line must be exactly SOD or EOD.
    - Remaining non-empty lines are treated as task/accomplishment items.
    - Bullet prefixes "-", "*", and "•" are removed.
    - EOD/SOD items may optionally include hours using:
        Task Name | 2h
        Task Name - 2h
        [2h] Task Name

    Hours are never invented. If an EOD line has no explicit duration,
    its hours value is None and no Timesheet is automatically created
    for that item.
    """

    if not isinstance(message, str):
        raise ValueError(
            "message must be a string."
        )

    lines = [
        line.strip()
        for line in message.splitlines()
        if line.strip()
    ]

    if not lines:
        raise ValueError(
            "Shift message cannot be empty."
        )

    keyword = lines[0].upper()

    if keyword not in {
        "SOD",
        "EOD",
    }:
        raise ValueError(
            "The first non-empty line must be exactly SOD or EOD."
        )

    items = []

    for raw_line in lines[1:]:
        line = re.sub(
            r"^\s*[-*•]\s*",
            "",
            raw_line,
        ).strip()

        if not line:
            continue

        task_name = line
        hours = None

        # Format: [2h] Task Name
        bracket_match = re.match(
            r"^\[\s*(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*\]\s*(.+)$",
            line,
            flags=re.IGNORECASE,
        )

        if bracket_match:
            hours = float(
                bracket_match.group(1)
            )
            task_name = bracket_match.group(2).strip()

        else:
            # Preferred format: Task Name | 2h
            pipe_match = re.match(
                r"^(.+?)\s*\|\s*(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*$",
                line,
                flags=re.IGNORECASE,
            )

            if pipe_match:
                task_name = pipe_match.group(1).strip()
                hours = float(
                    pipe_match.group(2)
                )

            else:
                # Also support: Task Name - 2h
                dash_match = re.match(
                    r"^(.+?)\s+-\s+(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*$",
                    line,
                    flags=re.IGNORECASE,
                )

                if dash_match:
                    task_name = dash_match.group(1).strip()
                    hours = float(
                        dash_match.group(2)
                    )

        task_name = clean_search(
            task_name
        )

        if not task_name:
            continue

        if hours is not None and hours <= 0:
            raise ValueError(
                f'Hours for "{task_name}" must be greater than 0.'
            )

        items.append(
            {
                "task": task_name,
                "hours": hours,
                "raw": raw_line,
            }
        )

    return {
        "keyword": keyword,
        "items": items,
    }


async def _get_or_create_shift_task(
    project: dict[str, Any],
    task_name: str,
    user: dict[str, Any],
):
    """
    Find a task by exact name inside one project, or create it if missing.

    This creates only project.task records. It does not create a project and
    therefore does not create a project email alias.
    """

    existing_tasks = await odoo.search_read(
        model="project.task",
        domain=[
            [
                "project_id",
                "=",
                project["id"],
            ],
            [
                "name",
                "=ilike",
                task_name,
            ],
            [
                "active",
                "=",
                True,
            ],
        ],
        fields=[
            "id",
            "name",
            "project_id",
            "user_ids",
            "stage_id",
        ],
        limit=2,
        order="id desc",
    )

    if len(existing_tasks) > 1:
        raise ValueError(
            f'More than one active task named "{task_name}" exists '
            f'in project "{project["name"]}".'
        )

    if existing_tasks:
        task = existing_tasks[0]

        current_user_ids = task.get(
            "user_ids"
        ) or []

        if user["id"] not in current_user_ids:
            new_user_ids = list(
                dict.fromkeys(
                    current_user_ids
                    + [
                        user["id"]
                    ]
                )
            )

            await _odoo_json2_call(
                model="project.task",
                method="write",
                payload={
                    "ids": [
                        task["id"]
                    ],
                    "vals": {
                        "user_ids": [
                            [
                                6,
                                0,
                                new_user_ids,
                            ]
                        ],
                    },
                },
            )

            task["user_ids"] = new_user_ids

        return {
            "created": False,
            "task": task,
        }

    task_result = await _odoo_json2_call(
        model="project.task",
        method="create",
        payload={
            "vals_list": [
                {
                    "name": task_name,
                    "project_id": project["id"],
                    "user_ids": [
                        [
                            6,
                            0,
                            [
                                user["id"],
                            ],
                        ]
                    ],
                }
            ],
        },
    )

    if isinstance(task_result, list):
        first_task = (
            task_result[0]
            if task_result
            else None
        )

        if isinstance(first_task, dict):
            task_id = first_task.get(
                "id"
            )
        else:
            task_id = first_task

    elif isinstance(task_result, dict):
        task_id = task_result.get(
            "id"
        )

    else:
        task_id = task_result

    if not isinstance(task_id, int):
        raise RuntimeError(
            "Unable to determine the created task ID "
            f"from Odoo response: {task_result!r}"
        )

    return {
        "created": True,
        "task": {
            "id": task_id,
            "name": task_name,
            "project_id": [
                project["id"],
                project["name"],
            ],
            "user_ids": [
                user["id"],
            ],
        },
    }


async def _shift_time_in(
    employee: dict[str, Any],
):
    """
    Internal deterministic Time In used by process_shift_update.
    """

    open_attendances = await odoo.search_read(
        model="hr.attendance",
        domain=[
            [
                "employee_id",
                "=",
                employee["id"],
            ],
            [
                "check_out",
                "=",
                False,
            ],
        ],
        fields=[
            "id",
            "employee_id",
            "check_in",
            "check_out",
        ],
        limit=1,
        order="check_in desc, id desc",
    )

    if open_attendances:
        return {
            "created": False,
            "already_checked_in": True,
            "attendance": open_attendances[0],
        }

    check_in = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    attendance_result = await _odoo_json2_call(
        model="hr.attendance",
        method="create",
        payload={
            "vals_list": [
                {
                    "employee_id": employee["id"],
                    "check_in": check_in,
                }
            ],
        },
    )

    if isinstance(attendance_result, list):
        first_attendance = (
            attendance_result[0]
            if attendance_result
            else None
        )

        if isinstance(first_attendance, dict):
            attendance_id = first_attendance.get(
                "id"
            )
        else:
            attendance_id = first_attendance

    elif isinstance(attendance_result, dict):
        attendance_id = attendance_result.get(
            "id"
        )

    else:
        attendance_id = attendance_result

    if not isinstance(attendance_id, int):
        raise RuntimeError(
            "Unable to determine the created attendance ID "
            f"from Odoo response: {attendance_result!r}"
        )

    return {
        "created": True,
        "already_checked_in": False,
        "attendance": {
            "id": attendance_id,
            "employee_id": [
                employee["id"],
                employee["name"],
            ],
            "check_in": check_in,
            "check_out": None,
        },
    }


async def _shift_time_out(
    employee: dict[str, Any],
):
    """
    Internal deterministic Time Out used by process_shift_update.
    """

    open_attendances = await odoo.search_read(
        model="hr.attendance",
        domain=[
            [
                "employee_id",
                "=",
                employee["id"],
            ],
            [
                "check_out",
                "=",
                False,
            ],
        ],
        fields=[
            "id",
            "employee_id",
            "check_in",
            "check_out",
        ],
        limit=1,
        order="check_in desc, id desc",
    )

    if not open_attendances:
        return {
            "updated": False,
            "no_open_attendance": True,
            "attendance": None,
        }

    attendance = open_attendances[0]

    check_out = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    await _odoo_json2_call(
        model="hr.attendance",
        method="write",
        payload={
            "ids": [
                attendance["id"]
            ],
            "vals": {
                "check_out": check_out,
            },
        },
    )

    return {
        "updated": True,
        "no_open_attendance": False,
        "attendance": {
            "id": attendance["id"],
            "employee_id": attendance.get(
                "employee_id"
            ),
            "check_in": attendance.get(
                "check_in"
            ),
            "check_out": check_out,
        },
    }


async def _create_shift_timesheet(
    employee: dict[str, Any],
    project: dict[str, Any],
    task: dict[str, Any],
    hours: float,
    description: str,
):
    """
    Create one Timesheet entry for an EOD item with explicit hours.
    """

    values = {
        "name": description,
        "employee_id": employee["id"],
        "project_id": project["id"],
        "task_id": task["id"],
        "unit_amount": hours,
    }

    result = await _odoo_json2_call(
        model="account.analytic.line",
        method="create",
        payload={
            "vals_list": [
                values
            ],
        },
    )

    if isinstance(result, list):
        first_row = (
            result[0]
            if result
            else None
        )

        if isinstance(first_row, dict):
            timesheet_id = first_row.get(
                "id"
            )
        else:
            timesheet_id = first_row

    elif isinstance(result, dict):
        timesheet_id = result.get(
            "id"
        )

    else:
        timesheet_id = result

    if not isinstance(timesheet_id, int):
        raise RuntimeError(
            "Unable to determine the created timesheet ID "
            f"from Odoo response: {result!r}"
        )

    return {
        "id": timesheet_id,
        "task_id": task["id"],
        "task_name": task["name"],
        "hours": hours,
        "description": description,
    }


@mcp.tool()
async def process_shift_update(
    user: str,
    project: str,
    message: str,
):
    """
    Process one employee SOD/EOD message.

    Write operation.

    Required format:

    SOD
    - Task 1
    - Task 2

    or:

    EOD
    - Task 1 | 2h
    - Task 2 | 3.5h

    Behavior:

    SOD
    - Resolves the existing Odoo user and employee.
    - Resolves an EXISTING project.
    - Times the employee in.
    - Finds each task by exact name inside the project.
    - Creates missing project.task records and assigns the user.
    - Does NOT create a project, so no project alias is created here.

    EOD
    - Resolves the same employee/project.
    - Finds or creates the listed project tasks.
    - Creates Timesheet entries only for items that explicitly include hours.
    - Never guesses or automatically divides hours.
    - Times the employee out after processing the listed items.

    Alias safety:
    - This tool never creates project.project records.
    - It therefore does not trigger creation of a new project mail alias.
    - If a project must be created separately, use create_project(), which
      already disables the automatically-created project alias.
    """

    tool = "process_shift_update"

    params = {
        "user": clean_search(user),
        "project": clean_search(project),
        "message": (
            message.strip()
            if isinstance(message, str)
            else ""
        ),
    }

    try:
        if not params["user"]:
            raise ValueError(
                "Provide a user name or login/email."
            )

        if not params["project"]:
            raise ValueError(
                "Provide an existing project name."
            )

        parsed = _parse_shift_update_message(
            params["message"]
        )

        employee_data = await _resolve_employee_from_user(
            params["user"]
        )

        resolved_project = await _resolve_existing_project(
            params["project"]
        )

        user_record = employee_data[
            "user"
        ]

        employee = employee_data[
            "employee"
        ]

        processed_tasks = []
        timesheets = []
        missing_hours = []

        if parsed["keyword"] == "SOD":
            attendance = await _shift_time_in(
                employee
            )

            for item in parsed["items"]:
                task_result = await _get_or_create_shift_task(
                    resolved_project,
                    item["task"],
                    user_record,
                )

                processed_tasks.append(
                    {
                        "name": item["task"],
                        "created": task_result[
                            "created"
                        ],
                        "task": task_result[
                            "task"
                        ],
                    }
                )

            result = {
                "success": True,
                "shift_type": "SOD",
                "message": (
                    f'SOD processed for {employee["name"]}.'
                ),
                "employee": employee,
                "project": resolved_project,
                "attendance": attendance,
                "tasks": processed_tasks,
                "timesheets": [],
                "alias_created": False,
            }

        else:
            for item in parsed["items"]:
                task_result = await _get_or_create_shift_task(
                    resolved_project,
                    item["task"],
                    user_record,
                )

                task = task_result[
                    "task"
                ]

                processed_tasks.append(
                    {
                        "name": item["task"],
                        "created": task_result[
                            "created"
                        ],
                        "task": task,
                        "hours": item[
                            "hours"
                        ],
                    }
                )

                if item["hours"] is None:
                    missing_hours.append(
                        item["task"]
                    )
                    continue

                timesheet = await _create_shift_timesheet(
                    employee,
                    resolved_project,
                    task,
                    item["hours"],
                    f'EOD - {item["task"]}',
                )

                timesheets.append(
                    timesheet
                )

            attendance = await _shift_time_out(
                employee
            )

            result = {
                "success": True,
                "shift_type": "EOD",
                "message": (
                    f'EOD processed for {employee["name"]}.'
                ),
                "employee": employee,
                "project": resolved_project,
                "attendance": attendance,
                "tasks": processed_tasks,
                "timesheets": timesheets,
                "missing_timesheet_hours": missing_hours,
                "alias_created": False,
            }

            if missing_hours:
                result["warning"] = (
                    "Some EOD items had no explicit hours, so no Timesheet "
                    "entry was created for those items. Use a format like "
                    "'Task Name | 2h'."
                )

        log_tool(
            tool,
            params,
            (
                1
                + len(processed_tasks)
                + len(timesheets)
            ),
        )

        return result

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Helpdesk
# ---------------------------------------------------------------------------

async def _resolve_helpdesk_team(
    search: str,
):
    """
    Resolve one existing Odoo Helpdesk team by name.

    Exact case-insensitive matches are preferred. If no exact match
    exists, a partial search is attempted. Ambiguous matches are rejected
    so a ticket is not created under the wrong Helpdesk team.
    """

    search = clean_search(search)

    if not search:
        raise ValueError(
            "Provide a Helpdesk team name."
        )

    exact_teams = await odoo.search_read(
        model="helpdesk.team",
        domain=[
            [
                "name",
                "=ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
        ],
        limit=2,
        order="name asc",
    )

    if len(exact_teams) == 1:
        return exact_teams[0]

    if len(exact_teams) > 1:
        raise ValueError(
            f'More than one Helpdesk team exactly matches "{search}". '
            "Use a more specific team name."
        )

    partial_teams = await odoo.search_read(
        model="helpdesk.team",
        domain=[
            [
                "name",
                "ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
        ],
        limit=2,
        order="name asc",
    )

    if not partial_teams:
        raise ValueError(
            f'No Helpdesk team found for "{search}".'
        )

    if len(partial_teams) > 1:
        matches = ", ".join(
            team.get("name") or ""
            for team in partial_teams
        )

        raise ValueError(
            f'Helpdesk team search "{search}" is ambiguous. '
            f"Matching teams include: {matches}. "
            "Use the exact team name."
        )

    return partial_teams[0]


@mcp.tool()
async def search_helpdesk_teams(
    search: str = "",
    limit: int = 20,
):
    """
    Search existing Odoo Helpdesk teams.

    Read-only.

    Parameters:
    - search: optional partial Helpdesk team name
    - limit: maximum number of teams to return
    """

    tool = "search_helpdesk_teams"

    params = {
        "search": clean_search(search),
        "limit": limit,
    }

    try:
        domain: list[Any] = []

        if params["search"]:
            domain.append(
                [
                    "name",
                    "ilike",
                    params["search"],
                ]
            )

        rows = await odoo.search_read(
            model="helpdesk.team",
            domain=domain,
            fields=[
                "id",
                "name",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="name asc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "teams": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


async def _resolve_existing_customer(
    search: str,
):
    """
    Resolve one existing Odoo customer/contact by name or email.

    Exact case-insensitive matches are preferred. If no exact match exists,
    a partial search is attempted. Ambiguous matches are rejected so the
    ticket is not linked to the wrong customer.
    """

    search = clean_search(search)

    if not search:
        raise ValueError(
            "Customer name or email cannot be empty."
        )

    exact_customers = await odoo.search_read(
        model="res.partner",
        domain=[
            "|",
            [
                "name",
                "=ilike",
                search,
            ],
            [
                "email",
                "=ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
            "email",
            "phone",
            "is_company",
            "company_id",
        ],
        limit=2,
        order="name asc",
    )

    if len(exact_customers) == 1:
        return exact_customers[0]

    if len(exact_customers) > 1:
        raise ValueError(
            f'More than one customer exactly matches "{search}". '
            "Use the customer's exact email address."
        )

    partial_customers = await odoo.search_read(
        model="res.partner",
        domain=[
            "|",
            [
                "name",
                "ilike",
                search,
            ],
            [
                "email",
                "ilike",
                search,
            ],
        ],
        fields=[
            "id",
            "name",
            "email",
            "phone",
            "is_company",
            "company_id",
        ],
        limit=2,
        order="name asc",
    )

    if not partial_customers:
        raise ValueError(
            f'No existing customer/contact found for "{search}".'
        )

    if len(partial_customers) > 1:
        matches = ", ".join(
            f'{customer.get("name")} ({customer.get("email") or "no email"})'
            for customer in partial_customers
        )

        raise ValueError(
            f'Customer search "{search}" is ambiguous. '
            f"Matching customers include: {matches}. "
            "Use the exact customer name or email."
        )

    return partial_customers[0]


@mcp.tool()
async def create_helpdesk_ticket(
    title: str,
    team: str,
    description: str = "",
    customer: str = "",
    priority: str = "0",
):
    """
    Create an Odoo Helpdesk ticket.

    Write operation.

    Parameters:
    - title: required ticket title
    - team: required existing Helpdesk team name
    - description: optional ticket description
    - customer: optional existing Odoo customer/contact name or email.
      This is written to helpdesk.ticket.partner_id (Many2one -> res.partner).
    - priority:
        "0" = Low
        "1" = Medium
        "2" = High
        "3" = Urgent

    Important:
    - The Helpdesk team must already exist.
    - The customer/contact must already exist in res.partner.
    - Ambiguous team/customer searches are rejected before ticket creation.
    - Assignee and tags are intentionally not handled by this tool for now.
    """

    tool = "create_helpdesk_ticket"

    title = clean_search(title)
    team = clean_search(team)
    description = (
        description.strip()
        if isinstance(description, str)
        else ""
    )
    customer = clean_search(customer)
    priority = str(priority).strip()

    params = {
        "title": title,
        "team": team,
        "description": description,
        "customer": customer,
        "priority": priority,
    }

    try:
        if not title:
            raise ValueError(
                "Provide a ticket title."
            )

        if not team:
            raise ValueError(
                "Provide a Helpdesk team name."
            )

        if priority not in {
            "0",
            "1",
            "2",
            "3",
        }:
            raise ValueError(
                "priority must be 0, 1, 2, or 3."
            )

        helpdesk_team = await _resolve_helpdesk_team(
            team
        )

        resolved_customer = None

        if customer:
            resolved_customer = await _resolve_existing_customer(
                customer
            )

        ticket_values: dict[str, Any] = {
            "name": title,
            "team_id": helpdesk_team["id"],
            "priority": priority,
        }

        if description:
            ticket_values["description"] = (
                description
            )

        if resolved_customer:
            # Confirmed from this Odoo database:
            # helpdesk.ticket.partner_id
            # Type: many2one
            # Relation: res.partner
            ticket_values["partner_id"] = (
                resolved_customer["id"]
            )

        ticket_result = await _odoo_json2_call(
            model="helpdesk.ticket",
            method="create",
            payload={
                "vals_list": [
                    ticket_values
                ],
            },
        )

        if isinstance(ticket_result, list):
            if not ticket_result:
                raise RuntimeError(
                    "Odoo did not return the created ticket ID."
                )

            first_ticket = ticket_result[0]

            if isinstance(first_ticket, dict):
                ticket_id = first_ticket.get(
                    "id"
                )
            else:
                ticket_id = first_ticket

        elif isinstance(ticket_result, dict):
            ticket_id = ticket_result.get(
                "id"
            )
        else:
            ticket_id = ticket_result

        if not isinstance(ticket_id, int):
            raise RuntimeError(
                "Unable to determine the created Helpdesk ticket ID "
                f"from Odoo response: {ticket_result!r}"
            )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "message": (
                f'Helpdesk ticket "{title}" created successfully.'
            ),
            "ticket": {
                "id": ticket_id,
                "title": title,
                "team": helpdesk_team,
                "description": description,
                "priority": priority,
                "customer": resolved_customer,
            },
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Helpdesk ticket search / evaluation
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_helpdesk_tickets(
    search: str = "",
    team: str = "",
    limit: int = 20,
):
    """
    Search Odoo Helpdesk tickets before evaluating a specific ticket.

    Read-only.

    Parameters:
    - search: optional partial ticket title
    - team: optional existing Helpdesk team name
    - limit: maximum number of tickets to return
    """

    tool = "search_helpdesk_tickets"

    params = {
        "search": clean_search(search),
        "team": clean_search(team),
        "limit": limit,
    }

    try:
        domain: list[Any] = []

        if params["search"]:
            domain.append(
                [
                    "name",
                    "ilike",
                    params["search"],
                ]
            )

        resolved_team = None

        if params["team"]:
            resolved_team = await _resolve_helpdesk_team(
                params["team"]
            )

            domain.append(
                [
                    "team_id",
                    "=",
                    resolved_team["id"],
                ]
            )

        rows = await odoo.search_read(
            model="helpdesk.ticket",
            domain=domain,
            fields=[
                "id",
                "name",
                "team_id",
                "user_id",
                "stage_id",
                "priority",
                "partner_id",
                "create_date",
                "write_date",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="create_date desc, id desc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "team": resolved_team,
            "tickets": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def evaluate_helpdesk_ticket(
    ticket_id: int,
):
    """
    Evaluate one existing Odoo Helpdesk ticket for basic support triage.

    Read-only. This tool does not change the ticket.

    The evaluation is intentionally deterministic. It returns the ticket
    facts plus simple operational observations that Claude can explain to
    the user.

    It checks:
    - ticket priority
    - current stage
    - whether someone is assigned
    - whether a description is present
    - whether a customer/contact is linked
    - whether the ticket should receive additional attention

    This is a triage aid, not a replacement for the support team's own
    business rules, SLA policy, or technical investigation.
    """

    tool = "evaluate_helpdesk_ticket"

    params = {
        "ticket_id": ticket_id,
    }

    try:
        positive_id(
            ticket_id,
            "ticket_id",
        )

        rows = await odoo.read(
            model="helpdesk.ticket",
            record_ids=[
                ticket_id,
            ],
            fields=[
                "id",
                "name",
                "description",
                "team_id",
                "user_id",
                "stage_id",
                "priority",
                "partner_id",
                "create_date",
                "write_date",
            ],
        )

        if not rows:
            raise ValueError(
                "Helpdesk ticket not found or access denied."
            )

        ticket = rows[0]

        priority_value = str(
            ticket.get("priority") or "0"
        )

        priority_labels = {
            "0": "Low",
            "1": "Medium",
            "2": "High",
            "3": "Urgent",
        }

        priority_label = priority_labels.get(
            priority_value,
            "Unknown",
        )

        assigned_user = ticket.get(
            "user_id"
        )

        assigned = bool(
            assigned_user
        )

        description = ticket.get(
            "description"
        ) or ""

        has_description = bool(
            str(description).strip()
        )

        partner = ticket.get(
            "partner_id"
        )

        has_customer = bool(
            partner
        )

        attention_reasons = []
        recommendations = []

        if priority_value == "3":
            attention_reasons.append(
                "Ticket is marked Urgent."
            )
            recommendations.append(
                "Treat this ticket as immediate support work and "
                "confirm ownership."
            )

        elif priority_value == "2":
            attention_reasons.append(
                "Ticket is marked High priority."
            )
            recommendations.append(
                "Review this ticket ahead of normal-priority work."
            )

        if not assigned:
            attention_reasons.append(
                "No Helpdesk user is currently assigned."
            )
            recommendations.append(
                "Assign the ticket to an appropriate Helpdesk user "
                "or allow the team's automatic assignment rules to handle it."
            )

        if not has_description:
            attention_reasons.append(
                "The ticket has no description."
            )
            recommendations.append(
                "Add enough issue details, reproduction information, "
                "and expected behavior for the support team to investigate."
            )

        if not has_customer:
            recommendations.append(
                "Consider linking a customer/contact if this is a "
                "customer-facing support request."
            )

        if not attention_reasons:
            attention_level = "Normal"
        elif priority_value == "3":
            attention_level = "Urgent"
        elif priority_value == "2":
            attention_level = "High"
        else:
            attention_level = "Needs Review"

        evaluation = {
            "attention_level": attention_level,
            "priority": {
                "value": priority_value,
                "label": priority_label,
            },
            "assigned": assigned,
            "assigned_to": assigned_user,
            "has_description": has_description,
            "has_customer": has_customer,
            "attention_reasons": attention_reasons,
            "recommendations": recommendations,
        }

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "ticket": {
                "id": ticket.get("id"),
                "name": ticket.get("name"),
                "team_id": ticket.get("team_id"),
                "user_id": assigned_user,
                "stage_id": ticket.get("stage_id"),
                "priority": ticket.get("priority"),
                "partner_id": ticket.get("partner_id"),
                "description": description,
                "create_date": ticket.get("create_date"),
                "write_date": ticket.get("write_date"),
            },
            "evaluation": evaluation,
            "note": (
                "This evaluation is based on ticket metadata and basic "
                "support-triage rules. Claude should use the returned "
                "ticket description and context when explaining the issue."
            ),
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_crm_opportunities(
    search: str = "",
    limit: int = 20,
):
    """
    Search active CRM opportunities.

    Read-only.
    """

    tool = "search_crm_opportunities"

    params = {
        "search": clean_search(search),
        "limit": limit,
    }

    try:
        domain = [
            ["type", "=", "opportunity"],
            ["active", "=", True],
        ]

        if params["search"]:
            domain.append(
                [
                    "name",
                    "ilike",
                    params["search"],
                ]
            )

        rows = await odoo.search_read(
            model="crm.lead",
            domain=domain,
            fields=[
                "id",
                "name",
                "partner_id",
                "user_id",
                "team_id",
                "stage_id",
                "expected_revenue",
                "probability",
                "date_deadline",
                "priority",
                "create_date",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="create_date desc, id desc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "opportunities": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def get_crm_opportunity(
    opportunity_id: int,
):
    """
    Get one CRM opportunity by ID.

    Read-only.
    """

    tool = "get_crm_opportunity"

    params = {
        "opportunity_id": opportunity_id,
    }

    try:
        positive_id(
            opportunity_id,
            "opportunity_id",
        )

        rows = await odoo.read(
            model="crm.lead",
            record_ids=[
                opportunity_id,
            ],
            fields=[
                "id",
                "name",
                "partner_id",
                "contact_name",
                "email_from",
                "phone",
                "user_id",
                "team_id",
                "stage_id",
                "expected_revenue",
                "probability",
                "date_deadline",
                "description",
                "create_date",
                "write_date",
            ],
        )

        if not rows:
            raise ValueError(
                "Opportunity not found or access denied."
            )

        log_tool(
            tool,
            params,
            1,
        )

        return {
            "success": True,
            "opportunity": rows[0],
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_sales_orders(
    search: str = "",
    state: str = "",
    limit: int = 20,
):
    """
    Search quotations and sales orders.

    Read-only.
    """

    tool = "search_sales_orders"

    params = {
        "search": clean_search(search),
        "state": state,
        "limit": limit,
    }

    try:
        state = choice(
            state,
            ALLOWED_SALES_STATES,
            "sales state",
        )

        domain = []

        if state:
            domain.append(
                [
                    "state",
                    "=",
                    state,
                ]
            )

        if params["search"]:
            domain.extend(
                [
                    "|",
                    [
                        "name",
                        "ilike",
                        params["search"],
                    ],
                    [
                        "partner_id",
                        "ilike",
                        params["search"],
                    ],
                ]
            )

        rows = await odoo.search_read(
            model="sale.order",
            domain=domain,
            fields=[
                "id",
                "name",
                "partner_id",
                "user_id",
                "team_id",
                "date_order",
                "commitment_date",
                "state",
                "currency_id",
                "amount_untaxed",
                "amount_tax",
                "amount_total",
                "invoice_status",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="date_order desc, id desc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "sales_orders": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def get_sales_order(
    order_id: int,
):
    """
    Get one sales order and its lines.

    Read-only.
    """

    tool = "get_sales_order"

    params = {
        "order_id": order_id,
    }

    try:
        positive_id(
            order_id,
            "order_id",
        )

        orders = await odoo.read(
            model="sale.order",
            record_ids=[
                order_id,
            ],
            fields=[
                "id",
                "name",
                "partner_id",
                "user_id",
                "date_order",
                "commitment_date",
                "state",
                "currency_id",
                "amount_untaxed",
                "amount_tax",
                "amount_total",
                "invoice_status",
                "order_line",
            ],
        )

        if not orders:
            raise ValueError(
                "Sales order not found or access denied."
            )

        order = orders[0]

        ids = (
            order.get("order_line") or []
        )[: settings.max_results]

        lines = []

        if ids:
            lines = await odoo.read(
                model="sale.order.line",
                record_ids=ids,
                fields=[
                    "id",
                    "product_id",
                    "name",
                    "product_uom_qty",
                    "qty_delivered",
                    "qty_invoiced",
                    "product_uom",
                    "price_unit",
                    "discount",
                    "price_subtotal",
                    "price_total",
                ],
            )

        log_tool(
            tool,
            params,
            1 + len(lines),
        )

        return {
            "success": True,
            "order": order,
            "lines": lines,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_products(
    search: str,
    limit: int = 20,
):
    """
    Search products and product-level stock figures.

    Read-only.
    """

    tool = "search_products"

    params = {
        "search": clean_search(search),
        "limit": limit,
    }

    try:
        if not params["search"]:
            raise ValueError(
                "Provide a product name, reference, or barcode."
            )

        domain = [
            "|",
            "|",
            [
                "name",
                "ilike",
                params["search"],
            ],
            [
                "default_code",
                "ilike",
                params["search"],
            ],
            [
                "barcode",
                "=",
                params["search"],
            ],
        ]

        rows = await odoo.search_read(
            model="product.product",
            domain=domain,
            fields=[
                "id",
                "name",
                "default_code",
                "barcode",
                "type",
                "uom_id",
                "qty_available",
                "virtual_available",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="name asc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "products": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_stock_by_location(
    product_id: int,
    limit: int = 50,
):
    """
    Return internal-location stock quants for one product.

    Read-only.
    """

    tool = "get_stock_by_location"

    params = {
        "product_id": product_id,
        "limit": limit,
    }

    try:
        positive_id(
            product_id,
            "product_id",
        )

        rows = await odoo.search_read(
            model="stock.quant",
            domain=[
                [
                    "product_id",
                    "=",
                    product_id,
                ],
                [
                    "location_id.usage",
                    "=",
                    "internal",
                ],
                [
                    "quantity",
                    "!=",
                    0,
                ],
            ],
            fields=[
                "id",
                "product_id",
                "location_id",
                "company_id",
                "quantity",
                "reserved_quantity",
                "available_quantity",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="location_id asc, id asc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "stock_quants": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


@mcp.tool()
async def search_inventory_transfers(
    reference: str = "",
    state: str = "",
    limit: int = 20,
):
    """
    Search receipts, deliveries, and internal transfers.

    Read-only.
    """

    tool = "search_inventory_transfers"

    params = {
        "reference": clean_search(reference),
        "state": state,
        "limit": limit,
    }

    try:
        state = choice(
            state,
            ALLOWED_PICKING_STATES,
            "transfer state",
        )

        domain = []

        if params["reference"]:
            domain.extend(
                [
                    "|",
                    [
                        "name",
                        "ilike",
                        params["reference"],
                    ],
                    [
                        "origin",
                        "ilike",
                        params["reference"],
                    ],
                ]
            )

        if state:
            domain.append(
                [
                    "state",
                    "=",
                    state,
                ]
            )

        rows = await odoo.search_read(
            model="stock.picking",
            domain=domain,
            fields=[
                "id",
                "name",
                "partner_id",
                "picking_type_id",
                "location_id",
                "location_dest_id",
                "scheduled_date",
                "date_deadline",
                "date_done",
                "state",
                "origin",
                "company_id",
            ],
            limit=clamp_limit(
                limit,
                settings.max_results,
            ),
            order="scheduled_date desc, id desc",
        )

        log_tool(
            tool,
            params,
            len(rows),
        )

        return {
            "success": True,
            "count": len(rows),
            "transfers": rows,
        }

    except Exception as exc:
        return failed(
            tool,
            exc,
            params,
        )


# ---------------------------------------------------------------------------
# Start server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if settings.transport == "stdio":
        mcp.run(
            transport="stdio"
        )
    else:
        mcp.settings.host = settings.host
        mcp.settings.port = settings.port

        mcp.run(
            transport="streamable-http"
        )