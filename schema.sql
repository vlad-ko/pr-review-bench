-- AI Code Review Metrics — local SQLite schema.
-- Idempotent: safe to run on every ingest.
--
-- Column-level additions to EXISTING tables are handled by run_migrations()
-- in ingest.py (SQLite doesn't have ALTER TABLE IF NOT EXISTS COLUMN). This
-- file only uses CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
-- which are safe on fresh DBs and no-ops on existing ones.

PRAGMA foreign_keys = ON;

-- One row per PR we've ingested.
CREATE TABLE IF NOT EXISTS prs (
  pr_number           INTEGER PRIMARY KEY,
  repo                TEXT NOT NULL,              -- "owner/repo"
  title               TEXT NOT NULL,
  author              TEXT NOT NULL,
  base_branch         TEXT,
  head_branch         TEXT,
  state               TEXT,                        -- OPEN / MERGED / CLOSED
  merged_at           TEXT,                        -- ISO8601, nullable
  commit_count        INTEGER,
  additions           INTEGER,
  deletions           INTEGER,
  changed_files       INTEGER,
  conventional_prefix TEXT,                        -- feat/fix/chore/docs/…
  ingested_at         TEXT NOT NULL,
  last_synced_at      TEXT NOT NULL
);

-- One row per commit we observed on the PR (ordered by sequence).
CREATE TABLE IF NOT EXISTS commits (
  sha           TEXT PRIMARY KEY,
  pr_number     INTEGER NOT NULL REFERENCES prs(pr_number) ON DELETE CASCADE,
  sequence      INTEGER NOT NULL,                  -- 1 = first commit on PR, 2 = next, …
  committed_at  TEXT NOT NULL,
  message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_commits_pr ON commits(pr_number, sequence);

-- One row per finding from any AI reviewer.
-- Uniqueness: (reviewer, external_id) — the reviewer's stable ID for that finding.
--
-- Body is FROZEN on first capture and never overwritten — so retracted/edited
-- findings remain forensically intact. Edits are tracked in finding_body_history.
-- Retracted findings: status = 'retracted' but the row stays (with last_seen_at
-- being the last sync where GitHub still returned it).
CREATE TABLE IF NOT EXISTS findings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number   INTEGER NOT NULL REFERENCES prs(pr_number) ON DELETE CASCADE,
  reviewer    TEXT NOT NULL,                       -- normalized: 'cursor' | 'coderabbitai' | 'seer' | 'greptile'
  external_id TEXT NOT NULL,                       -- the reviewer's own comment ID (stable)
  commit_sha  TEXT,                                -- commit GitHub attributes this finding to at first capture
  severity    TEXT,                                -- low | minor | medium | major | high | critical | null
  file_path   TEXT,                                -- raw path (internal / tier-A only)
  line        INTEGER,
  title       TEXT,                                -- short extracted heading
  body        TEXT NOT NULL,                       -- FROZEN on first capture — never overwritten
  url         TEXT,                                -- html URL to the finding
  created_at  TEXT NOT NULL,                       -- GitHub's reported creation timestamp
  -- Added by run_migrations() on existing DBs:
  -- first_seen_at           TEXT  — our first sync that observed this finding
  -- last_seen_at            TEXT  — our most recent sync that still saw it live
  -- status                  TEXT  — 'active' | 'retracted'
  -- has_suggested_fix       INTEGER — bool (0/1) extracted at ingest
  -- has_code_diff           INTEGER — bool
  -- has_analysis_script     INTEGER — bool, CodeRabbit "🏁 Script executed:" pattern
  -- cluster_key             TEXT  — "{file_path}:{line//10}" for same-region overlap queries
  UNIQUE(reviewer, external_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_pr ON findings(pr_number);
CREATE INDEX IF NOT EXISTS idx_findings_reviewer_severity ON findings(reviewer, severity);

-- History of body edits for a finding. Each row is a snapshot observed at a
-- given sync. Lets us reconstruct CodeRabbit's "review in progress" → final
-- summary evolution, and see what a finding said before it was retracted.
CREATE TABLE IF NOT EXISTS finding_body_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id  INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  body        TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  source      TEXT NOT NULL                        -- 'initial' | 'edit_observed' | 'retraction_detected'
);
CREATE INDEX IF NOT EXISTS idx_finding_body_history_finding ON finding_body_history(finding_id, captured_at);

-- Full reply chain for a finding's review thread. The initial finding maps to
-- findings.external_id; subsequent comments (replies from us, follow-ups from
-- the reviewer, acknowledgments) all live here. Lets us measure whether a
-- reviewer accepted an FP explanation, posted a positive ack, or re-argued.
CREATE TABLE IF NOT EXISTS finding_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id     INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  external_id    TEXT NOT NULL,                    -- GitHub's comment ID (stable)
  author_login   TEXT,                             -- raw author login, e.g. 'vlad-ko', 'coderabbitai[bot]'
  author_role    TEXT,                             -- 'user' | 'cursor' | 'coderabbitai' | 'seer' | 'greptile' | 'other'
  body           TEXT NOT NULL,                    -- frozen on first capture
  created_at     TEXT NOT NULL,
  captured_at    TEXT NOT NULL,
  UNIQUE(external_id)
);
CREATE INDEX IF NOT EXISTS idx_finding_events_finding ON finding_events(finding_id, created_at);

-- PR-level reviewer comments (non-inline). Includes:
--   - source='issue_comment': CodeRabbit's summary (with its "review in
--     progress" / final-summary edit cycle), Cursor's PR summary, etc.
--     external_id = raw GitHub issue-comment ID.
--   - source='review_body': PR review bodies submitted via
--     `pulls/{pr}/reviews` — CodeRabbit posts its Nitpick findings here,
--     for example. external_id = "review:{review_id}" to stay unique
--     against issue_comment IDs.
-- Each row is frozen on first capture; retractions are detected the same
-- way as findings (via pr_issue_comment_history and seen-set comparison).
-- Added by run_migrations() on existing DBs:
--   source         TEXT   — 'issue_comment' | 'review_body'
--   review_state   TEXT   — for source='review_body': APPROVED / CHANGES_REQUESTED / COMMENTED
CREATE TABLE IF NOT EXISTS pr_issue_comments (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number      INTEGER NOT NULL REFERENCES prs(pr_number) ON DELETE CASCADE,
  external_id    TEXT NOT NULL,
  author_login   TEXT NOT NULL,
  author_role    TEXT,                             -- same normalization as findings.reviewer
  body           TEXT NOT NULL,                    -- frozen on first capture
  created_at     TEXT NOT NULL,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'retracted'
  UNIQUE(external_id)
);
CREATE INDEX IF NOT EXISTS idx_pr_issue_comments_pr ON pr_issue_comments(pr_number);
CREATE INDEX IF NOT EXISTS idx_pr_issue_comments_author ON pr_issue_comments(pr_number, author_role);

-- Same history table for PR-level bot comments — captures CodeRabbit's
-- in-place edits from "review in progress" to final summary.
CREATE TABLE IF NOT EXISTS pr_issue_comment_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_comment_id  INTEGER NOT NULL REFERENCES pr_issue_comments(id) ON DELETE CASCADE,
  body              TEXT NOT NULL,
  captured_at       TEXT NOT NULL,
  source            TEXT NOT NULL                  -- 'initial' | 'edit_observed' | 'retraction_detected'
);
CREATE INDEX IF NOT EXISTS idx_pr_issue_comment_history_c ON pr_issue_comment_history(issue_comment_id, captured_at);

-- Our assessment of each finding. One row per finding; history preserved in verdicts_history.
CREATE TABLE IF NOT EXISTS verdicts (
  finding_id     INTEGER PRIMARY KEY REFERENCES findings(id) ON DELETE CASCADE,
  verdict        TEXT NOT NULL,                    -- valid_fixed | valid_deferred | false_positive | partial | pending
  category       TEXT,                             -- freeform: "toctou", "race", "markdown-lint", "regex-nuance", …
  fix_commit_sha TEXT,                             -- populated when verdict='valid_fixed'
  reply_body     TEXT,                             -- our reply text
  reply_at       TEXT,                             -- ISO8601 of first reply
  resolved_at    TEXT,                             -- ISO8601 (best effort — GitHub doesn't expose a true resolved-at)
  notes          TEXT,                             -- freeform retrospective notes
  updated_at     TEXT NOT NULL
);

-- Audit trail: every time we change a verdict, the previous value is appended here.
CREATE TABLE IF NOT EXISTS verdicts_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
  verdict    TEXT NOT NULL,
  category   TEXT,
  changed_at TEXT NOT NULL
);

-- Record each ingest invocation for debugging partial syncs.
CREATE TABLE IF NOT EXISTS sync_runs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_number         INTEGER NOT NULL,
  started_at        TEXT NOT NULL,
  completed_at      TEXT,
  findings_upserted INTEGER,
  commits_upserted  INTEGER,
  error             TEXT
);
