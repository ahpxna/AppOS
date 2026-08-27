import os
import sys
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb


DB_HOST = os.getenv("JOBOS_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("JOBOS_DB_PORT", "5433"))
DB_NAME = os.getenv("JOBOS_DB_NAME", "job_apply_os")
DB_USER = os.getenv("JOBOS_DB_USER", "jobos")
DB_PASSWORD = os.getenv("JOBOS_DB_PASSWORD", "jobos_local_dev_password_change_later")

DSN = (
    f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
    f"user={DB_USER} password={DB_PASSWORD}"
)

REJECT_BUCKETS = {
    "reject_missing_evidence": "Rejected by quality guard: missing evidence quote.",
    "reject_source_not_promotable": "Rejected by quality guard: source file role is not promotable as personal profile evidence.",
    "reject_generic_coursework_mismatch": "Rejected by quality guard: generic coursework claim is not supported by the evidence quote.",
    "reject_degree_claim_weak_evidence": "Rejected by quality guard: degree/senior claim is not supported by the evidence quote.",
}

NEEDS_EDIT_BUCKETS = {
    "needs_edit_broad_skill_from_career_relevance": "Needs edit by quality guard: broad skill claim was inferred from career relevance/role-alignment language.",
    "needs_edit_future_or_guidance": "Needs edit by quality guard: evidence contains future, guidance, recommendation, or positioning language.",
}


def fetch_candidates(cur, buckets, limit):
    cur.execute(
        """
        SELECT
          q.id,
          q.short_id,
          q.quality_bucket,
          q.status,
          q.category,
          q.subcategory,
          q.confidence,
          q.source_file,
          q.file_role,
          q.source_chunk_index,
          q.source_section,
          q.fact_text,
          q.evidence_quote,
          q.reasoning,
          cpf.source_file_id,
          cpf.source_chunk_id
        FROM v_candidate_fact_quality_review q
        JOIN candidate_profile_facts cpf
          ON cpf.id = q.id
        WHERE q.status = 'pending'
          AND q.quality_bucket = ANY(%s)
        ORDER BY q.quality_bucket, q.confidence DESC NULLS LAST, q.created_at DESC
        LIMIT %s;
        """,
        (list(buckets), limit),
    )
    return cur.fetchall()


def create_learning_records(cur, row, decision, label, note):
    (
        candidate_id,
        short_id,
        quality_bucket,
        status,
        category,
        subcategory,
        confidence,
        source_file,
        file_role,
        source_chunk_index,
        source_section,
        fact_text,
        evidence_quote,
        reasoning,
        source_file_id,
        source_chunk_id,
    ) = row

    input_json = {
        "source_file": source_file,
        "file_role": file_role,
        "source_chunk_index": source_chunk_index,
        "source_section": source_section,
        "quality_bucket": quality_bucket,
        "triage_source": "bulk_quality_guard",
    }

    output_json = {
        "candidate_id": str(candidate_id),
        "short_id": short_id,
        "category": category,
        "subcategory": subcategory,
        "fact_text": fact_text,
        "evidence_quote": evidence_quote,
        "reasoning": reasoning,
        "confidence": float(confidence) if confidence is not None else None,
        "quality_bucket": quality_bucket,
        "decision": decision,
    }

    cur.execute(
        """
        INSERT INTO component_runs (
          component_name,
          task_type,
          source_file_id,
          source_chunk_id,
          source_candidate_fact_id,
          input_json,
          output_json,
          output_text,
          status,
          model_provider,
          model_name,
          input_tokens,
          output_tokens,
          estimated_cost_usd,
          finished_at
        )
        VALUES (
          'candidate_fact_extractor',
          'quality_guard_review_candidate_fact',
          %s,
          %s,
          %s,
          %s,
          %s,
          %s,
          'completed',
          'deterministic',
          'quality_guard_v1',
          0,
          0,
          0,
          now()
        )
        RETURNING id;
        """,
        (
            source_file_id,
            source_chunk_id,
            candidate_id,
            Jsonb(input_json),
            Jsonb(output_json),
            fact_text,
        ),
    )
    run_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO component_feedback (
          component_run_id,
          component_name,
          task_type,
          feedback_source,
          reviewer,
          decision,
          review_note,
          usable_for_prompt,
          usable_for_finetune
        )
        VALUES (
          %s,
          'candidate_fact_extractor',
          'quality_guard_review_candidate_fact',
          'system_quality_guard',
          'quality_guard_v1',
          %s,
          %s,
          true,
          false
        )
        RETURNING id;
        """,
        (run_id, decision, note),
    )
    feedback_id = cur.fetchone()[0]

    positive_output_json = None
    negative_output_json = output_json if label == "negative" else None
    corrected_output_json = output_json if label == "edit" else None

    cur.execute(
        """
        INSERT INTO component_training_examples (
          source_feedback_id,
          source_run_id,
          component_name,
          task_type,
          input_json,
          positive_output_json,
          negative_output_json,
          corrected_output_json,
          label,
          rationale,
          split,
          quality_score,
          usable_for_prompt,
          usable_for_finetune
        )
        VALUES (
          %s,
          %s,
          'candidate_fact_extractor',
          'quality_guard_review_candidate_fact',
          %s,
          %s,
          %s,
          %s,
          %s,
          %s,
          'train',
          NULL,
          true,
          false
        );
        """,
        (
            feedback_id,
            run_id,
            Jsonb(input_json),
            Jsonb(positive_output_json) if positive_output_json else None,
            Jsonb(negative_output_json) if negative_output_json else None,
            Jsonb(corrected_output_json) if corrected_output_json else None,
            label,
            note,
        ),
    )


def apply_bucket(cur, row, new_status, note):
    candidate_id = row[0]
    cur.execute(
        """
        UPDATE candidate_profile_facts
        SET
          status = %s,
          review_note = %s,
          reviewed_at = now()
        WHERE id = %s
          AND status = 'pending'
        RETURNING id;
        """,
        (new_status, note, candidate_id),
    )
    return cur.fetchone() is not None


def main():
    mode = sys.argv[1] if len(sys.argv) >= 2 else "dry-run"
    limit = int(sys.argv[2]) if len(sys.argv) >= 3 else 500

    if mode not in {"dry-run", "apply"}:
        raise SystemExit("Usage: python bulk_triage_candidate_facts.py [dry-run|apply] [limit]")

    all_buckets = set(REJECT_BUCKETS) | set(NEEDS_EDIT_BUCKETS)

    print("===== BULK TRIAGE CANDIDATE FACTS =====")
    print(f"Mode:  {mode}")
    print(f"Limit: {limit}")
    print("")

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            rows = fetch_candidates(cur, all_buckets, limit)

            print(f"Candidates selected: {len(rows)}")
            counts = {}
            for r in rows:
                counts[r[2]] = counts.get(r[2], 0) + 1

            for bucket, count in sorted(counts.items()):
                print(f"- {bucket}: {count}")

            print("")
            for r in rows[:30]:
                print(f"{r[1]} {r[2]} :: {r[11][:140] if r[11] else ''}")

            if mode == "dry-run":
                print("")
                print("Dry run only. No DB changes.")
                return 0

            updated = 0

            for r in rows:
                bucket = r[2]

                if bucket in REJECT_BUCKETS:
                    new_status = "rejected"
                    decision = "rejected"
                    label = "negative"
                    note = REJECT_BUCKETS[bucket]
                elif bucket in NEEDS_EDIT_BUCKETS:
                    new_status = "needs_edit"
                    decision = "needs_edit"
                    label = "edit"
                    note = NEEDS_EDIT_BUCKETS[bucket]
                else:
                    continue

                did_update = apply_bucket(cur, r, new_status, note)
                if did_update:
                    create_learning_records(cur, r, decision, label, note)
                    updated += 1

        conn.commit()

    print("")
    print(f"Updated: {updated}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
