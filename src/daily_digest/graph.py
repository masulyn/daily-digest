"""The digest pipeline as a LangGraph graph.

Nodes are plain functions from state to a partial state update. Only `load_items` touches
anything outside the state (the local store); nothing here uses the network or a model.
"""

import operator
import re
from datetime import date, datetime, time, timedelta
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from pydantic import AwareDatetime, BaseModel

from .contracts import CandidateItem, ItemSummary
from .store import Store


class DigestState(BaseModel):
    target_date: date
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    items: list[CandidateItem] = []
    summaries: list[ItemSummary] = []
    notes: Annotated[list[str], operator.add] = []  # non-fatal; nodes return only new notes


def day_window(target: date) -> tuple[datetime, datetime]:
    """[local midnight of `target`, local midnight of the next day), in the machine's timezone."""
    start = datetime.combine(target, time.min).astimezone()
    end = datetime.combine(target + timedelta(days=1), time.min).astimezone()
    return start, end


def load_items(state: DigestState, store: Store) -> dict:
    """Read the day's unseen items from the store."""
    start, end = day_window(state.target_date)
    items = store.items_published_between(start, end)
    update: dict = {"window_start": start, "window_end": end, "items": items}
    if not items:
        update["notes"] = [f"quiet day: no new items for {state.target_date}"]
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


def build_graph(store: Store):
    """load_items -> summarize_stub -> end."""
    graph = StateGraph(DigestState)
    graph.add_node("load_items", lambda state: load_items(state, store))
    graph.add_node("summarize_stub", summarize_stub)
    graph.add_edge(START, "load_items")
    graph.add_edge("load_items", "summarize_stub")
    graph.add_edge("summarize_stub", END)
    return graph.compile()


def run_digest(store: Store, target: date) -> DigestState:
    """Run the graph for one day. LangGraph returns a dict, so validate it into the state model.

    LangGraph does not validate the last node's output, so this is the only check of it.
    """
    result = build_graph(store).invoke(DigestState(target_date=target))
    return DigestState.model_validate(result)
