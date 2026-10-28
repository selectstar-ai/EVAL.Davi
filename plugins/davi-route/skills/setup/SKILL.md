---
name: setup
description: Configure the davi-route plugin. Use when the user gives a Davi server URL to use for routing or davi-read, or asks to turn davi-route routing on or off, change its mode or judge, set one of its options, or show its current settings.
argument-hint: <http://davi-host:port> [--mode tiers|post|pre|both] [--judge davi|jev|claude] [--set KEY=VALUE] [--off] [--show]
allowed-tools: Bash(python3:*)
---

# davi-route setup

Run the plugin's configuration script once with the Bash tool. Run no other command (no `ls`, `cat` or checks of
your own): the script saves the options, checks the Davi server and prints the result.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/configure.py" <arguments>
```

Build `<arguments>` from what the user asked. The text after the command, if any, is: `$ARGUMENTS`

- A Davi server URL: pass it first, as `http://host:port`. Add `http://` if the user left it out.
- Routing mode: `--mode tiers`, `--mode post`, `--mode pre` or `--mode both`.
- Judge: `--judge davi`, `--judge jev` or `--judge claude`.
- Another option: `--set key=value`, for example `--set log_file=/tmp/route.jsonl`.
- Turn routing off: `--off`. Show the current settings: `--show`.
- Only davi-read, no routing: the URL, then `--off` (the URL is kept, routing stays off).
- Internal hosts davi-read may read: `--set read_allow_hosts=host1,host2`.

If the user asked to set up davi-route but gave no URL, ask for the Davi server URL before you run anything.

If `${CLAUDE_PLUGIN_ROOT}` was not replaced by a real path in the command above, find the script first:

```bash
find ~/.claude/plugins -path '*davi-route*/hooks/configure.py' 2>/dev/null | sort | tail -n 1
```

Then run that path instead. Do not edit any file yourself: the script writes the options file and prints where.

Afterwards, tell the user in two or three short sentences:

- where the options were saved (the path the script printed);
- whether the Davi server check passed;
- that routing applies from the next subagent call, in Claude Code (the CLI or the desktop app's Code tab), not in a
  regular chat; davi-read uses the same server URL from its next call.

Do not repeat the JSON. If the script prints an error or usage text, explain it and give the corrected request.
