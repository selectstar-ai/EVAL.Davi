# Reference

[README](../README.md) · [Claude Code plugin](plugin.md) · [Verification status](verification.md) · [한국어](ko.md)

Contents: [Names](#names) · [Installation details](#installation-details) · [Context filter](#context-filter) · [Pairwise judge adapter](#pairwise-judge-adapter) · [Configuration and limits](#configuration-and-limits) · [Directory structure](#directory-structure)

## Names

| | Name |
|---|---|
| Distribution (pip) | `judge-adapter` |
| Import | `judge_adapter` |
| JSONL CLI | `judge-adapter` (also `python -m judge_adapter.cli`) |
| FastChat wrapper CLI (optional) | `judge-adapter-fastchat` (needs the `fastchat` extra) |
| Claude Code plugin | `davi` (filters Tavily MCP search results; optional query-stage options, off by default), marketplace `datumo` |
| License | Apache-2.0 (see `LICENSE` and `NOTICE`) |

## Installation details

Python 3.9 or newer. Install from GitHub (this package is not on PyPI):

```bash
# core: depends only on httpx (no torch, no FastChat)
pip install "judge-adapter @ git+https://github.com/selectstar-ai/EVAL.Davi"

# optional FastChat binding: pulls FastChat at the pinned commit plus torch / transformers etc. (heavy)
pip install "judge-adapter[fastchat] @ git+https://github.com/selectstar-ai/EVAL.Davi"
```

The `fastchat` extra pins `fschat @ git+https://github.com/lm-sys/FastChat@587d5cfa1609a43d192cedb8441cac3c17db105d`
(fschat 0.2.36), the only host revision the binding was written and tested against, and adds the packages that
`fastchat.llm_judge` imports at load time (`torch`, `transformers`, `accelerate`, `anthropic`, `openai`, `pandas`,
`numpy`, `tqdm`). The core never imports any of them.

Install check of the extra (2026-10): `pip install "judge_adapter-0.1.0-py3-none-any.whl[fastchat]"` in a fresh
Python 3.13 venv on macOS arm64 resolved fschat 0.2.36 from the pinned commit and torch 2.14.1, and the installed
`judge-adapter-fastchat` ran a fake reference-category flow end to end with synthetic data files. Other platforms and
Python versions were not tried for the extra (the core was also tested on Python 3.9).

From a source checkout: `pip install .` (or `pip install -e ".[test]"` for development).

## Context filter

A search tool usually hands every result to the model. `judge_adapter.context_filter` asks a choice model one
question per document ("does this document give a direct answer or needed intermediate evidence?"), reads the
probability of the *keep* option, and keeps only documents at or above a threshold. The agent then reads fewer
documents, so its **input tokens go down** (see [Results](../README.md#results) for one measured run).

- **Model:** the filter talks to a Jev-compatible choice endpoint that you serve, for example on RunPod serverless.
  Davi is a Jev-style choice model that scores P(keep) per document in this way. This package ships no model and no
  worker; endpoint, model and price come only from your configuration.
- **Output:** document ids and decisions, in input order. The filter never rewrites, shortens or stores text.
- **Fail-open:** a document whose call fails is kept, and the reason is recorded.
- **Explicit policy:** there is no default policy; you pass `threshold(t)`, `top_k(k)`, `disabled()` or a named
  preset such as `v1-thr0.3-dev`.
- **Where it plugs in:** a Python API for your own tool executor, and a [Claude Code plugin](plugin.md)
  whose PostToolUse hook filters Tavily MCP search results before the model reads them.

### Real endpoint and runnable example

For a real endpoint: `AdapterConfig(transport='real', backend='choice_endpoint', choice_endpoint_id=...,
choice_usd_per_second=..., choice_expected_model=...)` (expected model optional), `build_backend(cfg)`, and
`ContextFilter(..., budget_cap_usd=..., max_submissions=...)`; both limits are required for a real transport unless
the policy is `disabled()`. Endpoint, model and rate come only from your configuration.

A runnable integration example (a function-call tool executor over a mock search with synthetic documents, no SDK):
`python examples/context_filter_tool_executor.py`.

### Context filter flow

```
 question ─┐
           ├─▶ one choice call per document ─▶ P(keep) ─▶ policy ─▶ keep / drop ─▶ selected_ids (input order)
 documents ┘   (question id "relevance",        (from the     (threshold,
               options opt_keep / opt_drop)      endpoint)     top_k, ...)
               a failed call ───────────────────────────────────────▶ keep (fail-open, status says why)
```

`judge_adapter.context_filter` scores retrieved documents against one question through the `choice_endpoint`
backend: one choice call per document, then a keep / drop decision by an explicit policy. Python API only (no CLI).
It returns ids and decisions; it never rewrites, shortens or stores document text.

### Input and output

- Input: `question` (non-empty string) and `documents`: `Document(id, title, url, text)` or mappings with those keys
  (other keys are ignored and never sent). `id` is yours: a non-empty string, unique within the call, never sent to the
  model. `title` / `url` may be empty strings; `text` must be non-empty.
- `FilterResult.documents`: one `DocResult` per input document, **in input order**: `index`, `id`, `score`, `status`,
  `reason`, `decision`, `decision_basis`, `provenance`.
  - `score`: P(keep), the probability the served model returns for `opt_keep` (`ok` and exact-tie outcomes only).
    `None` when the document was not scored or the call failed. It is not a calibrated or validated probability of
    usefulness, and it is never imputed.
  - `status`: `ok`, `error` (`reason` = the adapter failure reason: `http`, `timeout`, `format_error`, `backend_error`,
    `contract`, `local_config`, ...) or `not_scored` (`reason` = `policy_disabled`, `over_limit` or `not_submitted`).
  - `provenance`: prompt version, question id, policy, preset, per-call outcome / detail, served model name (as
    reported), submissions, status polls, cancels, client seconds, server timing, server usage, the per-call cost
    estimate and sha256 of the semantic input and raw output. No text, no endpoint ID, no key.
- `selected_ids`: kept ids, in input order. If nothing is kept it is `[]`; no substitute document is added.
- `usage` (filter calls only), `cost_estimate` (`is_estimate: true`; `usd: null` if any submitted call has unknown
  cost; fake runs are labeled `MOCK`), `timing` (measured only), `budget` (run-wide snapshot), `reduction` (below).

**Default: an explicit policy is required.** `ContextFilter` has no default policy, the same convention as
`AdapterConfig.transport` and the CLI's `--transport`: a call that can cost money or change what an agent sees is never
started by a default. Omitting both `policy` and `preset`, or passing both, raises `ValueError` before anything is
built or sent. `policy=disabled()` is the explicit off switch: zero model calls, every document kept with status
`not_scored` / `policy_disabled`.

### Policies

| Policy | Keeps |
|---|---|
| `threshold(t)` | scored documents with `score >= t` (t in [0, 1]); e.g. 0.2999 is dropped and 0.3 is kept at t = 0.3 |
| `top_k(k)` | the k scored documents with the highest score; exact ties go to the earlier input position |
| `disabled()` | everything, without calling the model |

Under every policy a document without a score (error, not submitted, over the limit when flagged) is **kept**
(fail-open; under `top_k` on top of the k) and its status shows why. Rows are never removed.

**Scope of fail-open (as implemented).** Fail-open is **per document**: an HTTP error, a timeout, an unusable
answer (format failure, handler error, truncation, invalid probabilities) or an unexpected client exception during
that document's call keeps that one document with status `error`; a run-wide stop keeps every later document as
`not_scored` / `not_submitted`. The other documents of the same `filter()` call are decided normally. An exact
0.5 / 0.5 answer is not a failure: it keeps its score and the policy decides. The whole call is refused, with an
exception and no result, only in these cases: invalid input (`FilterInputError`, raised before any call, 0
submissions), invalid constructor arguments (`ValueError`), or an exception raised by a caller-supplied `tokenizer`
or `logger`.

**Zero kept is a normal result.** With `threshold(t)` every scored document can fall below `t`; the result is then
`selected_ids == []` (an empty selection, not an error), and the rows show `status='ok'`, `decision='drop'`. The
package adds no fallback document, no minimum keep count and no re-search; what to do with an empty selection is the
caller's decision (for example, use the unfiltered documents, retry the search, or proceed without documents). In
the HLE run (see [Results](../README.md#results)) this happened in 25 of 72 searches with the preset `v1-thr0.3-dev`; that is a
per-search count from a single run, not a general drop rate or failure rate. `top_k(k)` with k >= 1 never returns an
empty selection when at least one document was scored. The Claude Code plugin's default for this case is to leave
the search result unchanged and say so (see [What the hook changes](plugin.md#what-the-hook-changes)).

### Preset `v1-thr0.3-dev` (opt-in)

`ContextFilter(cfg, backend, preset='v1-thr0.3-dev')` fixes the relevance wording `relevance/v1-dev` (English
instructions and option texts below), its document template (`제목: {title}\nURL: {url}\n\n{text}`; the title label
is Korean for "Title", as in the run it comes from), `threshold(0.3)` and the size range it was tried on (text <= 8,000,
title <= 300, URL <= 500 UTF-8 bytes). It is **a development candidate validated once on 25 questions; not a quality
guarantee**. It was chosen on development data and checked once in a private study; the evidence is not part of this
package. Results depend on the model you serve. It is not a default and not a recommendation for other tasks.
`limits=` may override its size range (recorded in the result); `prompt=` / `policy=` cannot be combined with it.

Instructions: "You are given a question and one web search document below. Decide whether this document provides a
direct answer to the question or intermediate evidence needed to find the answer. Being on the same topic as the
question is not enough on its own. The correct answer to the question is not given." Options: `opt_keep` "Provides:
this document contains a direct answer or needed intermediate evidence.", `opt_drop` "Does not provide: this document
contains neither a direct answer nor needed intermediate evidence."

Without a preset the default prompt `relevance/v1` uses the same wording with the English template
`Title: {title}\nURL: {url}\n\n{text}`, which was not the template that was tried. A custom
`RelevancePrompt(version, instructions, keep, drop, document_template)` may use only `{title}`, `{url}`, `{text}`.

### Wire contract (relevance question)

Same endpoint, polling, cancel and strict restoration as the pairwise contract, with question id `relevance`:

```json
{"input": {"state": {"question": "...", "document": "<rendered document>"},
           "questions": {"relevance": {"type": "choice", "instructions": "...",
                                       "criteria": {"opt_keep": "...", "opt_drop": "..."}}}},
 "policy": {"executionTimeout": 60000, "ttl": 600000}}
```

A valid job has `output.answers.relevance = {"type": "choice", "choice": ..., "probabilities": {"opt_keep": p,
"opt_drop": q}}` (finite, in [0, 1], sum 1 +- 0.01, choice = unique argmax). Probabilities are read by option id,
never by position, and never renormalized. An exact 0.5 / 0.5 tie has no model label but keeps its score.

### Who is responsible for what

- The package: validation before any call, one call per document through the existing choice-endpoint client, strict
  restoration, the policy decision, fail-open rows, run-wide stops, order restoration, the result record.
- The caller: storing the original documents and restoring them by id (`select_originals` helps; a tool can serve
  dropped ids on request, as in the example); choosing a policy; the endpoint, model, rate and caps; deciding whether
  filtering helps their task.
- Not verified: filter quality on your data, any model, task improvement, cost or latency savings. The tests use fake
  transports only; the single observed runs in [Results](../README.md#results) are outside the test suite and are not a
  verification.

## Pairwise judge adapter

Give it one question (one or two turns) and two responses, and it returns `A`, `B`, `tie` or an explicit `error`,
with a trace of every call. It has a JSONL command-line tool, a Python API and an optional binding for FastChat
`llm_judge` (MT-Bench style pairwise judging).

### What it keeps fixed on purpose

- **Fake by default, real by opt-in.** `--transport` has no default. A real (paid) CLI run needs `--allow-real`,
  `--max-submissions` and `--budget-cap-usd`.
- **Retries 0, fallback 0.** One model submission per call. A failure is reported, never turned into a tie or
  retried on another backend.
- **Run-wide limits.** `--max-submissions` and a client-side cost cap for the whole run (not reset per request).
- **Unknown cost stays unknown.** A submitted call without usage/timing has cost `null`, not `0`.
- **Failures before transmission send nothing.** A missing key, missing choice endpoint ID or rate, or a client setup
  error is `local_config` with 0 submissions, and the run stops.
- **Keys are never written.** Stored traces, CLI output files, error details and logs go through a scrubber (exact
  key value, `Bearer ...`, `sk-...`, `rpa_...`). Provider responses are parsed from the raw body, never from the
  scrubbed copy (see [Raw responses and stored traces](#raw-responses-and-stored-traces)).
- **No silent overwrite.** An output directory with results is refused unless `--overwrite`.
- **Rubric / reference are recorded.** Every result carries the prompt version and the sha256 of the rubric and
  reference that were sent.

### Quickstart (CLI)

No key and no network: everything below runs against in-process fake endpoints.

```bash
judge-adapter examples                                   # paths of the bundled synthetic examples
EX=$(judge-adapter examples | grep pairs.example.jsonl)
judge-adapter validate "$EX" --mode two_direction        # no call, no key read
judge-adapter run "$EX" --out ./run-fake --transport fake --mode two_direction
judge-adapter run "$EX" --out ./run-fake-choice --transport fake --backend choice_endpoint
```

A fake run prints a `[MOCK]` summary and writes `results.jsonl`, `summary.json`, `gate.json` and `trace/`. The fake
judge ignores answer quality; it follows test markers only (see [Fake transport](#fake-transport)).

### Pairwise judge flow

```
 question + response A + response B ─▶ backend call (AB; BA in two_direction) ─▶ opt_1 / opt_2 / opt_tie ─▶ A / B / tie
                                                                       a failure ─▶ explicit error (never a tie)
```

### JSONL input

One JSON object per line. The whole file is validated before any call; one bad line rejects the file (exit code 2)
with line numbers, and nothing is sent or written.

```json
{"id": "ex-1", "question_turns": ["Give me one tip for writing a clear bug report."], "response_a": {"id": "draft-v1", "turns": ["Write good reports."]}, "response_b": {"id": "draft-v2", "turns": ["State the exact steps to reproduce, what you expected, and what happened instead."]}}
{"id": "ex-2", "question_turns": ["Name a prime between 10 and 20.", "Now one between 20 and 30."], "response_a": {"id": "bot-x", "turns": ["13.", "23."]}, "response_b": {"id": "bot-y", "turns": ["11.", "25."]}, "turn": 2}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Request id, unique in the file |
| `question_turns` | yes | 1 or 2 user turns, non-empty strings |
| `response_a`, `response_b` | yes | `{"id": ..., "turns": [...]}`; one answer per question turn; ids must differ and must not be `"tie"` |
| `turn` | no | Judged turn; if given it must equal the number of question turns |
| `rubric` | no (v2) | Your evaluation standard, one string (max 8,000 UTF-8 bytes) |
| `reference` | no (v2) | One entry per question turn: a non-empty string or `null` (max 20,000 UTF-8 bytes per entry) |

- Schema v1 = the fields without `rubric` / `reference`; v1 files stay valid. The summary records
  `judge-adapter-pairs/v2`.
- The judged turn is always the last turn. With two turns, the first exchange is context.
- Response ids are never sent to the model; verdicts are restored to them.
- Rejected before any call: missing or unknown fields, duplicate ids, same A/B id, turn-count mismatch, more than 2
  turns, invalid JSON, empty lines, non-UTF-8 input, `criteria` / `instructions` / `judge_prompt` / `system_prompt`
  and near-miss names (`rubrics`, `references`, `reference_answer`, `ref_answer`), `rubric` / `reference` inside a
  response, and any size limit. The whole wire body of one call is limited to 200,000 UTF-8 bytes. Nothing is
  truncated.

### Rubric and reference (prompt `user-criteria/v1`)

- Without `rubric` and `reference`, the fixed default prompt (`default-fixed/v1`) is sent byte for byte.
- `rubric` replaces the general quality standard ("helpfulness, relevance, accuracy, ...") instead of being appended
  to it. `reference` adds a reference-answer field that the instructions describe as a guide, not a candidate.
- Fixed on both paths: the pairwise framing, the bias sentence, options `opt_1` / `opt_2` / `opt_tie`, the strict
  parser and the generation settings. Custom option ids or score scales are not supported.
- The reference block is not tied to a slot, so it does not move when A/B are swapped.
- The field separation does **not** guarantee that a model follows the rubric or resists prompt injection. The
  recorded hashes show what was sent, not that it was followed.

### CLI

```bash
judge-adapter validate PAIRS.jsonl [--backend choice_endpoint] [--mode two_direction] [--max-submissions N]
judge-adapter run PAIRS.jsonl --out DIR --transport fake [--backend choice_endpoint] [--mode two_direction]
judge-adapter run PAIRS.jsonl --out DIR --transport real --allow-real --max-submissions 4 --budget-cap-usd 0.05 \
    [--backend choice_endpoint --choice-endpoint-id ID --choice-usd-per-second RATE [--choice-expected-model NAME]] \
    [--mode two_direction] [--env-file PATH]
judge-adapter examples
```

| Option | Meaning |
|---|---|
| `--transport fake\|real` | Required, no default |
| `--backend haiku_direct\|choice_endpoint` | Default `haiku_direct` |
| `--mode single\|two_direction` | `two_direction` judges AB then BA; different labels -> `tie` with `tie_kind: inconsistent`; if AB fails, BA is not sent |
| `--max-submissions` | Run-wide submission limit (required for real) |
| `--budget-cap-usd` | Run-wide client-side cost cap (required for real) |
| `--allow-real` | Required for real |
| `--out`, `--overwrite` | Output dir; existing results are refused (exit 3) unless `--overwrite`, which replaces only this tool's 5 files |
| `--env-file` | Optional `KEY=VALUE` file, read only by a real run. No default path |
| `--choice-endpoint-id` | RunPod endpoint ID for `choice_endpoint` (else `JUDGE_ADAPTER_CHOICE_ENDPOINT_ID`) |
| `--choice-usd-per-second` | Per-second rate of your endpoint for the estimate and cap; required for a real `choice_endpoint` run (no default; missing -> refused, exit 3) |
| `--choice-expected-model` | Optional expected `output.model`; a different model is a contract failure. Unset: not checked, only recorded |

Output: `results.jsonl` (one row per input line: `outcome` ok / tie / error / unrun, `verdict`, `winner_id`,
`tie_kind`, `failure`, `calls[]`, `cost_estimate`, `criteria_info`), `summary.json`, `gate.json` (run-wide limit
log), `trace/judge_trace.jsonl`, `trace/judge_raw.jsonl` (HTTP bodies, keys removed). All output files are
scrubbed, so a stored body can differ from what the provider sent. No success rate is
reported; every request is counted, including errors and unrun requests.

### Python API

```python
from judge_adapter import AdapterConfig, JudgeAdapter, JudgeRequest, Response, build_backend
from judge_adapter import fakes

cfg = AdapterConfig(transport='fake', backend='haiku_direct', mode='two_direction', trace_dir='trace')
fake = fakes.fake_for(cfg.backend)                      # in-process fake server; no key, no network
backend = build_backend(cfg, transport=fake.transport(), key_provider=lambda: fakes.DUMMY_KEY)
adapter = JudgeAdapter(cfg, backend)

req = JudgeRequest(request_id='q1', question_turns=('What is 17 * 23?',),
                   response_a=Response('calc-x', ('391',)), response_b=Response('calc-y', ('381',)),
                   rubric='Only correctness matters.', reference_turns=('391',))
res = adapter.judge(req)
print(res.status, res.verdict, res.winner_id, res.criteria_info['prompt_version'], res.submissions)
adapter.close()
```

For a real call use `AdapterConfig(transport='real', ...)` and `build_backend(cfg)` (no custom transport). To apply
run-wide limits in your own loop, wrap the backend in `judge_adapter.RunGate(backend, mode, cap_usd,
max_submissions)` and call `gate.begin(request)` before each `adapter.judge(request)`; `judge_adapter.cli.run_file`
does exactly this.

### FastChat binding (optional)

`judge_adapter.hosts.fastchat` replaces `fastchat.llm_judge.common.run_judge_pair` at run time (host source
untouched); `judge-adapter-fastchat` runs FastChat's own `gen_judgment.py` / `show_result.py` through it.
Supported host revision: `587d5cfa1609a43d192cedb8441cac3c17db105d`. Other revisions are not checked.

```bash
# demo workdir: copies FastChat's own question / judge-prompt / reference files from YOUR FastChat checkout
judge-adapter-fastchat demo-workdir --workdir W --fastchat-root /path/to/FastChat
CFG=$(judge-adapter examples | grep config.fake.json)
judge-adapter-fastchat judge --config "$CFG" --workdir W --fastchat-root /path/to/FastChat --confirm -- \
    --mode pairwise-baseline --bench-name adapter_demo --judge-model judge-adapter \
    --baseline-model model-y --model-list model-x
judge-adapter-fastchat show --workdir W --fastchat-root /path/to/FastChat -- --mode pairwise-baseline \
    --bench-name adapter_demo --judge-model judge-adapter --baseline-model model-y
```

- No FastChat or MT-Bench data ships with this package. `--fastchat-root` points at a FastChat source checkout;
  without it the installed `fastchat` package is used. The pip-installed FastChat (`[fastchat]` extra) does **not**
  contain `fastchat/llm_judge/data`, so `demo-workdir` needs a source checkout; with only the extra installed, write
  your own `data/judge_prompts.jsonl` (entries named `pair-v2`, `pair-v2-multi-turn`, `pair-math-v1`,
  `pair-math-v1-multi-turn`; their text is replaced by the adapter) and `data/<bench>/question.jsonl` /
  `model_answer/*.jsonl` in the host's format.
- Host `--mode` must be `pairwise-baseline` or `pairwise-all`. Host `single` (its default) is refused before the host
  starts, because host single grading would call its own judge client outside the adapter.
- Adapter mode must be `single` (the host already judges both positions as g1 / g2).
- `transport: "real"` needs `--allow-real`. The wrapper has **no run-wide cap or submission limit** (only the JSONL
  CLI has `RunGate`).
- Host templates are **replaced, not reproduced**: `pair-v2`, `pair-v2-multi-turn` -> `default-fixed/v1`;
  `pair-math-v1`, `pair-math-v1-multi-turn` -> `user-criteria/v1`. The host's scores, explanations and
  `[[A]]`/`[[B]]`/`[[C]]` format are not used; the host's result rows still show the host prompt names.

#### Reference categories (explicit mapping)

Host categories `math`, `reasoning`, `coding`, `arena-hard-200` need a reference answer. The mapping is explicit:

- `--reference-file PATH`: a FastChat answer-format JSONL you provide (`question_id`, `choices[0].turns`), or
- `--reference-model NAME`: `W/data/<bench>/reference_answer/NAME.jsonl` (e.g. `gpt-4` for FastChat's MT-Bench
  reference in your own workdir).
- Python API: `judge_adapter.fastchat_runner.judge(..., reference_file=..., reference_model=...)`.

The reference is **never inferred from the judge name** and **never auto-filled**. The host looks references up by
`--judge-model`; the wrapper runs the host in a temporary staged workdir (built with **symlinks**) whose
`reference_answer/` holds only a byte copy of the mapped file, named `<judge-model>.jsonl`. A file under the judge
name in your workdir is ignored. The mapping is recorded in `<trace_dir>/fastchat_reference_mapping.jsonl` (source
path, sha256, staged name, mapped questions, `inferred_from_judge_name: false`, `auto_fill: false`, host revision,
template replacement). Reference-category requests send the user reference turns 1..turn as `reference_turns`;
other categories are built exactly as without a mapping (byte-identical wire bodies).

Rejected before any call (exit code 2; the host does not run, no trace dir is created): a missing row for a
reference-category question that will run (after `--first-n`), a reference turn count different from the
question's, an empty or non-text turn, a turn over 20,000 UTF-8 bytes, a duplicate `question_id`, a malformed row,
a missing file, both options at once, a name containing a path, a relative `--judge-file` outside the workdir, and a
host whose `NEED_REF_CATS` differs from the supported revision. Without a mapping, reference-category requests are
`unsupported_input` (0 calls).

Limits of the reference path: host single-mode reference grading (`single-math-v1*`) stays blocked; environments
that cannot create symlinks (some Windows setups) are untested; whether a model follows the reference, judgment
quality and preservation of the host's `pair-math-v1` policy are not verified.

## Configuration and limits

### Backends and configuration

| Backend | Endpoint | Key | Output |
|---|---|---|---|
| `haiku_direct` (default) | Anthropic Messages API, model `claude-haiku-4-5-20251001`, `max_tokens` 8, temperature 0, no system prompt | `ANTHROPIC_API_KEY` | Text that must be exactly `opt_1`, `opt_2` or `opt_tie` |
| `choice_endpoint` | `https://api.runpod.ai/v2/<endpoint-id>/runsync`: a choice-judge worker that you serve on RunPod serverless | `RUNPOD_API_KEY` | Probability per option (see contract below) |

Keys: from the environment variable, or from an env file only if you pass one (`--env-file` /
`AdapterConfig.env_file`). There is no default file path. A key is read only at the first real request, never at
import, validation or in a fake run. (The Claude Code plugin reads `CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY` first; see
[Configure](plugin.md#configure).)

Choice endpoint settings (nothing endpoint-specific is built in):

- endpoint ID: `AdapterConfig.choice_endpoint_id`, `--choice-endpoint-id` or `JUDGE_ADAPTER_CHOICE_ENDPOINT_ID`;
- per-second rate: `AdapterConfig.choice_usd_per_second` / `--choice-usd-per-second` (required for a real run);
- expected model name: `AdapterConfig.choice_expected_model` / `--choice-expected-model` (optional).

Without an endpoint ID or a rate, every real `choice_endpoint` call fails as `local_config` before anything is sent
(no key read, 0 submissions); the CLI refuses a real run without a rate before it starts (exit 3). The fake transport
needs no endpoint and uses a placeholder rate that is labeled `FAKE PLACEHOLDER` in every cost basis.

`AdapterConfig` fields: `transport` (required), `backend`, `mode`, `trace_dir`, `timeout_seconds`,
`choice_endpoint_id`, `choice_poll_interval_seconds`, `choice_max_status_polls`, `choice_usd_per_second`,
`choice_expected_model`, `env_file`.
`AdapterConfig.load(path)` reads a JSON file (keys starting with `_` are comments); see the bundled
`config.*.json` examples.

### Choice endpoint contract

`choice_endpoint` talks to a RunPod serverless worker that you deploy and that answers a multiple-choice judging
request with one probability per option. This package ships no model and no worker. The adapter sends one
`POST /runsync?wait=110000`:

```json
{"input": {"state": {"question": "...", "assistant_1_conversation": "...", "assistant_2_conversation": "..."},
           "questions": {"preference": {"type": "choice", "instructions": "...",
                                        "criteria": {"opt_1": "...", "opt_2": "...", "opt_tie": "..."}}}},
 "policy": {"executionTimeout": 60000, "ttl": 600000}}
```

With a rubric / reference, `state` also has `evaluation_criteria` / `reference_answers`. A valid completed job is
`status: COMPLETED` with `output.answers.preference = {"type": "choice", "choice": <option id>, "probabilities":
{"opt_1": p1, "opt_2": p2, "opt_tie": p3}}`. Probabilities must be finite, in [0, 1] and sum to 1 +- 0.01, and
`choice` must be the unique argmax. `output.model` is optional: it is recorded as `calls[].response_model`, and it is
checked only if you set an expected model. `output.usage`, `executionTime` and `delayTime` are read when present;
`output.error` / `output.truncated` flags are failures. If `/runsync` returns `IN_QUEUE` / `IN_PROGRESS`, the
adapter polls `GET /status/<job>` of the same job (bounded by `choice_max_status_polls` and the 600 s ttl), then
sends one `POST /cancel/<job>`. Anything else is a contract / format failure. The context filter uses the same
contract with the `relevance` question shown [above](#wire-contract-relevance-question).

### Context filter limits, stops and budget

Everything is validated before the first call; one problem refuses the whole input with `FilterInputError`
(0 submissions). Nothing is truncated.

| `FilterLimits` field | Default | Over the limit |
|---|---|---|
| `max_docs` (per `filter()` call) | 50 | refused |
| `max_question_bytes` | 8,000 | refused |
| `max_title_bytes` / `max_url_bytes` / `max_text_bytes` | 1,000 / 2,048 / 20,000 | refused (`oversize='reject'`, default) or kept unscored as `over_limit` (`oversize='flag'`) |
| `max_body_bytes` (one wire body) | 200,000 | as the per-document limits |

Missing, empty or duplicate ids, non-string fields and empty text are refused the same way. (The plugin hook uses the
preset's size range with `oversize='flag'`, so an oversized search result is kept unscored instead of refusing the
whole search.)

One `ContextFilter` is one run: `budget_cap_usd` and `max_submissions` cover all its `filter()` calls. The estimate
is the RunGate time estimate for the choice endpoint (wall clock since the first submission + 60 s idle allowance +
661 s reservation per open job, times your rate); it is client-side, not the bill. Submissions stop, and every later
document is `not_scored` / `not_submitted` and kept, after: the cap or the submission limit would be exceeded; a job
not observed final (HTTP error, timeout, unresolved queue; its reservation stays held); a final job without server
execution time (cost unknown); a contract failure; a local configuration failure (nothing sent, nothing held).
Format failures and handler errors of completed jobs are settled; that document is kept as `error` and the run goes
on. Retries 0.

### Concurrency

Default `concurrency=1`: one request at a time, in input order. `concurrency=N` (opt-in) runs at most N requests in
a thread pool. Each submission is admitted by one atomic check-and-reserve against the cap and the submission limit;
after a stop nothing new is admitted; requests already in flight are waited for and settled; results are restored to
input order; a partial failure affects only its document plus the stop rule above. This is tested with fake barriers
only. No latency improvement is claimed: parallel behaviour and numeric stability of a real server were not tested.

### Logging (local estimate, not billing)

Each `filter()` emits one INFO record on the logger `judge_adapter.context_filter` (also in `FilterResult.reduction`):
documents in / out, UTF-8 bytes of document text in / out, status counts, stop code, mock flag. With
`tokenizer=` (an object with `name`, `version`, `count(text) -> int`) it adds text token counts in / out and the
tokenizer name and version; without one, tokens are reported as unsupported. These are **local estimates of document
text reduction**, not billed usage, not all-turn cached / uncached / output usage of your agent, and they are never
converted into money. The record holds no question, no document text, no ids and no keys. Note that `httpx` logs request
URLs (which include your endpoint ID) at INFO; keep the `httpx` logger at WARNING if your logs are shared (the plugin
hook does this itself).

### Cost estimates (read before a real run)

- All cost numbers are **client-side estimates, not the bill**. The provider console is the source of truth. The
  cap is not an account billing limit.
- Haiku: usage x list price ($1 / $5 per MTok, as checked 2026-09; check the current price). The pre-call
  reservation is (body bytes + 200) x 2 x input price + 8 output tokens.
- choice_endpoint: server `executionTime` x the per-second rate **you configure** (no built-in rate; the package
  does not know your hardware or price). Excludes cold start, idle timeout and queue billing. The cap reserves
  661 s (ttl 600 s + idle 60 s + 1 s) per open job plus 60 s idle once, so the first job needs a cap of at least
  `(661 + 60) x rate` USD, and the first two_direction request `(2 x 661 + 60) x rate` USD.
- Failed requests are included. A submitted call with unknown usage keeps its full reservation and stops the run.

### Raw responses and stored traces

Provider responses are parsed from the body **as received**. Scrubbing (your exact key, `Bearer ...`, and key-like
substrings `sk-...` / `rpa_...`) is applied only where data leaves the process or is written down:

| Where | Content |
|---|---|
| In-memory results: `JudgeResult` / `CallRecord` fields parsed from the response (`label`, `usage`, `response_model`, `job_id`, `job_status`, `server_timing`), `FilterResult` scores and provenance, a backend's `call()` result (`raw_text`, `exchanges[].text`, `exchanges[].json`) | the provider's data, unmodified |
| `detail` / `failure.detail` and `exchanges[].error` (error messages, including text taken from provider error bodies) | scrubbed |
| `trace/judge_raw.jsonl`, `trace/judge_trace.jsonl`, CLI `results.jsonl` and `gate.json` | scrubbed (the whole record) |
| The context filter's INFO log | counts and sizes only |
| The plugin hook's log line | counts, bytes, status and the response's key names / types only |

Why: a legitimate payload can contain a key-like substring by chance. A URL-safe base64 value (for example an
opaque `encrypted_content` blob that a caller replays to the provider) may contain `-sk-` followed by 8+ characters;
scrubbing before parsing rewrote it to `[redacted]` and corrupted the value. Since this fix, values inside any JSON
string reach the caller byte-for-byte.

Compatibility: no signature changed. Before the fix, a response value that matched the scrubber came back with
`[redacted]` in it; it now comes back as sent. The stored files are unchanged in kind (scrubbed) and now scrub the
whole record, not only the HTTP body. `raw_output_sha256` keeps its meaning: the sha256 of the **stored (scrubbed)**
body in `judge_raw.jsonl`, so it can be checked against that file; it equals the hash of the received body whenever
nothing was scrubbed. If you log or save in-memory results yourself, scrub them first (`judge_adapter.keys.Scrubber`;
a backend's `scrub` attribute knows the key after the first request).

### Fake transport

`--transport fake` uses an in-process `httpx.MockTransport` (`judge_adapter.fakes`). The fake verdict reads only
markers in the judged answers:

| Marker | Fake result |
|---|---|
| `[[GOOD]]` | that answer wins (follows the response across swaps) |
| none / both | `tie` (explicit) |
| `FAKE:POSITION` | always slot 1 (two_direction -> `tie` inconsistent) |
| `FAKE:FORMAT` | `format_error` |
| `FAKE:TRUNC` / `FAKE:REFUSE` | `truncated` / `refused` (choice_endpoint: `backend_error`) |
| `FAKE:HTTP500` / `FAKE:TIMEOUT` | `http` / `timeout`; usage unknown, later requests unrun |
| `FAKE:QUEUE` | choice_endpoint: one status poll of the same job |
| `FAKE:LEAK` | the fake server echoes the auth header (scrubbing check) |

The context filter's fake (`fakes.FakeRelevanceEndpoint`) reads only the rendered document: `[[KEEP=p]]` sets P(keep) = p, `[[RELEVANT]]` 0.9, otherwise 0.1; `FAKE:HTTP500`, `FAKE:TIMEOUT`, `FAKE:FORMAT`, `FAKE:REFUSE`, `FAKE:QUEUE` as above; `FAKE:NOTIMING` omits the server timing (cost unknown).

Usage numbers in fake runs are invented. Identical fake results in both directions show that ids are restored
across the swap; they are **not evidence that a real model is order-invariant**.

### Tests

```bash
pip install -e ".[test]"
pytest                                   # model-free; FastChat tests are skipped
JUDGE_ADAPTER_FASTCHAT_ROOT=/path/to/FastChat pytest   # also runs the FastChat host tests (needs the extra's deps)
```

All tests use fake transports; network sockets and key reads are blocked, and the key / endpoint environment
variables are removed for each test. The plugin hook tests (`tests/test_plugin_hook.py`) drive the hook entry point
with sample hook inputs and a fake choice endpoint, and run the launcher as a subprocess;
`tests/test_query_hook.py` does the same for the optional PreToolUse options and the explicit-request protection,
`tests/test_query_hook_race.py` runs the search-gate retry state in real separate processes, and
`tests/test_toggles.py` covers the toggle file, the control CLI, the command files and each launcher's toggle;
`tests/test_plugin_common.py` checks the shared hook module (loaded without httpx, nothing heavy on the off path).

### Limits

- **No quality claim.** Nothing here shows that a backend judges well or better than another, or that a model
  follows a rubric or reference.
- **No guaranteed savings.** Cost numbers are estimates; the cap is client-side.
- **No claim of general host-policy preservation.** The FastChat binding replaces the host's judge prompts with this
  package's fixed prompts; it does not reproduce them.
- **Tests are mock tests.** This package's test suite runs only against fake transports. An earlier experiment
  version of this code made a 16-call real smoke run (8 Haiku, 8 choice-endpoint calls) through FastChat; that result applies to that
  experiment version, **not to this package**. The context filter at commit `331081c` was run once against a real
  choice endpoint (30 calls on synthetic documents, checking the wire contract, budget, ordering and concurrency; no
  quality claim). The pairwise judge in this package has not been run against a real model or API.
- **Plugin: drop → replace → model not verified in a real run.** See [Verification status](verification.md).
  The replacement path is covered by fake tests only; the shape in which Claude Code hands MCP output to hooks is not
  documented and was observed once.
- **Query-stage options are fake-tested only.** The domain presets were never run in a real session; the search gate
  ran once in a real session and denied a search the user had explicitly asked for (2026-10-11). The explicit-request
  protection added after that is fake-tested only and covers only the listed phrasings. Prompts and thresholds are
  untuned. The gate can still block a needed search (mitigated by the explicit-request check and by allowing the same
  query on retry); the domain lists are examples.
- Pairwise requests run sequentially; no parallelism, no resume. Two turns at most. Custom option ids and score
  scales are not supported. The context filter has opt-in bounded concurrency (default 1), tested with fakes only.

For the plugin and the query-stage options, [verification.md](verification.md) is the current record: the two
plugin bullets above predate the re-verification smoke of 2026-10-11 (the A1 prompt re-run after the fix, and one
observed `input_only` domain application); `ask`, the default, remains unverified.

## Directory structure

```
judge-adapter/
├── .claude-plugin/
│   ├── plugin.json              # plugin manifest: name, version, userConfig
│   └── marketplace.json         # marketplace "datumo" listing the plugin (source "./")
├── hooks/
│   ├── hooks.json               # PostToolUse: tavily_search; PreToolUse: WebSearch + tavily_search; UserPromptSubmit
│   ├── context_filter_hook.py   # stdlib launcher -> judge_adapter.plugin_hook (exits at once when toggled off)
│   ├── query_options_hook.py    # stdlib launcher -> judge_adapter.query_hook (exits at once when options are off)
│   ├── user_prompt_hook.py      # stdlib launcher -> judge_adapter.prompt_signal (UserPromptSubmit; gate only)
│   └── davi_toggle.py           # stdlib control CLI: status, filter / gate / domain on|off, domain-mode, reset
├── commands/                    # fixed commands: /davi:status, /davi:{filter,gate,domain}-{on,off}, /davi:domain-mode-*
├── bin/davi-toggle              # runs hooks/davi_toggle.py; on the Bash tool PATH while the plugin is enabled
├── src/judge_adapter/
│   ├── context_filter.py        # ContextFilter, policies, preset
│   ├── plugin_hook.py           # PostToolUse hook: parse Tavily results, filter, rebuild, fail-open
│   ├── query_hook.py            # PreToolUse hook: optional search gate and domain presets (off by default)
│   ├── query_presets.py         # EXPERIMENTAL wording, example presets, guard and explicit-request patterns
│   ├── prompt_signal.py         # UserPromptSubmit: explicit search request -> per-session state (no prompt text)
│   ├── toggles.py               # toggles.json: read, precedence, locked atomic writes
│   ├── plugin_common.py         # stdlib-only contracts shared by the hooks: options, env names, log line, wall budget
│   ├── plugin_names.py          # plugin / marketplace working names in one place
│   ├── backends.py              # haiku_direct and choice_endpoint backends (httpx, retries 0)
│   ├── core.py                  # JudgeAdapter, build_backend, trace writer
│   ├── contract.py              # AdapterConfig, request / result types
│   ├── gate.py                  # RunGate, JobLedger: run-wide submission and cost limits
│   ├── keys.py                  # key loading and scrubbing
│   ├── prompt.py, user_prompt.py  # fixed judge prompts, rubric / reference prompt
│   ├── cli.py                   # judge-adapter JSONL CLI
│   ├── fakes.py                 # in-process fake endpoints
│   ├── fastchat_runner.py, hosts/fastchat.py   # optional FastChat binding
│   └── examples/                # bundled synthetic configs and pairs
├── examples/context_filter_tool_executor.py
├── tests/                       # model-free tests (fake transports only)
├── LICENSE, NOTICE
└── pyproject.toml
```
