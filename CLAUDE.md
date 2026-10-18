# daily-digest

Python 3.12 CLI that turns RSS items into a cited digest. Read `docs/PRD.md` (what and why) and `docs/DESIGN.md` (how; normative for who may talk to what) before changing behaviour.

## Commands

- Install: `uv sync`
- Test: `uv run pytest`

## Conventions

- Tests use the mock LLM provider and saved feed fixtures under `tests/fixtures`. Tests and CI never call the network or a paid model, and CI never gets an API key.
- Handoffs between pipeline stages are validated with the pydantic models in `src/daily_digest/contracts.py`.
- Never commit secrets, `.env`, or `data/`.
- Commits use the repo's git identity (`masulyn`). Do not change git config.

## Implement-and-review loop

Use this when asked to implement a task, or when the user says "follow the loop rules". The reviewer is the `code-reviewer` subagent in `.claude/agents/`. It is development tooling, separate from the product's own Reviewer agent in the design doc.

1. Work on a feature branch, never on `main`.
2. Implement the task and run `uv run pytest` until it passes. Commit.
3. Call `code-reviewer`. Give it the output of `git diff main...HEAD`, the base branch, and the list of findings from earlier rounds (none in round 1).
4. Triage every finding as **Fix** or **Reject**. Reject only with a written reason. Fix blocking findings; when a bug has other instances, fix them all. Skip advisory findings unless they are trivial, and list the skipped ones.
5. Run `uv run pytest`, commit the fixes, and call `code-reviewer` again with only the diff since the last reviewed commit plus the prior findings.
6. Stop after at most **3 review rounds**.
   - If the reviewer returns `VERDICT: CLEAN` (or advisory only), go to step 7.
   - If blocking findings remain after round 3, stop. Do not push. Summarize each open finding and the disagreement, and ask the user to decide.
7. Ask the user before pushing. After they approve, push the branch and open a PR with `gh pr create`. Put a round-by-round summary in the PR body: findings, fixed or rejected (with reasons), and skipped advisories.
8. Never merge, never force-push, never approve the PR. The user does the final review and the merge.
