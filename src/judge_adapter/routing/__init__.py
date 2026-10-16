# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Routing: does a judge know when a stronger model is needed?

- ``config``     plugin options and secrets from the environment.
- ``questions``  the judge questions (answer, completion, pre, tier).
- ``verifiers``  one ``verify(task, candidate, rubric, kind)`` interface for
                 every judge (Davi, TypeSafe Jev, Claude, fake), returning
                 P(accept), latency and an estimated cost.
- ``policy``     P(accept) -> accept / escalate, difficulty -> cheap / mid /
                 strong, with the next model as a plain string.
- ``hooks``      the Claude Code hooks of ``plugins/davi-route``: pick the
                 model of an Agent call, judge a subagent's answer, check
                 completion at Stop.
- ``grading``    is a model's answer right?
- ``harness``    headless measurement with ``claude -p`` per arm.
- ``analysis``   accuracy / cost curves, baselines and demo tables.
- ``cli``        the ``davi-route`` command.
"""
