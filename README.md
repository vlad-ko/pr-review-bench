# pr-review-bench

A tiny SQLite-backed tool for benchmarking AI code reviewers against each other on real pull requests. Designed for teams running multiple AI reviewers in parallel (CodeRabbit, Sentry Seer, Greptile, Cursor BugBot, …) who want to answer the question marketing pages can't: *which one actually catches the bugs?*

This repo contains the ingester, the schema, and a sanitized export from the original 146-merged-PR / 679-finding / 446-review-event dataset described in the accompanying blog post: [*Best AI Code Reviewer in 2026?*](https://example.com/blog-post-url). The data lives in `data/` and is fully PII-stripped — anonymized PR IDs, no file paths, no comment bodies, no commit SHAs.

## What it does

For each PR you point it at, the tool walks the GitHub API and captures:

- Every commit on the PR (SHA + sequence + author timestamp)
- Every AI-reviewer comment from `coderabbitai`, `sentry`, `greptile-apps`, and `cursor` bots — verbatim body, severity, file/line, URL
- An auto-classified verdict per finding — `valid_fixed` from "Fixed in `<sha>`" replies, `false_positive` from "Not applicable" replies, else `pending`
- PR metadata (title, state, merged_at, commit count, conventional-commit prefix)

Re-runs are idempotent. The finding body is **frozen on first capture** so reviewer edits and retractions don't overwrite history. Edits are tracked in a separate audit table.

## Install

There's nothing to install. Three files, Python stdlib only.

Requirements:
- Python 3.10+
- The `gh` CLI, authenticated against the GitHub repo you want to ingest (`gh auth status` should show you're logged in)
- (Optional, for `gen_charts.py` only) `matplotlib` — install with `pip install matplotlib` if you want to regenerate the PNG charts in [`data/charts/`](./data/charts). The ingester itself has no third-party dependencies.

The SQLite database is created automatically on first run at `db/reviews.db` (relative to the working directory).

## Usage

```bash
# Ingest a single PR
./ingest.py --repo OWNER/REPO --pr 1234

# Re-ingest the same PR after new reviewer activity (idempotent, frozen-on-capture)
./ingest.py --repo OWNER/REPO --pr 1234

# Different DB path (default: ./db/reviews.db)
./ingest.py --repo OWNER/REPO --pr 1234 --db /path/to/your.db
```

The expected output:

```
Synced PR #1234: 12 commits, 7 findings upserted.
```

If the PR has been ingested before, the count reflects only new or updated findings. Frozen bodies are never rewritten — only the audit table grows.

### When to run it

Three useful moments:

1. **As soon as a new finding appears** — captures the reviewer's original body before any later edit or retraction. (CodeRabbit in particular edits its comments in place after fixes land.)
2. **Right after the PR's review cycle is clean** — captures all final verdicts.
3. **Once more after the PR is squash-merged** — refreshes `state` + `merged_at` so the DB doesn't carry stale `OPEN` rows.

Run as often as you like. The ingester is cheap.

## Schema

The schema is in [`schema.sql`](./schema.sql). Brief tour:

- **`prs`** — one row per PR. Key columns: `pr_number` (PK), `state`, `merged_at`, `commit_count`, `conventional_prefix`.
- **`commits`** — one row per commit on a PR. Key columns: `sha` (PK), `pr_number`, `sequence`, `committed_at`.
- **`findings`** — one row per AI-reviewer comment. Key columns: `reviewer` (`coderabbitai`/`seer`/`greptile`/`cursor`), `external_id` (the reviewer's stable comment ID), `commit_sha`, `severity`, `file_path`, `line`, `body` (frozen), `created_at`. Unique on `(reviewer, external_id)`.
- **`verdicts`** — one row per finding's classification (`valid_fixed`/`false_positive`/`pending`). Derived from your reply text.
- **`finding_body_history`** — audit table tracking body edits over time (CodeRabbit edits its comments in place).
- **`pr_issue_comments` / `pr_issue_comment_history`** — PR-level (issue-level) comments, separate from inline review comments.

## Example queries

The blog post's headline numbers come from queries like these. Run them against your own DB:

```sql
-- Findings per reviewer, with mean per PR
SELECT reviewer,
       COUNT(*) AS total,
       COUNT(DISTINCT pr_number) AS prs,
       ROUND(1.0 * COUNT(*) / COUNT(DISTINCT pr_number), 1) AS avg_per_pr
FROM findings GROUP BY reviewer ORDER BY total DESC;

-- False-positive rate by reviewer
SELECT f.reviewer,
       SUM(v.verdict='valid_fixed')    AS valid,
       SUM(v.verdict='false_positive') AS fp,
       ROUND(100.0 * SUM(v.verdict='false_positive') /
             NULLIF(SUM(v.verdict IN ('valid_fixed','false_positive')), 0), 1) AS fp_rate_pct
FROM findings f LEFT JOIN verdicts v ON v.finding_id = f.id
GROUP BY f.reviewer ORDER BY fp_rate_pct DESC;

-- Applyable-fix coverage (unified diff or one-click suggestion)
SELECT reviewer,
       ROUND(100.0 * SUM(CASE WHEN body LIKE '%```diff%'       THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_diff,
       ROUND(100.0 * SUM(CASE WHEN body LIKE '%```suggestion%' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_suggestion
FROM findings GROUP BY reviewer;

-- Reviewer latency: minutes from commit push to first finding on that commit
SELECT f.reviewer,
       ROUND(AVG((julianday(f.created_at) - julianday(c.committed_at)) * 1440), 1) AS mean_min,
       COUNT(*) AS samples
FROM findings f JOIN commits c ON c.sha = f.commit_sha
WHERE f.commit_sha IS NOT NULL AND f.created_at > c.committed_at
GROUP BY f.reviewer ORDER BY mean_min;

-- Convergent catches: file:line locations where multiple reviewers agreed
SELECT n_reviewers, COUNT(*) AS locations FROM (
  SELECT pr_number, file_path, line, COUNT(DISTINCT reviewer) AS n_reviewers
  FROM findings
  WHERE file_path IS NOT NULL AND line IS NOT NULL
  GROUP BY pr_number, file_path, line
) GROUP BY n_reviewers ORDER BY n_reviewers;
```

## Generating charts

A separate script renders the headline metrics as PNG charts suitable for blog posts, slide decks, or status reports:

```bash
pip install matplotlib       # one-time setup
./gen_charts.py              # writes 7 PNGs to data/charts/
./gen_charts.py --db /custom/path.db --out-dir /tmp/charts
```

The seven charts:

| File | What it shows |
|---|---|
| `findings_by_reviewer.png` | Pie chart: total findings per reviewer |
| `fp_rate_by_reviewer.png` | False-positive rate per reviewer (lower is better) |
| `applyable_fix_coverage.png` | % of findings that ship a one-click fix in GitHub (diff or suggestion) |
| `seer_fp_by_severity.png` | Seer's FP rate broken down by severity tier |
| `high_severity_fp_cross_reviewer.png` | Top-tier severity FP rate across all four reviewers |
| `reviewer_latency.png` | Mean time from commit push to first finding |
| `reviewer_agreement.png` | How many reviewers agreed at the same file:line |

Style: light theme, retina DPI, sans-serif fonts with safe fallbacks. Each chart computes its data live from the DB at render time, scoped to merged PRs only (matches the CSV exports). Re-running after a fresh ingest produces up-to-date PNGs.

The pre-rendered versions from the original dataset live in [`data/charts/`](./data/charts).

## The reply protocol the verdict classifier expects

The auto-classifier looks at PR-author replies to each finding and labels:

- **`valid_fixed`** — when a reply starts with `Fixed in <SHA>` (the commit that fixed it).
- **`false_positive`** — when a reply starts with `Not applicable:` (your reasoning).
- **`pending`** — anything else, including findings you addressed without using the canonical phrasing.

You don't have to follow this protocol, but if you do, the FP-rate numbers compute themselves. If you don't, every finding stays `pending` and you can hand-classify later by inserting rows into the `verdicts` table.

## The published dataset

The CSVs in [`data/`](./data) are the anonymized export of the original three-week run described in the blog post:

- **`findings_anonymized.csv`** — one row per finding. Columns: anonymized PR ID (e.g. `PR042`), reviewer, severity, verdict, has_unified_diff (0/1), has_suggestion_block (0/1), latency_min_to_finding, body_word_count_approx, created_date. No file paths, no comment bodies, no real PR numbers, no commit SHAs.
- **`summary_stats.csv`** — pre-aggregated per-reviewer metrics (28 rows) matching every number cited in the blog post. Reproducible from `findings_anonymized.csv` with grouping queries.

If you want to verify the blog post's numbers, load `findings_anonymized.csv` into SQLite (or any tool) and group by `reviewer`. Same shape as `findings` table minus the PII columns.

## What's NOT in this repo

This is a deliberately minimal tool. It is NOT a:

- Dashboard or visualization layer (use whatever — Datasette, Metabase, jq)
- Webhook receiver — the ingester is pull-only, run it when you want
- Hosted service — local SQLite only; data stays on your machine

The point is to make benchmarking AI reviewers something a team can do in an afternoon, not a quarter.

## License

MIT. See [LICENSE](./LICENSE).

## Contributing

This is a small, opinionated tool that solves one problem. PRs welcome for:
- New reviewer adapters (anything that posts comments via the GitHub API can be added)
- Better verdict-classification heuristics (regex tweaks, multi-language reply patterns)
- Bug fixes in the ingester or schema

Out of scope:
- Hosted multi-team SaaS-ification — that's a different project
- UI / frontend — by design
- Vendor-specific integrations beyond the four reviewers currently supported

## Related

If you found this useful, the [original blog post](https://example.com/blog-post-url) walks through three weeks of side-by-side data — false-positive rates, applyable-fix coverage, latency distributions, GitHub-integration quirks, and the pricing decision that cost us our second-favorite reviewer.
