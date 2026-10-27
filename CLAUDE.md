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
- `plugins/davi-route/hooks/route_hook.py` uses the standard library only until it imports the hook module.
- An installed plugin is self-contained: its code comes from `plugins/davi-route/lib`, and the SessionStart `setup`
  installs only `httpx` (into `${CLAUDE_PLUGIN_DATA}/pydeps`), only when a feature is on and `auto_install_deps` is not
  off.
- Option precedence: `DAVI_ROUTE_<KEY>` variables, then `~/.claude/davi-route.json` (written by `/davi-route:setup`,
  read by the launcher on every call), then userConfig. The claude.ai plugin page has no option fields, so the setup
  skill is the supported way to configure an installed plugin. Keep it a skill (`skills/setup`), not a `commands/` file
  with a `!` line: claude.ai-synced copies do not run `!` lines.
- `plugin.json` userConfig keys must exist on `Settings` with the same defaults (checked by a test).
- Run `davi-route generate` without `ANTHROPIC_API_KEY` in the environment: `claude -p` would bill that key instead
  of the logged-in plan.
