# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The MCP glue: an ``MCPServer`` with the one ``decide`` tool.

Needs the ``mcp`` extra (``mcp>=2.3,<3``, Python 3.10+). The SDK checks
the arguments against the input schema; its error text quotes the
rejected values (``input_value=...``), so ``DecideTool`` replaces it with
the field names and pydantic error types only. Every failure, from the
SDK or the decision core, comes back as an ``is_error`` result with a
``code`` and a ``message``; a failure is never turned into a normal
false or low answer. The judge call is synchronous and runs in a worker
thread (``anyio.to_thread.run_sync``). Logs go to stderr and name the
question type, outcome and latency only.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, Dict, List, Literal, Optional, Union

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, Field, ValidationError

from ..decision import DecisionError
from .tool import ARGUMENTS, MAX_CHOICE_OPTIONS, check_arguments

SERVER_NAME = 'davi-decision'
TOOL_NAME = 'decide'
LOGGER = logging.getLogger('judge_adapter.decision_mcp')
QUIET_LOGGERS = ('httpx', 'httpcore')

DESCRIPTION = (
    'Ask the configured judge model ({provider}) one question about some '
    'material and get its decision with probabilities. type "choice" picks '
    'one of 2-{max_options} options in criteria ({{option_id: '
    'description}}); type "noul" answers a true/false statement and takes '
    'criteria {{"true": ..., "false": ...}} and an optional threshold '
    '(default 0.5; value is p_true >= threshold). Give the material as '
    'state (text), state_files (up to 16 local text files inside the '
    "server's allowed folders), or both. Prefer state_files for material "
    'you have not read yet: the server reads the files, so you do not '
    'retype them. The contents of state and of every file in state_files '
    'are sent to the configured judge server ({provider}). Probabilities '
    'are model outputs, not guaranteed accuracy.'
)


class DecideMeta(BaseModel):
    provider: str
    model: Optional[str] = None
    noul_mode: Optional[Literal['choice', 'native']] = None
    latency_ms: float


class DecideResult(BaseModel):
    type: Literal['choice', 'noul']
    value: Union[bool, str]
    probabilities: Optional[Dict[str, float]] = None
    p_true: Optional[float] = None
    threshold: Optional[float] = None
    meta: DecideMeta


def error_result(code, message):
    """An ``is_error`` result; ``message`` must already be clean."""
    error = {'error': {'code': code, 'message': message}}
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps(error))],
        structured_content=error,
        is_error=True,
    )


def ok_result(result):
    return CallToolResult(
        content=[
            TextContent(
                type='text', text=json.dumps(result, ensure_ascii=False)
            )
        ],
        structured_content=result,
    )


def schema_error(error):
    """A clean message from a pydantic ``ValidationError``: top-level
    argument names and error types, never the values."""
    found = []
    for item in error.errors():
        location = item.get('loc') or ()
        name = location[0] if location else None
        name = name if name in ARGUMENTS else 'arguments'
        found.append(f'{name}: {item.get("type", "invalid")}')
    return 'invalid arguments (' + '; '.join(sorted(set(found))) + ')'


class DecideTool(Tool):
    """``Tool`` with argument checks before the SDK and clean SDK errors."""

    async def run(self, arguments, context, convert_result=False):
        try:
            check_arguments(arguments)
            return await super().run(arguments, context, convert_result)
        except DecisionError as error:
            return error_result(error.code, error.message)
        except ToolError as error:
            if isinstance(error.__cause__, ValidationError):
                return error_result(
                    'invalid_request', schema_error(error.__cause__)
                )
            if isinstance(error.__cause__, DecisionError):
                cause = error.__cause__
                return error_result(cause.code, cause.message)
            LOGGER.warning('decide failed: %s', type(error).__name__)
            return error_result('provider_error', 'the tool failed')


def _decide_function(decider):
    async def decide(
        type: Annotated[
            Literal['choice', 'noul'],
            Field(description='"choice" or "noul" (true/false).'),
        ],
        instructions: Annotated[
            str,
            Field(
                min_length=1,
                description='The question for the judge, e.g. "Which '
                'document best answers the query?" or "Does the passage '
                'support the claim?".',
            ),
        ],
        criteria: Annotated[
            Dict[str, str],
            Field(
                min_length=2,
                max_length=MAX_CHOICE_OPTIONS,
                description='choice: {option_id: description}, 2 to '
                f'{MAX_CHOICE_OPTIONS} options. noul: exactly '
                '{"true": ..., "false": ...}.',
            ),
        ],
        state: Annotated[
            Optional[str],
            Field(
                description='The material to judge, as text. Optional '
                'when state_files is given; read before the files.'
            ),
        ] = None,
        state_files: Annotated[
            Optional[List[str]],
            Field(
                max_length=16,
                description='Local UTF-8 text files to judge (paths '
                'relative to the server folder, up to 16, 256 KiB in '
                'all). Key, .env and .git files are refused. Their '
                'contents are sent to the judge server.',
            ),
        ] = None,
        threshold: Annotated[
            Optional[float],
            Field(
                ge=0.0,
                le=1.0,
                description='noul only: value is p_true >= threshold '
                '(default 0.5). Not allowed for choice.',
            ),
        ] = None,
    ) -> Annotated[CallToolResult, DecideResult]:
        arguments = {
            'type': type,
            'instructions': instructions,
            'criteria': criteria,
            'state': state,
            'state_files': state_files,
            'threshold': threshold,
        }
        arguments = {k: v for k, v in arguments.items() if v is not None}
        started = time.perf_counter()
        try:
            result = await anyio.to_thread.run_sync(decider.decide, arguments)
        except DecisionError as error:
            LOGGER.info('decide %s: %s', type, error.code)
            return error_result(error.code, error.message)
        elapsed = (time.perf_counter() - started) * 1000.0
        LOGGER.info('decide %s: ok in %.0f ms', type, elapsed)
        return ok_result(result)

    return decide


def build_server(decider, log_level='WARNING'):
    """The ``MCPServer``; closes ``decider`` when the server stops."""
    tool = DecideTool.from_function(
        _decide_function(decider),
        name=TOOL_NAME,
        description=DESCRIPTION.format(
            provider=decider.provider.capitalize(),
            max_options=MAX_CHOICE_OPTIONS,
        ),
    )
    tool.parameters['additionalProperties'] = False

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            decider.close()

    server = MCPServer(
        SERVER_NAME,
        instructions=(
            'One tool, decide: a choice or true/false judgment by the '
            f'configured {decider.provider} judge.'
        ),
        tools=[tool],
        lifespan=lifespan,
        log_level=log_level,
    )
    # httpx logs each request URL at INFO; the judge address stays private.
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return server
