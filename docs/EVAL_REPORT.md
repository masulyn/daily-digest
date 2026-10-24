# Eval report, Oct 22 to 23, 2026

A dated snapshot, not a living document.

**Method.** 12 cases run through the real pipeline (Editor-Writer, then the Reviewer loop): 3 real days of stored feed items and 9 invented cases (normal day, thin day, too-few-items day, trend and disagreement, hedges and numbers and quotes, an off-topic day, and 3 injection cases with canary strings). A second model (`claude-opus-5-5`) scores each digest 1 to 5 for faithfulness, coverage, audience fit and concision; the generator is `claude-sonnet-5-5`. The pipeline already enforces citations, quotes, length and the no-opinion rules, so the eval reports its outcomes and does not recompute them. One run per case.

**Baseline (10 cases, $0.81).** 5 approved, 2 errored, 1 failed, 1 did not converge. Judged means: faithfulness 4.67, coverage 4.67, audience fit 3.83, concision 3.17. The weak point was padding: on a 4-item day the writer restated each fact three or four times to reach the 200-word minimum, scored concision 1, and the Reviewer approved it, because padding is faithful.

**Change.** On days with 3 to 7 items ("light days") the digest may be short instead of padded, aims for about 100 words, and is accepted at 50 or more. It carries a "Light day" label.

| run | approved of attempted | faithfulness | coverage | audience fit | concision | cost |
|---|---|---|---|---|---|---|
| baseline | 5 of 9 | 4.67 | 4.67 | 3.83 | 3.17 | $0.81 |
| light-day floor 100 | 6 of 11 | 4.57 | 4.29 | 4.14 | 4.29 | $0.84 |
| final (aim 100, floor 50) | 9 of 11 | 4.67 | 4.89 | 4.00 | 4.22 | $0.79 |

On the 6 cases scored in both the baseline and the final run, concision rose from 3.17 to 4.00, with faithfulness -0.17 and coverage and audience fit +0.17 each (noise at this size). The padded thin-day digest went from concision 1 to 5. With a floor of 100 the writer wrote 73 to 97 words on thin days and three cases failed just under it; with a floor of 50, all six light-day digests were approved. No injection case showed a canary hit, and a search found no planted link or promotional wording.

**Still wrong.**
- The light-day digests came out at 54 to 96 words (mean about 77) against an aim of 100. They are valid and score well; raising `low_volume_aim_words` is the setting to try if longer is wanted.
- `real-2026-10-16` failed again. A blocking issue on its headline (a forecast stated as fact) cannot be fixed by removing a sentence, so the digest fails; the same day was approved in one run and not converged in another (#44).
- `offtopic-with-vendor-pr` fell 8 words under the 200 minimum: it has 9 items, so it is not a light day, though only 2 are relevant (#45).
- A revision that adds sentences is discarded whole (#43).
- Item selection and outcomes vary between runs on identical input.

**What the judge caught.** Across the 9 approved digests it flagged 5 sentences (3 overstated, 1 attribution, 1 unsupported) that the Reviewer had passed; in an earlier run it caught a misattribution ("the same piece" about a different article). Of the flags, the two checked by a person were about headline wording ("banks and insurers" generalized from one bank and one insurer; "race" among vendors from two announcements), which is a matter of judgement; the rest have not been checked.

**Trust.** Twelve cases, nine written by us, one run each; treat a one-point difference as noise. The judge is another model from the same family. A person rated five of the final run's digests against it: 18 of 20 scores were within one point and 9 were identical. The judge scored faithfulness lower than the person on two digests, both because of headline wording, and the largest gaps were on coverage and concision, so treat the judge as a guide to direction, not as exact scores. One rater and five digests is a small check. Faithfulness is judged against the text the pipeline saw, cut to 1,200 or 3,000 characters. Costs use the token counts the API returns and the config price table; they were not yet reconciled with the provider dashboard.
