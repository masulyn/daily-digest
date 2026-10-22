"""The `daily-digest eval ...` subcommands. Exit codes: 0 done, 1 a canary hit or flagged regression or other error,
2 setup problem (key, settings, rubric, case files, unknown ids), 4 stopped by the eval spend cap."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from ..config import DEFAULT_SETTINGS_PATH, PROJECT_ROOT, SettingsError, data_dir, load_eval_settings, load_llm_settings
from ..store import DatabaseNotFound, Store
from .cases import CaseError, load_cases, snapshot_day
from .report import (
    ReportError, compare, load_baseline, load_run, render_report, save_baseline, spot_check,
)
from .rubric import RubricError, load_rubric
from .runner import git_state, make_run_id, run_eval
from .terminal import printable
from .scripted import ScriptedClient

DEFAULT_EVALS_DIR = PROJECT_ROOT / "evals"
EXIT_CAP = 4


def _say(text: str, *, error: bool = False) -> None:
    print(printable(text), file=sys.stderr if error else sys.stdout)


def _paths(args) -> tuple[Path, Path]:
    evals_dir = args.evals_dir or DEFAULT_EVALS_DIR
    return evals_dir, args.out_dir or evals_dir / "results"


def _run(args) -> int:
    from ..cli import _debug_logging, _make_inner_client

    evals_dir, results_root = _paths(args)
    try:
        settings = load_llm_settings(DEFAULT_SETTINGS_PATH)
        eval_settings = load_eval_settings(DEFAULT_SETTINGS_PATH)
        rubric = load_rubric(evals_dir / "rubric.toml")
        cases = load_cases(evals_dir / "cases", evals_dir / "snapshots")
    except (SettingsError, RubricError, CaseError) as error:
        _say(f"error: {error}", error=True)
        return 2
    if args.cases:
        wanted = [c for c in args.cases.split(",") if c]
        unknown = [c for c in wanted if c not in {case.id for case in cases}]
        if unknown:
            _say(f"error: no such case: {', '.join(unknown)}", error=True)
            return 2
        cases = [c for c in cases if c.id in wanted]
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        _say("error: no cases to run", error=True)
        return 2
    judge_model = args.judge_model or eval_settings.judge_model
    if judge_model not in settings.prices:
        _say(f"error: no price entry for judge model {judge_model!r} in llm.prices", error=True)
        return 2
    if judge_model == settings.model and not args.no_judge:
        _say("warning: the judge is the same model as the generator, so it may share its blind spots", error=True)
    if args.mock:
        factory = lambda model: ScriptedClient()  # noqa: E731
    else:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if settings.provider != "anthropic" or not key.strip():
            _say("error: ANTHROPIC_API_KEY is not set (use --mock to run without a model)", error=True)
            return 2
        factory = lambda model: _make_inner_client(settings.model_copy(update={"model": model}), SecretStr(key))  # noqa: E731
    now = datetime.now(timezone.utc)
    commit, dirty = git_state()
    with _debug_logging(args.debug):
        run, run_dir = run_eval(
            cases, settings=settings, eval_settings=eval_settings, rubric=rubric, factory=factory,
            judge_model=judge_model, judge_enabled=not args.no_judge, mock=args.mock, out_root=results_root,
            run_id=make_run_id(now, commit), now=now, git_dirty=dirty,
        )
    _say(render_report(run))
    _say(f"Results: {run_dir}")
    if run.stopped_by_cap:
        _say(f"error: stopped by the ${float(run.config.max_usd_per_eval):.2f} eval cap; "
             f"{len(run.skipped_cases)} case(s) not run", error=True)
        return EXIT_CAP
    return 1 if any(c.canary_hits for c in run.cases) else 0


def _snapshot(args) -> int:
    evals_dir, _ = _paths(args)
    db_path = args.db or data_dir() / "digest.db"
    try:
        with Store(db_path, must_exist=True) as store:
            path = snapshot_day(store, args.date, evals_dir / "snapshots")
    except (DatabaseNotFound, CaseError) as error:
        _say(f"error: {error}", error=True)
        return 2
    _say(f"Wrote {path} (git-ignored: it holds article text from other sites)")
    return 0


def run_eval_command(args) -> int:
    try:
        if args.eval_command == "run":
            return _run(args)
        if args.eval_command == "snapshot":
            return _snapshot(args)
        evals_dir, results_root = _paths(args)
        run, run_dir = load_run(results_root, args.run_id)
        if args.eval_command == "report":
            _say(render_report(run))
        elif args.eval_command == "baseline":
            save_baseline(run, evals_dir / "baseline.json", force=args.force)
            _say(f"Wrote {evals_dir / 'baseline.json'}; commit it to keep the baseline")
        elif args.eval_command == "compare":
            before = load_baseline(evals_dir / "baseline.json") if args.to == "baseline" else load_run(results_root, args.to)[0]
            text, flags = compare(run, before)
            _say(text)
            return 1 if flags else 0
        elif args.eval_command == "spot-check":
            _say(f"Wrote {spot_check(run, run_dir, args.n, args.seed)}")
        return 0
    except ReportError as error:
        _say(f"error: {error}", error=True)
        return 2
