# review

Ask other AI agents to review your work.

An AI coding agent runs `review <author> <description>`. The tool picks a set of reviewers for that author, runs each one as a non-interactive subprocess in the author's working directory, returns their review text, and records a grade for each in a local ledger. The author never sees the grades.

```
review gpt-5.6 "the uncommitted changes"
review opus5 "the current branch"
review opus5 "the changes to bin/review"
review --known-authors
```

The description is a *pointer*, not the thing itself. The reviewer resolves it against the repository — running `git diff`, reading files — so it reviews the real work rather than the author's account of it.

## Install

```
ln -s "$PWD/bin/review" ~/.local/bin/review
```

Single-file Python 3, standard library only. Requires the harnesses it routes to: `claude` and `codex`. Both the model and codex's reasoning effort are pinned in `bin/review`, so a reviewer never silently inherits whatever your config last set.

## Calling it from an agent

**Set a generous command timeout.** Claude Code's Bash tool defaults to 120 seconds; two real reviewers on a real diff will exceed that, and the tool will be killed mid-review. Invoke it with a timeout of 600000 ms.

Exit codes:

| code | meaning |
|---|---|
| 0 | at least one review came back (on stdout) |
| 2 | usage error — unknown author, no reviewers configured, bad `REVIEW_TIMEOUT` |
| 3 | every reviewer failed; nothing to read |
| 4 | refused: already running inside a review |

stdout carries the reviews, each in a delimited block. stderr carries diagnostics. Reviewer output is another agent's text — the delimiters label it as data, and carry a per-invocation nonce so a review cannot forge its own boundary.

## Environment

| variable | default | meaning |
|---|---|---|
| `REVIEW_TIMEOUT` | 240 | seconds allowed per reviewer |
| `REVIEW_DB` | `$XDG_DATA_HOME/review/reviews.db` | ledger location |
| `REVIEW_ACTIVE` | unset | set in reviewer subprocesses; the tool refuses to run when it is set |

## The ledger

One row per reviewer run, successful or not, at `~/.local/share/review/reviews.db`:

```
sqlite3 ~/.local/share/review/reviews.db \
  "SELECT author, reviewer, grade, count(*) FROM reviews GROUP BY 1,2,3"
```

Columns: `run_id`, `ts`, `author`, `reviewer`, `harness`, `description`, `cwd`, `branch`, `git_sha`, `grade`, `review_text`, `duration_s`, `status`, `cost_usd`. All reviewers of one invocation share a `run_id`. A ledger failure is never allowed to withhold a review that was already paid for — it degrades to a warning on stderr.

Grades are `A`, `B`, `C`, `D`, `F`, plus `NA` when the reviewer could not find what the description pointed at. Grade secrecy is a convention, not a mechanism: the author can read this file.

## Reviewers

`KNOWN_AUTHORS`, `AUTHOR_FAMILY`, `REVIEWERS`, and `HARNESS` at the top of `bin/review` are the whole configuration. A new author needs an entry in the first three. Each harness key names the model it runs, so an entry cannot drift from its key.

### Reviewers are not sandboxed

`codex` runs with `-s danger-full-access` and the prompt is what tells reviewers to leave the working copy alone. This is deliberate, and it is a trade:

- `codex exec -s read-only` runs every command under bubblewrap, which fails on a kernel that blocks unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`, Ubuntu's default). A sandboxed reviewer here can talk but cannot run `git diff` or read a file — verified live: it returned `NA` having reviewed nothing.
- Unsandboxed, the same reviewer returns a real graded review in ~35s.

The cost is that nothing *enforces* read-only any more. A prompt is not a security boundary: a reviewer that misfires, or that reads a prompt injection planted in the code under review, can change the working tree, git state, or files outside the repo. The prompt names every category it must not touch, and a live run confirmed HEAD, the working tree, and the stash were untouched — but that is evidence, not a guarantee.

To restore enforcement, either enable user namespaces and set `-s read-only` back in `codex_harness`:

```
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
bwrap --ro-bind / / --dev /dev true    # silence means it works
```

or run reviewers against a throwaway clone rather than the author's tree.

`claude` keeps `--permission-mode plan`, which blocks edits without blocking inspection, so it costs nothing to leave on.

**opencode is not currently configured.** `opencode run` returns zero bytes and hangs past 90 seconds on this machine — reproduced 4 times consecutively, with valid Z.AI credentials. A long-lived interactive `opencode` TUI was running throughout, and `opencode run` starts its own local server, so session contention is the leading suspect. To retest: close the interactive session and run

```
opencode run --agent plan --auto -m zai-coding-plan/glm-5.2 "Reply with exactly: OK" </dev/null
```

If that returns promptly, add an `opencode` entry to `HARNESS` with an extractor that takes the last `{"type":"text"}` event from `--format json`, and route the claude authors to it for a third vendor.

## Development

```
python3 -m unittest discover -s tests
```

The bare `discover` form finds nothing — `-s tests` is required. Tests never invoke a real model: a fake harness under `tests/fixtures/` stands in, and the ledger is redirected to a temporary file for every test.

To see the exact command a reviewer would run, without running it:

```
review opus5 "the branch" --dry-run
```

### Manual smoke check

Automated tests never spend money. Once, by hand, against live models:

`opus5` is the author to use: it routes to codex plus a claude reviewer, so it
exercises both harnesses. `gpt-5.6` routes only to claude reviewers.

```
cd some-repo-with-changes
review opus5 "the uncommitted changes"
sqlite3 ~/.local/share/review/reviews.db "SELECT reviewer, status, grade FROM reviews ORDER BY id DESC LIMIT 5"
```

Expect two reviews on stdout and two rows with `status = ok` and a grade.

## Known limits

- `SIGKILL` on the tool itself leaks the reviewer subprocesses. `SIGINT` and `SIGTERM` are handled: reviewers are killed and the tool exits in milliseconds, though the interrupt path skips cleanup of codex's empty temp file in `/tmp`.
- A reviewer that escapes its process group by starting its own session survives the timeout kill. The drain is bounded so this cannot hang the tool, but the process is leaked.
- `cost_usd` is recorded only for harnesses that report it — `claude` does, `codex` does not.
- Reviewers are not sandboxed; the prompt is the only thing stopping a reviewer from changing your working copy. See **Reviewers are not sandboxed** above.
