"""
Single place to declare which Ollama model each pipeline stage uses.

WHY THIS FILE EXISTS: every script under services/*/ used to hardcode its
own default model string (`os.getenv("SOME_VAR", "qwen3:8b")`), repeated
independently in 17 files. Switching machines (e.g. running with
gemma2:27b + a 32b deepseek variant instead of qwen3:8b + deepseek-r1:14b)
meant either exporting a dozen env vars or hand-editing every file.
Confirmed as a real pain point 2026-08-01 running on a second machine.

HOW TO SWITCH MODELS ON A MACHINE: edit the three DEFAULT_* lines below
(or export the matching env var, same effect, no code edit needed) --
every script that calls get_model(...) picks it up automatically. You do
not need to touch any of the 17 individual service files.

Per-role env var overrides still work underneath (JOBOS_DOCGEN_MODEL,
PROFILE_ASSET_AUDITOR_MODEL, etc.) for the rare case where you want ONE
stage on a different model than the machine default, without changing the
machine default for everything else. Nothing about the old per-role env
var names changed; they just now fall back to the shared defaults below
instead of a hardcoded literal repeated in every file.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------
# Machine-wide defaults. Edit these three, or export the equivalent env
# var, to switch every stage on this machine at once.
# ---------------------------------------------------------------------
DEFAULT_CHAT_MODEL = os.getenv("JOBOS_DEFAULT_CHAT_MODEL", "qwen3:8b")
DEFAULT_AUDIT_MODEL = os.getenv("JOBOS_DEFAULT_AUDIT_MODEL", "deepseek-r1:14b")
DEFAULT_EMBED_MODEL = os.getenv("JOBOS_DEFAULT_EMBED_MODEL", "nomic-embed-text")
# Only used by services/profile-ingestion/deprecated_atom_fact_pipeline/*
# (superseded pipeline, kept for reference -- see 026_deprecate_atom_fact_pipeline...sql)
DEFAULT_LEGACY_LOCAL_MODEL = os.getenv("JOBOS_DEFAULT_LEGACY_LOCAL_MODEL", "llama3.2:3b")

# ---------------------------------------------------------------------
# Per-role resolution. Each role: its own env var (backward compatible
# with existing docs/scripts) falling back to one of the three defaults
# above instead of a hardcoded literal.
# ---------------------------------------------------------------------
MODELS: dict[str, str] = {
    # services/document-generation/
    "docgen": os.getenv("JOBOS_DOCGEN_MODEL", DEFAULT_CHAT_MODEL),
    "verifier": os.getenv("JOBOS_VERIFIER_MODEL", DEFAULT_CHAT_MODEL),
    # services/messaging/
    "reply": os.getenv("JOBOS_REPLY_MODEL", DEFAULT_CHAT_MODEL),
    # services/interview-prep/ (falls back through reply, then the chat default)
    "interview_prep": os.getenv(
        "JOBOS_INTERVIEW_PREP_MODEL", os.getenv("JOBOS_REPLY_MODEL", DEFAULT_CHAT_MODEL)
    ),
    # services/job-analysis/
    "job_fit": os.getenv("JOBOS_FIT_MODEL", DEFAULT_CHAT_MODEL),
    # services/profile-ingestion/
    "profile_asset_synthesizer": os.getenv("PROFILE_ASSET_SYNTHESIZER_MODEL", DEFAULT_CHAT_MODEL),
    "profile_asset_auditor": os.getenv("PROFILE_ASSET_AUDITOR_MODEL", DEFAULT_AUDIT_MODEL),
    "profile_document_mapper": os.getenv("PROFILE_DOCUMENT_MAPPER_MODEL", DEFAULT_CHAT_MODEL),
    "profile_evidence_unit": os.getenv("PROFILE_EVIDENCE_UNIT_MODEL", DEFAULT_CHAT_MODEL),
    "structured_evidence_unit": os.getenv("STRUCTURED_EVIDENCE_UNIT_MODEL", DEFAULT_CHAT_MODEL),
    "structured_asset_synth": os.getenv("STRUCTURED_ASSET_SYNTH_MODEL", DEFAULT_CHAT_MODEL),
    "embed": os.getenv("PROFILE_EMBED_MODEL", DEFAULT_EMBED_MODEL),
    # deprecated_atom_fact_pipeline/ only
    "legacy_local_llm": os.getenv("LOCAL_LLM_MODEL", DEFAULT_LEGACY_LOCAL_MODEL),
}


def get_model(role: str) -> str:
    """Look up the model name for a pipeline role (see MODELS above).

    Raises KeyError with the full list of valid roles if you typo one --
    intentionally loud rather than silently falling back to something
    unexpected.
    """
    try:
        return MODELS[role]
    except KeyError:
        raise KeyError(
            f"Unknown model role '{role}'. Valid roles: {sorted(MODELS)}"
        ) from None
