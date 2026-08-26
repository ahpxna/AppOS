from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from services.common.autofill_identity import canonical_page_url


class ApplicationBrowserBindingError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoundApplicationTarget:
    target_id: str
    current_url: str
    source: str


def _origin(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        raise ApplicationBrowserBindingError("application browser target has no HTTP(S) URL")
    return f"{p.scheme.casefold()}://{p.netloc.casefold()}"


def _bound_target_candidates(cur, application_id: str, *, browser_task_id: str | None = None) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    cur.execute(
        """SELECT current_url, detail_json
             FROM application_auth_sessions WHERE application_id=%s;""",
        (application_id,),
    )
    row = cur.fetchone()
    if row:
        detail = dict(row[1] or {})
        target_id = str(detail.get("target_id") or "").strip()
        if target_id:
            candidates.append((target_id, str(row[0] or ""), "auth_session"))

    if browser_task_id:
        cur.execute(
            """SELECT pinned_target_id, coalesce(expected_initial_url,''),
                      coalesce(finished_at, created_at)
                 FROM browser_tasks
                WHERE id=%s AND application_id=%s AND pinned_target_id IS NOT NULL
                  AND task_type='fill_application_form'
                LIMIT 1;""",
            (browser_task_id, application_id),
        )
        task_rows = cur.fetchall()
        source_name = "exact_autofill_task"
    else:
        cur.execute(
            """SELECT pinned_target_id, coalesce(expected_initial_url,''),
                      coalesce(finished_at, created_at)
                 FROM browser_tasks
                WHERE application_id=%s AND pinned_target_id IS NOT NULL
                  AND task_type='fill_application_form'
                ORDER BY coalesce(finished_at, created_at) DESC
                LIMIT 5;""",
            (application_id,),
        )
        task_rows = cur.fetchall()
        source_name = "autofill_task"
    for target_id, url, _ts in task_rows:
        tid = str(target_id or "").strip()
        if tid:
            candidates.append((tid, str(url or ""), source_name))

    # Do NOT deduplicate here. The same long-lived CDP target may have several
    # historical durable URLs. Each source must be checked against the live URL
    # first; otherwise a stale auth-session row can hide a newer exact task bind.
    return candidates


def resolve_application_bound_target(
    cur,
    transport,
    *,
    application_id: str,
    allow_focused_rebind: bool = False,
    expected_url: str = "",
    browser_task_id: str | None = None,
) -> BoundApplicationTarget:
    """Resolve browser authority from durable application bindings, never ambient focus.

    Automatic callers must leave ``allow_focused_rebind`` false. Human-approved
    refocus/bind handoffs may enable it, but only when an exact expected URL is
    available and the focused tab still matches that URL exactly.
    """
    live_by_target: dict[str, BoundApplicationTarget] = {}
    source_priority = {"exact_autofill_task": 3, "auth_session": 2, "autofill_task": 1}
    for target_id, stored_url, source in _bound_target_candidates(
        cur, application_id, browser_task_id=browser_task_id
    ):
        try:
            current = str(transport.current_url(target_id) or "")
            current_canon = canonical_page_url(current)
            stored_canon = canonical_page_url(str(stored_url or ""))
        except Exception:
            continue
        # A CDP target id is not permanent application identity. The same tab
        # can be manually navigated to another employer/application while its
        # target id survives. Automatic authority therefore requires BOTH the
        # durable target id and the exact durable URL to still match.
        if current_canon != stored_canon:
            continue
        candidate = BoundApplicationTarget(target_id, current, source)
        previous = live_by_target.get(target_id)
        if previous is None or source_priority.get(source, 0) > source_priority.get(previous.source, 0):
            live_by_target[target_id] = candidate

    live = list(live_by_target.values())
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        # Two distinct live tabs are never interchangeable authority even when
        # they show the same URL. Require an explicit human target-choice/rebind
        # rather than silently choosing whichever source happened to rank first.
        raise ApplicationBrowserBindingError(
            "multiple live browser targets are bound to this application; use a human refocus/target-choice handoff"
        )

    if not allow_focused_rebind:
        raise ApplicationBrowserBindingError(
            "this application has no live durable browser target binding; use the refocus/bind handoff"
        )
    if not expected_url:
        raise ApplicationBrowserBindingError("human refocus requires an exact expected URL")
    focused = transport.resolve_target()
    current = str(transport.current_url(focused.target_id) or focused.url or "")
    if canonical_page_url(current) != canonical_page_url(expected_url):
        raise ApplicationBrowserBindingError(
            "focused browser page does not match the exact application URL expected by this handoff"
        )
    return BoundApplicationTarget(str(focused.target_id), current, "human_refocus")
