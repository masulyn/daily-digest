"""Reports over recorded runs: summary, baseline, comparison and the human spot check."""

import random
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from .models import BAD_STATUSES, DIMENSIONS, Baseline, BaselineCase, CaseResult, RunResults

DROP_THRESHOLD = 0.5  # a dimension mean falling by this much is flagged
NOISE_NOTE = (
    "Each case ran once and model output varies between runs, so small differences are noise. "
    "This is not a significance test."
)


class ReportError(Exception):
    """A run, baseline or option cannot be used; the message says why."""


def load_run(results_root: Path, run_id: str) -> tuple[RunResults, Path]:
    if not run_id or "/" in run_id or run_id.startswith("."):
        raise ReportError(f"not a run id: {run_id!r}")
    run_dir = results_root / run_id
    try:
        return RunResults.model_validate_json((run_dir / "results.json").read_text(encoding="utf-8")), run_dir
    except FileNotFoundError:
        raise ReportError(f"no results for run {run_id!r} in {results_root}") from None
    except (OSError, ValidationError, ValueError):
        raise ReportError(f"results for run {run_id!r} are unreadable") from None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _judged(run: RunResults) -> list[CaseResult]:
    return [c for c in run.cases if c.judge]


def render_report(run: RunResults) -> str:
    lines = [
        f"# Eval run {run.run_id}", "",
        f"Generator `{run.config.generator_model}`, judge `{run.config.judge_model}`, rubric v{run.rubric_version}"
        + (", MOCK (scripted replies; the scores mean nothing)" if run.config.mock else "")
        + (", judge off" if not run.config.judge_enabled else "")
        + (", uncommitted changes in the tree" if run.config.git_dirty else "") + ".",
        "Prompt hashes: " + ", ".join(f"{k} {v}" for k, v in run.prompt_hashes.items()) + ".",
        f"Spent ${float(run.total_usd):.4f} of the ${float(run.config.max_usd_per_eval):.2f} eval cap.", "",
    ]
    hits = [(c.id, h) for c in run.cases for h in c.canary_hits]
    if hits:
        lines += ["## CANARY HITS (these cases failed)", *[f"- {case}: `{hit}` appeared in the digest" for case, hit in hits], ""]
    if run.stopped_by_cap:
        lines += [f"## Stopped by the eval spend cap: {len(run.skipped_cases)} case(s) not run",
                  ", ".join(run.skipped_cases), ""]
    lines += ["## Cases", "", "| case | category | status | faith | cover | fit | concise | mean | cost |", "|---|---|---|---|---|---|---|---|---|"]
    for c in run.cases:
        s = c.judge.scores if c.judge else {}
        lines.append(
            f"| {c.id} | {c.category} | {c.status}{' (CANARY)' if c.canary_hits else ''} | "
            + " | ".join(str(s.get(d, "-")) for d in DIMENSIONS)
            + f" | {_fmt(c.mean_score)} | ${c.total_usd:.4f} |"
        )
    judged = _judged(run)
    lines += ["", "## Weakest cases (the top 3 failures)", ""]
    problems = [c for c in run.cases if c.status in BAD_STATUSES or c.canary_hits]
    ranked = sorted(judged, key=lambda c: c.mean_score)[:3]
    if not (problems or ranked):
        lines.append("Nothing was judged.")
    for c in problems:
        reason = "canary in the digest" if c.canary_hits else (c.error or c.status)
        lines.append(f"- {c.id}: **{c.status}** ({reason})")
    for c in ranked:
        low = min(c.judge.scores, key=lambda d: c.judge.scores[d])
        lines.append(f"- {c.id}: mean {_fmt(c.mean_score)}; lowest {low} ({c.judge.scores[low]}): {c.judge.rationales[low]}")
    lines += ["", "## Judge findings", ""]
    if judged:
        means = {d: _mean([c.judge.scores[d] for c in judged]) for d in DIMENSIONS}
        lines.append("Mean scores: " + ", ".join(f"{d} {_fmt(m)}" for d, m in means.items()) + f" ({len(judged)} judged).")
        kinds = Counter(u.category for c in judged for u in c.judge.unsupported_sentences)
        lines.append("Problem types the judge flagged: " + (", ".join(f"{k} x{n}" for k, n in kinds.most_common()) or "none") + ".")
        lows = [f"{c.id} {d} ({c.judge.scores[d]}): {c.judge.rationales[d]}" for c in judged for d in DIMENSIONS if c.judge.scores[d] <= 2]
        lines += [f"- low score, {low}" for low in lows[:8]]
        must = [v for c in judged for v in c.judge.covers_must.values()]
        if must:
            lines.append(f"Must-cover items used: {sum(must)} of {len(must)}.")
    else:
        lines.append("No judge scores in this run.")
    lines += ["", "## Pipeline", ""]
    n = len(run.cases)
    counts = Counter(c.status for c in run.cases)
    if n:
        lines.append("Status: " + ", ".join(f"{k} {v} ({v / n:.0%})" for k, v in counts.most_common()) + ".")
        made = [c for c in run.cases if c.status not in ("no_digest", "error")]
        lines.append(f"Mean review rounds {_fmt(_mean([c.review_rounds for c in made]))}, "
                     f"mean sentences removed {_fmt(_mean([c.sentences_removed for c in made]))}, "
                     f"mean dropped-sentence notes {_fmt(_mean([c.dropped_sentence_notes for c in made]))}.")
        costs = [c.total_usd for c in run.cases]
        lines.append(f"Cost per case: mean ${_mean(costs):.4f}, max ${max(costs):.4f}.")
    return "\n".join(lines) + "\n"


def make_baseline(run: RunResults) -> Baseline:
    if run.config.mock:
        raise ReportError("a mock run has no real scores and cannot be a baseline")
    if not run.config.judge_enabled or not _judged(run):
        raise ReportError("a run without judge scores cannot be a baseline")
    if run.stopped_by_cap:
        raise ReportError("this run was stopped by the eval cap and is partial; run the rest first")
    return Baseline(
        run_id=run.run_id, config=run.config, prompt_hashes=run.prompt_hashes, rubric_version=run.rubric_version,
        cases=[BaselineCase(id=c.id, status=c.status, scores=c.judge.scores if c.judge else None, canary_hits=c.canary_hits)
               for c in run.cases],
    )


def save_baseline(run: RunResults, path: Path, *, force: bool) -> None:
    baseline = make_baseline(run)
    if path.exists() and not force:
        raise ReportError(f"{path.name} already exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> Baseline:
    try:
        return Baseline.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReportError(f"no baseline at {path}; create one with `eval baseline <run-id>`") from None
    except (OSError, ValidationError, ValueError):
        raise ReportError(f"the baseline at {path} is unreadable") from None


def _cases(source: RunResults | Baseline) -> dict[str, BaselineCase]:
    if isinstance(source, Baseline):
        return {c.id: c for c in source.cases}
    return {c.id: BaselineCase(id=c.id, status=c.status, scores=c.judge.scores if c.judge else None,
                               canary_hits=c.canary_hits) for c in source.cases}


def config_differences(now: RunResults | Baseline, before: RunResults | Baseline) -> list[str]:
    diffs = []
    a, b = now.config, before.config
    for name in ("generator_model", "judge_model", "mock", "judge_enabled"):
        if getattr(a, name) != getattr(b, name):
            diffs.append(f"{name}: {getattr(b, name)} -> {getattr(a, name)}")
    if a.prices != b.prices:
        diffs.append("price table changed")
    for key in sorted(set(a.settings) | set(b.settings)):
        if a.settings.get(key) != b.settings.get(key):
            diffs.append(f"setting {key}: {b.settings.get(key)} -> {a.settings.get(key)}")
    for key in sorted(set(now.prompt_hashes) | set(before.prompt_hashes)):
        if now.prompt_hashes.get(key) != before.prompt_hashes.get(key):
            diffs.append(f"{key} prompt changed")
    if now.rubric_version != before.rubric_version:
        diffs.append(f"rubric version: {before.rubric_version} -> {now.rubric_version}")
    return diffs


def compare(now: RunResults, before: RunResults | Baseline) -> tuple[str, list[str]]:
    """The comparison text and the flags raised. Flags are about size and new failures, not significance."""
    cur, old = _cases(now), _cases(before)
    both = [i for i in cur if i in old and cur[i].scores and old[i].scores]
    label = "the baseline" if isinstance(before, Baseline) else "run " + before.run_id
    lines = [f"# Run {now.run_id} compared with {label} (run {before.run_id})", ""]
    diffs = config_differences(now, before)
    lines.append("What differs: " + ("; ".join(diffs) + "." if diffs else "the configuration, prompts and rubric are identical."))
    lines += ["", f"## Dimension means over the {len(both)} case(s) scored in both runs", "", "| dimension | before | now | change |", "|---|---|---|---|"]
    flags: list[str] = []
    for d in DIMENSIONS:
        b, n = _mean([old[i].scores[d] for i in both]), _mean([cur[i].scores[d] for i in both])
        change = None if b is None or n is None else n - b
        lines.append(f"| {d} | {_fmt(b)} | {_fmt(n)} | {'-' if change is None else f'{change:+.2f}'} |")
        if change is not None and change <= -DROP_THRESHOLD:
            flags.append(f"{d} mean fell by {-change:.2f} (threshold {DROP_THRESHOLD})")
    lines += ["", "## Per case (change in score, then the status)", "", "| case | " + " | ".join(DIMENSIONS) + " | status |", "|---|" + "---|" * (len(DIMENSIONS) + 1)]
    for i in sorted(cur):
        c, o = cur[i], old.get(i)
        if o and c.scores and o.scores:
            cells = [f"{c.scores[d] - o.scores[d]:+d}" for d in DIMENSIONS]
        else:
            cells = ["-"] * len(DIMENSIONS)
        status = c.status if o is None or o.status == c.status else f"{o.status} -> {c.status}"
        lines.append(f"| {i} | " + " | ".join(cells) + f" | {status}{' (new case)' if o is None else ''} |")
        if c.canary_hits and not (o and set(c.canary_hits) <= set(o.canary_hits)):
            flags.append(f"new canary hit in {i}")
        if c.status in BAD_STATUSES and not (o and o.status in BAD_STATUSES):
            flags.append(f"{i} is now {c.status}")
        if o and o.scores and not c.scores and c.status not in BAD_STATUSES:
            flags.append(f"{i} was scored before and has no scores now")
    missing = sorted(set(old) - set(cur))
    if missing:
        lines += ["", "Cases in the earlier run but not this one: " + ", ".join(missing) + "."]
    lines += ["", "## Flags", ""]
    lines += [f"- {f}" for f in flags] or ["None."]
    lines += ["", NOISE_NOTE]
    return "\n".join(lines) + "\n", flags


def spot_check(run: RunResults, run_dir: Path, n: int, seed: int | None = None) -> Path:
    """Write `spot-check.md`: N random digests beside their sources, with a blank rating line to fill in."""
    candidates = [c for c in run.cases if (run_dir / c.id / "digest.md").is_file()]
    if not candidates:
        raise ReportError("this run saved no digests to check")
    picked = random.Random(seed).sample(candidates, min(n, len(candidates)))
    lines = ["# Spot check", "",
             "Read each digest against its sources and rate it yourself before looking at the judge's scores "
             "at the bottom of the entry.", ""]
    for c in picked:
        folder = run_dir / c.id
        lines += [f"## {c.id}", "", "### Digest", "", (folder / "digest.md").read_text(encoding="utf-8"), "",
                  "### Sources as the writer saw them", "", "```", (folder / "items.md").read_text(encoding="utf-8").rstrip(), "```", "",
                  "Your rating (1 to 5): faithfulness __  coverage __  audience fit __  concision __", "",
                  "Notes: ", "", "### Judge (read after rating)", ""]
        lines.append(", ".join(f"{d} {c.judge.scores[d]}" for d in DIMENSIONS) if c.judge else "not judged")
        lines.append("")
    path = run_dir / "spot-check.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
