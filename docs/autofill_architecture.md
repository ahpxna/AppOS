# Deterministic autofill architecture

The browser component is a **copilot**, never an autonomous applicant. It can
prepare and verify ordinary fields, but it does not submit an application.

```text
Accessibility/DOM snapshot
        -> form_inspector
        -> field_matcher
        -> autofill_planner + policy
        -> AutofillSession capability gate
        -> narrow executor adapter (OpenClaw | Playwright | CDP)
        -> fresh snapshot
        -> autofill_verifier
```

`OpenClawTransport` is an execution adapter only. It receives one command such as
`{action, target, value}` for the field being written; it never receives a
giant profile prompt or authority to browse and decide freely. Playwright and
CDP adapters implement the same command contract, so the matching and policy
logic remains unchanged.

## Field policy

| Class | Behaviour |
| --- | --- |
| Static identity | Auto-plan only for an exact high-confidence mapping and an approved profile value. |
| Derived | Pause unless its evidence and question semantics have been reviewed. |
| Sensitive/legal | Pause by default. Immigration, citizenship, US-person, EEO, export-control and similar prompts need an exact user-confirmed semantic answer. |
| Unknown | Leave blank and show it for review. |

Radio/checkbox controls are never blindly toggled: the inspector builds a
`QuestionGroup` with option reference, label and selected state. An immigration
answer is planned only when the exact question class has a user-confirmed
answer, e.g. `SPONSORSHIP_NOW_OR_FUTURE`; generic keyword matching is rejected.
The session reads the focused tab URL before every write and after it, requires
the approval-bound origin and allowed domain, then snapshots and verifies the
write. It consumes the exact approval on the first verified side effect, so a
crash cannot replay the capability in another task.

Resume/cover-letter upload is limited to a QA-passed, user-approved artifact
registered for the approval-bound document. The artifact SHA-256 is rechecked
before upload; post-upload verification compares its filename/attached value,
not the browser's fake local path.

`browser_queue_worker.py` is the only production caller of this pipeline. It
creates the session only for a queued, exact application/document/origin-bound
approval; no LLM is in its form-write path. The legacy `autofill_agent_v1.py`
remains a read-only snapshot/plan compatibility CLI.
