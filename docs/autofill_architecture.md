# Deterministic autofill architecture

The browser component is a **copilot**, never an autonomous applicant. It can
prepare and verify ordinary fields, but it does not submit an application.

```text
Accessibility/DOM snapshot
        -> form_inspector
        -> field_matcher
        -> autofill_planner + policy
        -> narrow executor adapter (OpenClaw | Playwright | CDP)
        -> fresh snapshot
        -> autofill_verifier
```

`OpenClaw` is an execution adapter only. It receives one command such as
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

Radio/checkbox controls are never blindly toggled: an adapter must expose the
question group, option label, and selected state; the verifier confirms the
requested option after the action. A run is `completed` only when every planned
write verifies. Any failed write is `partial`; any paused field is
`needs_review`.

The existing `autofill_agent_v1.py` remains a read-only OpenClaw snapshot
compatibility CLI while the adapter integrations are wired to these contracts.
Its write path is intentionally disabled, so no user data is exposed to an
LLM or sent to a browser without this plan/verify pipeline.
