# judge-adapter

**Spend fewer input tokens on retrieved context: a choice model scores each search result, and only the results
worth reading reach the agent.** Also a pairwise LLM-judge adapter with fixed options, retries 0 and run-wide limits.

[Docs](docs/) · [Plugin](docs/plugin.md) · [Verification](docs/verification.md) · [한국어](docs/ko.md) ·
[GitHub](https://github.com/selectstar-ai/EVAL.Davi)

## Installation

The project's repository is [selectstar-ai/EVAL.Davi](https://github.com/selectstar-ai/EVAL.Davi); the Python
distribution and import names (`judge-adapter`, `judge_adapter`) and the plugin name (`davi`) are unchanged.

Python 3.9 or newer. Install from GitHub (this package is not on PyPI):

```bash
# core: depends only on httpx (no torch, no FastChat)
pip install "judge-adapter @ git+https://github.com/selectstar-ai/EVAL.Davi"

# optional FastChat binding: pulls FastChat at the pinned commit plus torch / transformers etc. (heavy)
pip install "judge-adapter[fastchat] @ git+https://github.com/selectstar-ai/EVAL.Davi"
```

From a source checkout: `pip install .` (or `pip install -e ".[test]"` for development). Pinned FastChat revision
and install checks: [reference](docs/reference.md#installation-details).

Claude Code plugin (`davi`, marketplace `datumo`):

```bash
claude plugin marketplace add selectstar-ai/EVAL.Davi
claude plugin install davi@datumo
claude mcp add --transport stdio --env TAVILY_API_KEY="$TAVILY_API_KEY" tavily -- npx -y tavily-mcp@0.2.22
```

The hook runs `python3` and needs **httpx** in that interpreter (or in a private venv in the plugin's data
directory); see [plugin install](docs/plugin.md#install). It also needs your own Jev-compatible choice endpoint
(RunPod key, endpoint ID and per-second rate, set in [Configure](docs/plugin.md#configure)); without them it sends
nothing and changes nothing.

The marketplace is now `datumo` (install id `davi@datumo`). A `davi@datumo` install does not inherit the settings,
toggles or state of an earlier `davi@judge-adapter` install, and nothing moves or removes the old one. If both are
active, the hooks may run twice: check `/plugin` and keep only one.

## About

- **Context filter** (`judge_adapter.context_filter`): one choice call per retrieved document reads P(keep); an
  explicit policy (`threshold(t)`, `top_k(k)`, `disabled()` or the preset `v1-thr0.3-dev`) keeps or drops it. It
  returns ids and decisions in input order, never rewrites or stores text, and keeps a document whose call fails
  (fail-open). There is no default policy.
- **Pairwise judge adapter**: one question (one or two turns) and two responses in, `A`, `B`, `tie` or an explicit
  `error` out, with a trace of every call. JSONL CLI, Python API and an optional FastChat `llm_judge` binding. Fake
  transport by default; a real (paid) CLI run needs `--allow-real`, `--max-submissions` and `--budget-cap-usd`.
- **Claude Code plugin `davi`**: a PostToolUse hook that filters Tavily `tavily_search` results before the model reads
  them, plus two optional, default-off, experimental query-stage options (search gate, domain presets).
- This package ships no model and no worker; endpoint, model and price come only from your configuration.

## Quickstart

No key and no network: both examples run against in-process fake endpoints.

```python
from judge_adapter import AdapterConfig, build_backend, fakes
from judge_adapter.context_filter import ContextFilter, Document, select_originals, threshold

cfg = AdapterConfig(transport='fake', backend='choice_endpoint', trace_dir='unused')
fake = fakes.FakeRelevanceEndpoint()                    # in-process; [[KEEP=p]] in the text sets the fake P(keep)
backend = build_backend(cfg, transport=fake.transport(), key_provider=lambda: fakes.DUMMY_KEY)
flt = ContextFilter(cfg, backend, policy=threshold(0.5))   # no default policy: pass one, or preset=...
docs = [Document('d1', 'A title', 'https://example.invalid/1', 'synthetic text [[KEEP=0.8]]'),
        Document('d2', 'A title', 'https://example.invalid/2', 'synthetic text [[KEEP=0.1]]')]
res = flt.filter('a question', docs)
res.selected_ids                                        # ['d1'], input order
[(r.id, r.score, r.status, r.decision) for r in res.documents]
kept = select_originals(docs, res)                      # your own objects, unchanged
```

Judge the bundled synthetic pairs with the fake transport:

```bash
judge-adapter run "$(judge-adapter examples | grep pairs.example.jsonl)" --out ./run-fake --transport fake
```

Real endpoints, `validate`, `two_direction` and the FastChat binding: [reference](docs/reference.md).

## Results

Single runs outside this package's test suite; not guarantees. Setup: HLE (Humanity's Last Exam), 90 English
text-only questions, a GPT-5.2 agent with web search, 5 documents per search, preset `v1-thr0.3-dev`, compared with
keeping the top 3 documents and keeping all 5. Answers were graded by Haiku against the reference answers.

| | Filter (`v1-thr0.3-dev`) | Keep top 3 | Keep all 5 |
|---|---|---|---|
| Agent input tokens, filter vs. this setting (HLE, 86 questions where all three finished) | | **−31.4%** | **−44.9%** |
| Correct answers, HLE (90 English questions) | 28 | 27 | 31 |
| Correct answers, earlier run (25 Korean questions) | 14 | 10 | 12 |

- **Input tokens went down.** On the 86 questions where all three settings finished, agent input tokens were 31.4%
  lower than keeping the top 3 documents and 44.9% lower than keeping all 5. (Correction: an earlier version of this
  README said "summed over all 90 questions, about 44% lower" than top-3. That sum counted 4 interrupted trajectories
  as 0 tokens, 3 of them in the filter setting, which inflated the reduction; it is replaced by the 86-question
  figures above.)
- **Accuracy was not shown to change.** In both runs the filter answered at least as many questions as top-3 (+1/90,
  +4/25), but neither difference is statistically established. Against all 5 documents the results were mixed (−3/90,
  +2/25). The preset was selected on development data (see [Preset](docs/reference.md#preset-v1-thr03-dev-opt-in)).
- **Total cost was not shown to go down.** Filter decisions are input-only; at a Jev-style list price of $0.042 per 1M
  input tokens (an assumption; this package does not serve that model) they cost about $0.0003 per question. Total
  cost depended on the generator's output: on the 86 HLE questions the filter setting produced 6.7% more output tokens
  and cost 4.5% more in total than top-3. No total-cost saving is established.
- **One run per setting.** The token comparison uses the 86 of 90 HLE questions where all three settings finished, a
  subset fixed after the run (post hoc), not a pre-registered split. Quality improvement or equivalence is not
  established, and neither is a total-cost advantage.

In the same HLE run, all 5 documents were dropped in 25 of the 72 searches the filter setting made: a per-search
count from a single run, not a general drop rate or failure rate. Total cost also depends on the price you configure
for filter calls (`choice_usd_per_second`) and on the model you actually serve behind the endpoint; the package ties
neither to the other, and its estimate is not a bill.

**Intended use:** an opt-in input-reduction tool. It does not claim quality improvement or total-cost savings.


## Claude Code plugin

`davi` filters **only Tavily `tavily_search`** (`tavily-mcp` 0.2.22, matcher `^mcp__[A-Za-z0-9_-]+__tavily_search$`).
**Built-in WebSearch is not filtered**: in one observed run it returned only a title and a URL per result, with no
text to score. Every other tool is untouched by the result filter. On any error the result is left unchanged
(fail-open); every search waits for the filter calls (one per scored result), up to the wall budget.

Switch features inside Claude Code with fixed, argument-free commands:

| Command | Effect |
|---|---|
| `/davi:status` | Effective settings and where each comes from |
| `/davi:filter-on`, `/davi:filter-off` | Result filter (PostToolUse, Tavily) |
| `/davi:gate-on`, `/davi:gate-off` | Search gate (experimental) |
| `/davi:domain-on`, `/davi:domain-off` | Domain presets (experimental) |
| `/davi:domain-mode-ask`, `/davi:domain-mode-input-only` | How a domain preset is applied |

- **Each command uses one model turn**: Claude reads the command's output and relays it. The same switches work
  from a shell without a model turn (`python3 <plugin>/hooks/davi_toggle.py ...`).
- **The toggle file is install-wide**, not per session: a change applies to every session using that data directory,
  including sessions that are already running. userConfig `enabled=false` overrides every toggle.
- **Query-stage options are default-off and experimental**, with untuned prompts and thresholds and no quality claim.
  The domain apply mode defaults to `ask`; in headless (`-p`) runs `ask` can block the search.

Verification (details and every row: [docs/verification.md](docs/verification.md)):

| Status | What |
|---|---|
| Observed in a real run (once each) | Tavily search: 5 filter calls, all 5 kept, result unchanged · WebSearch: 0 filter calls, unchanged · `/davi:status`, `gate-on/off`, `domain-on/off` in headless `claude -p` · the known A1 prompt kept with no gate call (after the fix) · domain preset applied by `input_only` (once) |
| Fake tests only | Dropped results removed and the shorter result received by the model · query-stage shapes, deny / retry, fail-open · explicit-request phrasings other than A1 |
| Unverified | `ask`, the **default** domain mode (needs an interactive session) · `filter-*`, `domain-mode-*` and interactive command use · gate usefulness and **general** preservation of user intent |
| Failures recorded | A1: before the fix, the gate denied a search the user explicitly asked for (P(not needed) = 0.999); release-blocking, so the gate stays default-off · one `input_only` smoke timed out (15.3 s, fail-open) and the harness missed it as a stop condition |

The A1 fix (a local check of the user's prompt against a fixed list of explicit-request phrasings) protects the
known phrasing class only; a phrasing outside the list can still be denied. Install, configuration, logs and the
security notes for the commands: [docs/plugin.md](docs/plugin.md).

## How it works

```
 question ─┐
           ├─▶ one choice call per document ─▶ P(keep) ─▶ policy ─▶ keep / drop ─▶ selected_ids (input order)
 documents ┘   (question id "relevance",        (from the     (threshold,
               options opt_keep / opt_drop)      endpoint)     top_k, ...)
               a failed call ───────────────────────────────────────▶ keep (fail-open, status says why)

 question + response A + response B ─▶ backend call (AB; BA in two_direction) ─▶ opt_1 / opt_2 / opt_tie ─▶ A / B / tie
                                                                       a failure ─▶ explicit error (never a tie)

Tavily MCP result (mcp__<alias>__tavily_search)
   │
   ▼
hook: split into per-result snippets ──(unknown format / isError / structuredContent)──▶ result unchanged
   │   (header, Answer:, images, other blocks kept aside, never scored)
   ▼
1 filter call per result: P(keep) ──(error)──▶ keep that result
   │
   ▼
keep if P(keep) >= 0.3, else drop ──▶ same text / blocks with dropped results removed ──▶ model
Any other tool, incl. built-in WebSearch: not matched (zero intervention)
```

Details: [context filter](docs/reference.md#context-filter), [pairwise judge](docs/reference.md#pairwise-judge-adapter),
[plugin hook](docs/plugin.md#what-the-hook-changes), [configuration and limits](docs/reference.md#configuration-and-limits).

## What changed in 0.4.1

No model, selection-policy or plugin-option change. Internal restructuring and one concurrency fix:

- Hook contracts shared by the plugin (option reading, env names, flag/number parsing, state directory, log line,
  time budget) moved into one stdlib-only module, `plugin_common`; the run-wide filter ledger moved to
  `gate.JobLedger`. Hook output, logs, state files and cost caps were compared byte for byte before and after on 76
  fixed fake cases (identical). Total lines grew slightly (+52 in the touched modules); the gain is fewer copies and
  no private cross-module imports, not size.
- Fixed a race in the search gate's prompt state: pruning now deletes a day-old session file only while holding that
  session's lock (non-blocking; skipped if busy), and the lock inode is re-checked after locking. Three deterministic
  multi-process scenarios failed before the fix and pass after it. This applies where `fcntl` is available (not on
  Windows); temporary files and the no-`fcntl` path are handled as before.
- `docs/` now ships in the source distribution, so the README's links resolve there.
- Tests: 550 passed, 4 skipped (the FastChat modules, which need the optional extra and a checkout, were not run).
  The repository moved to `selectstar-ai/EVAL.Davi` and the install identifier is `davi@datumo` (see
  [Claude Code plugin](#claude-code-plugin)).

## Further work

Follow-up candidates, not a committed roadmap and not instructions to run anything:

1. **Verify the unverified parts of the current integration and check usability:** the `ask` default for domain
   presets in an interactive session; the `/davi:filter-*` and `/davi:domain-mode-*` commands in a real session; a
   real run in which the filter drops results and the model receives the shorter result; general preservation of user
   intent by the search gate.
2. Conditional, separate candidates: merge reference-answer (pointwise) grading support, which is not part of 0.4.1;
   resume the multi-turn evaluation, on hold because of license constraints on the evaluation corpus.

## Citation

If you use this software, please cite the repository:

```bibtex
@software{judge_adapter_2026,
  author  = {hundredeuk2},
  title   = {judge-adapter: context filtering and pairwise LLM-judge calls},
  year    = {2026},
  version = {0.4.1},
  url     = {https://github.com/selectstar-ai/EVAL.Davi}
}
```

## License

Apache-2.0. See `LICENSE` and `NOTICE` (the default pairwise judge wording is adapted from FastChat's MT-Bench judge
prompts).
