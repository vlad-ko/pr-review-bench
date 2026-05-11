#!/usr/bin/env python3
"""
Generate PNG charts from the metrics DB for the blog post.

Usage:
    ./gen_charts.py [--db PATH] [--out-dir PATH]

Defaults:
    --db        db/reviews.db
    --out-dir   data/charts/

Output: six PNG files at retina-quality DPI suitable for embedding in
markdown / blog posts.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import matplotlib.pyplot as plt

# Consistent reviewer palette across all charts. Order matches the post.
COLORS = {
    "coderabbitai":   "#5b8def",  # blue — CodeRabbit
    "seer":           "#e94f64",  # red  — Seer (paranoid one)
    "greptile":       "#10b981",  # green — Greptile (precision)
    "cursor":         "#a78bfa",  # purple — Cursor Bug Bot
}
LABEL = {
    "coderabbitai":   "CodeRabbit",
    "seer":           "Sentry Seer",
    "greptile":       "Greptile",
    "cursor":         "Cursor BugBot",
}
ORDER = ["coderabbitai", "seer", "cursor", "greptile"]


# SQL fragment: scope every chart query to MERGED PRs only, so the rendered
# charts match the scope of the published CSV exports in data/. Findings on
# open / closed-without-merge PRs are excluded — those aren't useful for the
# "what did the reviewers say on shipped code?" story.
MERGED_SCOPE = "f.pr_number IN (SELECT pr_number FROM prs WHERE state='MERGED')"


def setup_style() -> None:
    """Apply a clean, blog-friendly style to all charts."""
    plt.rcParams.update({
        # Use matplotlib's "sans-serif" family with a preferred font list.
        # The first available font on the system wins; matplotlib falls back
        # cleanly without printing warnings.
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 12,
        "axes.titlesize": 16,
        "axes.titleweight": "bold",
        "axes.labelsize": 12,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.2,
        "grid.linestyle": "--",
        "figure.dpi": 150,
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
        "savefig.facecolor": "white",
    })


def chart_findings_pie(conn: sqlite3.Connection, out_path: Path) -> None:
    """Pie chart: total findings per reviewer."""
    rows = dict(conn.execute(
        f"SELECT f.reviewer, COUNT(*) FROM findings f WHERE {MERGED_SCOPE} GROUP BY f.reviewer"
    ).fetchall())
    values = [rows[r] for r in ORDER]
    labels = [f"{LABEL[r]}\n({rows[r]})" for r in ORDER]
    colors = [COLORS[r] for r in ORDER]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.pie(
        values, labels=labels, colors=colors,
        autopct="%1.1f%%", startangle=90,
        textprops={"fontsize": 11},
        wedgeprops={"edgecolor": "white", "linewidth": 2},
    )
    total = sum(values)
    ax.set_title(f"Findings by reviewer (n = {total})", pad=20)
    plt.savefig(out_path)
    plt.close()


def chart_fp_rate(conn: sqlite3.Connection, out_path: Path) -> None:
    """Horizontal bar chart: false-positive rate per reviewer."""
    rows = conn.execute(f"""
        SELECT f.reviewer,
               100.0 * SUM(v.verdict='false_positive') /
               NULLIF(SUM(v.verdict IN ('valid_fixed','false_positive')), 0) AS fp_pct
        FROM findings f LEFT JOIN verdicts v ON v.finding_id = f.id
        WHERE {MERGED_SCOPE}
        GROUP BY f.reviewer
    """).fetchall()
    fp_by_reviewer = {r: (pct or 0) for r, pct in rows}

    # Order: lowest FP (best) at top of chart — matplotlib barh plots from
    # bottom to top, so we sort DESCENDING here to flip the visual.
    ordered = sorted(ORDER, key=lambda r: -fp_by_reviewer.get(r, 0))
    labels = [LABEL[r] for r in ordered]
    values = [fp_by_reviewer[r] for r in ordered]
    colors = [COLORS[r] for r in ordered]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", linewidth=1.5)
    ax.set_xlabel("False-positive rate (%)")
    ax.set_title("False-positive rate by reviewer (lower is better)", pad=15)
    ax.set_xlim(0, max(values) * 1.15 + 0.5)

    for bar, v in zip(bars, values):
        ax.text(
            bar.get_width() + 0.15, bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}%", va="center", fontsize=12, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def chart_applyable_fix(conn: sqlite3.Connection, out_path: Path) -> None:
    """Bar chart: % of findings with at least one applyable fix (diff OR suggestion).

    Uses the union, not a stacked breakdown — most CodeRabbit findings that
    include a suggestion ALSO include a diff, so a stacked chart would visually
    overstate coverage by double-counting. The union figure is what readers care
    about: "what fraction of findings come with a one-click fix?"
    """
    rows = conn.execute(f"""
        SELECT f.reviewer,
               100.0 * SUM(CASE WHEN body LIKE '%```diff%' OR body LIKE '%```suggestion%'
                                THEN 1 ELSE 0 END) / COUNT(*) AS either_pct,
               100.0 * SUM(CASE WHEN body LIKE '%```diff%' THEN 1 ELSE 0 END) / COUNT(*) AS diff_pct,
               100.0 * SUM(CASE WHEN body LIKE '%```suggestion%' THEN 1 ELSE 0 END) / COUNT(*) AS sug_pct
        FROM findings f WHERE {MERGED_SCOPE} GROUP BY f.reviewer
    """).fetchall()
    pct = {r: (e, d, s) for r, e, d, s in rows}

    # Order: highest applyable coverage at top of chart. Sort DESCENDING because
    # matplotlib barh plots from bottom to top.
    ordered = sorted(ORDER, key=lambda r: pct[r][0])
    labels = [LABEL[r] for r in ordered]
    values = [pct[r][0] for r in ordered]
    colors = [COLORS[r] for r in ordered]

    # Build per-bar annotation showing the breakdown. The two 0% reviewers
    # (Seer, Cursor BugBot) need explanatory annotations or the chart reads
    # as "missing data" rather than "this format isn't supported."
    ZERO_NOTES = {
        "seer":   "0%  (prose-only findings)",
        "cursor": "0%  (prose + Cursor-IDE deep-link only)",
    }
    annotations = []
    for r in ordered:
        e, d, s = pct[r]
        if e == 0:
            annotations.append(ZERO_NOTES.get(r, "0%"))
        elif d > 0 and s > 0:
            annotations.append(f"{e:.1f}%  ({d:.0f}% diff, {s:.0f}% suggestion)")
        elif d > 0:
            annotations.append(f"{e:.1f}%  (all diff)")
        else:
            annotations.append(f"{e:.1f}%  (all suggestion)")

    fig, ax = plt.subplots(figsize=(11, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", linewidth=1.5)
    ax.set_xlabel("% of findings with a one-click fix in GitHub")
    ax.set_title("How often does each reviewer ship a one-click fix?", pad=15)
    ax.set_xlim(0, 100)

    for bar, ann in zip(bars, annotations):
        ax.text(
            bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
            ann, va="center", fontsize=11, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def chart_seer_fp_by_severity(conn: sqlite3.Connection, out_path: Path) -> None:
    """Bar chart: Seer's FP rate broken down by severity."""
    sev_order = ["low", "medium", "high", "critical"]
    rows = {}
    for s in sev_order:
        r = conn.execute(f"""
            SELECT
              SUM(v.verdict='valid_fixed'),
              SUM(v.verdict='false_positive'),
              SUM(v.verdict IN ('valid_fixed','false_positive'))
            FROM findings f LEFT JOIN verdicts v ON v.finding_id=f.id
            WHERE f.reviewer='seer' AND f.severity=? AND {MERGED_SCOPE}
        """, (s,)).fetchone()
        valid, fp, total = r
        pct = (100.0 * (fp or 0) / total) if total else 0.0
        rows[s] = {"valid": valid or 0, "fp": fp or 0, "total": total or 0, "pct": pct}

    labels = [s.capitalize() for s in sev_order]
    values = [rows[s]["pct"] for s in sev_order]
    counts = [rows[s]["total"] for s in sev_order]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(labels, values, color=COLORS["seer"], edgecolor="white", linewidth=1.5)
    ax.set_ylabel("False-positive rate (%)")
    ax.set_title("Sentry Seer false-positive rate by severity", pad=15)
    ax.set_ylim(0, max(values) * 1.3 if max(values) > 0 else 5)

    for bar, v, n in zip(bars, values, counts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.4,
            f"{v:.1f}%\n(n={n})",
            ha="center", fontsize=10, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def chart_high_severity_fp(conn: sqlite3.Connection, out_path: Path) -> None:
    """Bar chart: FP rate at each reviewer's HIGHEST severity tier (high+critical
    where available; "major" for the binary-severity reviewers).

    The story: bug-prediction reviewers (Seer, Cursor) take more risk at the
    top tier and pay a higher FP rate. Conservative reviewers (Greptile) stay
    perfect. CodeRabbit's "critical" tier is small but noisier than its general
    rate.
    """
    # Build the comparison rows by reviewer + severity bucket
    queries = [
        ("seer",         "high",     "Seer high"),
        ("coderabbitai", "critical", "CodeRabbit critical"),
        ("cursor",       "high",     "Cursor BugBot high"),
        ("greptile",     "major",    "Greptile major (P1)"),
    ]
    data = []
    for reviewer, severity, label in queries:
        r = conn.execute(f"""
            SELECT
              SUM(v.verdict='false_positive'),
              SUM(v.verdict IN ('valid_fixed','false_positive'))
            FROM findings f LEFT JOIN verdicts v ON v.finding_id=f.id
            WHERE f.reviewer=? AND f.severity=? AND {MERGED_SCOPE}
        """, (reviewer, severity)).fetchone()
        fp, total = r[0] or 0, r[1] or 0
        pct = (100.0 * fp / total) if total else 0.0
        data.append((label, reviewer, pct, total))

    # Order: noisiest at bottom (sort DESCENDING; matplotlib barh flips bottom-up)
    data.sort(key=lambda x: -x[2])
    labels = [d[0] for d in data]
    values = [d[2] for d in data]
    counts = [d[3] for d in data]
    colors = [COLORS[d[1]] for d in data]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", linewidth=1.5)
    ax.set_xlabel("False-positive rate (%) at the reviewer's highest severity tier")
    ax.set_title("Top-tier severity FP rate by reviewer (lower is better)", pad=15)
    ax.set_xlim(0, max(values) * 1.3 if max(values) > 0 else 5)

    for bar, v, n in zip(bars, values, counts):
        ax.text(
            bar.get_width() + 0.25,
            bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}%  (n={n})",
            va="center", fontsize=12, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def chart_latency(conn: sqlite3.Connection, out_path: Path) -> None:
    """Horizontal bar chart: mean time-to-first-finding per reviewer."""
    rows = conn.execute(f"""
        WITH first_finding AS (
            SELECT f.reviewer, f.commit_sha,
                   MIN(f.created_at) AS first_at, c.committed_at
            FROM findings f JOIN commits c ON c.sha = f.commit_sha
            WHERE {MERGED_SCOPE}
            GROUP BY f.reviewer, f.commit_sha, c.committed_at
        )
        SELECT reviewer,
               AVG((julianday(first_at) - julianday(committed_at)) * 1440)
        FROM first_finding
        WHERE first_at > committed_at
        GROUP BY reviewer
    """).fetchall()
    mean_by_reviewer = {r: m for r, m in rows}

    # Order: fastest at top of chart (lowest mean). Sort DESCENDING because
    # matplotlib barh plots from bottom to top.
    ordered = sorted(ORDER, key=lambda r: -mean_by_reviewer.get(r, 0))
    labels = [LABEL[r] for r in ordered]
    values = [mean_by_reviewer[r] for r in ordered]
    colors = [COLORS[r] for r in ordered]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", linewidth=1.5)
    ax.set_xlabel("Mean time to first finding (minutes)")
    ax.set_title("Reviewer latency: commit push to first finding (lower is better)", pad=15)
    ax.set_xlim(0, max(values) * 1.18)

    for bar, v in zip(bars, values):
        ax.text(
            bar.get_width() + 0.15, bar.get_y() + bar.get_height() / 2,
            f"{v:.1f} min", va="center", fontsize=12, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def chart_agreement(conn: sqlite3.Connection, out_path: Path) -> None:
    """Horizontal bar chart: how many reviewers agreed at the same file:line.

    A pie was tempting but the 93/6/0.6 ratio crushes the small categories
    behind their labels. A bar handles the lopsided distribution cleanly.
    """
    rows = conn.execute(f"""
        WITH coords AS (
            SELECT pr_number, file_path, line, COUNT(DISTINCT reviewer) AS n
            FROM findings f
            WHERE file_path IS NOT NULL AND line IS NOT NULL AND {MERGED_SCOPE}
            GROUP BY pr_number, file_path, line
        )
        SELECT n, COUNT(*) FROM coords GROUP BY n ORDER BY n
    """).fetchall()
    by_n = {n: count for n, count in rows}
    total = sum(by_n.values())

    # Bottom-to-top in barh means: put "1 reviewer" at bottom (the headline
    # number visually most prominent), then 2 / 3 above.
    labels = ["3 reviewers\n(rare overlap)", "2 reviewers\n(convergent catch)", "1 reviewer\n(unique catch)"]
    values = [by_n.get(3, 0), by_n.get(2, 0), by_n.get(1, 0)]
    colors = ["#e94f64", "#5b8def", "#94a3b8"]

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="white", linewidth=1.5)
    ax.set_xlabel("Number of distinct file:line coordinates")
    ax.set_title(
        f"At each flagged file:line, how many reviewers agreed? (n = {total})",
        pad=15,
    )
    ax.set_xlim(0, max(values) * 1.18)

    for bar, v in zip(bars, values):
        pct = 100.0 * v / total if total else 0
        ax.text(
            bar.get_width() + max(values) * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{v}  ({pct:.1f}%)",
            va="center", fontsize=12, fontweight="bold",
        )

    plt.savefig(out_path)
    plt.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="db/reviews.db", help="Path to SQLite DB.")
    parser.add_argument("--out-dir", default="data/charts", help="Output directory for PNGs.")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    setup_style()
    conn = sqlite3.connect(db_path)

    charts = [
        ("findings_by_reviewer.png", chart_findings_pie),
        ("fp_rate_by_reviewer.png", chart_fp_rate),
        ("applyable_fix_coverage.png", chart_applyable_fix),
        ("seer_fp_by_severity.png", chart_seer_fp_by_severity),
        ("high_severity_fp_cross_reviewer.png", chart_high_severity_fp),
        ("reviewer_latency.png", chart_latency),
        ("reviewer_agreement.png", chart_agreement),
    ]

    for filename, fn in charts:
        out = out_dir / filename
        fn(conn, out)
        print(f"  wrote {out} ({out.stat().st_size} bytes)")

    conn.close()
    print(f"\nDone. {len(charts)} charts in {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
