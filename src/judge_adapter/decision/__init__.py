# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""The decision core: choice and noul questions to Davi or Jev.

    from judge_adapter.decision import build_client, NoulQuestion
    with build_client('davi', os.environ) as client:
        answer = client.decide(state, NoulQuestion(...))

It imports neither the routing package nor any MCP code.
"""

from .client import DaviClient, JevClient, SystemOneClient
from .settings import build_client, with_options_file
from .types import (
    ERROR_CODES,
    NOUL_MODES,
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionError,
    NoulAnswer,
    NoulQuestion,
    Question,
    Reply,
)

__all__ = [
    'ERROR_CODES',
    'NOUL_MODES',
    'ChoiceAnswer',
    'ChoiceQuestion',
    'DaviClient',
    'DecisionError',
    'JevClient',
    'NoulAnswer',
    'NoulQuestion',
    'Question',
    'Reply',
    'SystemOneClient',
    'build_client',
    'with_options_file',
]
