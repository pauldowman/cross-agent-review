# cross-agent-review

Ask other AI agents to review your work.

An AI coding agent runs the script bundled with the skill, passing `<author> <project> <goal> <description>`. The tool picks a set of reviewers for that author, runs each one as a non-interactive subprocess in the author's working directory, returns their review text, and records a grade for each in a local ledger. The author never sees the grades.

```
python3 skills/cross-agent-review/scripts/cross-agent-review claude-opus-5 my-app "add cursor pagination" "the uncommitted changes"
python3 skills/cross-agent-review/scripts/cross-agent-review gpt-5.6-sol my-app "fix the retry backoff" "the current branch"
```

| argument | meaning |
|---|---|
| `author` | the model name of the agent that did the work, precise and including its version. Any string is accepted — a model names itself — and it selects the reviewers. |
| `project` | the repository name. Groups the ledger across checkouts and machines; recorded as given, never checked against the repo, so worktrees and monorepo subdirectories can name themselves usefully. |
| `goal` | what the work was trying to achieve. Reviewers grade against it — `D` in the rubric is "misunderstands the goal". |
| `description` | what to review. |

The description is a *pointer*, not the thing itself. The reviewer resolves it against the repository — running `git diff`, reading files — so it reviews the real work rather than the author's account of it.

## Install

```
npx skills add pauldowman/cross-agent-review --skill cross-agent-review --global --agent '*'
```

The [`skills` CLI](https://www.npmjs.com/package/skills) installs the complete skill directory for each selected agent, including its bundled script. The skill resolves the script relative to its own `SKILL.md`, so no separate executable or `PATH` setup is needed.

Single-file Python 3, standard library only. Requires the harnesses selected by the routing config; `claude`, `codex`, and `opencode` are currently supported. Reviewer models are pinned in `reviewers.toml`, and codex's reasoning effort is pinned in the bundled script, so a reviewer never silently inherits either value from a harness's user settings.

## Calling it from an agent

**Set a generous command timeout.** Claude Code's Bash tool defaults to 120 seconds; two real reviewers on a real diff will exceed that, and the tool will be killed mid-review. Invoke it with a timeout of 600000 ms.

Exit codes:

| code | meaning |
|---|---|
| 0 | at least one review came back (on stdout) |
| 2 | usage error — no matching rule, no reviewers configured, bad `REVIEW_TIMEOUT` |
| 3 | every reviewer failed; nothing to read |
| 4 | refused: already running inside a review |

stdout carries the reviews, each in a delimited block. stderr carries diagnostics. Reviewer output is another agent's text — the delimiters label it as data, and carry a per-invocation nonce so a review cannot forge its own boundary.

## Environment

| variable | default | meaning |
|---|---|---|
| `REVIEW_TIMEOUT` | 240 | seconds allowed per reviewer |
| `REVIEW_DB` | `$XDG_DATA_HOME/cross-agent-review/reviews.db` | ledger location |
| `CROSS_AGENT_REVIEW_CONFIG` | `$XDG_CONFIG_HOME/cross-agent-review/reviewers.toml` | routing rules |
| `REVIEW_ACTIVE` | unset | set in reviewer subprocesses; the tool refuses to run when it is set |

## The ledger

One row per reviewer run, successful or not, at `~/.local/share/cross-agent-review/reviews.db`:

```
sqlite3 ~/.local/share/cross-agent-review/reviews.db \
  "SELECT project, author, harness, reviewer, grade, count(*) FROM reviews GROUP BY 1,2,3,4,5"
```

Columns: `run_id`, `ts`, `project`, `author`, `goal`, `reviewer`, `harness`, `description`, `cwd`, `branch`, `git_sha`, `grade`, `review_text`, `duration_s`, `status`, `cost_usd`. All reviewers of one invocation share a `run_id`. A ledger failure is never allowed to withhold a review that was already paid for — it degrades to a warning on stderr.

Grades are `A`, `B`, `C`, `D`, `F`, plus `NA` when the reviewer could not find what the description pointed at. Grade secrecy is a convention, not a mechanism: the author can read this file.

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

Route each author away from its own model, or it reviews itself.

`HARNESSES` in `skills/cross-agent-review/scripts/cross-agent-review` defines how supported harness families are invoked. Adding a new harness family still requires code for its command and output format; adding a model does not.

### Reviewer permissions

`codex` runs with `-s danger-full-access` and the prompt is what tells it to leave the working copy alone. This is deliberate, and it is a trade:

- `codex exec -s read-only` runs every command under bubblewrap, which fails on a kernel that blocks unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`, Ubuntu's default). A sandboxed reviewer here can talk but cannot run `git diff` or read a file — verified live: it returned `NA` having reviewed nothing.
- Unsandboxed, the same reviewer returns a real graded review in ~35s.

The cost is that nothing *enforces* read-only for codex. A prompt is not a security boundary: a reviewer that misfires, or that reads a prompt injection planted in the code under review, can change the working tree, git state, or files outside the repo. The prompt tells reviewers to make no changes, and a live run confirmed HEAD, the working tree, and the stash were untouched — but that is evidence, not a guarantee.

To restore enforcement, either enable user namespaces and set `-s read-only` back in `codex_harness`:

```
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
bwrap --ro-bind / / --dev /dev true    # silence means it works
```

or run reviewers against a throwaway clone rather than the author's tree.

`claude` uses `--permission-mode plan`, and `opencode` uses `run --agent plan`. OpenCode's plan agent denies its dedicated edit tool, but it still allows shell commands, which can change files; this is not read-only enforcement. OpenCode runs with `--format json`, and the tool extracts the last completed text event as the review. In the example, `opencode/x-preview-f-free` is the provider/model ID currently exposed for Ox Alpha; use `opencode models` to discover its replacement when that preview rotates.

## Development

```
python3 -m unittest discover -s tests
```

The skill agents use to call this lives in `skills/cross-agent-review/SKILL.md`, so it stays in step with the bundled script. See **Install** for installation through the `skills` CLI.

The bare `discover` form finds nothing — `-s tests` is required. Tests never invoke a real model: a fake harness under `tests/fixtures/` stands in, and the ledger is redirected to a temporary file for every test.

To see the exact command a reviewer would run from this checkout, without running it:

```
python3 skills/cross-agent-review/scripts/cross-agent-review claude-opus-5 my-app "the goal" "the branch" --dry-run
```

### Manual smoke check

Automated tests never spend money. Once, by hand, against live models:

With the routing shown above, run once as a Claude author to exercise codex and OpenCode, then once as a GPT/Codex author to exercise Claude and OpenCode.

```
cd some-repo-with-changes
python3 /path/to/cross-agent-review/skills/cross-agent-review/scripts/cross-agent-review claude-opus-5 "$(basename "$PWD")" "the goal" "the uncommitted changes"
python3 /path/to/cross-agent-review/skills/cross-agent-review/scripts/cross-agent-review gpt-5.6-sol "$(basename "$PWD")" "the goal" "the uncommitted changes"
sqlite3 ~/.local/share/cross-agent-review/reviews.db "SELECT project, reviewer, status, grade FROM reviews ORDER BY id DESC LIMIT 5"
```

Expect two ledger rows per invocation. Successful reviewers produce output with `status = ok` and a grade; a failed or timed-out reviewer gets its own explicit status without withholding the other review. The Ox Alpha preview accepted the configured provider/model ID in the latest live smoke test but emitted no events before it was interrupted, so its command and protocol are covered but a successful live review has not yet been observed.

## Schema changes

The ledger carries a `user_version` stamp. An older database is migrated in place on open (`MIGRATIONS` in `skills/cross-agent-review/scripts/cross-agent-review`); rows recorded before a column existed keep a NULL for it. A database written by a *newer* version is refused rather than relabelled, and the tool degrades to a warning rather than losing the review.

## Known limits

- `SIGKILL` on the tool itself leaks the reviewer subprocesses. `SIGINT` and `SIGTERM` are handled: reviewers are killed and the tool exits in milliseconds, though the interrupt path skips cleanup of codex's empty temp file in `/tmp`.
- A reviewer that escapes its process group by starting its own session survives the timeout kill. The drain is bounded so this cannot hang the tool, but the process is leaked.
- `cost_usd` is recorded only for harnesses that report it — `claude` does; `codex` and `opencode` do not.
- Codex is unsandboxed, and OpenCode's plan agent can still write through shell commands; their prompts are the control against those changes. See **Reviewer permissions** above.
