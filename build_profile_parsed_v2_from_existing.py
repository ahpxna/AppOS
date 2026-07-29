from pathlib import Path
import hashlib
import re
import shutil
import unicodedata

OLD_RAW = Path("data/profile_raw")
OLD_PARSED = Path("data/profile_parsed")
NEW_RAW = Path("data/profile_sources_v2")
NEW_PARSED = Path("data/profile_parsed_v2")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    s = re.sub(r"\.[a-z0-9]+$", "", s)
    s = re.sub(r"\.[0-9a-f]{8,16}$", "", s)
    s = re.sub(r"[^a-z0-9à-ỹ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parsed_base_name(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"\.[0-9a-f]{8,16}$", "", stem)
    return stem

def build_old_parsed_index():
    parsed_by_key = {}
    for p in OLD_PARSED.glob("*.txt"):
        parsed_by_key[norm_name(parsed_base_name(p))] = p
    return parsed_by_key

def find_parsed_for_old_raw(old_raw: Path, parsed_by_key):
    key = norm_name(old_raw.stem)
    if key in parsed_by_key:
        return parsed_by_key[key]

    candidates = []
    for k, p in parsed_by_key.items():
        if key and (key in k or k in key):
            candidates.append((abs(len(k) - len(key)), p))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return None

def main():
    NEW_PARSED.mkdir(parents=True, exist_ok=True)

    parsed_by_key = build_old_parsed_index()

    old_hash_to_parsed = {}
    old_hash_to_raw = {}

    for old in OLD_RAW.iterdir():
        if not old.is_file() or old.name.startswith("."):
            continue
        digest = sha256_file(old)
        parsed = find_parsed_for_old_raw(old, parsed_by_key)
        old_hash_to_raw[digest] = old
        if parsed:
            old_hash_to_parsed[digest] = parsed

    copied = 0
    missing = []

    for new in sorted(NEW_RAW.rglob("*")):
        if not new.is_file() or new.name.startswith("."):
            continue

        digest = sha256_file(new)
        parsed = old_hash_to_parsed.get(digest)

        rel = new.relative_to(NEW_RAW)
        out = (NEW_PARSED / rel).with_suffix(".txt")
        out.parent.mkdir(parents=True, exist_ok=True)

        if parsed and parsed.exists():
            shutil.copy2(parsed, out)
            copied += 1
            print(f"OK   {rel} -> {out.relative_to(NEW_PARSED)}")
        else:
            old_raw = old_hash_to_raw.get(digest)
            missing.append((str(rel), old_raw.name if old_raw else "NO_OLD_RAW_HASH_MATCH"))
            print(f"MISS {rel}")

    print("")
    print("===== SUMMARY =====")
    print(f"New source files: {copied + len(missing)}")
    print(f"Parsed copied:    {copied}")
    print(f"Missing parsed:   {len(missing)}")

    if missing:
        print("")
        print("===== MISSING DETAILS =====")
        for rel, old_name in missing:
            print(f"- new={rel} | matched_old={old_name}")

if __name__ == "__main__":
    main()
