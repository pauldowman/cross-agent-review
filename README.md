# cross-agent-review

Use other agents/models to do reviews.

It's common practice to have rules requiring coding agents to use a sub-agent with fresh context to review their work. `cross-agent-review` allows them to use other agents, potentially being driven by other harnesses, to do the review instead.

For each model, you can configure which other models review its work.

This makes it easier to experiment with new models, especially ones that may be less capable, so you can be sure that the smartest models are overseeing their work.

In addition to giving feedback on what to fix, it also uses a standard rubric to grade the work, and keeps a database of the scores so that you can see how each model is scored by the others.

## Install

```
npx skills add pauldowman/cross-agent-review --skill cross-agent-review --global --agent '*'
npx skills add pauldowman/cross-agent-review --skill summarize-review-data --global --agent '*'
```

The [`skills` CLI](https://www.npmjs.com/package/skills) installs the complete skill directory for each selected agent, including its bundled script. Each skill resolves its script relative to its own `SKILL.md`, so no separate executable or `PATH` setup is needed.

Single-file Python 3, standard library only. Requires the harnesses selected by the routing config; `claude`, `codex`, and `opencode` are currently supported. Reviewer models are pinned in `reviewers.toml`, and codex's reasoning effort is pinned in the bundled script, so a reviewer never silently inherits either value from a harness's user settings.

## Configuring who reviews whom

Routing lives in `$XDG_CONFIG_HOME/cross-agent-review/reviewers.toml` (default `~/.config/cross-agent-review/reviewers.toml`). There is no built-in fallback: if the file is missing the tool exits 2 and prints the location together with a config template whose model placeholders must be replaced.

```toml
[[rule]]
pattern = "^claude"
reviewers = [
    { harness = "codex", model = "gpt-5.6-sol" },
    { harness = "opencode", model = "opencode/x-preview-f-free" },
]

[[rule]]
pattern = "^gpt|^codex"
reviewers = [
    { harness = "claude", model = "claude-opus-5" },
    { harness = "opencode", model = "opencode/x-preview-f-free" },
]

[[rule]]
pattern = "."
reviewers = [
    { harness = "codex", model = "gpt-5.6-sol" },
    { harness = "claude", model = "claude-opus-5" },
]
```

Each `pattern` is a regular expression matched case-insensitively anywhere in the author's model name. **The first matching rule wins**, so order specific rules before general ones and end with a catch-all — an author matching no rule is an error. Each `reviewers` entry pairs a supported `harness` with the exact `model` string passed to it. Model names are opaque to the script: adding or replacing a model is a config-only change.

`HARNESSES` in `skills/cross-agent-review/scripts/cross-agent-review` defines how supported harness families are invoked. Adding a new harness family still requires code for its command and output format; adding a model does not.

### Timeouts

Each reviewer gets 480 seconds, overridable with `REVIEW_TIMEOUT=<seconds>`. A reviewer that overruns is killed, recorded as a `timeout`, and reported on stderr; the reviews that did finish are still delivered, and stderr says how many of the configured reviewers delivered.

The calling agent's own command timeout has to be larger than `REVIEW_TIMEOUT`, or it kills the tool before the tool can report anything. The bundled skill asks for 600s, which is the ceiling for a Claude Code `Bash` call.

## Summarizing review data

The `summarize-review-data` skill reads the ledger without modifying it and reports author averages, grade distributions, reviewer tendencies, author-by-reviewer results, reviewer agreement, and collection failures. Means use the ordinal mapping `A=4`, `B=3`, `C=2`, `D=1`, `F=0`; the report always keeps the distribution and sample size beside the mean.

```
python3 skills/summarize-review-data/scripts/summarize-review-data
python3 skills/summarize-review-data/scripts/summarize-review-data --project my-app --since 2026-08-01
```

It uses the same `REVIEW_DB` and default database path as `cross-agent-review`. `NA` and failed or unparsed attempts are excluded from means but included in coverage and status counts.

### Reviewer permissions

The tool does not pass flags that bypass permission checks or grant a reviewer unrestricted access. Codex inherits its sandbox and approval behavior from the user's Codex configuration. Claude uses `--permission-mode plan`, and OpenCode uses `run --agent plan`.

Every reviewer is expected to be configured with enough permission to read the repository and run non-mutating inspection commands such as `git diff`. The tool does not elevate a reviewer that cannot read the files; that reviewer may return `NA` or a failed run instead. Configure each harness with the least privilege that works in the environment.

The prompt tells reviewers to make no changes, but a prompt is not a security boundary. The harness's configured sandbox or permission profile is responsible for enforcing access. OpenCode's plan agent denies its dedicated edit tool but still allows shell commands, so its actual protections depend on the surrounding configuration. OpenCode runs with `--format json`, and the tool extracts the last completed text event as the review.

## Known limits

- `SIGKILL` on the tool itself leaks the reviewer subprocesses. `SIGINT` and `SIGTERM` are handled: reviewers are killed and the tool exits in milliseconds, though the interrupt path skips cleanup of codex's empty temp file in `/tmp`.
- A reviewer that escapes its process group by starting its own session survives the timeout kill. The drain is bounded so this cannot hang the tool, but the process is leaked.
- A failed reviewer's harness diagnostics are clipped to a tail of at most 20 lines or 2000 characters, whichever is smaller, preceded by a line saying how much was dropped. Codex narrates its whole session on stderr, and unclipped that transcript buries the reviews that succeeded under two orders of magnitude of noise. The dropped part is gone: nothing records it.
- `cost_usd` is recorded only for harnesses that report it — `claude` does; `codex` and `opencode` do not.
- Reviewer access depends on each harness's configured sandbox or permission profile. The tool does not elevate reviewers that cannot read the repository. See **Reviewer permissions** above.
