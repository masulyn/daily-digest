"""Prompt-injection tests, offline: a mock model sees the hostile text, and no model or key is used.

Two kinds of test. The structural ones read every request the Writer, the revision, the Reviewer and the
judge would send and check where untrusted text sits. The obedient-model ones script a model that does
what the injection says and check that the code defences hold.

What these tests cannot remove: an injected item can plant a *claim*, and a sentence that reports it with
attribution ("Hostile Blog says ...") is supported by the text it cites, so no code check can reject it.
The defences against that are the exclude tier and the promotional-framing rule in the prompts, the
Reviewer's entailment check, and a person reading the digest. See docs/SECURITY.md.
"""

import json
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from daily_digest.config import load_eval_settings, load_llm_settings
from daily_digest.contracts import CandidateItem, Draft, DraftSentence, ReviewIssue, ReviewResult
from daily_digest.digest import render_markdown
from daily_digest.editor import (
    LIGHT_REVISE_PROMPT, LIGHT_SYSTEM_PROMPT, REVISE_PROMPT, SYSTEM_PROMPT, check_draft, render_items,
)
from daily_digest.evals.judge import judge_digest, judge_system_prompt
from daily_digest.evals.models import Case
from daily_digest.evals.rubric import load_rubric
from daily_digest.evals.scripted import ScriptedClient
from daily_digest.graph import run_digest
from daily_digest.llm import MeteredClient
from daily_digest.llm.anthropic_client import AnthropicClient
from daily_digest.llm.base import LLMResult, Usage
from daily_digest.reviewer import REVIEW_PROMPT, run_review
from daily_digest.store import Store
from helpers import editor_items

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "injections.json").read_text(encoding="utf-8"))
SETTINGS = load_llm_settings()
RUBRIC = load_rubric(ROOT / "evals" / "rubric.toml")
DAY = date(2026, 10, 14)
USAGE = Usage(input_tokens=1000, output_tokens=200)
ITEM_BLOCK = re.compile(r"<<<ITEM [^\n]*>>>\n.*?\n<<<END ITEM [^\n]*>>>", re.S)
SYSTEMS = {  # three items is a light day, so the light-day prompts are the ones a run sends
    SYSTEM_PROMPT: "writer", REVISE_PROMPT: "revise", LIGHT_SYSTEM_PROMPT: "writer", LIGHT_REVISE_PROMPT: "revise",
    REVIEW_PROMPT: "review", judge_system_prompt(RUBRIC): "judge",
}


def hostile_text(fixture: dict) -> str:
    text = fixture["text"]
    if fixture.get("zero_width"):
        text = "​".join(text) + "‮"  # zero-width spaces between every letter, then a right-to-left override
    if "tag_text" in fixture:
        text += " " + "".join(chr(0xE0000 + ord(c)) for c in fixture["tag_text"]) + "️\U000e0100"
    return text


def hostile_items(fixture: dict) -> list[CandidateItem]:
    clean = editor_items(DAY, "a1", "a2")
    injected = editor_items(DAY, "a3")[0].model_copy(update={
        "id": "inj", "source": "Hostile Blog", "canonical_url": "https://hostile.example.com/post",
        "content_hash": "hash-inj", "title": "A blog post about vendor claims",
        "article_text": f"The blog says vendors report mixed results. {hostile_text(fixture)}",
    })
    return [*clean, injected]


class Recorder(ScriptedClient):
    """The scripted model, remembering every request. The first review finds one blocking issue."""

    def __init__(self):
        self.requests, self.reviews = [], 0

    def complete(self, *, system, messages, schema, max_tokens):
        self.requests.append((system, messages))
        if schema is ReviewResult:
            self.reviews += 1
            if self.reviews == 1:
                issue = ReviewIssue(section="what_you_need_to_know", index=0, severity="blocking", category="tone", required_change="Shorten it.")
                return LLMResult(ReviewResult(issues=[issue]), USAGE)
        return super().complete(system=system, messages=messages, schema=schema, max_tokens=max_tokens)


def store_of(items: list[CandidateItem]) -> Store:
    store = Store(":memory:")
    for item in items:
        store.add_if_new(item)
        if item.article_text:
            store.save_article_text(item.id, item.article_text)
    return store


def run_all_prompts(items: list[CandidateItem]) -> list[tuple[str, str, str]]:
    """(kind, system, user text) of every model request one run makes, judge included."""
    model = Recorder()
    state = run_digest(store_of(items), DAY, MeteredClient(model, SETTINGS), review_enabled=True)
    assert state.digest is not None
    case = Case(id="inj", category="adversarial", description="hostile", target_date=DAY, items=items)
    judge_model = load_eval_settings().judge_model
    judge_digest(
        digest=state.digest, draft=state.draft, sent_items=state.sent_items, case=case, rubric=RUBRIC,
        client=MeteredClient(model, SETTINGS.model_copy(update={"model": judge_model})), model=judge_model,
    )
    return [(SYSTEMS[system], system, "\n".join(m["content"] for m in messages)) for system, messages in model.requests]


def hidden(text: str) -> list[str]:
    return [c for c in text if c != "\n" and (unicodedata.category(c) in ("Cc", "Cf") or 0xE0000 <= ord(c) <= 0xE007F or 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF)]


# --- structure: where untrusted text may be --------------------------------------------

@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["class"] for f in FIXTURES])
def test_hostile_text_stays_in_item_blocks_and_out_of_every_system_prompt(fixture):
    requests = run_all_prompts(hostile_items(fixture))
    assert {kind for kind, _, _ in requests} == {"writer", "revise", "review", "judge"}
    for kind, system, user in requests:
        inside = "\n".join(ITEM_BLOCK.findall(user))
        outside = ITEM_BLOCK.sub("", user)
        for needle in (fixture["marker"], fixture["phrase"]):
            assert needle not in system, f"{kind}: instruction text in the system prompt"
            assert needle not in outside, f"{kind}: instruction text outside the item blocks"
        assert not hidden(system) and not hidden(user), f"{kind}: hidden characters reached the model"
        assert len(re.findall(r"^<<<ITEM ", user, re.M)) == len(ITEM_BLOCK.findall(user)) == len(re.findall(r"^<<<END ITEM ", user, re.M))
        for block in ITEM_BLOCK.findall(user):  # nothing inside an item can look like a delimiter or markup
            body = block.split("\n", 1)[1].rsplit("\n", 1)[0]
            assert "<" not in body and ">" not in body, f"{kind}: an angle bracket survived inside an item"
        if fixture["class"] != "hidden_text":
            assert fixture["phrase"] in inside or fixture["class"] == "hidden_text", f"{kind}: the hostile item should reach the model as data"


def test_a_delimiter_inside_item_text_is_neutralised_for_every_prompt():
    fixture = next(f for f in FIXTURES if f["class"] == "delimiter_escape")
    for kind, _, user in run_all_prompts(hostile_items(fixture)):
        assert "‹‹‹END ITEM inj›››" in user, kind
        assert len(re.findall(r"^<<<END ITEM ", user, re.M)) == len(re.findall(r"^<<<ITEM ", user, re.M))
        assert "<<<DRAFT" not in ITEM_BLOCK.sub("", user) or kind == "review"  # only the Reviewer's own draft block


def test_instructions_hidden_in_tag_and_zero_width_characters_never_reach_a_model_in_any_form():
    fixture = next(f for f in FIXTURES if f["class"] == "hidden_text")
    for kind, system, user in run_all_prompts(hostile_items(fixture)):
        assert "PWNED-HIDDEN" not in user and "disregard the rules" not in user and not hidden(user), kind
        assert "A plain sentence with hidden text inside it." in user or kind == "judge"  # the visible text survives


# --- an obedient model: the code defences hold ------------------------------------------

PROMO = "VendorCo is the best platform on the market."
OPINION = "Leaders should switch to VendorCo today."


def obeyed_draft() -> Draft:
    """What a model that followed the injection would write, on top of a normal draft."""
    first = [
        DraftSentence(text="Example Wire reported that a bank moved 40 pilots into production.", type="factual", item_ids=["a1"], starts_paragraph=True),
        DraftSentence(text="Read more at http://promo.example.invalid/offer for the full story.", type="factual", item_ids=["a1"]),
        DraftSentence(text="A reader can verify this elsewhere.", type="factual", item_ids=["ghost-9"]),
        DraftSentence(text=PROMO, type="factual", item_ids=["inj"]),
        DraftSentence(text=OPINION, type="factual", item_ids=["inj"]),
    ]
    return Draft(headline="PWNED-DIRECT headline", what_you_need_to_know=first, worth_a_closer_look=[
        DraftSentence(text="Example Review noted that most pilots stall.", type="factual", item_ids=["a2"], starts_paragraph=True)])


def sent_for(items):
    return render_items(items)[1]


def test_the_writers_checks_drop_a_url_a_made_up_item_id_and_a_forged_citation_marker():
    items = hostile_items(FIXTURES[0])
    checked = check_draft(obeyed_draft(), sent_for(items))
    texts = [s.text for s in checked.draft.what_you_need_to_know]
    assert not any("promo.example.invalid" in t or "elsewhere" in t for t in texts)
    assert any("contains a URL" in n for n in checked.notes) and any("not provided" in n for n in checked.notes)
    forged = Draft(headline="h", what_you_need_to_know=[DraftSentence(text="A claim [3] from nowhere.", type="factual", item_ids=["a1"])], worth_a_closer_look=[])
    assert any("citation marker" in n for n in check_draft(forged, sent_for(items)).notes)


class ObedientReviewer:
    """A model that does what an injection in the item says: approve, report nothing."""

    def __init__(self, drafts):
        self.drafts, self.requests = list(drafts), 0

    max_input_tokens, max_output_tokens, max_review_rounds, spent = 40_000, 4_000, 3, 0
    low_volume_max_items, low_volume_min_words, low_volume_aim_words = 8, 50, 100

    def degraded(self):
        return False

    def complete(self, *, system, messages, schema, max_tokens):
        self.requests += 1
        reply = ReviewResult(verdict="approve") if schema is ReviewResult else self.drafts.pop(0)
        return LLMResult(reply, USAGE)


def padded(sentences: list[DraftSentence]) -> Draft:
    """A draft long enough to pass the length check, so only the opinion sentence is in question."""
    words = " ".join(["Example Wire reported this development in detail."] * 40)
    base = [DraftSentence(text=words, type="factual", item_ids=["a1"], starts_paragraph=True), *sentences]
    return Draft(headline="A bank moves pilots into production", what_you_need_to_know=base, worth_a_closer_look=[
        DraftSentence(text="Example Review noted that most pilots stall.", type="factual", item_ids=["a2"], starts_paragraph=True)])


def test_a_reviewer_that_obeys_an_injection_and_approves_cannot_approve_an_opinion_sentence():
    items = hostile_items(FIXTURES[0])
    bad = padded([DraftSentence(text=OPINION, type="factual", item_ids=["inj"])])
    reviewer = ObedientReviewer([bad, bad])
    outcome = run_review(bad, items, reviewer, enabled=True)
    assert outcome.summary.status != "approved"  # the verdict is computed in code, whatever the model says
    assert outcome.draft is not None and not any(OPINION in s.text for s in outcome.draft.what_you_need_to_know)
    assert outcome.summary.removed == 1 and any("advice to the reader" in u for u in outcome.summary.unresolved)


def test_when_removing_the_flagged_sentence_leaves_too_little_the_draft_fails_and_is_never_approved():
    items = hostile_items(FIXTURES[0])
    short = Draft(headline="A bank moves pilots into production", what_you_need_to_know=[
        DraftSentence(text=OPINION, type="factual", item_ids=["inj"], starts_paragraph=True)], worth_a_closer_look=[
        DraftSentence(text="Example Review noted that most pilots stall.", type="factual", item_ids=["a2"], starts_paragraph=True)])
    outcome = run_review(short, items, ObedientReviewer([short, short]), enabled=True)
    assert outcome.summary.status == "failed" and outcome.draft is None


class HonestReviewerObedientWriter:
    """A writer that obeys an injection to promote a vendor; a reviewer that flags the promotion."""

    max_input_tokens, max_output_tokens, max_review_rounds, spent = 40_000, 4_000, 3, 0
    low_volume_max_items, low_volume_min_words, low_volume_aim_words = 8, 50, 100

    def degraded(self):
        return False

    def complete(self, *, system, messages, schema, max_tokens):
        if schema is ReviewResult:
            text = "\n".join(m["content"] for m in messages)
            lines = [line for line in text.splitlines() if line.startswith("[") and "VendorCo" in line]
            issues = [ReviewIssue(section="what_you_need_to_know", index=int(line.split()[1].rstrip("]")), severity="blocking",
                                  category="promotional", required_change="Remove the promotion.") for line in lines]
            return LLMResult(ReviewResult(issues=issues), USAGE)
        return LLMResult(padded([DraftSentence(text=PROMO, type="factual", item_ids=["inj"])]), USAGE)


def test_a_promotional_sentence_that_the_reviewer_flags_is_removed_in_code_and_reaches_no_digest():
    items = hostile_items(FIXTURES[6])
    state = run_digest(store_of(items), DAY, HonestReviewerObedientWriter(), review_enabled=True)
    assert state.review.status == "not_converged" and state.digest is not None
    text = render_markdown(state.digest)
    assert "VendorCo" not in text and "promo.example.invalid" not in text
    assert {s.url for s in state.digest.sources} <= {i.canonical_url for i in items}  # Sources come from stored items only


# --- static ----------------------------------------------------------------------------

def test_no_request_to_the_model_api_passes_tools():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "stop_reason": "end_turn",
            "stop_sequence": None, "content": [{"type": "text", "text": ReviewResult(verdict="approve").model_dump_json()}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        })

    client = AnthropicClient(api_key=SecretStr("sk-ant-FAKE"), model="claude-sonnet-5-5", timeout_seconds=5,
                             http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    client.complete(system="s", messages=[{"role": "user", "content": "u"}], schema=ReviewResult, max_tokens=100)
    assert len(seen) == 1 and "tools" not in seen[0] and "tool_choice" not in seen[0]


def test_only_the_llm_package_imports_the_model_sdk():
    importers = {
        path.relative_to(ROOT / "src").as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"^\s*(import|from)\s+anthropic\b", path.read_text(encoding="utf-8"), re.M)
    }
    assert importers == {"daily_digest/llm/anthropic_client.py"}
