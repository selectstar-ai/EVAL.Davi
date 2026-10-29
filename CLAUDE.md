# Coding rules

These rules apply to the routing code and the code built on the same rules: `src/judge_adapter/routing/`,
`src/judge_adapter/decision/`, `src/judge_adapter/reading/`, `plugins/davi-route/hooks/`, `tests/test_routing.py`,
`tests/test_decision.py` and `tests/test_reading.py`. The older modules (`judge_adapter` core, the `davi` filter plugin) are frozen in their own
style; do not reformat them.

Check before every commit:

```bash
ruff check && ruff format --check      # config in pyproject.toml ([tool.ruff])
PYTHONPATH=src davi-route bundle       # refresh plugins/davi-route/lib after src changes (generated, never edited)
PYTHONPATH=src pytest -q
```

## Branches

- `main` is the stable branch: it is what `davi-route@datumo` installs. Nothing is pushed or merged into it directly.
- `dev` collects finished features. Every change starts as a branch from `dev`, named for the work and never for an AI
  tool (no `claude` or `codex`), and goes into `dev` through a PR.
- On a release date, `dev` goes into `main` with one PR (`dev` → `main`).
- To try `dev`: `claude --plugin-dir plugins/davi-route` from a checkout, or `/plugin marketplace add
  selectstar-ai/EVAL.Davi#dev` (only one marketplace per name: removing `datumo` first uninstalls its plugins).

## PEP 8

- 4-space indentation, no tabs. At most **79 characters** per line.
- No semicolons; one statement per line.
- `snake_case` for functions, methods, variables and modules; `PascalCase` for classes; `UPPER_CASE` for
  module-level constants.
- `_name` for internal attributes and helpers; `__name` only when a subclass must not clash with it.
- Pythonic tests: `if items:` / `if not value:` / `if x is None:`, never `== True`, `== False` or `len(x) == 0`.
- Imports at the top: standard library, third party, then local (`ruff` sorts them). No wildcard imports, no
  one-letter module aliases (`import harness`, not `import harness as Hn`).
- Single quotes (`ruff format` keeps them). Break long strings with implicit concatenation, not backslashes.

## Object-oriented design

- **Encapsulation**: keep state on `self._name`; expose read-only values with `@property`. A caller never reaches into
  another object's `_` attributes (tests included).
- **Inheritance**: shared behaviour goes into a common parent class; subclasses call `super().__init__(...)`.
  Examples: `Verifier` → `HttpVerifier` → `SystemOneVerifier` → `DaviVerifier` / `JevVerifier`; `RouteHook` →
  `AgentToolHook` → `AgentPreHook` / `AgentPostHook`.
- **Polymorphism**: one interface, many implementations. Callers use `verify(...)`, `handle(payload)`,
  `check(task, result)` and never branch on the concrete class.
- **Abstraction**: a family's interface is an `abc.ABC` with `@abstractmethod`s (`Verifier._verify`,
  `HttpVerifier._headers`, `RouteHook.handle`, `Grader.check`, `ReferenceGrader._compare`). Template methods in the
  base class hold the shared rules (`Verifier.verify` never raises; it turns errors into a `Verdict`).
- Plain data stays plain: use `@dataclass` (frozen where possible) for records such as `Verdict`, `Route`,
  `Settings`, `Task`, `ReplayRow`. Do not wrap simple functions in classes for their own sake.

## Behaviour that must not change silently

- Hooks are off by default and **fail-open**: a hook error or a slow judge leaves the call unchanged, and a failed
  judgment follows `on_error` (default: the cheap model, never a block). A hook never raises into Claude Code.
- No task, answer or key text in logs; keys are read lazily and scrubbed from error details.
- `plugins/davi-route/hooks/route_hook.py` and `hooks/davi_read.py` use the standard library only until they import
  the hook module or the `davi-read` command.
- An installed plugin is self-contained: its code comes from `plugins/davi-route/lib`, and only `httpx` is installed
  (into `${CLAUDE_PLUGIN_DATA}/pydeps`), never when `auto_install_deps` is off: by the SessionStart `setup` when a hook
  feature is on, or by the first `davi-read` call. Commands run through the Bash tool get no `CLAUDE_PLUGIN_DATA`, so
  `davi_read.py` derives it (`<plugins>/data/<plugin>-<marketplace>`).
- No top-level `bin/` in the plugin: claude.ai does not install a plugin that has one. The `davi-read` skill runs
  `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/davi_read.py"`.
- Settings live in two places. The settings dialog (`plugin.json` userConfig) holds the connection only: the Davi URL
  and the keys (`DIALOG_KEYS` in `route_hook.py`, checked by a test). `/davi-route:setup` turns the features on and
  off (`on`, `off`, `stop on|off`, `--mode`, `--judge`) and sets every other option with `--set`. Do not add feature
  switches or tuning values to the dialog.
- Option precedence: `DAVI_ROUTE_<KEY>` variables, then the settings dialog, then `~/.claude/davi-route.json`
  (written by `/davi-route:setup`, read by the launcher on every call). Claude Code keeps exporting dialog values an
  older plugin version saved; the launchers drop every `CLAUDE_PLUGIN_OPTION_<KEY>` outside `DIALOG_KEYS`.
- Commands run through the Bash tool get no `CLAUDE_PLUGIN_OPTION_<KEY>`, so both skills pass the dialog's URL as
  `--dialog-url "${user_config.davi_base_url}"`; an unset option arrives as that placeholder and counts as unset.
  Dialog keys (sensitive) cannot be passed this way.
- The claude.ai plugin page has no settings dialog, so `/davi-route:setup` also takes the Davi URL. Keep it a skill
  (`skills/setup`), not a `commands/` file with a `!` line: claude.ai-synced copies do not run `!` lines.
- Run `davi-route generate` without `ANTHROPIC_API_KEY` in the environment: `claude -p` would bill that key instead
  of the logged-in plan.
