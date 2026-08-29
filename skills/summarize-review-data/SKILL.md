---
name: summarize-review-data
description: Summarize the cross-agent-review SQLite ledger to compare how authors are graded, how reviewers tend to grade, reviewer agreement, and collection reliability. Use when the user asks for review statistics, grade averages, reviewer bias, or trends in recorded cross-agent reviews.
---

# Summarize Review Data

Run the bundled `scripts/summarize-review-data` and use its Markdown report as the evidence for the answer. Resolve the script relative to this `SKILL.md`; it may be installed somewhere other than the current repository.

By default the script reads `REVIEW_DB`, then `$XDG_DATA_HOME/cross-agent-review/reviews.db`, then `~/.local/share/cross-agent-review/reviews.db`. It opens the ledger read-only. Pass `--db PATH`, `--project NAME`, or `--since DATE_OR_TIMESTAMP` only when the user asks for that scope.

```bash
python3 scripts/summarize-review-data
python3 scripts/summarize-review-data --project my-app --since 2026-08-01
```

Lead with the author results the user asked for. For every comparison, retain the mean, grade distribution, and sample size together. The report maps the ordinal grades to `A=4`, `B=3`, `C=2`, `D=1`, `F=0`; `NA`, missing grades, and failed attempts are excluded from the mean but remain visible in coverage and status counts.

Use the other sections to qualify the result:

- Reviewer tendencies show raw grading severity and operational reliability. They do not prove that one reviewer is better or harsher because reviewers may see different authors, projects, and tasks.
- Reviewer agreement compares grades from the same invocation. Investigate large spreads before trusting an overall mean.
- Exact, versioned author and reviewer names remain separate, as do different harnesses running the same reviewer. Do not silently merge model versions or aliases.
- Treat small samples as anecdotal. Avoid rankings or claims of trends unless each comparison has enough observations across comparable work.

When useful, suggest a project or time filter for a more comparable follow-up. Do not expose full review text unless the user asks for qualitative analysis of it.
