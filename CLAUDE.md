# daily-digest

Python 3.12 CLI that turns RSS items into a cited digest. Read `docs/PRD.md` (what and why) and `docs/DESIGN.md` (how; normative for who may talk to what) before changing behaviour.

## Commands

- Install: `uv sync`
- Test: `uv run pytest`

## Conventions

- Tests use the mock LLM provider and saved feed fixtures under `tests/fixtures`. Tests and CI never call the network or a paid model, and CI never gets an API key.
- Handoffs between pipeline stages are validated with the pydantic models in `src/daily_digest/contracts.py`.
- Code comments are short and only explain non-obvious choices (why, not what). Do not restate what the code says, describe self-explanatory implementation, or write anything that can go stale (callers, ticket numbers, "currently", "for now").
- Never commit secrets, `.env`, or `data/`.
- Commits use the repo's git identity (`masulyn`). Do not change git config.

## Implement-and-review loop

Use this when the user asks to "implement X with review loop" (or "with the review loop"). Without that phrase, implement normally and do not run the loop. The main session is the implementer. The reviewer is the `code-reviewer` subagent in `.claude/agents/`. It is development tooling, separate from the product's own Reviewer agent in the design doc.

1. Work on a feature branch, never on `main`.
2. Implement the task and run `uv run pytest` until it passes. Commit.
3. Call `code-reviewer`. Give it the output of `git diff main...HEAD`, the base branch, and the decisions made on earlier findings (none in round 1).
4. Triage every finding, blocking or advisory, as **Fix**, **Defer**, or **Reject**. Give a written reason for every Defer and Reject.
   - Blocking findings are fixed. When a bug has other instances, fix them all.
   - Advisory findings (style, nits, comments) are either fixed or deferred. Fix the cheap ones. Defer only what is out of scope or not worth the churn here. Never drop one silently.
5. Run `uv run pytest`, commit the fixes, and call `code-reviewer` again with only the diff since the last reviewed commit plus the decisions on prior findings. The reviewer accepts or objects to each decision. Settle objections in the next round.
6. Stop after at most **3 review rounds**.
   - If the reviewer returns `VERDICT: CLEAN` or `VERDICT: ADVISORY ONLY` with every advisory fixed or accepted as deferred, go to step 7.
   - If blocking findings or objections remain after round 3, stop. Do not push. Summarize each open item and the disagreement, and ask the user to decide.
7. Before asking to push, give the user a summary in the session with these sections:
   - **What the PR does**: the change in a few sentences, in terms of behaviour.
   - **How it is implemented**: a high-level description of the approach and the non-obvious choices, without walking through the code.
   - **File and folder changes**: files and folders added, removed, moved, or renamed, with one line each. Say "none" if the structure is unchanged.
   - **Deferred items**: every deferred finding with its reason and enough detail to paste into the next coding session. Say "none" if there are none.
8. Ask the user before pushing. After they approve, push the branch and open a PR with `gh pr create`. Use the step 7 summary as the PR body, without any Merge order section, plus a short round-by-round list of findings and how each was settled.
9. Never merge, never force-push, never approve the PR. The user does the final review and the merge.

## Parallel loops

Use these rules when the user asks to implement several things in parallel with the review loop. Several loops running at once cause merge conflicts unless each is isolated and the work does not overlap.

1. **Clear stale worktrees first.** Before starting, run `git fetch origin --prune` and `git worktree list`, and look at every worktree except the main checkout and the current one.
   - Remove a worktree (`git worktree remove <path>`, then delete its branch) only when it is clean and its branch is fully merged into `origin/main` or its PR is merged or closed.
   - If a worktree has uncommitted changes, unpushed commits, or an open PR, do not remove it. List it for the user with what it holds.
   - Run `git worktree prune` for entries whose directory is gone.
   - Report what was removed and what was kept.
2. **One worktree per loop.** Run each task's loop in its own git worktree and branch: `claude --worktree <task-name>`, or enter one with `EnterWorktree` if this session is not already in one. Never run two loops in the same checkout.
3. **Check overlap before starting.** List the files and modules the task will likely touch and compare them with the other parallel tasks (ask the user what they are if unknown). Shared modules such as `cli.py`, `contracts.py` and `config.py` are the likely collision points. If tasks overlap, tell the user before implementing and suggest running those tasks one after another.
4. **Rebase before the summary.** When the loop is clean and before step 7, run `git fetch origin`, `git rebase origin/main`, then `uv run pytest`. The branch is not pushed yet, so no force-push is needed.
   - Trivial conflicts: resolve them, rerun the tests, and call `code-reviewer` once on the rebased diff.
   - Nontrivial conflicts, or conflicts in logic rather than text: run `git rebase --abort`, stop, and ask the user.
5. **Recommend a merge order.** Add a **Merge order** section to the step 7 summary in the session only. Leave it out of the PR body. Rank this PR against the other parallel tasks you know of, foundational and lower-risk changes first, and give a one-line reason per position: files shared with another task, one change depending on another, or risk. Note which PRs will need their branch updated after each merge. If you only know your own task, say which files and modules you touch so the user can order the PRs across sessions.
