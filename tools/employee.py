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
    # 1. If the employee themselves is a Director and their manager is not a Director
    #    (or has no manager), they are the department head.
    if employee.level.strip().lower() == "director":
        manager = ctx.store.employee(employee.manager_id) if employee.manager_id else None
        if manager is None or manager.level.strip().lower() != "director":
            return {
                "resolved": True,
                "name": employee.name,
                "employee_id": employee.employee_id,
                "role": "Director",
                "department": employee.department,
                "basis": "requester is a Director with no Director manager; they are the department head",
            }
    # 2. If their manager is a Director IN THE SAME DEPARTMENT, that manager is the dept head.
    manager = ctx.store.employee(employee.manager_id) if employee.manager_id else None
    if manager is not None and manager.level.strip().lower() == "director" and manager.department == employee.department:
        return {
            "resolved": True,
            "name": manager.name,
            "employee_id": manager.employee_id,
            "role": "Director",
            "department": manager.department,
            "basis": "requester's manager is a Director in the same department",
        }
    # 3. Find a Director in the same department who has no Director manager
    directors_in_dept = [
        e for e in ctx.store.employees
        if e.level.strip().lower() == "director" and e.department == employee.department
    ]
    for d in directors_in_dept:
        m = ctx.store.employee(d.manager_id) if d.manager_id else None
        if m is None or m.level.strip().lower() != "director":
            return {
                "resolved": True,
                "name": d.name,
                "employee_id": d.employee_id,
                "role": "Director",
                "department": d.department,
                "basis": f"found Director {d.name} in {d.department} with no Director manager",
            }
    # 4. No Director in this department — check if there's a VP over this department
    vps = [e for e in ctx.store.employees if e.level.strip().lower() == "vp"]
    for vp in vps:
        # Heuristic: VP with no manager or VP in Operations (central) covers orphan depts
        m = ctx.store.employee(vp.manager_id) if vp.manager_id else None
        if m is None:
            return {
                "resolved": True,
                "name": vp.name,
                "employee_id": vp.employee_id,
                "role": "VP",
                "department": vp.department,
                "basis": f"no Director in {employee.department}; VP {vp.name} is the highest manager",
            }
    # 5. Final fallback
    all_directors = [e for e in ctx.store.employees if e.level.strip().lower() == "director"]
    return {
        "resolved": False,
        "name": None,
        "employee_id": None,
        "role": "Director",
        "department": None,
        "basis": (
            f"no Director row found for {employee.department}; "
            f"Procurement must identify the department head. "
            f"Known directors: {sorted(d.name for d in all_directors)}"
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
