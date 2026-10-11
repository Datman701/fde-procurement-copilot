// Searchable requester field.
//
// The old <select> showed only "E009 — David Chen", which left the user
// guessing which department someone was in or whether they could approve.
// This card surfaces department, reporting line and the resolved department
// head, and previews budget headroom before the analysis runs.
import { useEffect, useMemo, useRef, useState } from "react";

function matchesQuery(employee, q) {
  return [employee.name, employee.employee_id, employee.department, employee.level].some(
    (field) => String(field ?? "").toLowerCase().includes(q),
  );
}

function formatUsd(value) {
  return `$${Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export default function RequesterPicker({ employees = [], value, onChange, annualCost }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  const selected = useMemo(
    () => employees.find((e) => e.employee_id === value) ?? null,
    [employees, value],
  );

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return employees;
    return employees.filter((e) => matchesQuery(e, q));
  }, [employees, query]);

  // Keep the highlighted row in view when the list is long.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const node = listRef.current.querySelector('[data-active="true"]');
    node?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex]);

  function choose(employeeId) {
    onChange(employeeId);
    setOpen(false);
    setQuery("");
    setActiveIndex(0);
  }

  function clear() {
    onChange("");
    setQuery("");
    inputRef.current?.focus();
  }

  function handleKeyDown(event) {
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (!open && (event.key === "ArrowDown" || event.key === "Enter")) {
      event.preventDefault();
      setOpen(true);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((i) => (i + 1) % Math.max(matches.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((i) => (i - 1 + matches.length) % Math.max(matches.length, 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (matches[activeIndex]) choose(matches[activeIndex].employee_id);
    }
  }

  // ---------------------------------------------------------------- resolved
  if (selected) {
    const head = selected.department_head;
    const available = selected.budget?.available_usd;
    const overBudget =
      available != null && annualCost != null && Number(annualCost) > available;

    return (
      <div className="requester-card">
        <div className="requester-card-head">
          <div>
            <strong>{selected.name}</strong>{" "}
            <span className="requester-id">({selected.employee_id})</span>
            {selected.is_department_head && (
              <span className="chip heads-chip">heads {selected.department}</span>
            )}
          </div>
          <button type="button" className="linkish" onClick={clear}>
            Change
          </button>
        </div>

        <dl className="requester-detail">
          <div>
            <dt>Department</dt>
            <dd>{selected.department}</dd>
          </div>
          <div>
            <dt>Role</dt>
            <dd>{selected.level}</dd>
          </div>
          <div>
            <dt>Reports to</dt>
            <dd>
              {selected.manager_name
                ? `${selected.manager_name} (${selected.manager_id})`
                : "— (top of chain)"}
            </dd>
          </div>
          <div>
            <dt>Department head</dt>
            <dd>
              {head ? (
                `${head.name} (${head.employee_id})`
              ) : (
                <span className="warn-note">
                  unresolved — Procurement must identify the department head
                </span>
              )}
            </dd>
          </div>
        </dl>

        {available != null && (
          <p className={overBudget ? "budget-preview over" : "budget-preview"}>
            {selected.department} has{" "}
            <strong>{formatUsd(available)}</strong> available
            {annualCost != null && (
              <>
                {" · "}
                this request is <strong>{formatUsd(annualCost)}</strong>
                {overBudget && " — over budget, expect an escalation"}
              </>
            )}
          </p>
        )}
      </div>
    );
  }

  // ---------------------------------------------------------------- searching
  return (
    <div className="requester-picker">
      <div className="requester-input-wrap">
        <input
          ref={inputRef}
          id="requester-select"
          type="text"
          role="combobox"
          autoComplete="off"
          aria-expanded={open}
          aria-controls="requester-listbox"
          aria-autocomplete="list"
          aria-activedescendant={open && matches[activeIndex] ? `req-${matches[activeIndex].employee_id}` : undefined}
          placeholder="Search a name, department or ID…"
          value={open ? query : ""}
          onChange={(e) => {
            setQuery(e.target.value);
            setActiveIndex(0);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            // Delay so a click on a suggestion still registers.
            window.setTimeout(() => setOpen(false), 120);
          }}
          onKeyDown={handleKeyDown}
        />
        {value && (
          <button type="button" className="clear-btn" onClick={clear} aria-label="Clear requester">
            ×
          </button>
        )}
      </div>

      {open && (
        <ul id="requester-listbox" role="listbox" className="requester-list" ref={listRef}>
          {matches.length === 0 && <li className="requester-empty">No employee matches “{query}”.</li>}
          {matches.map((e, i) => (
            <li
              key={e.employee_id}
              id={`req-${e.employee_id}`}
              role="option"
              aria-selected={i === activeIndex}
              data-active={i === activeIndex}
              className={`requester-option${i === activeIndex ? " active" : ""}`}
              onMouseDown={(event) => {
                event.preventDefault();
                choose(e.employee_id);
              }}
              onMouseEnter={() => setActiveIndex(i)}
            >
              <span className="requester-option-name">
                {e.name} <span className="requester-id">({e.employee_id})</span>
              </span>
              <span className="requester-option-meta">
                {e.department}
                {e.is_department_head ? (
                  <span className="chip heads-chip">heads {e.department}</span>
                ) : e.manager_name ? (
                  <> · reports to {e.manager_name}</>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
