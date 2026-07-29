import psycopg

dsn = "host=127.0.0.1 port=5433 dbname=job_apply_os user=jobos password=jobos_local_dev_password_change_later"

LIMIT = 100

with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("""
        SELECT id
        FROM v_clean_promotable_candidate_facts
        LIMIT %s
        """, (LIMIT,))
        rows = cur.fetchall()

        print(f"Promoting {len(rows)} facts...")

        for (fact_id,) in rows:
            cur.execute("""
            UPDATE candidate_profile_facts
            SET status = 'promoted',
                reviewed_at = now()
            WHERE id = %s
            """, (fact_id,))

    conn.commit()

print("Done.")
