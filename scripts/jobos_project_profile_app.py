#!/usr/bin/env python3
"""Local desktop editor for JobOS's verified project registry.

Run ``python scripts/jobos_project_profile_app.py``.  It never opens a
browser, database connection, or LLM.  The saved JSON is later used to bind
parsed profile/repository records and resume bullets to the correct immutable
Word-template project block.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ModuleNotFoundError as exc:
    # Keep --print-skeleton usable on headless CI/minimal Ubuntu.  The GUI
    # path below exits with a useful install command before this placeholder
    # base class could ever be instantiated.
    tk = None
    messagebox = None

    class _UnavailableTtk:
        Frame = object

    ttk = _UnavailableTtk()
    TK_ERROR = exc
else:
    TK_ERROR = None


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from services.common.project_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    ProjectRegistryError,
    load_registry,
    map_parsed_records,
    save_registry,
)


LIST_FIELDS = {
    "asset_title_aliases": "Asset/title aliases (one per line)",
    "technology_tags": "Technologies/tools (one per line)",
    "skill_tags": "Skills (one per line)",
    "jd_keyword_tags": "JD keywords to match (one per line)",
    "allowed_facts": "Verified facts allowed in resume/cover letter (one per line)",
    "do_not_overclaim": "Do-not-overclaim boundaries (one per line)",
    "evidence_locations": "Evidence paths / filenames (one per line)",
    "source_urls": "Source/repository URLs (one per line)",
}


class ProjectProfileApp(ttk.Frame):
    """Edits only user-owned project facts; catalog IDs and slots are readonly."""

    def __init__(self, master: tk.Tk, registry_path: Path) -> None:
        super().__init__(master, padding=14)
        self.master = master
        self.registry_path = registry_path
        self.registry = load_registry(registry_path)
        self.current_index = 0
        self.variables: dict[str, tk.StringVar] = {}
        self.texts: dict[str, tk.Text] = {}
        self.grid(sticky="nsew")
        master.title("JobOS — Verified Project Profiles")
        master.minsize(980, 720)
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        self._build()
        self._load_project(0)

    def _build(self) -> None:
        ttk.Label(self, text="Verified project source of truth", font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            self,
            text=("Only you edit this data. Project ID and Word-template slots are locked; later parsers map evidence "
                  "by your aliases and leave unclear matches unmapped."),
            wraplength=900,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        sidebar = ttk.LabelFrame(self, text="Approved template project blocks", padding=8)
        sidebar.grid(row=2, column=0, rowspan=4, sticky="nsw", padx=(0, 12))
        self.project_list = tk.Listbox(sidebar, exportselection=False, height=8, width=29)
        self.project_list.pack(fill="y")
        for project in self.registry["projects"]:
            self.project_list.insert("end", f"{project['resume_slot_start']}-{project['resume_slot_start'] + 1}  {project['display_name']}")
        self.project_list.bind("<<ListboxSelect>>", self._switch_project)

        identity = ttk.LabelFrame(self, text="Verified template identity (shown in Word; LLM cannot change it)", padding=8)
        identity.grid(row=2, column=1, sticky="ew")
        self._add_entry(identity, 0, "Project ID / slots (locked)", "locked_identity", state="readonly")
        self._add_entry(identity, 1, "Exact project title", "template_title")
        self._add_entry(identity, 2, "Exact date range", "template_date")
        self._add_entry(identity, 3, "Exact GitHub URL", "template_github_url")

        overview = ttk.LabelFrame(self, text="Project context", padding=8)
        overview.grid(row=3, column=1, sticky="ew", pady=(10, 0))
        self._add_entry(overview, 0, "Scope/status (e.g. active_research, academic_project)", "scope_status")
        self._add_text(overview, 1, "One-paragraph verified project summary", "project_summary", height=4)

        notebook = ttk.Notebook(self)
        notebook.grid(row=4, column=1, sticky="nsew", pady=(10, 0))
        self.rowconfigure(4, weight=1)
        for fields in (
            ("asset_title_aliases", "technology_tags", "skill_tags", "jd_keyword_tags"),
            ("allowed_facts", "do_not_overclaim"),
            ("evidence_locations", "source_urls"),
        ):
            frame = ttk.Frame(notebook, padding=8)
            notebook.add(frame, text=("Matching" if fields[0] == "asset_title_aliases" else "Claims" if fields[0] == "allowed_facts" else "Evidence"))
            frame.columnconfigure(0, weight=1)
            for row, field in enumerate(fields):
                self._add_text(frame, row, LIST_FIELDS[field], field, height=7)

        actions = ttk.Frame(self)
        actions.grid(row=5, column=1, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="Save project profile", command=self._save).pack(side="left")
        ttk.Button(actions, text="Export mapping preview", command=self._show_preview).pack(side="left", padx=8)
        ttk.Button(actions, text="Reload saved file", command=self._reload).pack(side="right")
        self.status = ttk.Label(self, text=f"Registry: {self.registry_path}", wraplength=760)
        self.status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _add_entry(self, parent: ttk.Widget, row: int, label: str, key: str, state: str = "normal") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        variable = self.variables.setdefault(key, tk.StringVar())
        ttk.Entry(parent, textvariable=variable, state=state).grid(row=row, column=1, sticky="ew", pady=2, padx=(8, 0))
        parent.columnconfigure(1, weight=1)

    def _add_text(self, parent: ttk.Widget, row: int, label: str, key: str, height: int) -> None:
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=0, sticky="nsew", pady=3)
        ttk.Label(holder, text=label).pack(anchor="w")
        text = tk.Text(holder, height=height, wrap="word")
        text.pack(fill="both", expand=True, pady=(2, 0))
        self.texts[key] = text
        parent.rowconfigure(row, weight=1)

    def _switch_project(self, _event: object) -> None:
        selected = self.project_list.curselection()
        if not selected:
            return
        self._write_current_to_memory()
        self._load_project(selected[0])

    def _load_project(self, index: int) -> None:
        self.current_index = index
        project = self.registry["projects"][index]
        self.variables["locked_identity"].set(f"{project['project_id']}  |  slots {project['resume_slot_start']}-{project['resume_slot_start'] + 1}")
        for field in ("template_title", "template_date", "template_github_url", "scope_status"):
            self.variables[field].set(project.get(field, ""))
        self._write_text("project_summary", project.get("project_summary", ""))
        for field in LIST_FIELDS:
            self._write_text(field, "\n".join(project.get(field, [])))
        self.project_list.selection_clear(0, "end")
        self.project_list.selection_set(index)

    def _write_text(self, key: str, value: str) -> None:
        self.texts[key].delete("1.0", "end")
        self.texts[key].insert("1.0", value)

    def _lines(self, key: str) -> list[str]:
        return [line.strip() for line in self.texts[key].get("1.0", "end-1c").splitlines() if line.strip()]

    def _write_current_to_memory(self) -> None:
        project = self.registry["projects"][self.current_index]
        for field in ("template_title", "template_date", "template_github_url", "scope_status"):
            project[field] = self.variables[field].get().strip()
        project["project_summary"] = self.texts["project_summary"].get("1.0", "end-1c").strip()
        for field in LIST_FIELDS:
            project[field] = self._lines(field)

    def _save(self) -> None:
        self._write_current_to_memory()
        try:
            saved = save_registry(self.registry, self.registry_path)
        except (ProjectRegistryError, OSError) as exc:
            messagebox.showerror("JobOS project profile", str(exc))
            return
        self.status.configure(text=f"Saved verified project profiles: {saved}")
        messagebox.showinfo("JobOS project profile", "Saved. Future profile assets can now be mapped by these aliases.")

    def _reload(self) -> None:
        try:
            self.registry = load_registry(self.registry_path)
        except ProjectRegistryError as exc:
            messagebox.showerror("JobOS project profile", str(exc))
            return
        self._load_project(0)
        self.status.configure(text=f"Reloaded: {self.registry_path}")

    def _show_preview(self) -> None:
        self._write_current_to_memory()
        preview = {
            project["project_id"]: {
                "slots": [project["resume_slot_start"], project["resume_slot_start"] + 1],
                "aliases": project["asset_title_aliases"],
                "keywords": project["jd_keyword_tags"],
                "technology_tags": project["technology_tags"],
            }
            for project in self.registry["projects"]
        }
        dialog = tk.Toplevel(self.master)
        dialog.title("Project mapping preview")
        text = tk.Text(dialog, width=110, height=28, wrap="none")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", json.dumps(preview, ensure_ascii=False, indent=2))
        text.configure(state="disabled")


def main() -> int:
    parser = argparse.ArgumentParser(description="Edit JobOS's local verified project profile registry.")
    parser.add_argument("--path", type=Path, default=DEFAULT_REGISTRY_PATH, help="Local JSON registry path")
    parser.add_argument("--print-skeleton", action="store_true", help="Print the initial six-project JSON and exit")
    parser.add_argument("--map-input", type=Path, help="JSON array (or {records: [...]}) of parsed/aggregated records")
    parser.add_argument("--map-output", type=Path, help="Where to write records with jobos_project_mapping")
    args = parser.parse_args()
    if args.print_skeleton:
        print(json.dumps(load_registry(args.path), ensure_ascii=False, indent=2))
        return 0
    if args.map_input:
        if not args.map_output:
            parser.error("--map-output is required with --map-input")
        try:
            raw = json.loads(args.map_input.expanduser().read_text(encoding="utf-8"))
            records = raw.get("records", raw.get("assets", [])) if isinstance(raw, dict) else raw
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                raise ProjectRegistryError("Mapping input must be a JSON array of objects or {records: [...]}.")
            mapped = map_parsed_records(records, load_registry(args.path))
            output = args.map_output.expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(mapped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError, ProjectRegistryError) as exc:
            raise SystemExit(f"Project mapping blocked: {exc}") from exc
        print(f"Mapped {len(mapped)} records: {args.map_output.expanduser()}")
        return 0
    if tk is None:
        raise SystemExit("Tk desktop support is unavailable. On Ubuntu run: sudo apt install python3-tk") from TK_ERROR
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit("Tk desktop support is unavailable. On Ubuntu run: sudo apt install python3-tk") from exc
    ProjectProfileApp(root, args.path.expanduser())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
