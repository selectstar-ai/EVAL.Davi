# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The ``davi-decision-mcp`` server: one ``decide`` tool over stdio.

    files.py    StateFiles: read ``state_files`` inside the allowed roots
    tool.py     Decider: validate the tool arguments, ask the decision core
    server.py   the MCP glue (needs the ``mcp`` extra, Python 3.10+)
    cli.py      the command; fails with a clear message without the SDK

Only ``server.py`` imports the MCP SDK, so the rest runs on the base
install (Python 3.9, no SDK).
"""
