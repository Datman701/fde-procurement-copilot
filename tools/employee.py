"""``employee_lookup`` - requester, department, manager and department head.

Resolves the "Department Head" approver required by three of the four financial
tiers. Three departments have no Director row in ``employees.csv`` (ISS-06), so
the rule is: the requester's manager, when that manager is a Director. If it
cannot be resolved the tool says so instead of guessing a name.
"""

from __future__ import annotations

from typing import Any

from core import policy
from tools.registry import ToolContext, ToolError


def department_head(ctx: ToolContext, employee) -> dict[str, Any]:
    manager = ctx.store.employee(employee.manager_id) if employee.manager_id else None
    if manager is not None and manager.level.strip().lower() == "director":
        return {
            "resolved": True,
            "name": manager.name,
            "employee_id": manager.employee_id,
            "role": "Director",
            "department": manager.department,
            "basis": "requester's manager is a Director in employees.csv",
        }
    directors = [e for e in ctx.store.employees if e.level.strip().lower() == "director"]
    return {
        "resolved": False,
        "name": None,
        "employee_id": None,
        "role": "Director",
        "department": None,
        "basis": (
            f"no Director row found for {employee.department}; "
            f"Procurement must identify the department head. "
            f"Known directors: {sorted(d.name for d in directors)}"
        ),
    }


def run(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    employee = None
    if args.get("employee_id"):
        employee = ctx.store.employee(str(args["employee_id"]).strip())
        if employee is None:
            raise ToolError(f"unknown employee_id {args['employee_id']!r}")
    else:
        request = ctx.require_request()
        employee = ctx.store.employee(request.requester_id)
        if employee is None:
            raise ToolError(f"request {request.request_id} references unknown requester_id")

    manager = ctx.store.employee(employee.manager_id) if employee.manager_id else None
    head = department_head(ctx, employee)

    return {
        "employee_id": employee.employee_id,
        "requester_name": employee.name,
        "department": employee.department,
        "level": employee.level,
        "country": employee.country,
        "manager_id": employee.manager_id,
        "manager_name": manager.name if manager else None,
        "manager_level": manager.level if manager else None,
        "department_head": head,
        "approver_manager": employee.manager_id and employee.name,
        "manager_role": "Manager",
        "manager_resolvable": employee.manager_id is not None,
        "as_of": policy.as_of_iso(),
    }
