"""Run the real pipeline on each case, judge the result, and record everything."""

import hashlib
import logging
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ..config import PROJECT_ROOT, EvalSettings, LLMSettings
from ..contracts import Draft, ReviewRound
from ..digest import render_markdown
from ..editor import (
    HEADLINE_PROMPT, LIGHT_REVISE_PROMPT, LIGHT_SYSTEM_PROMPT, REVISE_PROMPT, SYSTEM_PROMPT, WriterFailed, render_items,
    word_count,
)
from ..graph import DigestState, run_digest
from ..llm import LLMClient, LLMError, MeteredClient
from ..publisher import markdown_for
from ..reviewer import HEADLINE_REVIEW_PROMPT, REVIEW_PROMPT
from ..store import Store
from .judge import JudgeFailed, judge_digest, judge_system_prompt
from .models import Case, CaseResult, RunConfig, RunResults
from .report import render_report
from .rubric import Rubric
from .terminal import printable, review_lines

log = logging.getLogger(__name__)

ClientFactory = Callable[[str], LLMClient]  # model id -> the unmetered client for it
_DROPPED = re.compile(r"^(first draft, )?dropped (what_you_need_to_know|worth_a_closer_look) sentence")


def prompt_hashes(rubric: Rubric) -> dict[str, str]:
    """A short hash per prompt, so a comparison can say which one changed."""
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    return {
        "writer": digest(SYSTEM_PROMPT), "revise": digest(REVISE_PROMPT),
        "writer_light_day": digest(LIGHT_SYSTEM_PROMPT), "revise_light_day": digest(LIGHT_REVISE_PROMPT),
        "review": digest(REVIEW_PROMPT), "judge": digest(judge_system_prompt(rubric)),
        "headline_rewrite": digest(HEADLINE_PROMPT), "headline_review": digest(HEADLINE_REVIEW_PROMPT),
    }


def git_state() -> tuple[str, bool]:
    """(short commit, whether the tree has uncommitted changes); ("nogit", False) outside a repository."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()

    try:
        return git("rev-parse", "--short", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return "nogit", False


def make_run_id(now: datetime, commit: str) -> str:
    return f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{commit}"


def _load_into_store(case: Case) -> Store:
    """A temporary in-memory store holding the case's items, all published on the case's day.

    Times are reset to local noon on that day: the pipeline reads a local-time window, so a case
    saved in one timezone must still land in its own day in another.
    """
    store = Store(":memory:")
    noon = datetime.combine(case.target_date, datetime.min.time().replace(hour=12)).astimezone()
    for seconds, item in enumerate(sorted(case.items, key=lambda i: i.published)):
        store.add_if_new(item.model_copy(update={"published": noon + timedelta(seconds=seconds)}))
    return store


def _capped(settings: LLMSettings, model: str, cap: Decimal) -> LLMSettings:
    return settings.model_copy(update={"model": model, "max_usd_per_run": cap})


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _failure_text(state: DigestState) -> str:
    """The review block and notes exactly as `draft` prints them, each note once."""
    lines = [*review_lines(state.review)] if state.review else []
    return printable("\n".join([*lines, *dict.fromkeys(state.notes)]))


def _draft_lines(draft: Draft) -> list[str]:
    lines = [f"headline: {' '.join(draft.headline.split())}"]
    for section in ("what_you_need_to_know", "worth_a_closer_look"):
        for index, s in enumerate(getattr(draft, section)):
            cites = ", ".join(s.item_ids) or "none"
            lines.append(f"[{section} {index}] ({s.type}; cites: {cites}) {' '.join(s.text.split())}")
    return lines


def _review_log(rounds: list[ReviewRound]) -> str:
    """Each round's draft and the issues found, so a case that never converged can be read without a rerun."""
    lines: list[str] = []
    for r in rounds:
        lines += [f"== round {r.number}: draft reviewed ==", *_draft_lines(r.draft), "", f"== round {r.number}: issues =="]
        for i in r.issues:
            where = i.section if i.index is None else f"{i.section} {i.index}"
            lines.append(f"- {i.severity} {i.category} at {where}: {' '.join(i.required_change.split())}")
        if not r.issues:
            lines.append("(none)")
        lines.append("")
    return printable("\n".join(lines))


def run_case(
    case: Case, *, settings: LLMSettings, eval_settings: EvalSettings, rubric: Rubric, factory: ClientFactory,
    judge_model: str, judge_enabled: bool, budget_left: Decimal, case_dir: Path,
) -> CaseResult:
    """One case: pipeline, then judge. The judge's cap is cut to what the pipeline left of `budget_left`."""
    case_dir.mkdir(parents=True, exist_ok=True)
    client = MeteredClient(factory(settings.model), _capped(settings, settings.model, settings.max_usd_per_run))
    result = CaseResult(id=case.id, category=case.category, status="no_digest")

    def record_pipeline_cost() -> None:
        result.calls, result.input_tokens, result.output_tokens = client.calls, client.input_tokens, client.output_tokens
        result.usd = f"{client.spent:.6f}"

    with _load_into_store(case) as store:
        try:
            state: DigestState = run_digest(store, case.target_date, client, review_enabled=True)
        except (LLMError, WriterFailed) as error:
            record_pipeline_cost()
            result.status = "error"
            # The checks that failed are fixed wording; neither message echoes the model's reply.
            result.error = printable(str(error))
            result.failure = f"error: {result.error}"
            _write(case_dir / "failure.txt", result.failure + "\n")
            return result
    record_pipeline_cost()
    _write(case_dir / "notes.txt", "\n".join(dict.fromkeys(state.notes)) + "\n")
    review, digest = state.review, state.digest
    if review is not None:
        result.status = review.status
        result.review_rounds, result.sentences_removed = review.rounds, review.removed
    result.dropped_sentence_notes = sum(1 for n in state.notes if _DROPPED.match(n))
    if review is not None and review.status != "approved":
        result.failure = _failure_text(state)
        _write(case_dir / "failure.txt", result.failure + "\n")
        last = state.review_rounds[-1].draft if state.review_rounds else state.draft
        if last is not None:
            _write(case_dir / "draft.json", last.model_dump_json(indent=2) + "\n")
        _write(case_dir / "review.txt", _review_log(state.review_rounds) or "(no model review ran)\n")
    if digest is None or state.draft is None:
        return result

    _write(case_dir / "digest.md", markdown_for(digest))
    _write(case_dir / "digest.json", digest.model_dump_json(indent=2) + "\n")
    _write(case_dir / "items.md", render_items(state.sent_items)[0] + "\n")
    result.words = word_count(state.draft)
    text = render_markdown(digest).lower()
    result.canary_hits = [c for c in case.canaries if c.lower() in text]

    left = budget_left - client.spent
    if not judge_enabled or result.status == "unreviewed":
        return result
    if left <= 0:
        result.judge_error = "the eval budget was used up before the judge could run"
        return result
    judge_client = MeteredClient(factory(judge_model), _capped(settings, judge_model, min(eval_settings.max_usd_per_judge, left)))
    try:
        result.judge = judge_digest(
            digest=digest, draft=state.draft, sent_items=state.sent_items, case=case, rubric=rubric,
            client=judge_client, model=judge_model,
        )
    except (JudgeFailed, LLMError) as error:  # both messages are fixed wording or built from the status only
        result.judge_error = str(error)
    result.judge_usd = f"{judge_client.spent:.6f}"
    if result.judge:
        _write(case_dir / "judge.json", result.judge.model_dump_json(indent=2) + "\n")
    return result


def run_eval(
    cases: list[Case], *, settings: LLMSettings, eval_settings: EvalSettings, rubric: Rubric, factory: ClientFactory,
    judge_model: str, judge_enabled: bool, mock: bool, out_root: Path, run_id: str, now: datetime, git_dirty: bool,
) -> tuple[RunResults, Path]:
    """Run the cases in order. A case does not start unless what is left of `max_usd_per_eval` covers a full
    pipeline cap and the dearest case so far, so a case is never squeezed into a different outcome by the
    eval cap. The judge is capped by what is left, so the total holds even when a case costs more.
    """
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    limit = eval_settings.max_usd_per_eval
    results: list[CaseResult] = []
    spent, dearest = Decimal(0), Decimal(0)
    skipped: list[str] = []
    for case in cases:
        if skipped or limit - spent < max(settings.max_usd_per_run, dearest):
            skipped.append(case.id)
            continue
        result = run_case(
            case, settings=settings, eval_settings=eval_settings, rubric=rubric, factory=factory, judge_model=judge_model,
            judge_enabled=judge_enabled, budget_left=limit - spent, case_dir=run_dir / case.id,
        )
        cost = Decimal(result.usd) + Decimal(result.judge_usd)
        spent, dearest = spent + cost, max(dearest, cost)
        results.append(result)
        log.debug("eval case done status=%s cumulative_usd=%s", result.status, f"{spent:.4f}")
    run = RunResults(
        run_id=run_id, started_at=now,
        config=RunConfig(
            generator_model=settings.model, judge_model=judge_model,
            prices={m: {"input_per_mtok": str(p.input_per_mtok), "output_per_mtok": str(p.output_per_mtok)}
                    for m, p in settings.prices.items()},
            settings={k: str(getattr(settings, k)) for k in (
                "max_usd_per_run", "max_input_tokens", "max_output_tokens", "max_run_input_tokens",
                "max_run_output_tokens", "max_review_rounds", "low_volume_max_items", "low_volume_min_words",
                "low_volume_aim_words")},
            max_usd_per_eval=str(limit), mock=mock, judge_enabled=judge_enabled, git_dirty=git_dirty,
        ),
        prompt_hashes=prompt_hashes(rubric), rubric_version=rubric.version, cases=results,
        skipped_cases=skipped, stopped_by_cap=bool(skipped), total_usd=f"{spent:.6f}",
    )
    _write(run_dir / "results.json", run.model_dump_json(indent=2) + "\n")
    _write(run_dir / "summary.md", render_report(run))
    return run, run_dir
