#!/usr/bin/env python3
"""Ingest AI-reviewer findings from a GitHub PR into the local SQLite DB.

Usage:
    ./ingest.py --pr 1600                         # default repo: OWNER/REPO
    ./ingest.py --pr 1600 --repo owner/name
    ./ingest.py --pr 1600 --dry-run               # show what would change; don't write

Design principles (see README for background):
- **Nothing is lost.** Findings/body/PR-level comments are frozen on first
  capture. Retractions mark `status='retracted'`; the row and the last body
  snapshot stay. Body edits appear in `*_body_history` with the captured
  timestamp so the evolution (e.g. CodeRabbit "review in progress" → final
  summary) is reconstructable.
- **Idempotent.** Re-running on the same PR upserts metadata and appends
  new events/history rows, but never rewrites a finding's body or its
  children. Running after every reviewer reply is safe and cheap.
- **Uses the `gh` CLI** for both REST and GraphQL — inherits your auth.
- **Auto-classifies verdicts** by reply prefix ("Fixed in <SHA>" ->
  valid_fixed, "Not applicable" -> false_positive). Unclassified stays
  'pending' for manual review.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
DB_PATH = TOOL_DIR / "db" / "reviews.db"
SCHEMA_PATH = TOOL_DIR / "schema.sql"

# Normalize bot logins to a canonical reviewer key.
# REST returns "cursor[bot]"; GraphQL returns "cursor". Both map to 'cursor'.
# Seer's actual login is `sentry[bot]` (verified against vlad-ko/sdk-playground
# PR #84 where Seer posted an inline finding). Prior guesses — "sentry-io[bot]"
# and "seer-by-sentry[bot]" — were wrong and have been removed; the app slug is
# `sentry` and the bot login is `sentry[bot]`.
# Greptile's GitHub App slug is `greptile-apps` (https://github.com/apps/greptile-apps),
# so REST returns `greptile-apps[bot]` and GraphQL returns `greptile-apps`.
REVIEWER_NORMALIZE = {
    "cursor[bot]": "cursor",
    "cursor": "cursor",
    "coderabbitai[bot]": "coderabbitai",
    "coderabbitai": "coderabbitai",
    "sentry[bot]": "seer",
    "sentry": "seer",
    "greptile-apps[bot]": "greptile",
    "greptile-apps": "greptile",
}

# Set of normalized reviewer keys recognized as "AI reviewer" by the upsert
# paths. Derived from REVIEWER_NORMALIZE so adding a new bot to that dict is
# the only change needed when onboarding a new reviewer.
KNOWN_REVIEWERS = frozenset(REVIEWER_NORMALIZE.values())

# Severity extraction — look for these tokens near the top of the body.
SEVERITY_TOKENS = ["critical", "high", "major", "medium", "minor", "low"]

# Greptile uses image-alt P0/P1/P2 badges instead of text severity tokens.
# Map to the canonical severity vocabulary so cross-reviewer comparison
# queries stay consistent. Parse the FIRST badge in the body — that's the
# finding's own severity. P0 = critical, P1 = major, P2 = minor.
GREPTILE_SEVERITY_RE = re.compile(r'alt="(P[0-2])"')
GREPTILE_SEVERITY_MAP = {
    "P0": "critical",
    "P1": "major",
    "P2": "minor",
}

# Conventional commit prefix regex (matches the repo's pr-title-check).
CONVENTIONAL_PREFIX_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|perf|test|style)(?:\([a-z0-9-]+\))?!?:",
    re.IGNORECASE,
)

FIX_SHA_RE = re.compile(r"\bFixed in `?([0-9a-f]{7,40})\b")

# Sentry's auto-resolve marker — emitted as a prefix when Sentry's
# bug-prediction model thinks a later commit fixed the underlying issue.
# Example: "*Resolved in [`abc1234`](https://github.com/.../commit/abc1234)*"
# Note: this CAN be a false positive on no-op merge commits — but the
# ingester's job is to capture the signal Sentry emitted, not second-guess
# it. Manual re-classification handles the edge cases retrospectively.
SENTRY_RESOLVED_RE = re.compile(
    r"^\s*\*?\s*Resolved in\s*\[?\s*`?([0-9a-f]{7,40})", re.IGNORECASE
)

# Anywhere-in-body pattern variants. The original classify_from_reply
# used startswith() which missed any reply that didn't lead with the
# canonical phrase (e.g. an issue-level "## Reviewer-finding follow-up
# — abc1234\n\n..." preface, or a "Fixed in abc1234. <explanation>"
# reply mid-paragraph). These match anywhere in the body.
NOT_APPLICABLE_RE = re.compile(r"\bNot applicable\b", re.IGNORECASE)
FOLLOW_UP_RE = re.compile(r"\bReviewer-finding follow-up\b", re.IGNORECASE)
DEFERRED_RE = re.compile(r"\bDeferred\b", re.IGNORECASE)


def is_bot_login(login: str | None) -> bool:
    """True for any [bot]-suffixed GitHub login. Accommodates the full
    AI-reviewer matrix (coderabbitai, sentry, greptile-apps, cursor,
    claude, ...) without an explicit allow-list, since the suffix is
    GitHub's own bot indicator."""
    return bool(login) and login.endswith("[bot]")

# Structural body-content detectors. Each returns 1/0 for a bool column.
# Order: widest match first, collapse to 1 on any hit.
SUGGESTED_FIX_PATTERNS = [
    r"<summary>\s*Suggested\s+fix\s*</summary>",
    r"<summary>\s*Suggested\s+hardening",
    r"💡\s*(?:Proposed|Suggested)\s+fix",
    r"```suggestion\b",
    r"<summary>\s*📝\s*Committable\s+suggestion",
]
CODE_DIFF_PATTERNS = [
    r"```diff\b",
    r"^\s*\+\+\+\s|^\s*---\s",          # unified diff markers at line start
]
# CodeRabbit's verification-script signature — only CodeRabbit emits this today,
# but match loosely in case Cursor/Seer adopt a similar pattern.
ANALYSIS_SCRIPT_PATTERNS = [
    r"🏁\s*Script\s+executed:",
    r"🧩\s*Analysis\s+chain",
]

# Columns that run_migrations() ensures exist on `findings` (added over time).
# Order matters only for readability — SQLite appends columns regardless.
FINDINGS_ADDED_COLUMNS: list[tuple[str, str]] = [
    ("first_seen_at",       "TEXT"),
    ("last_seen_at",        "TEXT"),
    ("status",              "TEXT NOT NULL DEFAULT 'active'"),
    ("has_suggested_fix",   "INTEGER NOT NULL DEFAULT 0"),
    ("has_code_diff",       "INTEGER NOT NULL DEFAULT 0"),
    ("has_analysis_script", "INTEGER NOT NULL DEFAULT 0"),
    ("cluster_key",         "TEXT"),
]

# Columns added to pr_issue_comments to distinguish source surfaces
# (issue comment vs PR review body).
PR_ISSUE_COMMENTS_ADDED_COLUMNS: list[tuple[str, str]] = [
    ("source",       "TEXT NOT NULL DEFAULT 'issue_comment'"),
    ("review_state", "TEXT"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def gh(args, input_data=None):
    """Run `gh` CLI and parse JSON output. Raises on non-zero exit."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        input=input_data,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


# ---------------------------------------------------------------------------
# GitHub fetchers
# ---------------------------------------------------------------------------

def fetch_pr(repo: str, pr_number: int) -> dict:
    return gh(["api", f"repos/{repo}/pulls/{pr_number}"])


def fetch_pr_commits(repo: str, pr_number: int) -> list:
    return gh(["api", "--paginate", f"repos/{repo}/pulls/{pr_number}/commits"])


def fetch_pr_review_comments(repo: str, pr_number: int) -> list:
    """Inline review comments on the PR (the ones tied to a file/line)."""
    return gh(["api", "--paginate", f"repos/{repo}/pulls/{pr_number}/comments"])


def fetch_pr_issue_comments(repo: str, pr_number: int) -> list:
    """PR-level (non-inline) comments — CodeRabbit's summary lives here."""
    return gh(["api", "--paginate", f"repos/{repo}/issues/{pr_number}/comments"])


def fetch_pr_reviews(repo: str, pr_number: int) -> list:
    """Submitted PR reviews — a distinct surface from issue comments.
    CodeRabbit posts its Nitpick-level findings inside the review body
    (not as inline comments), so this surface MUST be ingested or those
    findings go unrecorded entirely (observed on PR #1606)."""
    return gh(["api", "--paginate", f"repos/{repo}/pulls/{pr_number}/reviews"])


def fetch_review_threads(repo: str, pr_number: int) -> list:
    """GraphQL — the only way to get each thread's isResolved flag."""
    owner, name = repo.split("/", 1)
    query = f'''
    {{
      repository(owner: "{owner}", name: "{name}") {{
        pullRequest(number: {pr_number}) {{
          reviewThreads(first: 100) {{
            nodes {{
              id
              isResolved
              comments(first: 50) {{
                nodes {{
                  databaseId
                  author {{ login }}
                  body
                  createdAt
                }}
              }}
            }}
          }}
        }}
      }}
    }}
    '''
    result = gh(["api", "graphql", "-f", f"query={query}"])
    return result["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def normalize_reviewer(login: str | None) -> str | None:
    if not login:
        return None
    return REVIEWER_NORMALIZE.get(login)


def author_role(login: str | None) -> str:
    """Coarse role for finding_events.author_role / pr_issue_comments.author_role.
    Bot logins map to their normalized reviewer key; anything else is 'user'."""
    reviewer = normalize_reviewer(login)
    if reviewer:
        return reviewer
    if not login:
        return "other"
    return "user"


def extract_severity(body: str) -> str | None:
    """Heuristic severity extraction from the top ~500 chars of the body.

    Greptile uses image-alt P0/P1/P2 badges instead of text severity tokens,
    so we check that first (case-sensitive — the alt attribute is uppercase
    by spec). If a badge is present, it is the authoritative severity for
    Greptile findings — fall through to the text-token search only if no
    badge matches.
    """
    head = body[:500]
    badge = GREPTILE_SEVERITY_RE.search(head)
    if badge:
        return GREPTILE_SEVERITY_MAP[badge.group(1)]
    head_lower = head.lower()
    for token in SEVERITY_TOKENS:
        if token in head_lower:
            return token
    return None


_LEADING_BADGE_RE = re.compile(r'^<a[^>]*><img[^>]*></a>\s*')


def extract_title(body: str) -> str:
    """Pull a short heading out of the first non-empty line.

    Strips a leading ``<a><img></a>`` severity badge before extracting,
    so Greptile findings (which start with ``<img alt="P2">``) surface
    the human-readable title rather than the badge HTML. Generic enough
    that any reviewer using the same prefix pattern degrades cleanly.
    """
    for line in body.splitlines():
        stripped = _LEADING_BADGE_RE.sub("", line.strip()).lstrip("#*_ ").rstrip("_* ")
        if stripped:
            return stripped[:200]
    return ""


def extract_conventional_prefix(pr_title: str) -> str | None:
    m = CONVENTIONAL_PREFIX_RE.match(pr_title)
    return m.group(1).lower() if m else None


def extract_fix_sha(reply_body: str) -> str | None:
    m = FIX_SHA_RE.search(reply_body or "")
    return m.group(1) if m else None


def _any_pattern(body: str, patterns: list[str]) -> int:
    """Return 1 if any regex in `patterns` matches `body`, else 0."""
    for p in patterns:
        if re.search(p, body, re.MULTILINE):
            return 1
    return 0


def extract_structural_flags(body: str) -> dict[str, int]:
    return {
        "has_suggested_fix":   _any_pattern(body, SUGGESTED_FIX_PATTERNS),
        "has_code_diff":       _any_pattern(body, CODE_DIFF_PATTERNS),
        "has_analysis_script": _any_pattern(body, ANALYSIS_SCRIPT_PATTERNS),
    }


def compute_cluster_key(file_path: str | None, line: int | None) -> str | None:
    """Bucket findings into 10-line regions of the same file so cross-reviewer
    overlap can be queried with a simple GROUP BY. File-less findings or
    unknown-line findings get None (they won't cluster with anything)."""
    if not file_path or line is None:
        return None
    return f"{file_path}:{line // 10}"


def classify_from_reply(reply_body: str | None) -> str:
    """Auto-classify a verdict from the reply's body. Patterns match
    anywhere in the body — the original startswith() check missed
    multi-paragraph replies and prefaced acknowledgments. Order matters:
    the strongest signal (an explicit "Fixed in <sha>") wins over a
    looser one (a generic "follow-up" mention)."""
    if not reply_body:
        return "pending"
    if FIX_SHA_RE.search(reply_body):
        return "valid_fixed"
    if NOT_APPLICABLE_RE.search(reply_body):
        return "false_positive"
    if DEFERRED_RE.search(reply_body) or FOLLOW_UP_RE.search(reply_body):
        return "valid_deferred"
    return "pending"


def classify_finding(
    finding_body: str | None,
    finding_created_at: str | None,
    inline_replies: list,
    thread_resolved: bool,
    issue_comments: list,
    pr_author: str | None,
    pr_state: str,
) -> dict:
    """Layered finding classifier — broader than classify_from_reply.

    Signals checked, in priority order:
      1. Sentry "*Resolved in [<sha>]*" body prefix on the finding itself
         (Sentry's auto-resolve marker)
      2. Inline non-bot reply with "Fixed in <sha>" / "Not applicable"
         / "Reviewer-finding follow-up" patterns (the original ingester
         only honored the PR author, missing co-author replies)
      3. GraphQL reviewThread.isResolved=true (no matching reply needed —
         resolution itself is a strong addressed signal, e.g., when a
         reviewer auto-resolves after seeing the underlying code change
         or when a maintainer manually marks resolved)
      4. Issue-level comment by the PR author after finding_created_at
         containing one of the canonical patterns (covers review-body
         findings that have no thread to resolve and no inline reply
         chain — these need a separate issue-level acknowledgment per
         the wizard SKILL's protocol)

    Returns a dict with all the columns we'd want to write into verdicts:
      verdict, fix_commit_sha, reply_body, reply_at, resolved_at, category, notes
    `verdict` may be 'pending' if no signal matched.
    """
    # (1) Sentry auto-resolve marker on the finding's body
    if finding_body:
        m = SENTRY_RESOLVED_RE.match(finding_body.strip())
        if m:
            return {
                "verdict": "valid_fixed",
                "fix_commit_sha": m.group(1),
                "reply_body": finding_body[:300],
                "reply_at": None,
                "resolved_at": None,
                "category": "sentry-auto-resolved",
                "notes": "Auto-classified: Sentry '*Resolved in <sha>*' body prefix.",
            }

    # (2) Inline non-bot replies — ANY non-bot reply, not just PR author
    for r in inline_replies:
        login = (r.get("user") or {}).get("login")
        if is_bot_login(login):
            continue
        body = r.get("body") or ""
        if FIX_SHA_RE.search(body):
            sha_match = FIX_SHA_RE.search(body)
            return {
                "verdict": "valid_fixed",
                "fix_commit_sha": sha_match.group(1) if sha_match else None,
                "reply_body": body,
                "reply_at": r.get("created_at"),
                "resolved_at": r.get("created_at") if thread_resolved else None,
                "category": "inline-reply-fixed-in",
                "notes": "Auto-classified: inline non-bot reply with 'Fixed in <sha>'.",
            }
        if NOT_APPLICABLE_RE.search(body):
            return {
                "verdict": "false_positive",
                "fix_commit_sha": None,
                "reply_body": body,
                "reply_at": r.get("created_at"),
                "resolved_at": r.get("created_at") if thread_resolved else None,
                "category": "inline-reply-not-applicable",
                "notes": "Auto-classified: inline non-bot reply with 'Not applicable'.",
            }

    # (3) Thread-resolved with no canonical reply pattern. This is the
    # case Sentry's body-marker doesn't cover and the reply chain doesn't
    # have a "Fixed in" — but the thread is closed, so it's been
    # acknowledged in some way (resolved by the maintainer, auto-resolved
    # by the reviewer, auto-resolved by GitHub's autofix flow, etc.).
    if thread_resolved:
        # Try to capture a SHA from any non-bot reply, even without the
        # exact 'Fixed in' phrase, for retrospective traceability.
        sha = None
        for r in inline_replies:
            if is_bot_login((r.get("user") or {}).get("login")):
                continue
            sha_match = FIX_SHA_RE.search(r.get("body") or "")
            if sha_match:
                sha = sha_match.group(1)
                break
        return {
            "verdict": "valid_fixed",
            "fix_commit_sha": sha,
            "reply_body": "(thread resolved)",
            "reply_at": None,
            "resolved_at": None,  # GitHub doesn't expose a resolved-at
            "category": "thread-resolved",
            "notes": "Auto-classified: GraphQL reviewThread.isResolved=true.",
        }

    # (4) Issue-level acknowledgment by PR author
    if pr_author and finding_created_at:
        finding_ts = finding_created_at  # ISO 8601 string-comparable
        for ic in issue_comments:
            if (ic.get("user") or {}).get("login") != pr_author:
                continue
            if (ic.get("created_at") or "") <= finding_ts:
                continue
            body = ic.get("body") or ""
            if FIX_SHA_RE.search(body) or FOLLOW_UP_RE.search(body):
                sha_match = FIX_SHA_RE.search(body)
                # If the body says both "Fixed in" and "Not applicable",
                # the more specific signal (Fixed in) wins.
                if sha_match:
                    return {
                        "verdict": "valid_fixed",
                        "fix_commit_sha": sha_match.group(1),
                        "reply_body": body[:500],
                        "reply_at": ic.get("created_at"),
                        "resolved_at": ic.get("created_at") if thread_resolved else None,
                        "category": "issue-level-fixed-in",
                        "notes": "Auto-classified: issue-level reply by PR author with 'Fixed in <sha>'.",
                    }
                # 'Reviewer-finding follow-up' alone — typically wraps
                # multiple findings; treat as valid_fixed (the wrapper
                # implies the contents were addressed) but flag so a
                # human knows the SHA wasn't directly captured.
                return {
                    "verdict": "valid_fixed",
                    "fix_commit_sha": None,
                    "reply_body": body[:500],
                    "reply_at": ic.get("created_at"),
                    "resolved_at": None,
                    "category": "issue-level-follow-up",
                    "notes": "Auto-classified: issue-level 'Reviewer-finding follow-up' by PR author. SHA not directly captured; check the comment for the fix commit.",
                }
            if NOT_APPLICABLE_RE.search(body):
                return {
                    "verdict": "false_positive",
                    "fix_commit_sha": None,
                    "reply_body": body[:500],
                    "reply_at": ic.get("created_at"),
                    "resolved_at": None,
                    "category": "issue-level-not-applicable",
                    "notes": "Auto-classified: issue-level 'Not applicable' by PR author.",
                }

    # No signal — leave for manual review
    return {
        "verdict": "pending",
        "fix_commit_sha": None,
        "reply_body": None,
        "reply_at": None,
        "resolved_at": None,
        "category": None,
        "notes": None,
    }


def pr_state(pr: dict) -> str:
    if pr.get("merged_at"):
        return "MERGED"
    return (pr.get("state") or "").upper()


# ---------------------------------------------------------------------------
# Schema migrations — add columns to existing tables (CREATE TABLE IF NOT
# EXISTS won't extend the column list on an already-created table).
# ---------------------------------------------------------------------------

def run_migrations(conn) -> None:
    """Add any missing columns to already-created tables. Each (table, spec)
    pair is idempotent — columns present on the table are skipped."""
    table_migrations = [
        ("findings",          FINDINGS_ADDED_COLUMNS),
        ("pr_issue_comments", PR_ISSUE_COMMENTS_ADDED_COLUMNS),
    ]
    for table, spec in table_migrations:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in spec:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


# ---------------------------------------------------------------------------
# DB writers (all run inside a single transaction in sync_pr)
# ---------------------------------------------------------------------------

def upsert_pr(conn, repo: str, pr: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO prs (pr_number, repo, title, author, base_branch, head_branch,
                         state, merged_at, commit_count, additions, deletions,
                         changed_files, conventional_prefix, ingested_at, last_synced_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pr_number) DO UPDATE SET
          title          = excluded.title,
          state          = excluded.state,
          merged_at      = excluded.merged_at,
          commit_count   = excluded.commit_count,
          additions      = excluded.additions,
          deletions      = excluded.deletions,
          changed_files  = excluded.changed_files,
          last_synced_at = excluded.last_synced_at
        """,
        (
            pr["number"],
            repo,
            pr["title"],
            pr["user"]["login"],
            pr["base"]["ref"],
            pr["head"]["ref"],
            pr_state(pr),
            pr.get("merged_at"),
            pr.get("commits"),
            pr.get("additions"),
            pr.get("deletions"),
            pr.get("changed_files"),
            extract_conventional_prefix(pr["title"]),
            now,
            now,
        ),
    )


def upsert_commit(conn, pr_number: int, seq: int, commit: dict) -> None:
    conn.execute(
        """
        INSERT INTO commits (sha, pr_number, sequence, committed_at, message)
        VALUES (?,?,?,?,?)
        ON CONFLICT(sha) DO UPDATE SET
          sequence = excluded.sequence,
          message  = excluded.message
        """,
        (
            commit["sha"],
            pr_number,
            seq,
            commit["commit"]["committer"]["date"],
            commit["commit"]["message"],
        ),
    )


def upsert_finding(conn, pr_number: int, comment: dict, now: str) -> int | None:
    """Insert (first sight) or touch (last_seen_at) a finding. Body is FROZEN
    on first capture — ON CONFLICT only updates metadata (severity/line/etc.)
    and last_seen_at, never the body. Returns finding.id or None if skipped."""
    reviewer = normalize_reviewer(comment.get("user", {}).get("login"))
    if not reviewer:
        return None
    if comment.get("in_reply_to_id"):
        return None  # skip replies — they land in finding_events instead

    body = comment["body"]
    flags = extract_structural_flags(body)
    line = comment.get("line") or comment.get("original_line")

    existed = conn.execute(
        "SELECT id FROM findings WHERE reviewer=? AND external_id=?",
        (reviewer, str(comment["id"])),
    ).fetchone()

    if existed is None:
        conn.execute(
            """
            INSERT INTO findings (pr_number, reviewer, external_id, commit_sha,
                                  severity, file_path, line, title, body, url, created_at,
                                  first_seen_at, last_seen_at, status,
                                  has_suggested_fix, has_code_diff, has_analysis_script,
                                  cluster_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)
            """,
            (
                pr_number,
                reviewer,
                str(comment["id"]),
                comment.get("commit_id"),
                extract_severity(body),
                comment.get("path"),
                line,
                extract_title(body),
                body,
                comment.get("html_url"),
                comment["created_at"],
                now,                 # first_seen_at
                now,                 # last_seen_at
                flags["has_suggested_fix"],
                flags["has_code_diff"],
                flags["has_analysis_script"],
                compute_cluster_key(comment.get("path"), line),
            ),
        )
        fid = conn.execute(
            "SELECT id FROM findings WHERE reviewer=? AND external_id=?",
            (reviewer, str(comment["id"])),
        ).fetchone()[0]
        # Archive the original body — our immutable record of how it looked
        # at first capture.
        conn.execute(
            "INSERT INTO finding_body_history (finding_id, body, captured_at, source) "
            "VALUES (?,?,?,'initial')",
            (fid, body, now),
        )
        return fid

    fid = existed[0]
    # Existing row — touch metadata that can legitimately drift (severity
    # re-extraction if heuristics improve, line might be renumbered by GitHub)
    # and last_seen_at. NEVER update body; if it differs, record the drift in
    # finding_body_history as an 'edit_observed' row.
    conn.execute(
        """
        UPDATE findings SET
          severity     = ?,
          file_path    = ?,
          line         = ?,
          title        = ?,
          last_seen_at = ?,
          status       = 'active',
          has_suggested_fix   = ?,
          has_code_diff       = ?,
          has_analysis_script = ?,
          cluster_key  = ?
        WHERE id = ?
        """,
        (
            extract_severity(body),
            comment.get("path"),
            line,
            extract_title(body),
            now,
            flags["has_suggested_fix"],
            flags["has_code_diff"],
            flags["has_analysis_script"],
            compute_cluster_key(comment.get("path"), line),
            fid,
        ),
    )
    # Detect body drift (reviewer edited in place). Compare against the
    # most recent history row so repeat ingests don't append duplicate rows.
    prev_body = conn.execute(
        "SELECT body FROM finding_body_history WHERE finding_id=? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (fid,),
    ).fetchone()
    if prev_body is None or prev_body[0] != body:
        conn.execute(
            "INSERT INTO finding_body_history (finding_id, body, captured_at, source) "
            "VALUES (?,?,?,'edit_observed')",
            (fid, body, now),
        )
    return fid


def upsert_finding_event(conn, finding_id: int, comment: dict, now: str) -> None:
    """Persist a thread comment (initial OR reply) as an event. Bodies are
    frozen; re-ingest updates nothing."""
    external_id = str(comment["id"])
    exists = conn.execute(
        "SELECT 1 FROM finding_events WHERE external_id = ?", (external_id,)
    ).fetchone()
    if exists:
        return
    conn.execute(
        """
        INSERT INTO finding_events
          (finding_id, external_id, author_login, author_role, body, created_at, captured_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            finding_id,
            external_id,
            (comment.get("user") or {}).get("login"),
            author_role((comment.get("user") or {}).get("login")),
            comment["body"],
            comment["created_at"],
            now,
        ),
    )


def _upsert_pr_surface_comment(
    conn,
    pr_number: int,
    external_id: str,
    login: str | None,
    body: str,
    created_at: str,
    source: str,            # 'issue_comment' | 'review_body'
    review_state: str | None,
    now: str,
) -> None:
    """Shared upsert path for PR-level reviewer posts from either surface
    (issue comments or review bodies). Both land in `pr_issue_comments` with
    a `source` column distinguishing them. Body is frozen on first sight;
    edits land in pr_issue_comment_history."""
    role = author_role(login)
    if role not in KNOWN_REVIEWERS:
        return

    existed = conn.execute(
        "SELECT id FROM pr_issue_comments WHERE external_id = ?", (external_id,)
    ).fetchone()

    if existed is None:
        conn.execute(
            """
            INSERT INTO pr_issue_comments
              (pr_number, external_id, author_login, author_role, body, created_at,
               first_seen_at, last_seen_at, status, source, review_state)
            VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?)
            """,
            (pr_number, external_id, login, role, body, created_at, now, now,
             source, review_state),
        )
        row_id = conn.execute(
            "SELECT id FROM pr_issue_comments WHERE external_id = ?", (external_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pr_issue_comment_history (issue_comment_id, body, captured_at, source) "
            "VALUES (?,?,?,'initial')",
            (row_id, body, now),
        )
        return

    row_id = existed[0]
    conn.execute(
        "UPDATE pr_issue_comments SET last_seen_at=?, status='active' WHERE id=?",
        (now, row_id),
    )
    prev_body = conn.execute(
        "SELECT body FROM pr_issue_comment_history WHERE issue_comment_id=? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (row_id,),
    ).fetchone()
    if prev_body is None or prev_body[0] != body:
        conn.execute(
            "INSERT INTO pr_issue_comment_history (issue_comment_id, body, captured_at, source) "
            "VALUES (?,?,?,'edit_observed')",
            (row_id, body, now),
        )


def upsert_issue_comment(conn, pr_number: int, comment: dict, now: str) -> None:
    """PR-level (non-inline) comments from AI reviewers."""
    _upsert_pr_surface_comment(
        conn,
        pr_number=pr_number,
        external_id=str(comment["id"]),
        login=(comment.get("user") or {}).get("login"),
        body=comment["body"],
        created_at=comment["created_at"],
        source="issue_comment",
        review_state=None,
        now=now,
    )


def upsert_review_body(conn, pr_number: int, review: dict, now: str) -> None:
    """PR review bodies (pulls/{pr}/reviews endpoint). CodeRabbit posts its
    Nitpick findings here; without this surface they go uncaptured entirely.
    external_id is namespaced as 'review:{id}' so it can't collide with
    issue-comment IDs in the UNIQUE(external_id) constraint. Empty-body
    reviews (e.g. pure APPROVE with inline comments only) are skipped — the
    inline comments are captured on their own surface."""
    body = (review.get("body") or "").strip()
    if not body:
        return
    _upsert_pr_surface_comment(
        conn,
        pr_number=pr_number,
        external_id=f"review:{review['id']}",
        login=(review.get("user") or {}).get("login"),
        body=review["body"],
        # Reviews use `submitted_at`, not `created_at`.
        created_at=review.get("submitted_at") or now,
        source="review_body",
        review_state=review.get("state"),
        now=now,
    )


def mark_retracted(conn, pr_number: int, seen_finding_ids: set[int],
                   seen_issue_comment_ids: set[int], now: str) -> None:
    """Anything we had for this PR that GitHub no longer returns is retracted.
    The row stays intact (including the most-recent body); we just flip
    status='retracted' and append a history row with the last-known body so
    the archival trail records when we last saw it."""

    # Findings
    stale_findings = conn.execute(
        "SELECT id, body FROM findings "
        "WHERE pr_number=? AND status='active' AND id NOT IN (%s)"
        % (",".join(str(i) for i in seen_finding_ids) if seen_finding_ids else "0"),
        (pr_number,),
    ).fetchall()
    for fid, body in stale_findings:
        conn.execute(
            "UPDATE findings SET status='retracted' WHERE id=?", (fid,)
        )
        conn.execute(
            "INSERT INTO finding_body_history (finding_id, body, captured_at, source) "
            "VALUES (?,?,?,'retraction_detected')",
            (fid, body, now),
        )

    # PR-level issue comments
    stale_issues = conn.execute(
        "SELECT id, body FROM pr_issue_comments "
        "WHERE pr_number=? AND status='active' AND id NOT IN (%s)"
        % (",".join(str(i) for i in seen_issue_comment_ids) if seen_issue_comment_ids else "0"),
        (pr_number,),
    ).fetchall()
    for cid, body in stale_issues:
        conn.execute(
            "UPDATE pr_issue_comments SET status='retracted' WHERE id=?", (cid,)
        )
        conn.execute(
            "INSERT INTO pr_issue_comment_history (issue_comment_id, body, captured_at, source) "
            "VALUES (?,?,?,'retraction_detected')",
            (cid, body, now),
        )


def ensure_verdict(conn, finding_id: int, now: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM verdicts WHERE finding_id=?", (finding_id,)
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO verdicts (finding_id, verdict, updated_at) VALUES (?, 'pending', ?)",
        (finding_id, now),
    )


def apply_reply_verdict(
    conn,
    finding_id: int,
    reply: dict | None,
    thread_resolved: bool,
    now: str,
) -> None:
    """Legacy single-reply verdict updater. Kept for backward compatibility
    with any caller that hasn't migrated to apply_finding_verdict yet."""
    if not reply:
        return
    current = conn.execute(
        "SELECT verdict FROM verdicts WHERE finding_id=?", (finding_id,)
    ).fetchone()
    if not current or current[0] != "pending":
        return

    new_verdict = classify_from_reply(reply["body"])
    if new_verdict == "pending":
        return

    conn.execute(
        """
        UPDATE verdicts SET
          verdict        = ?,
          fix_commit_sha = ?,
          reply_body     = ?,
          reply_at       = ?,
          resolved_at    = ?,
          updated_at     = ?
        WHERE finding_id = ?
        """,
        (
            new_verdict,
            extract_fix_sha(reply["body"]),
            reply["body"],
            reply["created_at"],
            reply["created_at"] if thread_resolved else None,
            now,
            finding_id,
        ),
    )


def apply_finding_verdict(
    conn,
    finding_id: int,
    finding_body: str | None,
    finding_created_at: str | None,
    inline_replies: list,
    thread_resolved: bool,
    issue_comments: list,
    pr_author: str,
    pr_state: str,
    now: str,
) -> None:
    """Layered classifier: applies the best-available signal. Re-runnable —
    re-classifies a 'pending' finding when new replies/threads land. Will
    NOT clobber a manually-set verdict (anything other than 'pending')."""
    current = conn.execute(
        "SELECT verdict FROM verdicts WHERE finding_id=?", (finding_id,)
    ).fetchone()
    if not current:
        return
    if current[0] != "pending":
        return  # respect manual classification

    decision = classify_finding(
        finding_body=finding_body,
        finding_created_at=finding_created_at,
        inline_replies=inline_replies,
        thread_resolved=thread_resolved,
        issue_comments=issue_comments,
        pr_author=pr_author,
        pr_state=pr_state,
    )
    if decision["verdict"] == "pending":
        return  # leave for manual review

    conn.execute(
        """
        UPDATE verdicts SET
          verdict        = ?,
          fix_commit_sha = ?,
          reply_body     = ?,
          reply_at       = ?,
          resolved_at    = ?,
          category       = COALESCE(?, category),
          notes          = COALESCE(?, notes),
          updated_at     = ?
        WHERE finding_id = ?
        """,
        (
            decision["verdict"],
            decision["fix_commit_sha"],
            decision["reply_body"],
            decision["reply_at"],
            decision["resolved_at"],
            decision["category"],
            decision["notes"],
            now,
            finding_id,
        ),
    )


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def find_first_reply(comments: list, in_reply_to_id: int, author_login: str) -> dict | None:
    for c in comments:
        if c.get("in_reply_to_id") == in_reply_to_id and c["user"]["login"] == author_login:
            return c
    return None


def thread_comments_for(threads: list, top_level_external_id: int) -> list[dict]:
    """Return every GraphQL comment node on the thread whose top-level comment
    has the given databaseId. Shape-normalized to match REST comment fields
    we consume elsewhere (id, body, created_at, user.login)."""
    for thread in threads:
        nodes = thread["comments"]["nodes"]
        if not nodes or nodes[0]["databaseId"] != top_level_external_id:
            continue
        return [
            {
                "id": n["databaseId"],
                "body": n["body"],
                "created_at": n["createdAt"],
                "user": {"login": (n.get("author") or {}).get("login")},
            }
            for n in nodes
        ]
    return []


def sync_pr(repo: str, pr_number: int, dry_run: bool = False) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text())
    run_migrations(conn)

    now = now_iso()
    run_id = conn.execute(
        "INSERT INTO sync_runs (pr_number, started_at) VALUES (?, ?)",
        (pr_number, now),
    ).lastrowid
    conn.commit()

    try:
        pr = fetch_pr(repo, pr_number)
        commits = fetch_pr_commits(repo, pr_number)
        review_comments = fetch_pr_review_comments(repo, pr_number)
        issue_comments = fetch_pr_issue_comments(repo, pr_number)
        pr_reviews = fetch_pr_reviews(repo, pr_number)
        threads = fetch_review_threads(repo, pr_number)

        # Map a thread's top-level comment ID -> isResolved.
        thread_resolved_by_top_id: dict[int, bool] = {}
        for t in threads:
            nodes = t["comments"]["nodes"]
            if nodes:
                thread_resolved_by_top_id[nodes[0]["databaseId"]] = t["isResolved"]

        if dry_run:
            print(f"[dry-run] PR #{pr_number}: {len(commits)} commits, "
                  f"{len(review_comments)} review comments, "
                  f"{len(issue_comments)} issue comments, "
                  f"{len(pr_reviews)} PR reviews, {len(threads)} threads.")
            conn.execute(
                "UPDATE sync_runs SET completed_at=?, error=? WHERE id=?",
                (now_iso(), "dry-run", run_id),
            )
            conn.commit()
            return

        findings_upserted = 0
        seen_finding_ids: set[int] = set()
        seen_issue_comment_ids: set[int] = set()

        with conn:  # transaction
            upsert_pr(conn, repo, pr, now)
            for seq, commit in enumerate(commits, start=1):
                upsert_commit(conn, pr_number, seq, commit)

            pr_author = pr["user"]["login"]
            current_pr_state = pr_state(pr)
            for comment in review_comments:
                fid = upsert_finding(conn, pr_number, comment, now)
                if fid is None:
                    continue
                findings_upserted += 1
                seen_finding_ids.add(fid)
                ensure_verdict(conn, fid, now)

                # Reply chain: persist EVERY thread comment (initial + follow-ups
                # from reviewer, our replies, acks). The initial top-level comment
                # IS the finding's external_id; include it so finding_events is a
                # complete conversation trail on its own.
                for thread_comment in thread_comments_for(threads, comment["id"]):
                    upsert_finding_event(conn, fid, thread_comment, now)

                # Layered classifier: scans ALL signals, not just a single
                # PR-author reply. Catches Sentry's '*Resolved in <sha>*'
                # body marker, thread.isResolved=true alone, issue-level
                # acknowledgments, and any non-bot reply with the canonical
                # patterns. Falls back to 'pending' only when no signal
                # is present.
                inline_replies = [
                    c for c in review_comments
                    if c.get("in_reply_to_id") == comment["id"]
                ]
                thread_resolved = thread_resolved_by_top_id.get(comment["id"], False)
                apply_finding_verdict(
                    conn=conn,
                    finding_id=fid,
                    finding_body=comment.get("body"),
                    finding_created_at=comment.get("created_at"),
                    inline_replies=inline_replies,
                    thread_resolved=thread_resolved,
                    issue_comments=issue_comments,
                    pr_author=pr_author,
                    pr_state=current_pr_state,
                    now=now,
                )

            # PR-level issue comments — CodeRabbit summary, Cursor PR summary,
            # Cursor bug-prediction comments, etc. Bodies frozen on first sight.
            for ic in issue_comments:
                upsert_issue_comment(conn, pr_number, ic, now)
                # Track seen-set regardless of whether it was a known role; the
                # upsert function itself filters to bots, but for retraction
                # detection we only care that we looked at the id this sync.
                row = conn.execute(
                    "SELECT id FROM pr_issue_comments WHERE external_id=?",
                    (str(ic["id"]),),
                ).fetchone()
                if row:
                    seen_issue_comment_ids.add(row[0])

            # PR review bodies (pulls/{pr}/reviews). CodeRabbit posts Nitpick
            # findings inside these; without this surface we'd silently lose
            # them (observed on PR #1606).
            for review in pr_reviews:
                upsert_review_body(conn, pr_number, review, now)
                row = conn.execute(
                    "SELECT id FROM pr_issue_comments WHERE external_id=?",
                    (f"review:{review['id']}",),
                ).fetchone()
                if row:
                    seen_issue_comment_ids.add(row[0])

            # Anything we had last time and didn't see this time = retracted.
            mark_retracted(conn, pr_number, seen_finding_ids, seen_issue_comment_ids, now)

            conn.execute(
                "UPDATE sync_runs SET completed_at=?, findings_upserted=?, commits_upserted=? WHERE id=?",
                (now_iso(), findings_upserted, len(commits), run_id),
            )

        print(f"Synced PR #{pr_number}: {len(commits)} commits, {findings_upserted} findings upserted.")
    except Exception as e:
        conn.execute(
            "UPDATE sync_runs SET completed_at=?, error=? WHERE id=?",
            (now_iso(), str(e), run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest AI-reviewer data for a PR into reviews.db.")
    parser.add_argument("--pr", type=int, required=True, help="PR number to sync.")
    parser.add_argument("--repo", required=True, help="owner/name (e.g. octocat/hello-world)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and summarize; write nothing.")
    args = parser.parse_args()

    try:
        sync_pr(args.repo, args.pr, dry_run=args.dry_run)
    except subprocess.CalledProcessError as e:
        print(f"gh command failed: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
