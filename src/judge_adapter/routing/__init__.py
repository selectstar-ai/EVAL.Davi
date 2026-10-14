# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Routing experiments (scenario 3): does a verifier know when a stronger model is needed?

- ``verifiers``: one pointwise ``verify(task, candidate, rubric, kind)`` for three judges (Davi choice endpoint,
  TypeSafe Jev, Claude), returning P(accept), latency and an estimated cost.
- ``policy``: P(accept) -> accept / escalate, with the next model as a plain string (``Route.next_model``).
- ``hooks``: Claude Code hook logic (A: verify a subagent result after the Agent tool; E: set the model of the next
  Agent call; B: verify completion at Stop). Loaded by ``plugins/davi-route``.
- ``harness``: headless measurement (C): run tasks with ``claude -p`` per arm, judge the cheap answers, compute
  accuracy / cost curves and baselines offline. CLI: ``davi-route``.

EXPERIMENTAL. Prompts and thresholds are first drafts; nothing here is a quality claim.
"""
