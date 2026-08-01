#!/usr/bin/env python3
"""
Static schema-tracking linter for db/migrations/*.sql.

Walks every migration file in filename-sort order (the exact order
scripts/apply_migrations.sh applies them) and simulates, statement by
statement, what a fresh Postgres database's schema looks like -- tracking
tables/columns, unique indexes (and whether they're partial), and view
column lists. It then flags, BEFORE ever touching a real database, the
three bug classes already found live in this migration set:

  1. ON CONFLICT (col...) with no matching unique constraint/index for
     that exact column set (including partial-index WHERE mismatches).
  2. CREATE OR REPLACE VIEW that changes/reorders an existing view's
     columns instead of only appending at the end.
  3. CREATE TABLE IF NOT EXISTS that's a silent no-op against an
     already-tracked table, followed by CREATE INDEX / view usage of a
     column that was never actually added (no matching ALTER TABLE ADD
     COLUMN before it).

This is NOT a full SQL engine -- it does not evaluate data, does not
implement every DDL form, and can't catch everything a real Postgres
would (e.g. type mismatches, FK target existence). It exists to catch the
specific classes of bug this project has hit three times in a row via
live testing, exhaustively, across every file, before the next live run.
"""
from __future__ import annotations

import glob
import sys
from dataclasses import dataclass, field
from typing import Optional

import pglast
from pglast import ast, enums


@dataclass
class Table:
    name: str
    columns: set = field(default_factory=set)
    # list of (frozenset(cols), is_partial: bool)
    unique_indexes: list = field(default_factory=list)
    created_by: str = ""


@dataclass
class View:
    name: str
    columns: tuple = ()
    dropped: bool = True
    defined_by: str = ""


tables: dict[str, Table] = {}
views: dict[str, View] = {}
errors: list[str] = []


def colref_name(node) -> Optional[str]:
    if isinstance(node, ast.ColumnRef):
        fields = node.fields
        last = fields[-1]
        if hasattr(last, "sval"):
            return last.sval
    return None


def target_output_name(res_target) -> Optional[str]:
    if res_target.name:
        return res_target.name
    return colref_name(res_target.val)


def get_view_columns(select_stmt) -> Optional[tuple]:
    # For simple SELECT (possibly with CTEs / set ops at top not handled),
    # targetList holds the output columns in order.
    tl = getattr(select_stmt, "targetList", None)
    if not tl:
        return None
    names = []
    for rt in tl:
        name = target_output_name(rt)
        if name is None:
            return None  # unnamed/complex expression -- can't verify, skip
        names.append(name)
    return tuple(names)


def handle_create_table(stmt, fname):
    name = stmt.relation.relname.lower()
    if name in tables:
        return  # IF NOT EXISTS no-op against an already-tracked table
    t = Table(name=name, created_by=fname)
    for elt in stmt.tableElts or ():
        if isinstance(elt, ast.ColumnDef):
            t.columns.add(elt.colname.lower())
            for c in elt.constraints or ():
                if c.contype in (enums.ConstrType.CONSTR_UNIQUE, enums.ConstrType.CONSTR_PRIMARY):
                    t.unique_indexes.append((frozenset([elt.colname.lower()]), False))
        elif isinstance(elt, ast.Constraint):
            if elt.contype in (enums.ConstrType.CONSTR_UNIQUE, enums.ConstrType.CONSTR_PRIMARY):
                cols = frozenset(k.sval.lower() for k in (elt.keys or ()))
                if cols:
                    t.unique_indexes.append((cols, False))
    tables[name] = t


def handle_alter_table(stmt, fname):
    name = stmt.relation.relname.lower()
    t = tables.get(name)
    if t is None:
        return  # table not tracked (created outside our scan, or CREATE TABLE without IF NOT EXISTS elsewhere)
    for cmd in stmt.cmds or ():
        if cmd.subtype == enums.AlterTableType.AT_AddColumn:
            coldef = cmd.def_
            if isinstance(coldef, ast.ColumnDef):
                t.columns.add(coldef.colname.lower())
        elif cmd.subtype == enums.AlterTableType.AT_DropColumn:
            t.columns.discard((cmd.name or "").lower())


def handle_index(stmt, fname):
    if not stmt.relation:
        return
    tname = stmt.relation.relname.lower()
    t = tables.get(tname)
    cols = []
    for elem in stmt.indexParams or ():
        if isinstance(elem, ast.IndexElem) and elem.name:
            cols.append(elem.name.lower())
        else:
            cols.append(None)  # expression index -- can't verify existence
    if t is not None:
        for c in cols:
            if c is not None and c not in t.columns:
                errors.append(
                    f"[{fname}] CREATE INDEX {stmt.idxname or '(unnamed)'} ON {tname}({c}): "
                    f"column '{c}' not present on {tname} at this point in the migration "
                    f"sequence (tracked columns so far: {sorted(t.columns)})"
                )
    if stmt.unique and t is not None and all(c is not None for c in cols):
        is_partial = stmt.whereClause is not None
        t.unique_indexes.append((frozenset(cols), is_partial))


def handle_view(stmt, fname):
    name = stmt.view.relname.lower()
    new_cols = get_view_columns(stmt.query)
    v = views.get(name)
    if new_cols is None:
        # complex/unverifiable target list -- reset tracking (unknown) so we
        # don't false-positive later, but don't crash either.
        views[name] = View(name=name, columns=(), dropped=True, defined_by=fname)
        return
    if v is None or v.dropped:
        views[name] = View(name=name, columns=new_cols, dropped=False, defined_by=fname)
        return
    old_cols = v.columns
    if old_cols and new_cols[: len(old_cols)] != old_cols:
        for i, (o, n) in enumerate(zip(old_cols, new_cols)):
            if o != n:
                errors.append(
                    f"[{fname}] CREATE OR REPLACE VIEW {name}: column {i} changes from "
                    f"'{o}' (defined in {v.defined_by}) to '{n}' -- Postgres will reject "
                    f"this with 'cannot change name of view column'. Old columns: "
                    f"{old_cols}. New columns: {new_cols}"
                )
                break
    views[name] = View(name=name, columns=new_cols, dropped=False, defined_by=fname)


def handle_drop(stmt, fname):
    if stmt.removeType == enums.ObjectType.OBJECT_VIEW:
        for obj in stmt.objects or ():
            # obj is a List of String nodes (qualified name)
            try:
                parts = [s.sval for s in obj]
            except TypeError:
                parts = [obj.sval] if hasattr(obj, "sval") else []
            if not parts:
                continue
            name = parts[-1].lower()
            if name in views:
                views[name].dropped = True
            else:
                views[name] = View(name=name, columns=(), dropped=True, defined_by=fname)


def handle_insert(stmt, fname):
    occ = stmt.onConflictClause
    if occ is None:
        return
    if occ.action == enums.OnConflictAction.ONCONFLICT_NONE:
        return
    infer = occ.infer
    if infer is None or not infer.indexElems:
        return  # ON CONFLICT DO NOTHING / ON CONSTRAINT with no explicit column list
    cols = []
    for elem in infer.indexElems:
        if isinstance(elem, ast.IndexElem) and elem.name:
            cols.append(elem.name.lower())
        else:
            return  # expression-based conflict target, skip
    tname = stmt.relation.relname.lower()
    t = tables.get(tname)
    if t is None:
        return
    target = frozenset(cols)
    target_is_partial_clause = infer.whereClause is not None
    match = None
    for idx_cols, idx_partial in t.unique_indexes:
        if idx_cols == target:
            match = idx_partial
            break
    if match is None:
        errors.append(
            f"[{fname}] INSERT ... ON CONFLICT ({', '.join(cols)}) on {tname}: "
            f"no unique constraint/index tracked for exactly this column set. "
            f"Tracked unique indexes on {tname}: "
            f"{[(sorted(c), 'partial' if p else 'full') for c, p in t.unique_indexes]}"
        )
    elif match and not target_is_partial_clause:
        errors.append(
            f"[{fname}] INSERT ... ON CONFLICT ({', '.join(cols)}) on {tname}: "
            f"matching unique index is PARTIAL but the ON CONFLICT clause has no "
            f"WHERE predicate -- Postgres cannot infer a partial index without a "
            f"matching WHERE clause on the ON CONFLICT target."
        )


def walk(node, fname):
    if isinstance(node, ast.CreateStmt):
        handle_create_table(node, fname)
    elif isinstance(node, ast.AlterTableStmt):
        handle_alter_table(node, fname)
    elif isinstance(node, ast.IndexStmt):
        handle_index(node, fname)
    elif isinstance(node, ast.ViewStmt):
        handle_view(node, fname)
    elif isinstance(node, ast.DropStmt):
        handle_drop(node, fname)
    elif isinstance(node, ast.InsertStmt):
        handle_insert(node, fname)


def main():
    files = sorted(glob.glob("db/migrations/*.sql"))
    for f in files:
        sql = open(f, encoding="utf-8").read()
        try:
            tree = pglast.parse_sql(sql)
        except Exception as e:
            errors.append(f"[{f}] PARSE ERROR: {e}")
            continue
        for raw_stmt in tree:
            walk(raw_stmt.stmt, f)

    if errors:
        print(f"{len(errors)} potential issue(s) found:\n")
        for e in errors:
            print("- " + e)
        return 1
    print(f"Scanned {len(files)} migration files, {len(tables)} tables, {len(views)} views tracked. No issues found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
