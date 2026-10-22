"""The digest pipeline as a LangGraph graph.

Nodes are plain functions from state to a partial state update. Only `load_items` reads
outside the state (the local store); the editor reaches a model only through the client it is given.
"""

import operator
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from pydantic import AwareDatetime, BaseModel

from .contracts import CandidateItem, Draft, FinalDigest, ItemSummary, ReviewRound, ReviewSummary
from .digest import build_digest
from .domains import item_link_allowed
from .editor import MIN_ITEMS, fit_items, hidden_text_note, write_draft
from .llm import BudgetedClient
from .reviewer import run_review
from .store import Store


class DigestState(BaseModel):
    target_date: date
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    items: list[CandidateItem] = []
    summaries: list[ItemSummary] = []
    generated_at: AwareDatetime | None = None
    draft: Draft | None = None
    sent_items: list[CandidateItem] = []  # the items the Writer was shown, after fit_items
    digest: FinalDigest | None = None
    review: ReviewSummary | None = None
    review_rounds: list[ReviewRound] = []  # what each model review saw and found
    notes: Annotated[list[str], operator.add] = []  # non-fatal; nodes return only new notes


def day_window(target: date) -> tuple[datetime, datetime]:
    """[local midnight of `target`, local midnight of the next day), in the machine's timezone."""
    start = datetime.combine(target, time.min).astimezone()
    end = datetime.combine(target + timedelta(days=1), time.min).astimezone()
    return start, end


def load_items(state: DigestState, store: Store, allowed: Mapping[str, Iterable[str]] | None = None) -> dict:
    """Read the day's unseen items from the store.

    With `allowed` (feed name to domains), an item whose feed is unknown or whose link is off its feed's
    list is dropped before any model sees it. `None` is for callers whose items are not from the feed list.
    """
    start, end = day_window(state.target_date)
    items = store.items_published_between(start, end)
    kept = items if allowed is None else [i for i in items if item_link_allowed(i, allowed)]
    update: dict = {"window_start": start, "window_end": end, "items": kept}
    if len(kept) < len(items):
        update["notes"] = [f"dropped {len(items) - len(kept)} items whose feed or link is not on the allowed list"]
    items = kept
    if not items:
        update["notes"] = [*update.get("notes", []), f"quiet day: no new items for {state.target_date}"]
    return update


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    return re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]


def summarize_stub(state: DigestState) -> dict:
    """Placeholder summaries, no model: the first sentence of each item's text, plus its URL."""
    summaries = [
        ItemSummary(
            item_id=item.id,
            key_points=[_first_sentence(item.article_text or item.extract) or item.title],
            source_url=item.canonical_url,
        )
        for item in state.items
    ]
    return {"summaries": summaries}


def editor_writer(state: DigestState, client: BudgetedClient) -> dict:
    """Pick and write the digest with one model call; too few items skip the call."""
    if len(state.items) < MIN_ITEMS:
        return {"notes": [f"No significant developments: fewer than {MIN_ITEMS} items, so no model call was made"]}
    if state.generated_at is None:
        raise ValueError("generated_at must be set before the editor_writer node runs")
    sent_items, fit_notes = fit_items(state.items, client)
    draft, notes = write_draft(sent_items, client, fit=False)
    return {"draft": draft, "sent_items": sent_items, "notes": [*fit_notes, *hidden_text_note(sent_items), *notes]}


def review(state: DigestState, client: BudgetedClient, enabled: bool) -> dict:
    """Review and revise the draft, then build the digest. A draft that cannot be repaired gives no digest."""
    if state.draft is None:
        return {}
    outcome = run_review(state.draft, state.sent_items, client, enabled=enabled)
    if outcome.draft is None:
        return {"review": outcome.summary, "review_rounds": outcome.rounds, "notes": outcome.notes}
    # The digest keeps every note of the run; the state's own notes only gain this step's.
    digest = build_digest(
        outcome.draft, state.sent_items, state.target_date, state.generated_at,
        [*state.notes, *outcome.notes], outcome.summary,
    )
    return {
        "draft": outcome.draft, "digest": digest, "review": outcome.summary,
        "review_rounds": outcome.rounds, "notes": outcome.notes,
    }


def build_graph(
    store: Store, client: BudgetedClient | None = None, *, review_enabled: bool = False,
    allowed: Mapping[str, Iterable[str]] | None = None,
):
    """load_items -> editor_writer -> review -> end, or load_items -> summarize_stub -> end without a client.

    The client carries its own budget (`MeteredClient`), so the graph holds no cost logic. With
    `review_enabled=False` the digest comes out marked unreviewed.
    """
    graph = StateGraph(DigestState)
    graph.add_node("load_items", lambda state: load_items(state, store, allowed))
    graph.add_edge(START, "load_items")
    if client is None:
        graph.add_node("summarize_stub", summarize_stub)
        graph.add_edge("load_items", "summarize_stub")
        graph.add_edge("summarize_stub", END)
    else:
        graph.add_node("editor_writer", lambda state: editor_writer(state, client))
        graph.add_node("review", lambda state: review(state, client, review_enabled))
        graph.add_edge("load_items", "editor_writer")
        graph.add_edge("editor_writer", "review")
        graph.add_edge("review", END)
    return graph.compile()


def run_digest(
    store: Store, target: date, client: BudgetedClient | None = None, *, review_enabled: bool = False,
    allowed: Mapping[str, Iterable[str]] | None = None,
) -> DigestState:
    """Run the graph for one day. LangGraph returns a dict, so validate it into the state model.

    LangGraph does not validate the last node's output, so this is the only check of it.
    """
    state = DigestState(target_date=target, generated_at=datetime.now(timezone.utc))
    result = build_graph(store, client, review_enabled=review_enabled, allowed=allowed).invoke(state)
    return DigestState.model_validate(result)
