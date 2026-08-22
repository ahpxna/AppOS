#!/usr/bin/env python3
"""Read-only GitHub repository inventory for the isolated audit workflow."""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def get_json(url: str, token: str | None) -> list[dict]:
    """Call GitHub's read-only repository API; a token is never written to output."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "jobos-repo-audit/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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
    return [{
        "full_name": repo["full_name"], "clone_url": repo["clone_url"],
        "default_branch": repo.get("default_branch"), "private": bool(repo.get("private")),
        "fork": bool(repo.get("fork")), "archived": bool(repo.get("archived")),
        "updated_at": repo.get("updated_at"), "pushed_at": repo.get("pushed_at"),
        "language": repo.get("language"), "topics": repo.get("topics") or [],
        "description": repo.get("description"), "homepage": repo.get("homepage"),
        "html_url": repo.get("html_url"),
    } for repo in repos]


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
