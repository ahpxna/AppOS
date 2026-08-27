#!/usr/bin/env python3
"""Read-only GitHub repository inventory for the isolated audit workflow."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.common.value_coercion import coerce_bool


def get_json(url: str, token: str | None) -> Any:
    """Call GitHub's read-only repository API; a token is never written to output."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "jobos-repo-audit/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))



def head_sha(repo_full_name: str, branch: str, token: str | None) -> str | None:
    """Return the exact default-branch HEAD SHA; metadata timestamps are not revision identity."""
    owner, name = repo_full_name.split("/", 1)
    url = (
        "https://api.github.com/repos/" + urllib.parse.quote(owner) + "/" + urllib.parse.quote(name)
        + "/commits/" + urllib.parse.quote(branch, safe="")
    )
    try:
        payload = get_json(url, token)
    except Exception:
        return None
    sha = str(payload.get("sha") or "") if isinstance(payload, dict) else ""
    return sha if len(sha) == 40 else None

def inventory(*, github_user: str | None, token: str | None) -> list[dict]:
    """Return a minimal manifest for public or token-authorised repositories.

    Repository facts remain a separate evidence source; this function does not
    clone, edit, or convert source code into profile chunks.
    """
    if github_user:
        endpoint = "https://api.github.com/users/" + urllib.parse.quote(github_user) + "/repos?per_page=100&sort=updated"
    elif token:
        endpoint = "https://api.github.com/user/repos?affiliation=owner,collaborator,organization_member&per_page=100&sort=updated"
    else:
        raise ValueError("Provide --github-user for public repos or set GH_TOKEN for your accessible repos.")
    repos = get_json(endpoint, token)
    if not isinstance(repos, list):
        raise ValueError("GitHub repository inventory response must be a JSON list.")
    result = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("full_name") or "").strip()
        clone_url = str(repo.get("clone_url") or "").strip()
        html_url = str(repo.get("html_url") or "").strip()
        if (not full_name or full_name.count("/") != 1
                or not clone_url.startswith(("https://", "http://"))
                or not html_url.startswith(("https://", "http://"))):
            continue
        branch = str(repo.get("default_branch") or "main").strip() or "main"
        topics_raw = repo.get("topics")
        topics = [item.strip() for item in topics_raw if isinstance(item, str) and item.strip()] if isinstance(topics_raw, list) else []
        result.append({
        "full_name": full_name, "clone_url": clone_url,
        "default_branch": branch, "revision_sha": head_sha(full_name, branch, token),
        "private": coerce_bool(repo.get("private")),
        "fork": coerce_bool(repo.get("fork")), "archived": coerce_bool(repo.get("archived")),
        "updated_at": repo.get("updated_at"), "pushed_at": repo.get("pushed_at"),
        "language": repo.get("language"), "topics": topics,
        "description": repo.get("description"), "homepage": repo.get("homepage"),
        "html_url": html_url,
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only GitHub repo inventory")
    parser.add_argument("--github-user", help="Public GitHub account; omit to list repos accessible to GH_TOKEN.")
    parser.add_argument("--write-manifest", help="Write the reviewed inventory locally; no GitHub write occurs.")
    args = parser.parse_args()
    repos = inventory(github_user=args.github_user, token=os.getenv("GH_TOKEN"))
    payload = {"source": "github_read_only_api", "repo_count": len(repos), "repos": repos}
    print(json.dumps(payload, indent=2))
    if args.write_manifest:
        output = Path(args.write_manifest)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
