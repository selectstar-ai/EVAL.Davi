# davi-route

**Send each subagent task to the cheapest Claude model that can solve it.** Before a Claude Code subagent runs, a
judge rates how hard the task is and the plugin picks **haiku**, **sonnet** or **opus** for that one call. Easy work
stays cheap; hard work gets the strong model.

[Routing docs (Korean)](docs/routing.md) · [Legacy filter plugin](docs/plugin.md) · [Reference](docs/reference.md) ·
[GitHub](https://github.com/selectstar-ai/EVAL.Davi)

> Experimental. Every hook is off by default and fail-open: a hook error or a slow judge leaves the call unchanged,
> and a failed judgment falls back to the cheap model (`on_error`). A hook never blocks Claude Code on its own error.

## How it works

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

- **A hook cannot switch the main session's model**, so routing is applied where Claude Code accepts a model per
  call: the `model` field of each `Agent` tool call (PreToolUse `updatedInput`, verified in a real Claude Code run).
- The judge reads **only the task**, before any answer exists, so routing costs one judge call and no extra model
  run. With Davi (`/v1/systemone`) the five levels come back as probabilities in one call, typically under a second.
- Other modes are kept for comparison (`route_mode`): `post` (haiku first, the judge reads its answer, opus re-runs
  when needed), `pre` (two models, routed up front) and `both`. A Stop hook can also check completion
  (`stop_verify_enabled`).

## Results

KLEAR (174 hard Korean QA items), each item run with haiku 5.5, sonnet 5.5 and opus 5.5 headless (`claude -p`), five
rounds, 870 runs per policy. Correct answers and model-run cost over the five rounds:

| Policy | Correct (of 870) | Accuracy | Model-run cost |
|---|---|---|---|
| haiku only | 451 | 51.8% | $0.96 |
| sonnet only | 574 | 66.0% | $9.15 |
| 3-tier, Claude Haiku 5.5 as judge | 587 | 67.5% | $10.18 |
| 3-tier, TypeSafe Jev as judge | 604 | 69.4% | $9.66 |
| **3-tier, Davi as judge** | **619** | **71.1%** | **$10.78** |
| opus only | 612 | 70.3% | $14.99 |

- **Against opus only:** 7 more correct answers (+0.8 points) at **28% lower cost** ($4.21 less). Davi's 3-tier
  policy matched or beat opus in 3 of 5 rounds and cost less in every round (70–75% of opus).
- **Against the other judges:** Davi's routing was ahead of Claude's and Jev's in every round. In a fair split
  (cut-offs chosen on half of the items, measured on the other half, 80% of the opus budget) accuracy was Davi
  69.9%, Jev 66.9%, Claude 63.4%.
- **Consistency:** across the five rounds, Davi changed its tier for 2 items, Jev for 14 and Claude for 40.
- **Inside Claude Code:** 43 candidate items were run four times through the real plugin (`Agent` tool,
  `route_mode=tiers`, Davi). 24 items got the same model and a correct answer in all four runs.

Caveats, stated plainly:

- The default cut-offs (0.48 / 0.78) were fitted on this same data, so the headline numbers are optimistic; the
  split-half figures above are the fairer comparison. Recalibrate for your own workload.
- Costs are Claude Code `total_cost_usd` list-price estimates of the model runs, not bills. **Judge calls are not
  included** (Davi's price is not set; Jev's $0.042/MTok is an assumption).
- One dataset, one language, one task type (closed-book QA). Coding or long agentic tasks were not measured.
- The 24 demo items were selected on outcomes; they illustrate the behaviour and are not a performance estimate.

## Install

Python 3.9+ (`python3` on the PATH). Install the plugin from the `datumo` marketplace (this repository), in the CLI or in the
Claude app's plugin settings:

```bash
claude plugin marketplace add selectstar-ai/EVAL.Davi
claude plugin install davi-route@datumo
```

Then, in Claude Code (the terminal or the desktop app's **Code tab**), point it at your Davi server. This turns
routing on (`tiers` mode) and checks that the server answers:

```
/davi-route:setup http://<your-davi-host>:<port>
```

`setup` is a skill, so asking in words also works ("use the Davi server at http://host:port for davi-route"); approve
the `python3 .../configure.py` run when asked. The plugin works only where hooks run: Claude Code and Cowork. In a
regular claude.ai chat, hooks and subagents do not run, so nothing is routed there.

Everything is off until then. The claude.ai plugin page has no fields for plugin options, so the skill writes them to
`~/.claude/davi-route.json` instead. The hooks read that file on every call, so it applies from the next subagent call.
Change or inspect options the same way (`--mode post`, `--judge claude`, `--set log_file=/tmp/route.jsonl`,
`--off`, `--show`). Option precedence: `DAVI_ROUTE_<KEY>` environment variables (also from the `env` block of Claude
Code's `settings.json`), then `~/.claude/davi-route.json`, then the plugin options (`claude plugin configure`).

The installed copy is self-contained: the routing code ships in `plugins/davi-route/lib` (generated by
`davi-route bundle`), and at session start the plugin pip-installs `httpx` into `${CLAUDE_PLUGIN_DATA}/pydeps` if
`python3` cannot import it (needs network once; option `auto_install_deps`, default on). For development, load it from
a checkout instead (the checkout's `src` then wins over `lib`):

```bash
git clone https://github.com/selectstar-ai/EVAL.Davi && cd EVAL.Davi
pip install -e ".[test,lint]"
claude --plugin-dir plugins/davi-route
```

The hook runs `python3`. If it cannot import `httpx` and `auto_install_deps` is off (or the install failed: no network,
no pip), the hook tries `${CLAUDE_PLUGIN_DATA}/venv` and otherwise logs `dependency_missing` and changes nothing.
After changing anything under `src/judge_adapter`, run `davi-route bundle` (a test fails on a stale `lib`).

## Quickstart

```
/davi-route:setup http://<your-davi-host>:<port> --set log_file=/tmp/route.jsonl
```

Without the command, the same options work as environment variables (shell or `settings.json` `env`):

```bash
export DAVI_ROUTE_DAVI_BASE_URL=http://<your-davi-host>:<port>   # POST /v1/systemone
export DAVI_ROUTE_ROUTE_ENABLED=true
export DAVI_ROUTE_ROUTE_MODE=tiers
export DAVI_ROUTE_LOG_FILE=$PWD/route.jsonl
claude --plugin-dir plugins/davi-route
```

Then ask for work that uses subagents ("Use the Agent tool to ..."). Each `Agent` call gets a model, and each hook
call writes one JSON line (level, tier, model, latency; never the task text):

```bash
davi-route hooklog route.jsonl
```

Settings come from `DAVI_ROUTE_<KEY>` environment variables, then `~/.claude/davi-route.json` (written by
`/davi-route:setup`; `DAVI_ROUTE_CONFIG` moves it), then the plugin's userConfig (`CLAUDE_PLUGIN_OPTION_<KEY>`).

## Settings

| Key | Default | Meaning |
|---|---|---|
| `route_enabled` | `false` | Turn subagent routing on |
| `route_mode` | `post` | `tiers` (3 models by difficulty), `post`, `pre`, `both` |
| `route_judge` | `davi` | `davi`, `jev`, `claude`, `fake` |
| `davi_base_url` / `davi_api_key` | (unset) | Davi server (`POST /v1/systemone`) and its optional key. Set them in the plugin options in the Claude app; they override the `DAVI_BASE_URL` / `DAVI_API_KEY` environment variables |
| `route_cheap_model` / `route_mid_model` / `route_strong_model` | `haiku` / `sonnet` / `opus` | Model strings |
| `route_tier_low` / `route_tier_high` | `0.48` / `0.78` | Difficulty cut-offs (KLEAR-fitted) |
| `route_tier_check` | `false` | `tiers`: also judge answers that went to the cheap model |
| `route_threshold` / `route_pre_threshold` | `0.5` / `0.5` | `post` / `pre` thresholds on P(accept) |
| `route_pin_cheap` | `always` | `post`: when to set the cheap model on an `Agent` call |
| `route_agent_types` | (all) | Comma-separated `subagent_type` filter |
| `stop_verify_enabled` | `false` | Completion check at Stop (blocks at most `stop_max_blocks` times) |
| `on_error` | `accept` | On a judge failure: `accept` (cheap model, fail-open) or `escalate` |
| `wall_budget_seconds` | `25` | Time limit per hook call; past it nothing changes |
| `log_file` | (none) | JSONL log of every hook call |

Every option, its effect and the judge prompts: [docs/routing.md](docs/routing.md).

## Judges

| Judge | Endpoint | Probability | Cost estimate |
|---|---|---|---|
| `davi` | `DAVI_BASE_URL` + `/v1/systemone` (optional key `DAVI_API_KEY`); else a RunPod choice endpoint | Choice probabilities (5 levels for `tier`) | Unknown until `davi_usd_in_per_mtok` is set |
| `jev` | `https://api.typesafe.ai/v1/systemone`, key `JEV_API_KEY` or `TYPESAFE_API_KEY` | Choice probabilities | Usage × $0.042/MTok (assumed) |
| `claude` | Anthropic Messages, key `ANTHROPIC_API_KEY`, default `claude-haiku-5-5`, effort low | A verbalized number (no logprobs) | Usage × list price |
| `fake` | In process; `[[ACCEPT=p]]` in the text sets P | Fixed | 0 |

```bash
davi-route verify --judge davi --kind tier --task "What is 17*23?" --candidate ""
```

## Measuring your own workload

The `davi-route` CLI runs tasks headless, judges them and replays routing policies offline:

```bash
# 1) run every task once per model (spends money: needs --yes; --dry-run prints the commands)
davi-route generate --tasks tasks.jsonl --cache runs/x \
    --arm cheap=haiku --arm mid=sonnet --arm strong=opus --yes
# 2) judge the tasks (tier) or the cheap answers (answer)
davi-route judge --tasks tasks.jsonl --cache runs/x --source-arm cheap \
    --judge davi --judge claude --kind tier
# 3) curves, baselines and a frozen threshold on a held-out split
davi-route analyze --cache runs/x --cheap cheap --strong strong
```

A task line has `id`, `prompt` and either `answer` + `grader` (`contains`, `exact`, `regex`, `number`, `llm`) or a
`check_cmd`. Other commands: `regrade`, `curate`, `demo`, `hooklog`, `probe`. Details: [docs/routing.md](docs/routing.md).

## Code layout

`src/judge_adapter/routing/`, PEP 8 (79 columns) and the conventions in [CLAUDE.md](CLAUDE.md):

| Module | Contents |
|---|---|
| `config.py` | Options and secrets from the environment (`DAVI_ROUTE_*` over `CLAUDE_PLUGIN_OPTION_*`) |
| `questions.py` | Judge questions per kind (`answer`, `completion`, `pre`, `tier`) and the Davi phrasing ensemble |
| `verifiers.py` | `Verifier` (abstract) → `FakeVerifier`, `DaviRunPodVerifier`, `HttpVerifier` → `SystemOneVerifier` (`DaviVerifier`, `JevVerifier`), `ClaudeVerifier` |
| `policy.py` | `RoutePolicy` (accept / escalate) and `TierPolicy` (cheap / mid / strong) |
| `hooks.py` | `Settings`, `State`, `RouteHook` (abstract) → `AgentPreHook`, `AgentPostHook`, `StopHook`, and `main` |
| `grading.py` | `Grader` (abstract) → exact, contains, regex, number, command, LLM graders |
| `harness.py` | Tasks, arms, `RunCache`, `claude -p` runs, judging, regrading |
| `analysis.py` | `ReplayRow`, baselines, sweeps, frozen thresholds, demo tables |
| `cli.py`, `probe.py` | The `davi-route` command and the hook mechanism probes |

```bash
ruff check && ruff format --check
pytest tests/test_routing.py
```

## Legacy: the `davi` search-result filter

This repository started as `judge-adapter`: a pairwise LLM-judge adapter and a Claude Code plugin (`davi`) that
filters Tavily search results with a choice model before the agent reads them. It is unchanged and still installs
as before (`claude plugin install davi@datumo`). See [docs/plugin.md](docs/plugin.md),
[docs/reference.md](docs/reference.md) and [docs/verification.md](docs/verification.md).

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
