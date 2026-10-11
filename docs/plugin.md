# Claude Code plugin `davi`

[README](../README.md) · [Reference](reference.md) · [Verification status](verification.md) · [한국어](ko.md)

The repository is also a Claude Code plugin marketplace. Its plugin `davi` adds a **PostToolUse hook on the Tavily
MCP search tool** (`tavily_search`). It runs each search result through `ContextFilter` (preset `v1-thr0.3-dev`) and
replaces the tool result with only the kept results before the model reads it.

**Requires a Jev-compatible choice endpoint** (for example a self-hosted model on RunPod serverless; see
[Choice endpoint contract](reference.md#choice-endpoint-contract)). Without an endpoint ID, key and per-second rate the hook sends
nothing and changes nothing. **Requires the Tavily MCP server** (`tavily-mcp` 0.2.22), whose results carry
per-result text.

**Built-in WebSearch is not filtered.** In one observed run, Claude Code's built-in WebSearch returned only a title and
a URL per result (`results: [{tool_use_id, content: [{title, url}, ...]}, "<model-written summary>", ...]`), with no
per-result text to score. The result filter therefore does not match WebSearch. (The hook still parses that shape and
passes it through unchanged if you route WebSearch to it yourself.) The only hook that matches WebSearch is the
PreToolUse launcher of the [optional query-stage options](#optional-query-stage-options-experimental); with both options
off (the default) it exits before reading its input (about 0.4 ms over a bare Python start, see that section). A
UserPromptSubmit hook serves only the search gate; with the gate off it exits the same way on every prompt. Inside
Claude Code, `/davi:status`, `/davi:filter-on`, `/davi:filter-off`, `/davi:gate-on`, `/davi:gate-off`,
`/davi:domain-on` and `/davi:domain-off` show and switch the features ([Turning features on and off](#turning-features-on-and-off)).

## Supported search tool

Only one tool is supported and matched: **Tavily `tavily_search`** from the npm package `tavily-mcp` **0.2.22**. The
format was checked in the published 0.2.22 build and in the source at commit
[`1c1d54c`](https://github.com/tavily-ai/tavily-mcp/tree/1c1d54c2a619afe52544f775c8d82a56ac6d8bb5)
([format-results.ts](https://github.com/tavily-ai/tavily-mcp/blob/1c1d54c2a619afe52544f775c8d82a56ac6d8bb5/src/format-results.ts),
[index.ts](https://github.com/tavily-ai/tavily-mcp/blob/1c1d54c2a619afe52544f775c8d82a56ac6d8bb5/src/index.ts)) on
2026-10-11. In Claude Code the tool is `mcp__<server alias>__tavily_search`; the plugin's matcher is
`^mcp__[A-Za-z0-9_-]+__tavily_search$` (0.2.22 registers only `tavily_search`, so the hyphenated spelling in older
docs is not matched).

- Format: one text block with optional `Request ID:` / `Answer:` lines, `Detailed Results:`, then per result `Title:`,
  optional `ID:`, `URL:`, `Content:`, optional `Raw Content:` / `Favicon:`, then an optional `Images:` section.
- Only the `Content` snippet is scored (not `Raw Content`). The header, the `Answer:` summary and the image list are
  kept byte for byte and never scored; no text is generated or summarised.
- **Zero intervention** by the result filter for every other tool: other Tavily tools (extract, crawl, map, research),
  other search MCP servers, read / write tools and the built-in WebSearch. Tests check this.
- A response with `isError: true` or with `structuredContent` is passed through unchanged (the hook could not keep
  `structuredContent` consistent with an edited text block). Other content blocks (text, image, resource) are kept.
- Not checked: Tavily's hosted remote server (`https://mcp.tavily.com/mcp/`) and other `tavily-mcp` versions.
- The MCP tool output shape that Claude Code passes to hooks is not documented. The hook accepts a list of content
  blocks, an object with a `content` list (other keys kept) or a plain string, and writes back the same shape.

## Install

```bash
claude plugin marketplace add selectstar-ai/EVAL.Davi
claude plugin install davi@datumo
```

The install id is `<plugin>@<marketplace>`: plugin `davi`, marketplace `datumo`. Inside a session,
`/plugin marketplace add selectstar-ai/EVAL.Davi` and `/plugin install davi@datumo` do the same.

The marketplace was renamed from `judge-adapter` to `datumo` (no model or selection-policy change; the install
identifier and the default state location change). A `davi@datumo` install has its own data directory
(`~/.claude/plugins/data/davi-datumo/`) and does not inherit the settings, toggles or state of an earlier
`davi@judge-adapter` install; the plugin never moves, deletes or modifies the old install or its files. If both are
active, the hooks may run twice: check `/plugin` and keep only one.

Add the Tavily MCP server as a local stdio server, **pinned to the verified version** (never an unpinned `npx`
latest). The key is your own Tavily API key from tavily.com; `--env` stores it in your Claude Code MCP configuration,
so prefer a shell variable over typing it:

```bash
claude mcp add --transport stdio --env TAVILY_API_KEY="$TAVILY_API_KEY" tavily -- npx -y tavily-mcp@0.2.22
```

The tool then appears as `mcp__tavily__tavily_search`. Tavily bills its own searches (credits); check its terms and
pricing. Without `TAVILY_API_KEY`, tavily-mcp 0.2.22 starts in a keyless mode in which search is still available.

The hook runs `python3` (exec form, no shell) and needs **httpx** in that interpreter. Pick one:

```bash
# A) into the python3 on your PATH
python3 -m pip install "httpx>=0.24,<1"

# B) a private venv in the plugin's data directory (used automatically when python3 lacks httpx).
#    The directory is ~/.claude/plugins/data/<id>/ with <id> = "davi@datumo" with every character other than
#    a letter, digit, _ or - replaced by "-", i.e. ~/.claude/plugins/data/davi-datumo/
D=~/.claude/plugins/data/davi-datumo
python3 -m venv "$D/venv" && "$D/venv/bin/pip" install "httpx>=0.24,<1"
#    or with uv:  uv venv "$D/venv" && uv pip install --python "$D/venv/bin/python" "httpx>=0.24,<1"
```

B is the most robust choice: it does not depend on which `python3` comes first on PATH and does not touch your system
Python. The launcher (`hooks/context_filter_hook.py`) uses only the standard library, puts the plugin's bundled `src`
first on `sys.path`, re-runs itself with `${CLAUDE_PLUGIN_DATA}/venv` when `python3` cannot import httpx, and
otherwise logs `dependency_missing` and leaves the result unchanged. It never installs anything. On Windows, `python3`
must resolve to a real executable.

## Configure

Claude Code prompts for the plugin's options when it is enabled; later, use `/plugin` → the plugin → **Configure
options**, or `claude plugin configure davi@datumo`. The key and endpoint ID are `sensitive`: Claude Code keeps
them in secure storage, not in `settings.json`. Prefer the dialog for them, so they do not land in shell history.

| Option (`userConfig`) | Default | Meaning |
|---|---|---|
| `runpod_api_key` (sensitive) | none | RunPod API key. Missing → nothing is sent |
| `choice_endpoint_id` (sensitive) | none | RunPod endpoint ID of your choice worker. Missing → nothing is sent |
| `usd_per_second` | none | Your endpoint's per-second price, for the client-side estimate and cap. Missing → nothing is sent |
| `enabled` | `true` | Global switch: `false` turns the filter and both query-stage options off without uninstalling; no toggle overrides it |
| `preset` | `v1-thr0.3-dev` | Named prompt + threshold; currently the only preset (threshold 0.3) |
| `concurrency` | `1` | Filter calls in flight per search (1-8) |
| `max_docs` | `10` | Results scored per search (1-50); later results pass through unscored |
| `wall_budget_seconds` | `20` | Time budget per search (1-60); after it the original result is used |
| `on_all_dropped` | `keep_original` | `keep_original` or `drop_all` (see below) |
| `mcp_tools` | empty | Overrides: comma-separated `name` or `name=tavily\|off` |
| `log_file` | empty | Also append the per-search log line to this file |
| `search_gate_enabled` | `false` | Experimental search gate before WebSearch / `tavily_search` ([details](#optional-query-stage-options-experimental)) |
| `search_gate_threshold` | `0.85` | Deny only if P(not needed) ≥ this (0.5-1); pre-fixed, not calibrated |
| `domain_presets_enabled` | `false` | Experimental domain presets before WebSearch / `tavily_search` |
| `domain_presets` | empty | Your presets as JSON or a file path; empty = the built-in EXAMPLE presets |
| `domain_min_confidence` | `0.6` | Apply the chosen preset only if its probability ≥ this (0-1); pre-fixed, not calibrated |
| `domain_apply_mode` | `ask` | `ask` (documented; you confirm the modified search; in `-p` runs it can block the search) or `input_only` (experimental, undocumented, effect unverified). No allow mode |

The toggle file ([Turning features on and off](#turning-features-on-and-off)) takes precedence over
`search_gate_enabled`, `domain_presets_enabled` and `domain_apply_mode`, and can switch the filter off, but never over
`enabled=false`. Keys, the endpoint ID and the rate are set here only.

`mcp_tools`: `mcp__tavily__tavily_search=off` excludes the default tool; `mcp__corp__web_lookup` (or `=tavily`) treats
another tool name as Tavily output. Claude Code starts the hook only for tools the
plugin's matcher names, so an extra tool name also needs a hook that reaches it, for example in your own
`settings.json` with `"command": "python3", "args": ["/path/to/judge-adapter/hooks/context_filter_hook.py"]` and the
options as `JUDGE_ADAPTER_HOOK_<KEY>` environment variables.

How the values reach the hook: Claude Code exports every option to hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>`
(e.g. `CLAUDE_PLUGIN_OPTION_RUNPOD_API_KEY`). The hook reads those first and falls back to the package's own variables
`RUNPOD_API_KEY` and `JUDGE_ADAPTER_CHOICE_ENDPOINT_ID`, and to `JUDGE_ADAPTER_HOOK_<KEY>` for the other options, so
the same hook also works from a plain `settings.json` hook or a `--plugin-dir` session. The key is read only at the
first request and is never logged or passed on a command line.

## What the hook changes

```
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

- **Question:** the search query from `tool_input.query`.
- **Some dropped:** `updatedToolOutput` is the original result in the **same shape and format** with the dropped
  results removed (the result blocks are cut out of the text; nothing else in the text changes), plus `additionalContext: "context filter (v1-thr0.3-dev) kept k/n results"`.
- **None dropped:** no output; the result is unchanged.
- **All dropped:** by default (`keep_original`) the result is **left unchanged** and the hook adds
  `additionalContext: "context filter (v1-thr0.3-dev) kept 0/n results for this search; the original results are shown
  unchanged."` The filter can drop every document of a search (25 of 72 searches in the HLE run in the [README results](../README.md#results)); silently
  hiding every result would leave the model with nothing and no explanation. `drop_all` removes them all and says
  `kept 0/n results`.
- **Results without a snippet** are passed through unscored; if no result has one, nothing is done.
- **Limitation, the `Answer:` line:** when the Tavily response includes an `Answer:` summary, it is kept unchanged, so
  information from dropped results can still reach the model through it.
- **Hook field:** `updatedToolOutput`. The hooks reference lists `updatedMCPToolOutput` as "Replaces the output for MCP
  tools only. Prefer `updatedToolOutput`, which works for all tools", and warns that "MCP tool output is passed through
  without schema validation", which is why the hook rebuilds the exact input shape.

## Optional: route web searches to Tavily

The filter only sees Tavily results. If you want web searches in a session to go through Tavily instead of the
built-in WebSearch, you can deny the built-in tool yourself, e.g. in your own `settings.json`:

```json
{ "permissions": { "deny": ["WebSearch"] } }
```

What this does and does not do:

- It blocks only the built-in `WebSearch` tool. The hook then applies to **supported Tavily search responses in a setup
  where web search is limited to Tavily**.
- It does **not** route everything through the filter: `WebFetch`, `Bash` (e.g. `curl`), other MCP tools,
  unsupported or error responses, and every fail-open case below still reach the model unfiltered.
- The plugin never changes your permissions or settings; this is an opt-in step you take.

## Optional: query-stage options (experimental)

Two options run **before** a search, in a PreToolUse hook on exactly two tools: the built-in `WebSearch` and Tavily
`tavily_search` (same matcher as the filter). Both are **off by default**, untuned and unvalidated: their prompts
(`gate-v0-dev`, `domain-v0-dev`, in `src/judge_adapter/query_presets.py`) and thresholds are first drafts with no
quality claim, and a high probability from your model is not treated as a correct judgment. Each option adds **one
endpoint call per search** while enabled (two with both), and the search waits for it, up to the wall budget.

**What is sent to your choice endpoint:** the search query only (`tool_input.query`, at most 2,000 bytes; longer
queries get no decision). For domain presets, also your preset names, descriptions and domain lists. Nothing else: the
PreToolUse input has no user-message field, and the hook never reads the transcript file. The UserPromptSubmit hook
(search gate only) reads the user's prompt locally to look for an explicit search request; the prompt is never sent,
stored or logged. The endpoint is your own; its logs and retention are yours to check.

**Search gate** (`search_gate_enabled`): a default-off **development option**. Its real usefulness, and whether it
preserves what the user wants, are **unverified**; its prompt and threshold are not tuned, and it never reads the
transcript. It asks "is this search needed?" with the options needed / not needed /
cannot tell. It denies the search only if P(not needed) ≥ `search_gate_threshold` (default 0.85). Claude then sees a
fixed notice (the model's reasoning is never shown or presented as fact):

> davi search gate (experimental, optional): this search was skipped because an automated check rated it as probably
> unnecessary. This is not a verified judgment. If the user asked for a search, or the answer needs current
> information, a specific source or verification, retry the same query and it will run. Otherwise answer from the
> context you already have.

- **Explicit user request, no call (UserPromptSubmit).** A real smoke on 2026-10-11 showed a **semantic failure**
  that blocks release: with the gate on, the prompt "Using web search, confirm how many days there are in a week.
  Answer in one sentence." led Claude to WebSearch "how many days in a week"; the gate, seeing the query only, rated
  P(not needed) = 0.999 and **denied a search the user had explicitly asked for**. The PreToolUse input has no
  user-message field, so the plugin now also has a UserPromptSubmit hook. Its input carries the prompt (hooks
  reference: "`prompt` | The user's prompt text", plus the common `session_id` and `prompt_id`). While the gate is
  on, the hook checks each prompt locally against a fixed, case-insensitive list of explicit-request phrasings in
  English and Korean (`EXPLICIT_SEARCH_PATTERNS` in `src/judge_adapter/query_presets.py`: any form of "search" incl.
  "web search", "search the web", "search for", "use (the) search"; "look (it) up"; "google"; "online", "internet",
  "browse"; "cite", "source", "reference", "link"; "verify", "confirm", "fact-check"; "latest", "current", "today",
  "now", "news", a year; 웹 검색, 검색(해/해서/해줘), 서치, 구글, 인터넷, 찾아봐, 찾아줘, 출처, 근거, 링크, 확인, 검증, 최신,
  최근, 오늘, 현재, 지금, 뉴스). It is deliberately broad: a false positive only keeps a search. It writes one small
  state file per session (`${CLAUDE_PLUGIN_DATA}/prompt-state/<sha256(session id)>.json`, 0600, atomic replace)
  holding a time, an explicit flag and hash prefixes only; no prompt text. It never writes to stdout (plain stdout
  would be added to Claude's context), never blocks a prompt and makes no network call.
  - The gate reads that state first. **Explicit request → status `explicit_request_keep`: no endpoint call, no
    output** (the normal permission flow; never an explicit allow). The gate asks the endpoint only when a fresh
    state (under 30 minutes) says "no explicit request" for this session. **No state** (e.g. the gate was just
    switched on), an unreadable state, a state of another session, an expired state, a state written before the last
    toggle change, or a `prompt_id` mismatch → the search is **kept, with no call** (`keep_*` statuses).
  - Update rule per prompt (conservative): a signal sets the flag. While a fresh flag exists, a prompt **without** a
    signal leaves it in place until it expires (30 minutes from the explicit prompt, not refreshed), whatever its
    length: "continue", "yes, go ahead", "계속", "그걸로", "do the same for hours in a day" all keep it. Only a prompt
    that **clearly starts a new task** clears it: one that begins with a fixed marker (`NEW_TASK_PREFIXES`: "new
    task", "new question", "new topic", "different question/topic", "unrelated question", "change of topic", 새 작업,
    새 질문, 다른 질문, 다른 주제, 주제를 바꿔 …). Ambiguous → keep. Without a fresh flag, a prompt without a signal
    writes "no explicit request". Each prompt's read-decide-write runs under a per-session file lock, so a concurrent
    prompt cannot clear a flag it did not see; a lock or write failure removes the state (a signal is still written
    if possible), so the gate keeps.
  - Granularity: when both the state and the PreToolUse input carry `prompt_id`, they must match; otherwise the state
    is matched by session and age only. That is a time window, **not an exact turn**.
  - **What this fixes, and what it does not.** *Known A1 fix:* the observed failure class (prompts that say "using
    web search", "search for", "look up", "검색해서", "출처" and the other listed phrasings) is protected: such a
    prompt keeps every search of its turn (and of its follow-ups, until the TTL) with no gate call. *General intent
    preservation:* **not solved.** A phrasing that is not in the list can still be denied; a keyword miss is not
    evidence that the user did not want a search; adding the phrase of a known failure fixes that phrase class, not
    the problem in general. This is **not a basis for turning the gate on by default**. **This fix has fake tests
    only**; it has not been re-run in a real session. The gate stays **default-off and experimental**.
- **Keyword guard, no call:** queries containing recency words (years, "latest", "today", "news", "version", "price",
  최신 / 오늘 / 현재 …), verification words ("verify", "source", "official", 출처 / 확인 …) or a specific source
  (`site:`, a URL or domain); Tavily `time_range` / `start_date` / `end_date`; and any domain field already set. It
  is a keyword heuristic on the query, not exhaustive, and **does not preserve user intent**.
- **Input limit:** the gate's model sees only the query, and **the query alone cannot identify an explicit user
  request to search**. Apart from the explicit-request list above, the gate can deny a search the user asked for in
  other words, or one that needed fresh information or verification. Allowing the same query on retry is the other
  mitigation, and it relies on Claude retrying.
- **Retry (loop prevention):** a denied query is recorded as a SHA-256 of (session id, tool name, normalized query) with
  its time, one file per record under `${CLAUDE_PLUGIN_DATA}/search-gate-state/` (or the system temp directory). No
  query text is stored. The final check and the write run as one short inter-process critical section under an
  `fcntl.flock` lock on a per-key lock file, taken only after the endpoint has answered (never during a call), so
  concurrent hook processes deny the same key at most once. Pruning of expired records takes the same lock and
  re-checks inside it, so it never removes a fresh record. The same query in the same session within 10 minutes gets
  **no output at all** (normal permission flow). Another session or another tool is not affected. No session id, a lock
  or I/O problem, a lock wait over 1 s, or no `fcntl` (Windows): the gate never denies.
- `gate_use_last_user_message` (proposed earlier) is **not provided**: the PreToolUse input has no user-message field,
  reading the transcript file is out of scope, and the prompt is never sent to the endpoint (only the local
  explicit-request check above uses it).

**Domain presets** (`domain_presets_enabled`): picks ONE preset from your fixed list for the query; `none` (no
restriction) is always available. Only the preset's own fixed list is applied; nothing the model writes is executed.

- Format: `{"name": {"description": "...", "include_domains": ["a.example"]}}` (or `exclude_domains`), as JSON in the
  option or in a file whose path you give. Exactly one list per preset (the WebSearch API rejects `allowed_domains`
  and `blocked_domains` together), bare lower-case domains only, at most 8 presets × 20 domains. Without your own list
  the built-in **EXAMPLE** presets `docs` (docs.python.org, developer.mozilla.org, learn.microsoft.com,
  docs.github.com) and `academic` (arxiv.org, aclanthology.org, openreview.net, pubmed.ncbi.nlm.nih.gov) are used;
  they are examples, not the right domains for every query.
- Mapping: include → `allowed_domains` (WebSearch) / `include_domains` (Tavily); exclude → `blocked_domains` /
  `exclude_domains`. `updatedInput` is the **full original input** with only that one field added; the query and
  every other field are unchanged.
- No change: when the input already has either of the tool's domain fields (even an empty list), when `none` wins, when
  the top probability is below `domain_min_confidence` (default 0.6), and on any failure.
- How it is applied: the hooks reference documents `updatedInput` together with `"allow"` or `"ask"`. The hook never
  returns `"allow"` (it would skip your own permission prompt) and has no allow mode. `domain_apply_mode=ask`
  (default) returns `"ask"` with the modified input, so you confirm each restricted search. **In headless (`-p`) runs
  `ask` can block the search**: with no one to answer the prompt, Claude Code denies that call. Keep domain presets off
  for unattended runs. `input_only` is an **experimental** mode that returns `updatedInput` without a decision; the
  reference does not document that combination, so its real effect is **unverified**. The hook never switches to it
  on its own. tavily-mcp's own `DEFAULT_PARAMETERS` setting overrides
  the domain lists.

**Order and limits:** the gate runs first; if it denies, the domain decision is skipped. Both share the hook's wall
budget (`wall_budget_seconds`), at most one submission per option, and one client-side estimate cap per hook call
(`usd_per_second × (calls × 661 s + 60 s + wall budget)`), like the filter. `enabled=false` turns both off.

**Fail-open:** the hook prints nothing (normal permission flow) on: both options off, `enabled=false`, another tool, no
query, an explicit user request or no fresh prompt state (gate), missing key / endpoint / rate, an invalid option, an HTTP error, timeout, unusable or tied answer, any
exception, httpx missing, or the wall budget. It never returns `"allow"`, so it cannot loosen your permission rules;
the documented precedence `deny > defer > ask > allow` keeps your own deny rules in force.

**Start-up when off:** with both options off, `hooks/query_options_hook.py` exits before reading stdin or importing
the package (no key read, no network, no state, no output); with the gate off, `hooks/user_prompt_hook.py` does the
same on every user prompt (the prompt is not read). To decide, both read only the option environment variables and the
small toggle file (an explicit exception: a stat / read of `toggles.json`, parsed without importing `json` when it is
in the form the CLI writes). Measured on macOS, `/usr/bin/python3` 3.9.6, 40 runs each, medians: `python3 -c pass`
14.4 ms; query launcher all off 14.9 ms without a toggle file and 14.8 ms with one; user-prompt launcher gate off
14.9 / 14.7 ms; filter launcher toggled off 14.8 ms, i.e. about 0.3-0.5 ms over a bare interpreter start (before this
change the query launcher measured 14.2 ms in the same run). The interpreter start itself (~14 ms) is paid on every
WebSearch / `tavily_search` call and, since this change, on every user prompt while the plugin is installed. With the
gate on, the user-prompt hook took 30.7 ms (pattern check and one state write).

**Logs:** one JSON line per call, tag `davi-query-options-hook`: status, tool name, query byte count, and per option
status, calls, seconds, server execution time, rounded probabilities (gate) or the chosen preset name and top
probability (domain). Never the query, URLs, domain lists, preset descriptions, the session id, keys or the endpoint ID.
The UserPromptSubmit hook logs one line, tag `davi-user-prompt-hook`: status (`explicit_set`, `follow_up_kept`,
`cleared`, `write_failed`, `lock_failed`, ...), whether a signal matched, the prompt's byte count and `data_dir_id`
(also in the query-options log). Never the prompt, the session id or the prompt id.

## Turning features on and off

The three features can be switched without the configuration dialog, from inside Claude Code or from a shell. They
are **independent**: the filter toggle affects only the PostToolUse filter, the gate toggle only the search gate (and
its UserPromptSubmit hook), the domain toggle only the domain presets.

| Command (in a session) | Same from a shell | Effect |
|---|---|---|
| `/davi:status` | `python3 <plugin>/hooks/davi_toggle.py status` | Effective settings and where each comes from; warnings for states that cannot run |
| `/davi:filter-on`, `/davi:filter-off` | `... davi_toggle.py filter on\|off` | Result filter (PostToolUse, Tavily) |
| `/davi:gate-on`, `/davi:gate-off` | `... davi_toggle.py gate on\|off` | Search gate (experimental) |
| `/davi:domain-on`, `/davi:domain-off` | `... davi_toggle.py domain on\|off` | Domain presets (experimental) |
| `/davi:domain-mode-ask`, `/davi:domain-mode-input-only` | `... davi_toggle.py domain-mode ask\|input_only` | How a preset is applied |
| (none) | `... davi_toggle.py reset` | Remove the toggle file; the userConfig values apply again |

**Where and how wide:** one file, `${CLAUDE_PLUGIN_DATA}/toggles.json` (0600). For the marketplace install that is
`~/.claude/plugins/data/davi-datumo/toggles.json`; the shell CLI uses `--data-dir DIR`, else
`CLAUDE_PLUGIN_DATA`, else that path. A `--plugin-dir` session has another plugin id and data directory, so pass
`--data-dir` there (the slash commands always use the session's own `${CLAUDE_PLUGIN_DATA}`). The toggles are
**install-wide, not per session**: a change applies to every session and project using that data directory,
**including sessions that are already running**, from their next hook call.

**Precedence (every hook and `status`):** (1) userConfig `enabled=false` turns all three off, and no toggle
overrides it; (2) otherwise the toggle file, for each key it sets; (3) otherwise userConfig / environment: filter on,
gate = `search_gate_enabled`, domain = `domain_presets_enabled`, mode = `domain_apply_mode`. `status` notes each
toggle that overrides an explicit userConfig value and each toggle that has no effect because `enabled=false`. A
missing toggle file means no toggles. A corrupt or unreadable one is ignored by the hooks (userConfig values apply),
reported by `status`, and left untouched by the CLI, which refuses to change it until `reset`. Keys, the endpoint ID
and the rate are never stored in the toggle file.

**Writes:** strict arguments (exact lower-case words; anything else is a usage error, exit 2, nothing written); the
read-modify-write runs under an exclusive `fcntl.flock`, so concurrent changes are all kept; the new file is written to
a 0600 temp file and moved into place atomically, then read back. A failed write or a bad existing file is reported
as `FAILED` (exit 1), never as success. No `fcntl` (Windows): changes are refused.

**What `status` reads and shows:** the toggle file; userConfig values from the environment when present (hook
processes get `CLAUDE_PLUGIN_OPTION_*`, the Bash tool does not), then the non-sensitive values Claude Code saved under
`pluginConfigs["davi@datumo"].options` in `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`;
only that block and only this plugin's non-sensitive option names are used), then the manifest defaults. It shows
filter / gate / domain on/off with their source, the domain mode, the thresholds, the filter preset, the domain
preset names (never the domains or a file path), the rate as set/missing, and the key and endpoint ID as
"set"/"missing" when the environment shows them, else as "not visible here" (they are in secure storage and reach only
hook processes; a hook log status `missing_key` / `missing_endpoint` tells you). Warnings cover states that cannot run:
a missing rate, key or endpoint (when visible), invalid domain presets or thresholds, the built-in EXAMPLE presets in
use, `ask` mode in headless runs, httpx not importable, a corrupt toggle file.

**How the slash commands work (and what they cost):** they are plugin command files (`commands/*.md`, namespaced as
`/davi:<name>`). Each runs `davi-toggle` (the plugin's `bin/` executable, which runs `hooks/davi_toggle.py`) through
Claude Code's documented `` !`command` `` injection, which runs **before** the command text is sent to Claude; the
output replaces the line. Then **Claude reads that output and relays it, so each command uses one model turn**; they
are not built-in commands and are not free. Frontmatter: `disable-model-invocation: true` (only you can run them; they
are not listed in Claude's context) and `allowed-tools: Bash(davi-toggle *)`, the narrowest documented pre-approval:
injected commands need an allow rule or they abort, and this grant also lets Claude run `davi-toggle` during that one
turn. **The commands take no arguments, and no user text is ever placed into shell code:** each command file has
one constant injected line, e.g. `` !`davi-toggle --data-dir "${CLAUDE_PLUGIN_DATA}" gate on` ``, whose only
expansion is the data path Claude Code substitutes (an earlier draft put `$ARGUMENTS` into that line, where the shell
would interpret user text before the script's validation; quoting would not have fixed that, so it was removed).
A local sentinel test renders every command with hostile arguments (`on; touch …`, `$(…)`, backticks, newlines, …)
and runs the injected line; the sentinel file is never created, while the same harness on the old `$ARGUMENTS` form
does create it. Text typed after a command name is not used by the script; Claude Code appends unconsumed arguments
to the command text as `ARGUMENTS: …` for Claude to read, and the docs say `!` substitution "runs once over the
original file" (that this appended text is never scanned for `!` was not checked in a real session). Each command
prints the resolved data directory and a `data dir id` (a hash of its path) that the hooks also log as
`data_dir_id`. `disableSkillShellExecution` in your settings disables the commands. A
plugin with a top-level `bin/` is not installed by claude.ai or Cowork (Claude Code only). Five of the commands
(`status`, `gate-on`, `gate-off`, `domain-on`, `domain-off`) were observed working in headless `claude -p` sessions
loaded with `--plugin-dir`; the others and interactive use were not run (see
[Verification status](verification.md)). The shell CLI works without them.

## Verification status

What was observed in real runs, what is covered by fake tests only, and what is unverified: see
[verification.md](verification.md).

## Fail-open

The hook always exits 0. Any of the following prints nothing, so Claude Code keeps the original result: the filter is
disabled; the tool is not enabled; no query; an unrecognised result format (including server error messages such as
`Tavily API error: ...`), `isError: true`, `structuredContent`; missing key, endpoint ID or rate; an invalid option; an unknown
preset; input the filter refuses; any exception; httpx missing; the wall budget running out (in-flight requests are
abandoned). Inside the filter, a failed call (HTTP error, per-request timeout, unusable answer) keeps that document,
and after an unsettled job the remaining documents are kept unscored. The hook's own `timeout` in `hooks/hooks.json`
is 75 s, above the largest wall budget; Claude Code discards the output of a hook it cancels, which also leaves the
result unchanged. A job abandoned at the wall budget may still run on the server until its own limits
(`executionTimeout` 60 s, `ttl` 600 s).

## Latency and cost

One filter call per scored result (Tavily returns 5 by default → 5 calls), sequential by default; raise
`concurrency` to overlap them. The search result reaches the model only after the hook finishes, so every search waits
for the filter, up to the wall budget. Cold starts on serverless endpoints can dominate. Per-search cap: the hook gives
each search its own client-side estimate cap of `usd_per_second × (scored results × 661 s + 60 s + wall budget)`, the
reservation the filter's ledger needs to admit every scored result; it is an estimate, not a bill. No latency
improvement is claimed.

## Logs

One JSON line per search on stderr (visible in Claude Code's debug log) and optionally in `log_file`: status, tool
name, format, result counts in / scored / out, filter status counts, submissions, text and response bytes in / out,
seconds, and the structural shape of the tool response (key names and types only). Never the query, document text,
titles, URLs, the key or the endpoint ID. The `shape` field shows a tool's result layout safely.

## Smoke test after installing

1. Configure the key, endpoint ID and rate, set `log_file` (e.g. `~/davi-hook.log`), and add a search MCP server.
2. Start a session and ask for something that needs a web search through that server.
3. Read the log line: `status` should be `filtered`, `all_kept` or `all_dropped_kept_original`. `unrecognized_shape`
   or `no_text_in_results` means the layout (`shape`) is not one the hook can filter; the result was left unchanged.
4. For a model-free check of the hook itself, pipe a sample hook input into it:

```bash
printf '%s' '{"hook_event_name":"PostToolUse","tool_name":"mcp__tavily__tavily_search","tool_input":{"query":"q"},
"tool_response":[{"type":"text","text":"Detailed Results:\n\nTitle: t\nURL: https://example.invalid\nContent: s"}]}' \
  | env -u RUNPOD_API_KEY python3 hooks/context_filter_hook.py     # -> stderr status "missing_key", no stdout
```

To try the plugin without installing it: `claude --plugin-dir /path/to/judge-adapter`.
