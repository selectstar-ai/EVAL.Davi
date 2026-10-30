---
name: setup
description: Turn davi-route's features on or off and show its settings. Use when the user asks to turn subagent routing or the completion check on or off, change the routing mode or judge, set an advanced davi-route option, see davi-route's current settings, or gives a Davi server URL for routing or davi-read.
argument-hint: [on|off] [stop on|off] [--mode tiers|post|pre|both] [--judge davi|jev|claude] [http://davi-host:port] [--set KEY=VALUE]
allowed-tools: Bash(python3:*)
---

# davi-route setup

Run the plugin's configuration script once with the Bash tool. Run no other command (no `ls`, `cat` or checks of
your own): the script saves the options, checks the Davi server and prints the result.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/configure.py" --dialog-url "${user_config.davi_base_url}" <arguments>
```

Keep `--dialog-url` and its value exactly as written, even when the value looks like a placeholder: it passes on the
URL from the plugin's settings dialog. Build `<arguments>` from what the user asked. The text after the command, if
any, is: `$ARGUMENTS`

- Show the settings: no arguments.
- Subagent routing: `on` or `off`. `on` uses the tiers mode and the Davi judge unless others were chosen.
- Completion check at Stop: `stop on` or `stop off`.
- Routing mode: `--mode tiers`, `--mode post`, `--mode pre` or `--mode both`.
- Judge: `--judge davi`, `--judge jev` or `--judge claude`.
- A Davi server URL, as `http://host:port` (add `http://` if the user left it out). It is used when the settings
  dialog has none, as on claude.ai.
- An advanced option: `--set key=value`, for example `--set log_file=/tmp/route.jsonl` or
  `--set read_allow_hosts=host1,host2` (internal hosts davi-read may read).

If the user asked to set up davi-route without saying what, run it with no arguments and explain what can be turned on.

If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real path in the command above, find the script first:

```bash
find ~/.claude/plugins -path '*davi-route*/hooks/configure.py' 2>/dev/null | sort | tail -n 1
```

Then run that path instead, without `--dialog-url`. Do not edit any file yourself: the script writes the options file
and prints where.

Afterwards, tell the user in two or three short sentences:

- what is on and off now, and which Davi server is used (the script prints where the URL came from);
- whether the Davi server check passed, if it ran;
- that changes apply from the next subagent call, in Claude Code (the CLI or the desktop app's Code tab), not in a
  regular chat.

If the script says there is no Davi server URL, tell the user to enter it in the plugin's settings (`/plugin` →
Installed → davi-route → Configure options), or to give it to this command where there are no plugin settings
(claude.ai). Keys (Davi, TypeSafe, Anthropic) also go in the plugin's settings: never ask for them in chat.

Do not repeat the script's output line by line. If it prints an error or usage text, explain it and give the corrected
request.
