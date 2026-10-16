# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""A local web dashboard that shows davi-route routing as it happens.

One question runs in two headless Claude Code sessions at once:

- **routed**   the davi-route plugin rates the subagent task and picks
               haiku, sonnet or opus (``route_mode=tiers``, Davi judge);
- **opus**     the same setup with every subagent pinned to opus.

Next to them, Davi, Jev and Claude rate the same question, to show where
each judge would have sent it. Every live run is recorded and can be
replayed later with the same screen, without network or cost.

- ``events``    an append-only event channel per run (read by the browser
                through server-sent events).
- ``tasks``     demo questions and the two session setups.
- ``sessions``  one ``claude -p`` session plus its hook log.
- ``runs``      ``DemoRun`` (abstract) -> ``LiveRun`` / ``ReplayRun``.
- ``store``     recordings on disk.
- ``server``    the HTTP server and the ``davi-route dashboard`` command.
"""
