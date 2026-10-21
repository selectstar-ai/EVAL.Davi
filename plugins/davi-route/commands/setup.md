---
description: Point davi-route at a Davi server and turn routing on (or change, show, turn off its options)
argument-hint: <http://davi-host:port> [--mode tiers|post|pre|both] [--judge davi|jev|claude] [--set KEY=VALUE] [--off] [--show]
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/configure.py" $ARGUMENTS`

The davi-route options were just updated by the command above; its output is shown. Tell the user in two or three
short sentences: where the options were saved, whether the Davi server check passed, and that routing applies from
the next subagent call. Do not run anything else and do not repeat the JSON. If the output shows an error or usage
text, explain it and give the corrected command, for example `/davi-route:setup http://<host>:20100`.
