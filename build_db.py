#!/usr/bin/env python3
"""
build_db.py — generates index.yml for the VHBB fork.

CHANGES IN THIS REVISION (`date` simplified to match upstream)
--------------------------------------------------------------------
Earlier revisions of this script tried resolving `date` from each
repo's *first* eligible release (oldest non-draft release), on the
theory that the "New" tab should reflect original publish date rather
than latest-update date. In practice that produced nonsensical results
across a lot of entries — old/renamed tags, transferred repos, and
repos that re-published their initial release under a new tag all made
"first release" an unreliable signal, and it also diverged from how
the actual upstream catalog (robin994/NeoVitaDB-Catalog) does it.

Per explicit instruction, that logic is gone. `date` now simply mirrors
upstream's own behavior: it's always the *latest* eligible release's
publish date — the exact same release object already used to resolve
`version` and the download `url`. There is no more "first release" /
"historical original date" concept anywhere in this script, and no
separate `updated` field either (it would have been redundant with
`date` now that both mean the same thing).

Fixes kept from earlier revisions:
  1. No datetime.now() fallback, ever. Priority order for `date`:
        a) `published_at` of the latest eligible release (same release
           used for version/url)
        b) `created_at` of that release, if published_at is missing —
           logged as DATE_APPROX_RELEASE_CREATED
        c) --previous index.yml's existing value for this titleid, if
           any (e.g. repo has direct_url, or no releases at all) —
           logged as DATE_REUSED_PREVIOUS
        d) the repo's `pushed_at` (last activity) — clearly logged as
           DATE_APPROX_PUSHED_AT
        e) if none of the above are available, the entry is SKIPPED
           and written to review.csv instead of guessing.
  2. GitHub 403 rate-limit responses are detected explicitly (vs. a
     genuine 404/no-releases) and abort the run with a clear message
     instead of silently degrading every remaining entry.
  3. Category resolution (catalog's own field, normalized to singular,
     overrides.yml wins) cross-checks every resolved type against
     keywords in the entry's own description ("emulator", "wrapper/
     port", "utility"/"manager"/"installer") and writes any mismatch
     to review.csv instead of silently trusting the catalog.
  4. --previous lets you re-run incrementally: entries whose GitHub
     data resolves fine get refreshed; entries that fail resolution
     keep last known good data instead of being blanked or guessed.
  5. review.csv logs every non-primary date source and every type
     mismatch, plus a final pass flagging any entry whose resolved
     date equals the build's run date (DATE_SUSPECT_TODAY) — a strong
     signal a stale fallback slipped through and needs a human look.

REQUIRED OUTPUT SCHEMA (from src/homebrew.cpp, confirmed against the
fork's own working index.yml at vitagit-main/index.yml):

    - titleid: STRING            # 9-char id, required
      name: STRING                # required
      icon: STRING                 # bare filename, required
      version: STRING              # required
      author: STRING               # required
      type: game|port|emulator|utility   # required, singular, case-insensitive
      description: STRING          # required
      date: 'YYYY-MM-DD'           # required, exact shape (src/date.cpp substr)
                                     # — always the latest release's date, see above
      url: STRING                  # required, direct download URL
      long_description: STRING     # optional, falls back to description
      trailer: STRING               # optional
      screenshots: [STRING, ...]    # optional
      data: STRING                  # optional

Usage:
    GITHUB_TOKEN=xxxx python3 build_db.py \
        --catalog /path/to/NeoVitaDB-Catalog-main \
        --previous /path/to/last/known/good/index.yml \
        --out index.yml

    A GITHUB_TOKEN is *strongly* recommended — without one this WILL
    hit the 60/hour unauthenticated limit on any catalog with more
    than a couple dozen repos and abort (by design, rather than
    silently degrading data — see fix #1 above).

Output:
    index.yml    — the corrected database
    review.csv   — entries needing manual attention: unresolved dates,
                   catalog-category vs. description-keyword mismatches,
                   skipped entries and why
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

VALID_TYPES = {"game", "port", "emulator", "utility"}

TYPE_ALIASES = {
    "game": "game", "games": "game",
    "port": "port", "ports": "port",
    "emulator": "emulator", "emulators": "emulator",
    "utility": "utility", "utilities": "utility", "tools": "utility", "tool": "utility",
}

# Keyword heuristics used ONLY to flag likely mismatches for a human to
# review — never to silently override the catalog/overrides.yml value.
TYPE_KEYWORDS = {
    "emulator": [r"\bemulat", r"\bloader\b.*\bruns?\b", r"\bcfw\b", r"\becfw\b"],
    "utility":  [r"\butility\b", r"\bmanager\b", r"\binstaller\b", r"\bplugin\b", r"\bdriver\b", r"\btool\b"],
    "port":     [r"\bwrapper/port\b", r"\bsourceport\b", r"\bport of\b", r"\bnative port\b"],
    "game":     [r"\boriginal game\b", r"\bhomebrew game\b"],
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class RateLimited(Exception):
    pass


def api_get(path: str):
    req = urllib.request.Request(API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "vitagit-db-build")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = e.headers.get("X-RateLimit-Reset")
            reset_str = ""
            if reset:
                reset_str = f" (resets {datetime.fromtimestamp(int(reset), tz=timezone.utc).isoformat()})"
            raise RateLimited(f"GitHub API rate limit hit{reset_str}. "
                               f"{'Set GITHUB_TOKEN.' if not TOKEN else 'Wait for reset.'}")
        raise


def pick_release(repo: str, allow_prerelease: bool):
    """Latest eligible (non-draft, prerelease-respecting) release —
    used for version, url, AND date. Matches how upstream
    NeoVitaDB-Catalog's own build resolves all three from one call."""
    try:
        releases = api_get(f"/repos/{repo}/releases?per_page=20")
    except RateLimited:
        raise
    except urllib.error.HTTPError as e:
        log(f"  ! {repo}: releases unavailable ({e.code})")
        return None
    except urllib.error.URLError as e:
        log(f"  ! {repo}: network error ({e})")
        return None
    for rel in releases:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not allow_prerelease:
            continue
        return rel
    return None


def pick_asset(release: dict, pattern: str):
    for asset in release.get("assets", []):
        if fnmatch.fnmatch(asset["name"].lower(), pattern.lower()):
            return asset
    return None


def repo_pushed_at(repo: str) -> str | None:
    """Last-resort, clearly-approximate date source: repo's last push."""
    try:
        info = api_get(f"/repos/{repo}")
    except (RateLimited, urllib.error.HTTPError, urllib.error.URLError):
        return None
    pushed = info.get("pushed_at", "")
    return pushed[:10] if pushed else None


def resolve_type(entry: dict, overrides: dict) -> str | None:
    repo_key = entry.get("repo", "").lower()
    raw = overrides.get(repo_key) or entry.get("category", "")
    return TYPE_ALIASES.get(str(raw).strip().lower())


def flag_type_mismatch(resolved_type: str, description: str) -> str | None:
    """Return a suspected-better-type string if description keywords
    strongly suggest a different category than the one resolved, else None.
    Purely advisory — written to review.csv, never applied automatically."""
    desc_lower = (description or "").lower()
    hits = {}
    for t, patterns in TYPE_KEYWORDS.items():
        for p in patterns:
            if re.search(p, desc_lower):
                hits.setdefault(t, 0)
                hits[t] += 1
    if not hits:
        return None
    best = max(hits, key=hits.get)
    if best != resolved_type and hits[best] >= 1:
        return best
    return None


def load_overrides(catalog_root: Path) -> dict:
    path = catalog_root / "overrides.yml"
    if not path.exists():
        alt = Path("overrides.yml")
        path = alt if alt.exists() else path
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {str(k).lower(): str(v).lower() for k, v in data.items()}


def load_previous(path: str | None) -> dict:
    """titleid -> previous entry dict, for reuse when re-resolution fails."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        log(f"  ! --previous file {path} not found, ignoring")
        return {}
    data = yaml.safe_load(p.read_text()) or []
    return {e["titleid"]: e for e in data if "titleid" in e}


def build_entry(entry: dict, release: dict | None, asset: dict | None,
                 previous: dict, review_rows: list) -> tuple[dict | None, bool]:
    """Returns (entry_or_None, date_is_approximate).

    `release`/`asset` is the single source for version, url, AND date —
    the latest eligible release with a matching asset. No separate
    "first release" resolution anymore."""
    titleid = entry.get("titleid", "")
    name = entry.get("name", "<unnamed>")
    repo = entry.get("repo", "")
    if not titleid or len(titleid) != 9:
        log(f"  ! skipped {name}: invalid/missing titleid")
        review_rows.append([name, titleid, "SKIPPED", "invalid/missing titleid"])
        return None, False

    date_is_approx = False
    prev_entry = previous.get(titleid)

    if release is not None and asset is not None:
        version = release.get("tag_name") or entry.get("version") or "latest"
        url = asset["browser_download_url"]

        published = release.get("published_at") or ""
        if published:
            date = published[:10]
        else:
            created = release.get("created_at") or ""
            if created:
                date = created[:10]
                date_is_approx = True
                review_rows.append([name, titleid, "DATE_APPROX_RELEASE_CREATED",
                                     "latest release had no published_at; used its "
                                     f"created_at ({date}) instead"])
            else:
                date = None
    else:
        # No usable release (no releases at all, rate-limited, asset
        # pattern didn't match, or entry uses direct_url) — fall back to
        # whatever the entry/catalog provides, then --previous, then
        # repo activity. Never guess with today's date.
        version = entry.get("version") or (prev_entry.get("version") if prev_entry else "latest")
        url = entry.get("direct_url") or (prev_entry.get("url") if prev_entry else "")
        if not url:
            log(f"  ! skipped {name}: no release asset and no direct_url")
            review_rows.append([name, titleid, "SKIPPED", "no release asset and no direct_url"])
            return None, False
        date = None

    if not date and prev_entry and prev_entry.get("date"):
        date = prev_entry["date"]
        date_is_approx = True
        review_rows.append([name, titleid, "DATE_REUSED_PREVIOUS",
                             "no usable release date; kept last known date from --previous"])

    if not date:
        fallback = repo_pushed_at(repo) if repo else None
        if fallback:
            date = fallback
            date_is_approx = True
            review_rows.append([name, titleid, "DATE_APPROX_PUSHED_AT",
                                 f"no release date and no --previous entry; "
                                 f"used repo pushed_at ({fallback}) as an approximation"])

    if not date:
        log(f"  ! skipped {name}: no resolvable date (no release, no --previous, no repo pushed_at)")
        review_rows.append([name, titleid, "SKIPPED", "no resolvable date from any source"])
        return None, False

    description = (entry.get("description") or "").strip()
    if not description:
        log(f"  ! skipped {name}: empty description")
        review_rows.append([name, titleid, "SKIPPED", "empty description"])
        return None, False

    out = {
        "titleid": titleid,
        "name": entry["name"],
        "version": str(version),
        "author": entry["author"],
        "type": None,  # filled in by caller
        "description": description,
        "date": date,
        "url": url,
        "icon": f"{titleid}.png",
    }

    long_desc = entry.get("long_description") or description
    if long_desc != description:
        out["long_description"] = long_desc
    if entry.get("trailer"):
        out["trailer"] = entry["trailer"]
    if entry.get("screenshots"):
        out["screenshots"] = list(entry["screenshots"])
    if entry.get("data"):
        out["data"] = entry["data"]

    return out, date_is_approx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True, help="Path to NeoVitaDB-Catalog-main")
    ap.add_argument("--out", default="index.yml")
    ap.add_argument("--previous", default=None,
                     help="Path to a last-known-good index.yml, used to avoid "
                          "guessing dates/urls when GitHub resolution fails")
    ap.add_argument("--review-out", default="review.csv")
    ap.add_argument("--skip-network", action="store_true",
                     help="Debug only: skip GitHub release resolution, use catalog/--previous fields as-is")
    args = ap.parse_args()

    if not TOKEN and not args.skip_network:
        log("! WARNING: no GITHUB_TOKEN set. Unauthenticated GitHub API calls are "
            "limited to 60/hour — this build will very likely hit the rate limit "
            "partway through a catalog of any real size and abort (rather than "
            "silently degrade the rest of the data). Set GITHUB_TOKEN and re-run.")

    catalog_root = Path(args.catalog)
    apps_dir = catalog_root / "apps" / "vita"
    if not apps_dir.exists():
        sys.exit(f"Couldn't find {apps_dir} -- is --catalog pointing at NeoVitaDB-Catalog-main?")

    overrides = load_overrides(catalog_root)
    previous = load_previous(args.previous)

    entries = []
    for path in sorted(apps_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            entries.append(json.loads(path.read_text()))
        except json.JSONDecodeError as e:
            log(f"  ! skipping {path.name}: invalid JSON ({e})")

    log(f"{len(entries)} candidate entries")

    output = []
    seen_titleids = set()
    review_rows = []
    approx_count = 0

    for entry in entries:
        name = entry.get("name", "<unnamed>")
        repo = entry.get("repo", "")
        log(f"- {name} ({repo})")

        type_ = resolve_type(entry, overrides)
        if type_ not in VALID_TYPES:
            log(f"  ! skipped: unrecognized category '{entry.get('category')}' "
                f"(add an override in overrides.yml if this repo needs manual classification)")
            review_rows.append([name, entry.get("titleid", ""), "SKIPPED",
                                 f"unrecognized category '{entry.get('category')}'"])
            continue

        mismatch = flag_type_mismatch(type_, entry.get("description", ""))
        if mismatch:
            review_rows.append([name, entry.get("titleid", ""), "TYPE_SUSPECT",
                                 f"catalog says '{type_}' but description reads like '{mismatch}' — verify manually"])

        release = asset = None
        if not args.skip_network and repo:
            try:
                release = pick_release(repo, entry.get("prerelease", False))
                if release:
                    asset = pick_asset(release, entry.get("asset", "*.vpk"))
                    if not asset:
                        log(f"  ! no asset matching {entry.get('asset', '*.vpk')} in latest release, "
                            f"falling back")
                        release = None
                else:
                    log("  ! no usable GitHub release, falling back")
            except RateLimited as e:
                log(f"FATAL: {e}")
                log(f"Processed {len(output)} entries before hitting the rate limit. "
                    f"Re-run with GITHUB_TOKEN set, or pass --previous {args.out} to resume "
                    f"incrementally without losing already-resolved data.")
                sys.exit(2)

        built, approx = build_entry(entry, release, asset, previous, review_rows)
        if built is None:
            continue
        built["type"] = type_
        if approx:
            approx_count += 1

        if built["titleid"] in seen_titleids:
            log(f"  ! skipped: duplicate titleid {built['titleid']}")
            review_rows.append([name, built["titleid"], "SKIPPED", "duplicate titleid"])
            continue
        seen_titleids.add(built["titleid"])
        output.append(built)

    output = [e for e in output if e["titleid"] != "VHBB00001"]
    output.sort(key=lambda e: e["name"].lower())

    # Final sanity pass: an entry dated exactly the day this build ran is
    # only legitimate if the app genuinely shipped a release today —
    # anything else is almost certainly a stale fallback (e.g. a bad
    # --previous value, or an approximate pushed_at that happens to
    # coincide with today) that slipped through. Flag it rather than
    # silently trust it.
    today = datetime.now(timezone.utc).date().isoformat()
    for e in output:
        if e.get("date") == today:
            review_rows.append([e["name"], e["titleid"], "DATE_SUSPECT_TODAY",
                                 f"resolved date ({today}) matches the build run date — "
                                 f"verify this isn't a stale/approximate fallback"])

    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False,
                   default_flow_style=False, width=1000)

    # Also write a human-readable index.json alongside index.yml, with just
    # the field set BetterHomebrewBrowser's VitaDB parser reads.
    json_path = out_path.with_suffix(".json")
    json_fields = ("titleid", "name", "version", "author", "type",
                   "description", "date", "url", "icon", "data")
    json_output = [
        {k: e[k] for k in json_fields if k in e}
        for e in output
    ]
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(json_output, f, indent=4, ensure_ascii=False)
        f.write("\n")
    log(f"wrote {json_path} ({len(json_output)} entries)")

    if review_rows:
        with open(args.review_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "titleid", "flag", "detail"])
            w.writerows(review_rows)

    log(f"wrote {out_path} ({len(output)} entries, {approx_count} with an approximate/reused date)")
    if review_rows:
        log(f"wrote {args.review_out} ({len(review_rows)} entries need a human look)")


if __name__ == "__main__":
    main()
