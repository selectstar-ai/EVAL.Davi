# Verification status

[README](../README.md) · [Claude Code plugin](plugin.md) · [Reference](reference.md) · [한국어](ko.md)

This page records how each part of the `davi` plugin and the query-stage options was checked for 0.4.0. "Observed"
means seen in a real Claude Code session, usually once; it is not a measured rate. Fake tests use in-process fake
endpoints and synthetic content. No row is a quality claim.

## Summary

**Observed in a real run (one session each unless stated):**

- Built-in WebSearch: the hook was reached; no per-result text; 0 filter calls; result unchanged.
- Tavily `tavily_search` (Claude Code 2.1.285, tavily-mcp 0.2.22, one search, 5 results): 5 filter calls, 5 valid
  answers, all 5 kept, so the result was left unchanged.
- Fixed `/davi:*` commands `status`, `gate-on`, `gate-off`, `domain-on`, `domain-off` in headless `claude -p` with
  `--plugin-dir` (2026-10-11).
- The known A1 prompt with the gate on (after the fix): `explicit_set`, then the gate kept the WebSearch with 0 filter
  calls (`explicit_request_keep`). This covers the known A1 phrasing only.
- Gate and domain both off: no query-hook log line, no filter call.
- Domain preset applied by `input_only`: observed **once** (`allowed_domains: ["docs.python.org"]`).
- After a gate deny (before the fix), Claude retried the same query, which then ran (`retry_allowed`): observed once.

**Fake tests only:**

- Dropped results removed and the shorter result received by the model (no result was dropped in the real run).
- Query-stage output shapes, gate deny / retry / TTL / session isolation, domain fields per tool, fail-open, zero
  intervention, logs.
- Explicit-request protection beyond the A1 prompt: other English and Korean phrasings, state handling, follow-ups.

**Unverified:**

- Whether Claude Code applies `updatedInput` from `domain_apply_mode=ask`, the **default** (needs an interactive
  session).
- `/davi:filter-*`, `/davi:domain-mode-*` and interactive use of the commands.
- Gate usefulness and **general** preservation of user intent (phrasings outside the explicit-request list can still
  be denied).

**Failures and deviations recorded:**

- **A1 semantic failure (real smoke, 2026-10-11, before the fix):** with the gate on, "Using web search, confirm how
  many days there are in a week. Answer in one sentence." led to a WebSearch that the gate denied with
  P(not needed) = 0.999, a search the user had explicitly asked for. Release-blocking for the gate; the gate stays
  default-off and experimental. The UserPromptSubmit fix protects the listed phrasings only
  ([details](plugin.md#optional-query-stage-options-experimental)).
- **`input_only` timeout (same smoke):** the single filter request timed out after 15.3 s (`no_answer`), so the input
  was left unchanged (fail-open observed). The cause was not established and the remote job's cost is unsettled. The
  smoke harness missed this in-hook timeout as a stop condition, so two further sessions ran (a deviation, kept as
  recorded).

## Full table

| What | How it was checked | Result |
|---|---|---|
| Parsing, keep/drop, same-shape rebuild, fail-open, zero intervention on other tools | Fake transport tests with fixtures rebuilt from the Tavily formatter (synthetic content) | Pass |
| Manifest and marketplace | `claude plugin validate --strict` (Claude Code 2.1.285) | Pass |
| Built-in WebSearch | One real headless session | Hook reached; no per-result text; 0 filter calls; result unchanged |
| Tavily `tavily_search` | One real headless session (Claude Code 2.1.285, tavily-mcp 0.2.22, one search, 5 results) | Hook ran; 5 filter calls, 5 valid answers; all 5 kept, so the result was left unchanged |
| Dropped results removed and the shorter result received by the model | Fake tests only | **Not verified in a real run** (no result was dropped in the real run) |
| Query-stage options: output shapes, gate deny / retry / TTL / session isolation, domain fields per tool, fail-open, zero intervention, logs | Fake transport tests only (`tests/test_query_hook.py`) | Pass. **Not run against a real endpoint, model or Claude Code session**; no quality claim |
| Search-gate retry state across processes: at most one deny per key; TTL refresh and pruning keep the new record; sessions independent | Real separate processes with a fake endpoint (`tests/test_query_hook_race.py`, POSIX) | Pass |
| Search gate, explicit user request (real smoke 2026-10-11, before the fix): "Using web search, confirm how many days there are in a week. Answer in one sentence." | One real Claude Code session, gate on | **Semantic failure**: WebSearch "how many days in a week" denied, P(not needed) = 0.999. Release-blocking |
| Explicit-request protection (UserPromptSubmit state → gate keeps with no call): the A1 prompt and query, English and Korean phrasings, no state / expired / unreadable / other session / follow-ups / prompt_id, off → on, no prompt text stored, logged or sent | Fake tests only (`tests/test_query_hook.py`, `tests/test_toggles.py`) | Pass. **Not re-run in a real session**; phrasings outside the list are not covered |
| Toggles: CLI validation, 0600 + atomic writes, concurrent changes from separate processes, corrupt files, precedence, the three toggles independent, each hook and launcher honours its toggle, status without secrets | Local tests (`tests/test_toggles.py`; launchers as real subprocesses) | Pass |
| Fixed `/davi:*` commands (`status`, `gate-on`, `gate-off`, `domain-on`, `domain-off`) in headless `claude -p` (re-verification smoke, 2026-10-11, `--plugin-dir`) | One real session per command; toggle file compared with the expected value, the data-dir id in the command output and in the hook log compared | Observed working: each command changed or showed the toggle file as expected. `filter-*`, `domain-mode-*` and interactive use were not run |
| Explicit-request protection in a real session (same smoke): the A1 prompt with the gate on | One real session | Observed: user-prompt hook `explicit_set`, then the gate kept the WebSearch with 0 filter calls (`explicit_request_keep`). This covers the known A1 phrasing only; general intent preservation is unverified |
| Gate and domain both off (same smoke) | One real session with a WebSearch | Observed: no query-hook log line, no filter call |
| Domain preset applied by `input_only` (same smoke): the model's WebSearch input had no domain fields | One real session | Not verified in that run: the single filter request timed out after 15.3 s (`no_answer`), so the input was left unchanged (fail-open observed). The cause of the timeout was not established (a cold start is a hypothesis only); the remote job's cost is unsettled. The smoke harness also missed this in-hook timeout as a stop condition, so two further sessions ran (a deviation, kept as recorded) |
| Domain preset applied by `input_only`, same fixed prompt, one more session (2026-10-11, endpoint warmed by the user) | One real session | Observed once: the model's WebSearch input had no domain fields; one filter call chose `python_docs` (top probability 0.902); the hook returned `updatedInput` with `allowed_domains: ["docs.python.org"]` and no permission decision; Claude Code's debug log recorded "modified tool input keys: [query, mode, allowed_domains]". All result hostnames were `docs.python.org` (indirect evidence that the server applied the restriction). `ask` (the default) is still unverified |
| Gate usefulness and preservation of user intent | Not checked | **Unverified** (development option) |
| Whether Claude Code applies `updatedInput` from `domain_apply_mode=ask` | Not run (`ask` needs an interactive session) | **Unverified**; `input_only` was observed once (row above) |
| What Claude does after a gate deny | One earlier real session (before the fix): it retried the same query, which then ran (`retry_allowed`) | Observed once |

The real runs used a self-hosted choice endpoint; the exact filter-call cost of those runs was not recorded, and
client-side reservation caps are not bills.

## Related limits

Copied from the reference [Limits](reference.md#limits):

- **Tests are mock tests.** This package's test suite runs only against fake transports. An earlier experiment
  version of this code made a 16-call real smoke run (8 Haiku, 8 choice-endpoint calls) through FastChat; that result applies to that
  experiment version, **not to this package**. The context filter at commit `331081c` was run once against a real
  choice endpoint (30 calls on synthetic documents, checking the wire contract, budget, ordering and concurrency; no
  quality claim). The pairwise judge in this package has not been run against a real model or API.
- **Plugin: drop → replace → model not verified in a real run.** See [Verification status](#verification-status).
  The replacement path is covered by fake tests only; the shape in which Claude Code hands MCP output to hooks is not
  documented and was observed once.
- **Query-stage options are fake-tested only.** The domain presets were never run in a real session; the search gate
  ran once in a real session and denied a search the user had explicitly asked for (2026-10-11). The explicit-request
  protection added after that is fake-tested only and covers only the listed phrasings. Prompts and thresholds are
  untuned. The gate can still block a needed search (mitigated by the explicit-request check and by allowing the same
  query on retry); the domain lists are examples.

Note: the last two bullets were written before the re-verification smoke of 2026-10-11. Where they differ from the
table above (the gate's A1 prompt was re-run after the fix, and a domain preset was applied by `input_only` once in a
real session), the table is the newer record; `ask` remains unverified.
