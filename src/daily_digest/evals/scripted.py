"""A model client for `eval run --mock`: valid replies for any case, no network, no key.

It writes a plain digest from the item titles, approves it, and gives every score a 3. It exists to
exercise the whole path (store, graph, judge, results, reports), not to say anything about quality.
"""

import re

from pydantic import BaseModel

from ..contracts import Draft, DraftSentence, ReviewResult
from ..llm.base import LLMResult, T, Usage
from .models import DIMENSIONS, CoversMust, JudgeScore, JudgeVerdict

_ITEM = re.compile(r"<<<ITEM (\S+?)>>>\nsource: .*\ntitle: (.*)\n")
_PAD = "Taken together, these items describe the day's developments for enterprise adopters."
HEADLINE = "Enterprise AI developments from the day's news"


def _draft_from(prompt: str) -> Draft:
    items = _ITEM.findall(prompt)
    facts = [DraftSentence(text=title.rstrip(".") + ".", type="factual", item_ids=[item_id]) for item_id, title in items]
    pads = lambda n: [DraftSentence(text=_PAD, type="transition") for _ in range(n)]  # noqa: E731
    half = max(1, len(facts) // 2)
    first, second = facts[:half], facts[half:] or facts[:1]
    words = sum(len(s.text.split()) for s in [*first, *second])
    needed = max(0, -(-(230 - words) // len(_PAD.split())))
    first = [*first, *pads(needed)]
    for sentences in (first, second):
        sentences[0] = sentences[0].model_copy(update={"starts_paragraph": True})
    return Draft(headline=HEADLINE, what_you_need_to_know=first, worth_a_closer_look=second)


class ScriptedClient:
    def complete(self, *, system: str, messages: list[dict], schema: type[T], max_tokens: int) -> LLMResult[T]:
        prompt = messages[0]["content"]
        reply: BaseModel
        if schema is Draft:
            reply = _draft_from(prompt)
        elif schema is ReviewResult:
            reply = ReviewResult(verdict="approve")
        elif schema is JudgeVerdict:
            must = re.search(r"^must_cover: (.*)$", prompt, re.M)
            ids = [i.strip() for i in must.group(1).split(",")] if must else []
            reply = JudgeVerdict(
                **{name: JudgeScore(score=3, rationale="Scripted score.") for name in DIMENSIONS},
                covers_must=[CoversMust(item_id=i, covered=True) for i in ids],
            )
        else:
            raise AssertionError(f"no scripted reply for {schema.__name__}")
        return LLMResult(reply, Usage(input_tokens=len(prompt) // 3, output_tokens=600))
