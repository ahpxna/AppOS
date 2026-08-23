#!/usr/bin/env python3
"""A local desktop form for adding a user-pasted JD to the JobOS pipeline.

Run with ``python scripts/jobos_intake_app.py``.  This is a Tk desktop window;
it neither launches a browser nor requires OpenClaw/CDP.  Expensive analysis
is a separate explicit button and stops at existing human approval gates.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ModuleNotFoundError as exc:  # Ubuntu's minimal Python omits Tk.
    raise SystemExit(
        "Tk desktop support is unavailable. On Ubuntu run: sudo apt install python3-tk"
    ) from exc

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from services.intake.manual_job_intake import JobDraft, ManualIntakeError, create_application


def load_project_env() -> None:
    """Load simple untracked .env values for this desktop process only.

    The app also passes this environment to child pipeline commands, so its DB
    and LLM configuration agrees with the CLI without requiring ``source .env``
    (which would be unsafe for a general dotenv file).
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.replace("_", "").isalnum() and key not in os.environ:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def dsn() -> str:
    return (
        f"host={os.getenv('JOBOS_DB_HOST', '127.0.0.1')} "
        f"port={os.getenv('JOBOS_DB_PORT', '5433')} "
        f"dbname={os.getenv('JOBOS_DB_NAME', 'job_apply_os')} "
        f"user={os.getenv('JOBOS_DB_USER', 'jobos')} "
        f"password={os.getenv('JOBOS_DB_PASSWORD', 'jobos_local_dev_password_change_later')}"
    )


class IntakeApp(ttk.Frame):
    """Single-record UI; state-changing and paid actions stay visibly separate."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.master = master
        self.grid(sticky="nsew")
        master.title("JobOS — Paste a Job Description")
        master.minsize(850, 650)
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        for column in (1, 2, 3):
            self.columnconfigure(column, weight=1)
        self.rowconfigure(10, weight=1)
        self.application_id: str | None = None
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.vars = {name: tk.StringVar() for name in (
            "company", "job_title", "job_url", "location", "seniority_level", "deadline", "salary_range"
        )}
        self.source = tk.StringVar(value="manual_paste")
        self.work_mode = tk.StringVar(value="unknown")
        self.screen_after_save = tk.BooleanVar(value=True)
        self._build()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        ttk.Label(self, text="Paste a job description", font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(self, text="Local only. No browser or LinkedIn login is used.").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        fields = [("Company *", "company"), ("Job title *", "job_title"), ("Posting URL (optional)", "job_url"),
                  ("Location", "location"), ("Seniority", "seniority_level"),
                  ("Deadline (YYYY-MM-DD)", "deadline"), ("Salary range", "salary_range")]
        for row, (label, key) in enumerate(fields, start=2):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(self, textvariable=self.vars[key], width=72).grid(row=row, column=1, columnspan=2, sticky="ew", pady=3)
        row = 9
        ttk.Label(self, text="Source").grid(row=row, column=0, sticky="w", pady=(9, 3))
        ttk.Combobox(self, textvariable=self.source, state="readonly", values=(
            "manual_paste", "linkedin_copy", "company_career_page", "recruiter", "job_board", "referral"
        )).grid(row=row, column=1, sticky="w", pady=(9, 3))
        ttk.Label(self, text="Work mode").grid(row=row, column=2, sticky="w", padx=(18, 3), pady=(9, 3))
        ttk.Combobox(self, textvariable=self.work_mode, state="readonly", values=("unknown", "remote", "hybrid", "on_site")).grid(row=row, column=3, sticky="ew", pady=(9, 3))

        ttk.Label(self, text="Job description *").grid(row=10, column=0, sticky="nw", pady=(9, 3))
        jd_frame = ttk.Frame(self)
        jd_frame.grid(row=10, column=1, columnspan=3, sticky="nsew", pady=(9, 3))
        jd_frame.columnconfigure(0, weight=1)
        jd_frame.rowconfigure(0, weight=1)
        self.jd_text = tk.Text(jd_frame, height=15, wrap="word")
        self.jd_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(jd_frame, orient="vertical", command=self.jd_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.jd_text.configure(yscrollcommand=scrollbar.set)
        self.jd_text.bind("<KeyRelease>", lambda _event: self._update_count())
        self.character_count = ttk.Label(self, text="0 characters")
        self.character_count.grid(row=11, column=1, sticky="w")
        ttk.Button(self, text="Paste JD from clipboard", command=self._paste_jd).grid(row=11, column=2, sticky="e")

        ttk.Label(self, text="Private notes (optional)").grid(row=12, column=0, sticky="nw", pady=(9, 3))
        self.notes = tk.Text(self, height=4, wrap="word")
        self.notes.grid(row=12, column=1, columnspan=3, sticky="ew", pady=(9, 3))

        ttk.Checkbutton(self, text="Run free rule-based screen immediately after saving", variable=self.screen_after_save).grid(
            row=13, column=0, columnspan=4, sticky="w", pady=(10, 3)
        )
        actions = ttk.Frame(self)
        actions.grid(row=14, column=0, columnspan=4, sticky="ew", pady=6)
        self.save_button = ttk.Button(actions, text="Save job to JobOS", command=self._save)
        self.save_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Analyze + draft documents", command=self._run_pipeline, state="disabled")
        self.run_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Clear form", command=self._clear).pack(side="right")
        self.status = tk.Text(self, height=8, wrap="word", state="disabled", background="#f6f6f6")
        self.status.grid(row=15, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        self.rowconfigure(15, weight=1)
        self._append("Ready. Save records to queue market-demand extraction; analysis is optional.")

    def _draft(self) -> JobDraft:
        return JobDraft(
            company=self.vars["company"].get(), job_title=self.vars["job_title"].get(),
            jd_text=self.jd_text.get("1.0", "end-1c"), job_url=self.vars["job_url"].get(),
            source=self.source.get(), location=self.vars["location"].get(), work_mode=self.work_mode.get(),
            seniority_level=self.vars["seniority_level"].get(), deadline=self.vars["deadline"].get(),
            salary_range=self.vars["salary_range"].get(), notes=self.notes.get("1.0", "end-1c"),
        )

    def _save(self) -> None:
        try:
            with psycopg.connect(dsn(), autocommit=False) as conn:
                with conn.cursor() as cur:
                    application_id, _ = create_application(cur, self._draft())
                    final_step = "intake"
                    if application_id and self.screen_after_save.get():
                        from services.orchestrator.orchestrator_v1 import load_rules, run_filter
                        run_filter(cur, application_id, load_rules(cur))
                        cur.execute("SELECT current_step FROM applications WHERE id = %s;", (application_id,))
                        final_step = cur.fetchone()[0]
                conn.commit()
        except (ManualIntakeError, psycopg.Error, OSError) as exc:
            messagebox.showerror("JobOS intake", str(exc))
            return
        if application_id is None:
            self._append("Duplicate JD: no new application was created.")
            return
        self.application_id = application_id
        can_analyze = final_step in {"intake", "screened"}
        self.run_button.configure(state="normal" if can_analyze else "disabled")
        self._append(f"Saved application {application_id}. Market-demand extraction is queued by the database trigger.")
        if can_analyze:
            self._append("Use ‘Analyze + draft documents’ only when you are ready for configured LLM/API usage.")
        else:
            self._append(f"The free screen paused this job at {final_step}; no paid analysis was started.")

    def _run_pipeline(self) -> None:
        if not self.application_id:
            return
        if not messagebox.askokcancel(
            "Run analysis?",
            "This may call your configured LLM/API and public company research. It will never open a browser, submit an application, or bypass an approval gate. Continue?",
        ):
            return
        self.run_button.configure(state="disabled")
        threading.Thread(target=self._pipeline_worker, args=(self.application_id,), daemon=True).start()

    def _pipeline_worker(self, application_id: str) -> None:
        command = [sys.executable, str(REPO_ROOT / "services/orchestrator/orchestrator_v1.py"), "advance", "--application-id", application_id, "--apply"]
        try:
            for attempt in range(4):
                self.events.put(("log", f"Pipeline step {attempt + 1}/4…"))
                proc = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=1900)
                output = ((proc.stdout or "") + (proc.stderr or "")).strip()
                self.events.put(("log", output[-3000:] or f"Pipeline exited {proc.returncode}."))
                if proc.returncode:
                    break
                with psycopg.connect(dsn(), autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT current_step, status FROM applications WHERE id = %s;", (application_id,))
                        row = cur.fetchone()
                if not row or row[0] in {"awaiting_fit_review", "awaiting_approval", "fit_rejected", "filtered_out", "docs_verified", "docs_failed_qa"}:
                    self.events.put(("done", f"Pipeline paused at {row[0] if row else 'unknown'}; review the JobOS approval/status next."))
                    return
            self.events.put(("done", "Pipeline run finished or paused. Review the status log."))
        except (OSError, subprocess.TimeoutExpired, psycopg.Error) as exc:
            self.events.put(("done", f"Pipeline stopped safely: {exc}"))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, text = self.events.get_nowait()
                self._append(text)
                if kind == "done":
                    self.run_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append(self, text: str) -> None:
        self.status.configure(state="normal")
        self.status.insert("end", text.rstrip() + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    def _paste_jd(self) -> None:
        try:
            value = self.master.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("JobOS intake", "Clipboard has no text to paste.")
            return
        self.jd_text.delete("1.0", "end")
        self.jd_text.insert("1.0", value)
        self._update_count()

    def _update_count(self) -> None:
        self.character_count.configure(text=f"{len(self.jd_text.get('1.0', 'end-1c')):,} characters")

    def _clear(self) -> None:
        for value in self.vars.values():
            value.set("")
        self.source.set("manual_paste")
        self.work_mode.set("unknown")
        self.jd_text.delete("1.0", "end")
        self.notes.delete("1.0", "end")
        self.application_id = None
        self.run_button.configure(state="disabled")
        self._update_count()


def main() -> int:
    load_project_env()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit("Tk desktop support is unavailable. On Ubuntu run: sudo apt install python3-tk") from exc
    IntakeApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
