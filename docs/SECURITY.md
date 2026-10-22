# Security notes

Daily Digest reads text written by strangers (feeds and article pages), shows it to a language model, and publishes what the model writes. This note says what is trusted, how untrusted text moves through the code, which defence sits where, and what is not defended. It is about v1: a local command-line tool with no web interface and no server.

## What is trusted and what is not

| Trusted (we wrote it or the owner controls it) | Not trusted |
|---|---|
| The code, the prompts, `config/settings.toml`, `config/feeds.toml`, the eval rubric | Everything fetched: feed entries (titles, links, text), article pages, `robots.txt` files, server responses and redirects |
| The API key, read from the environment and never stored | Everything a model returns, including the Reviewer's verdict and the judge's scores |
| The local database and the saved digests, as written by this tool | The text of items already stored (it stays as it was fetched) |

## How untrusted text moves

1. **Fetch.** One fetcher makes every request. Every URL, and every redirect hop, must be `https` and resolve to public addresses only; the connection goes to the address that was checked (so DNS cannot change its answer). Responses are size-capped, redirects are limited, and one fetch has a total time limit.
2. **Ingest.** Feeds are parsed, HTML is stripped to plain text, text is cut to 1,200 characters, and each item's link must point to a domain its feed is allowed to link to. Items that do not are dropped and counted.
3. **Extract.** Article text is fetched only from the feed's allowed domains (each redirect hop included), after `robots.txt` and the site's crawl delay. A `robots.txt` may redirect only within its own registrable domain. Only plain text, capped in length, is kept.
4. **Rendering for a model.** All item text, titles and source names pass through one function (`render_items`): look-alike characters are folded (NFKC), invisible and reordering characters are removed, angle brackets are neutralised, and each item sits between `<<<ITEM` and `<<<END ITEM` lines. The system prompts say that everything between those lines is data. The Writer, the revision, the Reviewer and the eval judge all use this function.
5. **Model output.** Every reply must match a schema. Code then checks it against the text that was sent, and builds citation markers and the Sources list itself.
6. **Publish.** `generate` drops stored items whose feed is unknown or whose link is off its feed's list before any model call, then the Publisher checks the finished digest again and writes it. Sources come only from stored items. (`draft` shows what is stored and does not apply the list.)

## The defences, and where they live

| Defence | Where | What it stops |
|---|---|---|
| Public-address-only fetching, DNS pinning, https only, per-hop checks | `fetch.py` | Requests to internal services, cloud metadata addresses, `file://` and similar, DNS rebinding, redirects into private networks |
| `robots.txt` per RFC 9309, crawl delay, rate limit, `Retry-After` | `robots.py`, `fetch.py`, `extract.py` | Crawling where a site asks us not to, hammering a site |
| Size, redirect and total-time limits; entity bombs are not expanded | `fetch.py`, feed parser | Oversized, endless or slow-drip responses |
| Per-feed domain allowlist; userinfo, odd ports and look-alike hosts do not match | `domains.py`, `ingest.py`, `extract.py`, `publisher.py` | A hostile feed pointing the article fetch, or a digest's Sources, at any site |
| Hidden-text stripping before any model sees text | `normalize.py` (`strip_hidden`), `editor.py` (`render_items`) | Instructions written in zero-width, bidirectional, tag or variation-selector characters |
| Delimiters and angle-bracket escaping; "this is data" rule in every prompt | `editor.py`, `reviewer.py`, `evals/judge.py` | Item text that pretends to close its block or to be an instruction |
| No tools: model requests carry no `tools`; only `llm/` imports the SDK | `llm/anthropic_client.py` | A model reply that tries to make the program do something |
| Schema-validated replies | `llm/`, `contracts.py` | Malformed or surprising output |
| Writer checks: no URL or citation marker in a sentence, cited ids must exist, quotes and speakers must appear in the cited text, host names must appear in it | `editor.py` | Made-up links and sources, altered quotes |
| No-opinion scan and a verdict computed in code (a model "approve" is never trusted) | `reviewer.py` | A model told to approve, or to add advice or promotion |
| Flagged sentences are removed in code, never reworded without review | `reviewer.py` | Unsupported or promotional sentences surviving the review loop |
| Publisher validation: markers match Sources, URLs come from the day's items and their feed's domains, one-line titles, no HTML or Markdown links | `publisher.py` | Fabricated or injected links in a saved digest |
| Terminal-safe printing of feed, model and config text | `cli.py` | Escape sequences from a hostile title reaching your terminal |
| Secrets: key from the environment only, never printed; secret scan in CI | `cli.py`, `.github/workflows/ci.yml` | Key leakage in logs, files or commits |

## Residual risks

- **A planted claim.** An injected item can state a false claim in ordinary prose. If the Writer reports it with attribution ("Hostile Blog says ..."), that sentence is supported by the text it cites, so no code check can reject it. What reduces this: the exclude tier and the promotional-framing rule in the prompts, the Reviewer's entailment check, the source and attribution shown in the digest, and a person reading the digest before relying on it. Tests cover an obedient model, but they cannot remove this risk.
- **The model can still be steered.** Delimiters and rules make injection harder, not impossible. The defences above are what limit the damage: a steered model can write text, but it cannot add a link, a quote or a source that is not in the stored items, cannot approve its own draft, and cannot call tools.
- **Feed URLs are chosen by you, and their redirects are not confined.** A feed that redirects to another host is followed (many legitimate feeds do), subject to the address, robots and size checks. Only the links inside a feed and article fetches are held to the allowlist. For the same reason, the `robots.txt` of a feed served on a port other than 80 or 443 is not confined to its own domain.
- **Stored text is as fetched.** Hidden characters are removed when text is shown to a model, not when it is stored, so the database can hold them.
- **The allowlist trusts a domain, not a page.** A feed's own domain can still host a hostile article. The default is the feed's registrable domain, which is wide for a shared one such as a university's; a feed can list narrower domains. The registrable domain is found with a short built-in list of two-part suffixes, not the full public suffix list, so a feed on an unusual suffix should list its domains explicitly.
- **The key is in the process environment.** Anything running as you can read it. Use a dedicated key with a spend limit (see the README).
- **No lock, no sandbox.** Two runs can race (see the Publisher notes in the design doc), and the process has your permissions.
- **Not defended:** a compromised dependency, a compromised model provider, and a person with access to your machine.

## Test coverage of the PRD's security cases (FR-9.x)

| Case | Covered by | Notes |
|---|---|---|
| FR-9.1 prompt injection | `tests/test_injection.py` (eight fixture classes, all four prompts, an obedient-model set), `tests/test_hidden_text.py`, `tests/test_editor.py`, `tests/test_reviewer.py`, the eval cases `injection-*` (real model, run by hand) | Encoded text is covered by the hidden-character cases; base64 or other encodings are not decoded or tested. The test fixtures are generic classes, not a catalogue of attacks. |
| FR-9.2 SSRF | `tests/test_fetch.py` (private, loopback, link-local, metadata, mapped and literal addresses; non-https schemes; unsafe redirects; DNS pinning), `tests/test_domains.py` | A DNS answer that changes between lookups is covered by the pinning test. |
| FR-9.3 crawling etiquette | `tests/test_robots_fetch_config.py`, `tests/test_fetch.py` (spacing, `Retry-After`), `tests/test_extract.py` (disallow, crawl delay, redirect hops) | Uses fake transports, not a local server. |
| FR-9.4 resource abuse | `tests/test_fetch.py` (oversize, redirect loop), `tests/test_resource_abuse.py` (entity bomb, slow-drip) | Malformed XML other than the entity bomb relies on the feed parser's tolerance. |
| FR-9.5 XSS in newsletter content | `tests/test_generate.py` (no HTML, no Markdown links or images, escaped titles and previews in the index) | There is no web interface in v1, so there is no browser to attack; the web-specific cases wait for it. |
| FR-9.6 secrets | `tests/test_ci_workflow.py`, `tests/test_generate.py`, `tests/test_review_cli.py`, `tests/test_evals.py` (no key in logs, saved JSON or errors) | |
| FR-9.7 privilege | `tests/test_injection.py` (no `tools`; only `llm/` imports the SDK) | "Only ingest and extract fetch the open web" is not tested; it holds by structure (`graph.py` imports no fetcher). |
| FR-9.8 citation fabrication | `tests/test_editor.py` (unknown ids, URLs), `tests/test_generate.py` (Sources must come from the day's items and their feed's domains) | |
| FR-9.9 grounding and no opinion | `tests/test_editor.py`, `tests/test_reviewer.py`, `tests/test_injection.py` | |
| FR-9.10 generate endpoint | Not covered: there is no web interface in v1. | |
