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


def list_pending(limit=30):
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  cpf.id,
                  cpf.status,
                  cpf.category,
                  cpf.subcategory,
                  cpf.confidence,
                  rf.file_name,
                  pc.chunk_index,
                  pc.section,
                  cpf.fact_text,
                  cpf.evidence_quote
                FROM candidate_profile_facts cpf
                LEFT JOIN raw_files rf ON rf.id = cpf.source_file_id
                LEFT JOIN profile_chunks pc ON pc.id = cpf.source_chunk_id
                WHERE cpf.status IN ('pending', 'needs_edit', 'approved')
                ORDER BY cpf.created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()

    for i, row in enumerate(rows, start=1):
        (
            cid,
            status,
            category,
            subcategory,
            confidence,
            file_name,
            chunk_index,
            section,
            fact_text,
            evidence_quote,
        ) = row

        print("")
        print(f"[{i}] {str(cid)[:8]}  status={status}  confidence={confidence}")
        print(f"    category={category} / {subcategory}")
        print(f"    source={file_name} | chunk={chunk_index} | section={section}")
        print(f"    fact: {fact_text}")
        print(f"    evidence: {evidence_quote}")

    print("")
    print(f"Total shown: {len(rows)}")


def fetch_candidate(cur, candidate_id):
    cur.execute(
        """
        SELECT
          cpf.id,
          cpf.extractor_name,
          cpf.extractor_version,
          cpf.source_file_id,
          cpf.source_chunk_id,
          pc.text_content,
          cpf.category,
          cpf.subcategory,
          cpf.fact_text,
          cpf.evidence_quote,
          cpf.reasoning,
          cpf.confidence,
          cpf.status,
          rf.file_name,
          pc.chunk_index,
          pc.section
        FROM candidate_profile_facts cpf
        LEFT JOIN profile_chunks pc
          ON pc.id = cpf.source_chunk_id
        LEFT JOIN raw_files rf
          ON rf.id = cpf.source_file_id
        WHERE cpf.id = %s;
        """,
        (candidate_id,),
    )
    return cur.fetchone()


def create_learning_records(
    cur,
    candidate_id,
    decision: str,
    label: str,
    review_note: Optional[str] = None,
    corrected_output: Optional[dict] = None,
):
    row = fetch_candidate(cur, candidate_id)
    if not row:
        return

    (
        cid,
        extractor_name,
        extractor_version,
        source_file_id,
        source_chunk_id,
        input_text,
        category,
        subcategory,
        fact_text,
        evidence_quote,
        reasoning,
        confidence,
        candidate_status,
        file_name,
        chunk_index,
        section,
    ) = row

    input_json = {
        "source_file": file_name,
        "source_chunk_index": chunk_index,
        "source_section": section,
        "chunk_text": input_text,
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
    }

    output_json = {
        "category": category,
        "subcategory": subcategory,
        "fact_text": fact_text,
        "evidence_quote": evidence_quote,
        "reasoning": reasoning,
        "confidence": float(confidence) if confidence is not None else None,
        "candidate_status_after_review": candidate_status,
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
          finished_at
        )
        VALUES (
          'candidate_fact_extractor',
          'extract_candidate_fact',
          %s,
          %s,
          %s,
          %s,
          %s,
          %s,
          'completed',
          'local_ollama',
          NULL,
          now()
        )
        RETURNING id;
        """,
        (
            source_file_id,
            source_chunk_id,
            cid,
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
          corrected_output_json,
          usable_for_prompt,
          usable_for_finetune
        )
        VALUES (
          %s,
          'candidate_fact_extractor',
          'extract_candidate_fact',
          'human',
          'user',
          %s,
          %s,
          %s,
          true,
          false
        )
        RETURNING id;
        """,
        (
            run_id,
            decision,
            review_note,
            Jsonb(corrected_output) if corrected_output else None,
        ),
    )
    feedback_id = cur.fetchone()[0]

    positive_output_json = output_json if label == "positive" else None
    negative_output_json = output_json if label == "negative" else None
    corrected_output_json = corrected_output if corrected_output else None

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
          'extract_candidate_fact',
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
            review_note,
        ),
    )


def update_status(short_id, status, note=None):
    allowed = {"pending", "approved", "rejected", "needs_edit"}
    if status not in allowed:
        raise SystemExit(f"Invalid status. Use one of: {sorted(allowed)}")

    decision_map = {
        "approved": "approved",
        "rejected": "rejected",
        "needs_edit": "needs_edit",
        "pending": "needs_edit",
    }

    label_map = {
        "approved": "positive",
        "rejected": "negative",
        "needs_edit": "edit",
        "pending": "edit",
    }

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE candidate_profile_facts
                SET
                  status = %s,
                  review_note = %s,
                  reviewed_at = now()
                WHERE id::text LIKE %s
                  AND status IN ('pending', 'needs_edit', 'approved')
                RETURNING id, status, fact_text;
                """,
                (status, note, short_id + "%"),
            )
            rows = cur.fetchall()

            for r in rows:
                create_learning_records(
                    cur,
                    candidate_id=r[0],
                    decision=decision_map[status],
                    label=label_map[status],
                    review_note=note,
                )

        conn.commit()

    if not rows:
        print("No matching candidate updated.")
        return

    for r in rows:
        print(f"updated {r[0]} -> {r[1]} :: {r[2]}")


def promote(short_id):
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate AS (
                  SELECT *
                  FROM candidate_profile_facts
                  WHERE id::text LIKE %s
                    AND status IN ('pending', 'approved')
                  ORDER BY created_at DESC
                  LIMIT 1
                ),
                inserted AS (
                  INSERT INTO profile_facts (
                    category,
                    subcategory,
                    fact_text,
                    evidence_source,
                    evidence_file_id,
                    evidence_chunk_id,
                    evidence_quote,
                    confidence,
                    approved_by_user,
                    is_active,
                    conflict_status
                  )
                  SELECT
                    category,
                    subcategory,
                    fact_text,
                    'candidate_profile_facts:' || id::text,
                    source_file_id,
                    source_chunk_id,
                    evidence_quote,
                    confidence,
                    true,
                    true,
                    'no_conflict'
                  FROM candidate
                  RETURNING id
                )
                UPDATE candidate_profile_facts cpf
                SET
                  status = 'promoted',
                  promoted_profile_fact_id = inserted.id,
                  reviewed_at = now(),
                  review_note = COALESCE(cpf.review_note, '') || E'\\nPromoted to profile_facts.'
                FROM candidate, inserted
                WHERE cpf.id = candidate.id
                RETURNING cpf.id, cpf.promoted_profile_fact_id, cpf.fact_text;
                """,
                (short_id + "%",),
            )
            rows = cur.fetchall()

            for candidate_id, _profile_fact_id, _fact_text in rows:
                create_learning_records(
                    cur,
                    candidate_id=candidate_id,
                    decision="approved",
                    label="positive",
                    review_note="Promoted to profile_facts.",
                )

        conn.commit()

    if not rows:
        print("No matching candidate promoted.")
        return

    for candidate_id, profile_fact_id, fact_text in rows:
        print(f"promoted candidate={candidate_id} -> profile_fact={profile_fact_id}")
        print(f"fact: {fact_text}")


def usage():
    print("Usage:")
    print("  python review_candidate_facts.py list [limit]")
    print("  python review_candidate_facts.py approve <short_id> [note]")
    print("  python review_candidate_facts.py reject <short_id> [note]")
    print("  python review_candidate_facts.py needs_edit <short_id> [note]")
    print("  python review_candidate_facts.py promote <short_id>")


def main():
    if len(sys.argv) < 2:
        usage()
        return 1

    cmd = sys.argv[1]

    if cmd == "list":
        limit = int(sys.argv[2]) if len(sys.argv) >= 3 else 30
        list_pending(limit)
        return 0

    if cmd in {"approve", "reject", "needs_edit"}:
        if len(sys.argv) < 3:
            usage()
            return 1
        short_id = sys.argv[2]
        note = sys.argv[3] if len(sys.argv) >= 4 else None
        status = "approved" if cmd == "approve" else cmd
        update_status(short_id, status, note)
        return 0

    if cmd == "promote":
        if len(sys.argv) < 3:
            usage()
            return 1
        promote(sys.argv[2])
        return 0

    usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
