# daily-digest

Turns RSS items into a cited digest. See `docs/PRD.md` (what and why) and `docs/DESIGN.md` (how).

## Commands

```
uv run daily-digest ingest                    # read the feeds into the local database
uv run daily-digest extract                   # fetch full text for items whose feed text is short, teaser-like or cut at the extract limit (--days N: how far back, default 2)
uv run daily-digest draft --stub              # offline placeholder summaries; no key needed
uv run daily-digest draft [--date YYYY-MM-DD] # writes the day's digest as Markdown, then a Reviewer checks it (at least two model calls)
uv run daily-digest draft --debug             # also log every model call and review round (counts and dollars, never text or keys) to stderr
uv run daily-digest draft --no-review        # skip the Reviewer: cheaper, but the digest is marked NOT REVIEWED and the exit code is 3
uv run daily-digest generate [--date YYYY-MM-DD] [--out-dir PATH]   # make yesterday's digest for real: review it, save it, mark its items seen
```

`draft` prints the digest (headline, two sections with `[n]` citations, a numbered Sources list), then any notes, then a usage line (calls, tokens in and out, dollars). The usage line prints on every exit, including errors. An illustrative run (numbers made up):

```
$ ANTHROPIC_API_KEY=... uv run daily-digest draft --date 2026-10-16
# Pilots reach production as governance lags

Covered 2026-10-16 · generated 2026-10-20 09:14 PDT · 2 min read

## What you need to know
...
## Sources

1. [Title](https://example.com/...) — Example Wire, 2026-10-16
usage: 1 call(s), 9,800 tokens in, 900 out, $0.0286
```

A day with fewer than three items makes no model call and prints a "No significant developments" note.

### Review

After the digest is written, a Reviewer checks each sentence against the sources it cites and asks for fixes, for up to 3 rounds (`max_review_rounds` in `config/settings.toml`). Approval is decided by code, never by the model: a draft is approved only when neither the code checks nor the model leave a blocking issue. A `review:` line follows the digest:

- `review: approved after 2 round(s)` - no blocking issue is left. Advisory issues are listed as bullets.
- `review: not converged after 3 round(s); 1 sentence removed` - a blocking issue survived every round, so code removed the flagged sentence and re-checked the rest. The removed sentences are listed as `unresolved` bullets.
- `review: degraded at 80% of the budget after 1 round(s); 1 sentence removed` - spending reached 80% of the $0.25 cap, so no further model call was started; the same removal was applied to the last draft the Reviewer judged.
- `review: NOT REVIEWED` - the Reviewer could not run (budget or an error) or you passed `--no-review`. The digest is printed under a banner and must not be published.
- `review: FAILED ...` - the draft could not be repaired (for example, removing a flagged sentence left it under 200 words, or a blocking issue on the headline survived the last round: the headline cannot be fixed by removing a sentence, so it is never published as written). No digest is printed.

Exit codes: `0` a reviewed digest (approved, not converged or degraded) or nothing to write; `1` an error or a draft that could not be repaired; `2` a setup problem (no key, bad settings, missing database); `3` the digest was not reviewed. A review adds about 2 to 4 cents per round; most days should approve in one or two rounds.

### Generate

`generate` is `draft` made permanent. It runs the same pipeline (writer, then Reviewer), and if the digest passes a last check it saves three things and records the day's items as seen:

- `newsletters/YYYY-MM-DD.md` - the digest exactly as `draft` prints it, plus a final italic line saying how it was reviewed (for example `*Review: approved after 2 round(s).*`).
- `newsletters/YYYY-MM-DD.json` - the digest as data, plus a provenance record for every sentence (its type, the items behind it, the quote and speaker) and what the run cost. It holds no prompts and no key, and no article text beyond what the digest itself quotes (its notes can quote sentences that were dropped).
- `newsletters/index.md` - every saved digest, newest first, with a one-line preview. It is rebuilt from the saved files each time, so it heals itself if you delete one.

Every item the pipeline considered that day (not only the cited ones) goes into the seen-items ledger, but only after the files are saved, so a failed run never uses items up. `newsletters/` is git-ignored: digests quote articles and are your data. To show one publicly, copy it into `examples/` on purpose.

- **Date:** defaults to yesterday. `--date` must be earlier than today.
- **One digest per date, never overwritten.** If `newsletters/<date>.md` exists, `generate` prints `ALREADY_EXISTS: <path>` before reading the key or calling any model, and exits 0. There is no way to replace a digest in v1.
- **What gets saved:** `approved`, `not_converged` and `degraded` digests. A digest the Reviewer never judged (`unreviewed`, including `--no-review`) or could not repair (`failed`) is never saved and marks nothing as seen.
- **A last check before saving** looks at what code built from the draft: every `[n]` marker has a Sources entry and the other way round with numbers 1 to N, every Sources URL is one of that day's items, Sources titles are one line, and the text has no angle brackets, HTML or Markdown links. The words themselves were already checked by the Reviewer. A failure prints a fixed reason, saves nothing and exits 1.
- **A quiet day** (fewer than 3 items) saves nothing and exits 0.
- **Exit codes:** `0` saved, already exists, or nothing to write; `1` error, a draft that could not be repaired, a failed check, or a ledger that could not be updated (the files stay and the message says the next digest may repeat items); `2` setup problem (no key, bad settings, missing database, a bad `--date`); `3` not reviewed, nothing saved.
- **Cost:** the same as `draft`, since it runs the same pipeline. `draft` itself still saves nothing and writes no ledger rows.

## Model access and cost

`draft` without `--stub` calls the Anthropic API at least twice per run (the draft, then a review; each fix adds a revision call and another review). Model, prices, and caps live in `config/settings.toml`. The default hard cap is **$0.25 per run** (one call is typically 2 to 4 cents; a retry after a failed check can double it). Before each call the app estimates the worst case (about 2.7 characters per token plus the output format, calibrated on one real digest, which errs a little high for English but can undercount other text) and makes no call if the estimate would cross the cap; the real cost is computed from the usage the API returns. If an attempt times out or the connection drops after the request was sent, it may still have been billed, so the usage line counts it at the worst-case estimate and says so; the Console usage page is the source of truth for actual spend. A timed-out attempt reserves a full output allowance (6,000 tokens), so a second timeout usually ends the run because the next attempt would cross the $0.25 cap. Prices in the file are third-party-reported; check them against the [pricing page](https://platform.claude.com/docs/en/about-claude/pricing).

### 1. Create a key

1. Sign in to the [Claude Console](https://platform.claude.com/) and open the [API keys](https://platform.claude.com/settings/keys) page.
2. Click **Create key**, name it, and choose an expiration. Use a personal key for your own development.
3. Copy the key (it starts with `sk-ant-`). The Console shows it only once.

### 2. Set a spend limit as a second backstop

The app's cap stops a single run; a Console spend limit stops everything else. Limits cannot be set on the Default Workspace, so make a dedicated one.

1. On the [Workspaces](https://platform.claude.com/settings/workspaces) page, create a workspace (for example `daily-digest`) and create the key in step 1 scoped to it.
2. Select the workspace, click **Spend limits**, and set a monthly cap.

### 3. Keep the key out of your shell

The app reads the key only from the `ANTHROPIC_API_KEY` environment variable and never stores, prints, or logs it. Do not `export` it in your shell profile or type it into a command that lands in shell history.

On macOS, store it once in the login keychain. With `-w` last and no value, `security` prompts for it, so it never enters shell history:

```
security add-generic-password -a "$USER" -s daily-digest-anthropic -w
```

Then read it for a single run, so it exists only in that process:

```
ANTHROPIC_API_KEY=$(security find-generic-password -a "$USER" -s daily-digest-anthropic -w) uv run daily-digest draft
```

To remove it: `security delete-generic-password -a "$USER" -s daily-digest-anthropic`.

Without a key, `draft` exits with a one-line error; `draft --stub` needs none.

## Secret scanning

CI runs [Gitleaks](https://github.com/gitleaks/gitleaks) on the commits of every push to `main` and every pull request, and fails the build on a finding. A feature branch is covered once its PR is open. The job downloads a pinned release binary and checks it against a pinned sha256 taken from the release's published checksums file. It has read-only permissions, and CI uses no secrets or tokens at all (the tests use the mock LLM provider).

To scan locally on macOS, download the same release with `curl` (unlike a browser, it does not trigger Gatekeeper quarantine), check its sha256 (the command fails if it differs), and run the binary from where you extracted it. Use `arm64` on Apple silicon and `x64` on Intel:

```
V=8.30.1; ARCH=arm64   # or x64
case $ARCH in
  arm64) SUM=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  x64)   SUM=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
esac
curl -sSfL -o gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v$V/gitleaks_${V}_darwin_$ARCH.tar.gz"
echo "$SUM  gitleaks.tar.gz" | shasum -a 256 -c -   # stops here if the hash differs
mkdir -p ~/bin/gitleaks-$V && tar -xzf gitleaks.tar.gz -C ~/bin/gitleaks-$V gitleaks
```

Then scan the whole history of every branch from the repo root. `--redact` keeps any secret out of the output:

```
V=8.30.1   # same version as above; set it again in a new terminal
~/bin/gitleaks-$V/gitleaks git --log-opts="--all" --redact .
```

The exit code is 1 when something is found and 0 when clean. To scan only what your branch adds, use `--log-opts="origin/main..HEAD"`. If you downloaded the archive in a browser instead, macOS may refuse to run it; `xattr -d com.apple.quarantine gitleaks` on the extracted binary fixes that.
