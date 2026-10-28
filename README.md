# Davi as a router for Claude Code subagents

Coding agents usually run every task on one model. A small model is cheap but misses hard tasks; a strong model
solves more of them but spends its price on easy work too. Routing each task to the right model would get the best
of both, but only if the router itself is cheap, fast, and stable.

This project tests a decision model as that router. `davi-route` is a Claude Code plugin: before a subagent runs,
Davi rates how hard the task is, and the plugin picks **haiku**, **sonnet** or **opus** for that one call. We compare
Davi with two other judges, TypeSafe Jev and Claude Haiku 5.5, and with single-model baselines on the same tasks,
measuring accuracy, cost, and consistency.

[한국어 README](docs/README.ko.md) · [Routing experiment log (Korean)](docs/routing.md) ·
[Reference](docs/reference.md) ·
[GitHub](https://github.com/selectstar-ai/EVAL.Davi)

> Experimental. Every hook is off by default and fail-open: a hook error or a slow judge leaves the call unchanged,
> and a failed judgment falls back to the cheap model (`on_error`). A hook never blocks Claude Code on its own error.

## What does davi-route do?

A hook cannot switch the main session's model, so routing happens where Claude Code accepts a model per call: the
`model` field of each `Agent` tool call. The plugin rewrites it in a PreToolUse hook (`updatedInput`), which we
verified in a real Claude Code run.

```
main agent ── Agent tool call (prompt, model?) ──▶ PreToolUse hook (davi-route)
                                                     │
                                                     ▼
                                    judge: "how hard is this task for a small
                                    model answering from memory?"  levels 1-5
                                                     │  expected level, 0-1
                         ┌───────────────────────────┼───────────────────────────┐
                    level < 0.48               0.48 ≤ level < 0.78           level ≥ 0.78
                         ▼                           ▼                           ▼
                   model = haiku               model = sonnet              model = opus
                         └──────────── updatedInput.model ──▶ subagent runs ─────┘

judge failed ──▶ cheap model (on_error=accept) or strong (on_error=escalate)
judge too slow (wall_budget_seconds) ──▶ the call is left as it was
```

The judge returns probabilities over five difficulty levels. Their expected value, scaled to `0`–`1`, is compared
with two cut-offs:

| Expected level | Model | Typical task |
| --- | --- | --- |
| below `0.48` | haiku | A small model should answer it from memory |
| `0.48` to `0.78` | sonnet | Needs more knowledge or reasoning |
| `0.78` and above | opus | Hard for anything but the strong model |

## Why judge the task instead of the answer?

The obvious router runs the cheap model first, checks its answer, and escalates when the answer looks wrong. That
costs a cheap run on every task and a second run on every escalation, and the check happens after the latency is
already spent.

`davi-route` judges **only the task**, before any answer exists. Routing then costs one judge call and no extra
model run. With Davi (`/v1/systemone`) the five levels come back as probabilities in one call, typically in under a
second.

A judge that sees only the task can still be consistently wrong, so we measure routing by what matters: whether the
routed model answered correctly, and what it cost.

## Experiment

We used K-HLE, an in-house set of 174 hard Korean closed-book QA items, and ran each item with haiku 5.5,
sonnet 5.5 and opus 5.5 headless (`claude -p`). We repeated the whole run five times, giving 870 runs per policy,
and graded every answer against its reference.

Each judge rated the difficulty of every task, and the 3-tier policy above turned its rating into a model choice:

| Judge | How it rates difficulty |
| --- | --- |
| Davi | Choice probabilities over the five levels, one `/v1/systemone` call |
| TypeSafe Jev | Choice probabilities over the five levels, one `/v1/systemone` call |
| Claude Haiku 5.5 | A verbalized level (no logprobs), Anthropic Messages API |

We compare the routed policies with three baselines that send every task to one model.

## Results

### Accuracy and cost

Correct answers and model-run cost over the five rounds:

| Policy | Correct (of 870) | Accuracy | Model-run cost |
| --- | ---: | ---: | ---: |
| haiku only | 451 | 51.8% | $0.96 |
| sonnet only | 574 | 66.0% | $9.15 |
| 3-tier, Claude Haiku 5.5 as judge | 587 | 67.5% | $10.18 |
| 3-tier, TypeSafe Jev as judge | 604 | 69.4% | $9.66 |
| **3-tier, Davi as judge** | **619** | **71.1%** | **$10.78** |
| opus only | 612 | 70.3% | $14.99 |

Against opus only, Davi's routing got 7 more answers right (+0.8 points) at **28% lower cost** ($4.21 less). It
matched or beat opus in 3 of 5 rounds and cost less in every round (70–75% of opus).

The default cut-offs were fitted on this same data, so these numbers are optimistic. For a fairer comparison we chose
the cut-offs on half of the items, under a budget of 80% of opus only, and measured on the other half:

| Judge | Held-out accuracy |
| --- | ---: |
| Davi | 69.9% |
| TypeSafe Jev | 66.9% |
| Claude Haiku 5.5 | 63.4% |

Davi's routing was ahead of Jev's and Claude's in every round.

### Consistency

A router should send the same task to the same model every time. Across the five rounds, Davi changed its tier for
**2** of 174 items, Jev for 14, and Claude for 40.

| Judge | Items whose tier changed across rounds |
| --- | ---: |
| Davi | 2 |
| TypeSafe Jev | 14 |
| Claude Haiku 5.5 | 40 |

### Inside Claude Code

The numbers above come from offline replay. To check the plugin end to end, we ran 43 candidate items four times
through the real plugin (`Agent` tool, `route_mode=tiers`, Davi as judge). 24 items got the same model and a correct
answer in all four runs. These 24 were selected on outcomes; they show the mechanism working, not a performance
estimate.

### Limits

- **One dataset.** One language, one task type (closed-book QA). Coding or long agentic tasks were not measured.
- **Fitted cut-offs.** The defaults (`0.48` / `0.78`) come from K-HLE, which is hard by design. Recalibrate for your
  own workload; the split-half figures are the fairer comparison.
- **Costs are estimates.** They are Claude Code `total_cost_usd` list-price estimates of the model runs, not bills.
  **Judge calls are not included**: Davi's price is not set, and Jev's $0.042/MTok is an assumption.

## What cheap routing changes

Most subagent calls in a session do not need the strongest model, and a router that costs one sub-second call per
task makes it practical to stop paying for it everywhere. The saving is only worth having if accuracy holds, which
is why the cut-offs, the judge, and the fail-open defaults are all things you should check against your own tasks
before relying on them.

## Run the plugin

Requirements:

- **Python 3.10+** (`python3` on the PATH)
- **Claude Code** with PreToolUse `updatedInput` support for the `Agent` tool. Tested with Claude Code 2.1.299 and
  2.1.300; check yours with `claude --version`.
- A judge: a Davi server, or a TypeSafe API key for Jev ([Use Jev as the judge](#use-jev-as-the-judge))

Install the plugin from the `datumo` marketplace (this repository), in the CLI or in the Claude app's plugin
settings:

```bash
claude plugin marketplace add selectstar-ai/EVAL.Davi
claude plugin install davi-route@datumo
```

Then, in Claude Code (the terminal or the desktop app's **Code tab**), point it at your Davi server. This turns
routing on (`tiers` mode) and checks that the server answers:

```
/davi-route:setup http://<your-davi-host>:<port> --set log_file=/tmp/route.jsonl
```

`setup` is a skill, so asking in words also works ("use the Davi server at http://host:port for davi-route");
approve the `python3 .../configure.py` run when asked. Inspect or turn off routing the same way (`--show`, `--off`).

Then ask for work that uses subagents ("Use the Agent tool to ..."). Each `Agent` call gets a model, and each hook
call writes one JSON line (level, tier, model, latency; never the task text):

```bash
davi-route hooklog /tmp/route.jsonl
```

The plugin works only where hooks run: Claude Code and Cowork. In a regular claude.ai chat, hooks and subagents do
not run, so nothing is routed there.

### Use Jev as the judge

Davi is our own model. If you do not have a Davi server, the same 3-tier routing runs with TypeSafe Jev, which
answers the same five-level question through `https://api.typesafe.ai/v1/systemone`. In our experiment Jev's routing
was second to Davi's (69.4% vs 71.1%) at a lower model-run cost.

1. Get an API key from [TypeSafe](https://docs.typesafe.ai/introduction) and export it before you start Claude Code,
   so the key never goes through the chat:

   ```bash
   export JEV_API_KEY=<your-typesafe-key>     # TYPESAFE_API_KEY also works
   claude
   ```

2. Turn routing on with Jev. Without a Davi URL, `setup` does not switch routing on by itself, so set it explicitly:

   ```
   /davi-route:setup --judge jev --mode tiers --set route_enabled=true --set log_file=/tmp/route.jsonl
   ```

   Or, without the skill:

   ```bash
   export DAVI_ROUTE_ROUTE_ENABLED=true
   export DAVI_ROUTE_ROUTE_MODE=tiers
   export DAVI_ROUTE_ROUTE_JUDGE=jev
   ```

3. Check the judge directly:

   ```bash
   davi-route verify --judge jev --kind tier --task "What is 17*23?" --candidate ""
   ```

The default cut-offs (`0.48` / `0.78`) were fitted for Davi. Jev's levels fall differently, so recalibrate them for
Jev with [Measure your own workload](#measure-your-own-workload) and set `route_tier_low` / `route_tier_high`.

### Without the setup skill

The claude.ai plugin page has no fields for plugin options, so the skill writes them to `~/.claude/davi-route.json`
instead; the hooks read that file on every call. The same options also work as environment variables (shell or
`settings.json` `env`):

```bash
export DAVI_ROUTE_DAVI_BASE_URL=http://<your-davi-host>:<port>   # POST /v1/systemone
export DAVI_ROUTE_ROUTE_ENABLED=true
export DAVI_ROUTE_ROUTE_MODE=tiers
export DAVI_ROUTE_LOG_FILE=$PWD/route.jsonl
claude --plugin-dir plugins/davi-route
```

Option precedence: `DAVI_ROUTE_<KEY>` environment variables, then `~/.claude/davi-route.json` (`DAVI_ROUTE_CONFIG`
moves it), then the plugin options (`claude plugin configure`, read as `CLAUDE_PLUGIN_OPTION_<KEY>`).

### Dependencies

The installed copy is self-contained: the routing code ships in `plugins/davi-route/lib` (generated by
`davi-route bundle`), and at session start the plugin pip-installs `httpx` into `${CLAUDE_PLUGIN_DATA}/pydeps` if
`python3` cannot import it (needs network once; option `auto_install_deps`, default on). If that fails, the hook
tries `${CLAUDE_PLUGIN_DATA}/venv` and otherwise logs `dependency_missing` and changes nothing.

For development, load the plugin from a checkout instead (the checkout's `src` then wins over `lib`):

```bash
git clone https://github.com/selectstar-ai/EVAL.Davi && cd EVAL.Davi
pip install -e ".[test,lint]"
claude --plugin-dir plugins/davi-route
```

After changing anything under `src/judge_adapter`, run `davi-route bundle` (a test fails on a stale `lib`).

### Configuration

| Key | Default | Purpose |
| --- | --- | --- |
| `route_enabled` | `false` | Turn subagent routing on |
| `route_mode` | `post` | Use `tiers` (3 models by difficulty); `setup` with a Davi URL sets it. Other modes: [Planned features](#planned-features) |
| `route_judge` | `davi` | `davi`, `jev`, `claude`, `fake` |
| `davi_base_url` / `davi_api_key` | (unset) | Davi server (`POST /v1/systemone`) and its optional key; override the `DAVI_BASE_URL` / `DAVI_API_KEY` environment variables |
| `typesafe_api_key` | (unset) | Jev key; `JEV_API_KEY` or `TYPESAFE_API_KEY` in the environment also works |
| `route_cheap_model` / `route_mid_model` / `route_strong_model` | `haiku` / `sonnet` / `opus` | Model strings |
| `route_tier_low` / `route_tier_high` | `0.48` / `0.78` | Difficulty cut-offs (fitted on K-HLE with Davi) |
| `route_agent_types` | (all) | Comma-separated `subagent_type` filter |
| `on_error` | `accept` | On a judge failure: `accept` (cheap model, fail-open) or `escalate` |
| `wall_budget_seconds` | `25` | Time limit per hook call; past it nothing changes |
| `log_file` | (none) | JSONL log of every hook call |

Every option, its effect and the judge prompts: [docs/routing.md](docs/routing.md).

### Judges

| Judge | Endpoint | Probability | Cost estimate |
| --- | --- | --- | --- |
| `davi` | `DAVI_BASE_URL` + `/v1/systemone` (optional key `DAVI_API_KEY`); else a RunPod choice endpoint | Choice probabilities over 5 levels | Unknown until `davi_usd_in_per_mtok` is set |
| `jev` | `https://api.typesafe.ai/v1/systemone`, key `JEV_API_KEY` or `TYPESAFE_API_KEY` | Choice probabilities over 5 levels | Usage × $0.042/MTok (assumed) |
| `claude` | Anthropic Messages, key `ANTHROPIC_API_KEY`, default `claude-haiku-5-5`, effort low | A verbalized number (no logprobs) | Usage × list price |
| `fake` | In process; `[[ACCEPT=p]]` in the text sets P | Fixed | 0 |

## Measure your own workload

The `davi-route` CLI reproduces the experiment on any task set: it runs tasks headless, judges them, and replays
routing policies offline.

```bash
# 1) run every task once per model (spends money: needs --yes; --dry-run prints the commands)
davi-route generate --tasks tasks.jsonl --cache runs/x \
    --arm cheap=haiku --arm mid=sonnet --arm strong=opus --yes
# 2) rate each task's difficulty with one or more judges
davi-route judge --tasks tasks.jsonl --cache runs/x --source-arm cheap \
    --judge davi --judge jev --kind tier
# 3) curves, baselines and a frozen threshold on a held-out split
davi-route analyze --cache runs/x --cheap cheap --strong strong
```

Run `generate` without `ANTHROPIC_API_KEY` in the environment: `claude -p` would bill that key instead of the
logged-in plan.

A task line has `id`, `prompt` and either `answer` + `grader` (`contains`, `exact`, `regex`, `number`, `llm`) or a
`check_cmd`; see [examples/routing/](examples/routing/). Other commands: `regrade`, `curate`, `demo`, `hooklog`,
`probe`. Details: [docs/routing.md](docs/routing.md).

## Project layout

- `plugins/davi-route/` — The Claude Code plugin: hooks, setup skill, bundled `lib`
- `src/judge_adapter/routing/` — Routing code (PEP 8, 79 columns, conventions in [CLAUDE.md](CLAUDE.md))
  - `config.py` — Options and secrets from the environment (`DAVI_ROUTE_*` over `CLAUDE_PLUGIN_OPTION_*`)
  - `questions.py` — Judge questions per kind (`answer`, `completion`, `pre`, `tier`) and the Davi phrasing ensemble
  - `verifiers.py` — `Verifier` (abstract) → `FakeVerifier`, `DaviRunPodVerifier`, `HttpVerifier` →
    `SystemOneVerifier` (`DaviVerifier`, `JevVerifier`), `ClaudeVerifier`
  - `policy.py` — `RoutePolicy` (accept / escalate) and `TierPolicy` (cheap / mid / strong)
  - `hooks.py` — `Settings`, `State`, `RouteHook` (abstract) → `AgentPreHook`, `AgentPostHook`, `StopHook`, and `main`
  - `grading.py` — `Grader` (abstract) → exact, contains, regex, number, command, LLM graders
  - `harness.py` — Tasks, arms, `RunCache`, `claude -p` runs, judging, regrading
  - `analysis.py` — `ReplayRow`, baselines, sweeps, frozen thresholds, demo tables
  - `cli.py`, `probe.py` — The `davi-route` command and the hook mechanism probes
- `src/judge_adapter/decision/` — Decision core: `ChoiceQuestion` / `NoulQuestion` and the `/v1/systemone`
  clients (`DaviClient`, `JevClient`) with strict reading and five error codes
- `src/judge_adapter/reading/` — `davi-read`: blocks, fetching limits, two-pass judging, choosing, the command
- `plugins/davi-route/skills/davi-read/`, `plugins/davi-route/hooks/davi_read.py` — The `davi-read` skill and the
  plugin's launcher for it
- `examples/routing/` — Example task and arm files
- `docs/` — Routing details (Korean), legacy plugin and reference docs
- `tests/test_routing.py`, `tests/test_decision.py`, `tests/test_reading.py` — Tests

```bash
ruff check && ruff format --check
PYTHONPATH=src pytest -q
```

## References

- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code plugins](https://code.claude.com/docs/en/plugins)
- [TypeSafe introduction (Jev)](https://docs.typesafe.ai/introduction)

## Planned features

The features below are in the code but not part of the 3-tier routing measured above. They are experimental, have
no published results yet, and may change.

- **Answer-checking modes** (`route_mode`):
  - `post` (the current default value): haiku runs first, the judge reads its answer, and opus re-runs when the
    answer is likely wrong.
  - `pre`: two models, routed up front by a yes/no judgment on the task.
  - `both`: `pre` and `post` together.
- **Tier answer check** (`route_tier_check`): in `tiers` mode, also judge answers that went to the cheap model.
- **Completion check at Stop** (`stop_verify_enabled`): before the session ends, the judge checks whether the work is
  done, and blocks the stop at most `stop_max_blocks` times.

| Key | Default | Purpose |
| --- | --- | --- |
| `route_tier_check` | `false` | `tiers`: also judge answers that went to the cheap model |
| `route_threshold` / `route_pre_threshold` | `0.5` / `0.5` | `post` / `pre` thresholds on P(accept) |
| `route_pin_cheap` | `always` | `post`: when to set the cheap model on an `Agent` call |
| `stop_verify_enabled` | `false` | Completion check at Stop (blocks at most `stop_max_blocks` times) |

Turn one on with `setup`, for example `/davi-route:setup --mode post` or `--set stop_verify_enabled=true`. To measure
the answer-checking modes offline, judge the cheap answers with `davi-route judge ... --kind answer`.

## Experimental: read a page with `davi-read`

`davi-read` prints the parts of a web page (or a local HTML, Markdown or text file) that answer a question, copied
verbatim with their heading path. An agent can call it instead of WebFetch, which returns a paraphrase. It is a new
feature with no measured results yet: it is not shown to improve answers, only to return the source text.

It comes with the `davi-route` plugin: install the plugin, give it your Davi server once, and Claude Code uses the
`davi-read` skill for pages where it needs a specific fact. httpx is installed for the plugin at the first call.

```text
/plugin install davi-route@datumo
/davi-route:setup http://<davi-host>:<port> --off     # --off: davi-read only, routing stays off
```

From a checkout, the same command runs in a terminal:

```bash
pip install -e .            # provides the davi-read command
davi-read https://docs.python.org/3/library/functools.html "What happens if lru_cache maxsize is None?"
davi-read --file docs/routing.md "route_mode는 무엇을 바꾸나요?"
```

- **How (the same method as [jevfetch](https://github.com/a2sys-ai/jevfetch), our own code):** the page is split
  into blocks (paragraphs, list items, code, table rows; documentation notes kept; a sentence ending in ":" joined
  with its list). The judge rates every block in windows of about 3,000 tokens (one request per window, one `noul`
  question per block); each window's likely blocks are rated again alone with their neighbouring text. Kept: blocks
  at or above the keep threshold, neighbours under the same heading that pass on their own score, the first block,
  and the header rows of kept table rows. Pages up to about 2,000 tokens are returned whole. With the judge fixed,
  the judge model is the only difference from jevfetch.
- **Judge:** Davi by default, read from the same options as the routing hooks (`DAVI_BASE_URL`, `/davi-route:setup`).
  Because Davi runs on your own server, internal pages stay in house. `--judge jev` uses TypeSafe Jev instead and
  sends the page text to its API.
- **Fail-open:** a failed judge request is tried again (up to 3 times); a part of the page that still cannot be
  judged is included unjudged, and only when the whole judge fails is the page printed unjudged (`judge_failed`).
- **Limits:** hosts that resolve to private addresses are refused unless allowed (`--allow-host`, or the
  `read_allow_hosts` option for an intranet); pages over 5 MB are refused. A PDF or other file is saved and its path
  printed (`# saved:`) for the agent's file reader. A page whose static HTML has almost no text is re-read with
  headless Chrome when it is installed (`--no-render` to skip). The thresholds (0.6 / 0.8 / 0.6) are jevfetch's,
  not fitted to Davi.
- **Agent setup:** the plugin's [davi-read skill](plugins/davi-route/skills/davi-read/SKILL.md) tells Claude when to
  use it and how to read the result; it runs the plugin's own copy of the code (`plugins/davi-route/lib`).

On the Python `functools` page (2026-10-27, Davi, one run each), it kept 6 of 143 blocks (1,327 of 32,464
characters) in English and 7 of 143 (1,410 of 27,358) in Korean, both including the sentence that answers the
question, in about 6 seconds.

## Legacy: the `davi` search-result filter

This repository started as `judge-adapter`: a pairwise LLM-judge adapter and a Claude Code plugin (`davi`) that
filters Tavily search results with a choice model before the agent reads them. It is unchanged and still installs
as before (`claude plugin install davi@datumo`). See [docs/plugin.md](docs/plugin.md),
[docs/reference.md](docs/reference.md), [docs/verification.md](docs/verification.md) and the Korean summary
[docs/ko.md](docs/ko.md).

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
