# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Recordings of live runs: one JSON file per run.

A recording holds the question and the answers, so keep the directory out
of version control (the default ``dashboard-recordings/`` is ignored).
"""

from __future__ import annotations

import json
import os
import re
import time

from .events import SUMMARY

RECORDING_VERSION = 1
_UNSAFE_RE = re.compile(r'[^A-Za-z0-9_.-]+')
_ID_RE = re.compile(r'^[A-Za-z0-9_.-]+$')


class RecordingStore:
    """Saves, lists and loads recordings in one directory."""

    def __init__(self, directory, clock=time.time):
        self._directory = directory
        self._clock = clock

    @property
    def directory(self):
        return self._directory

    def save(self, task, events):
        """Write a recording; returns its id."""
        os.makedirs(self._directory, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(self._clock()))
        base = f'{stamp}-{_UNSAFE_RE.sub("-", task.id)[:60]}'
        recording_id, number = base, 1
        while os.path.exists(self._path(recording_id)):
            number += 1
            recording_id = f'{base}-{number}'
        data = {
            'version': RECORDING_VERSION,
            'id': recording_id,
            'recorded_at': self._clock(),
            'task': task.to_dict(),
            'events': [event.to_dict() for event in events],
        }
        with open(self._path(recording_id), 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        return recording_id

    def load(self, recording_id):
        if not _ID_RE.match(recording_id or ''):
            raise KeyError(recording_id)
        try:
            with open(self._path(recording_id), encoding='utf-8') as handle:
                return json.load(handle)
        except FileNotFoundError as error:
            raise KeyError(recording_id) from error

    def list(self):
        """Short descriptions of every recording, newest first."""
        if not os.path.isdir(self._directory):
            return []
        items = []
        for name in os.listdir(self._directory):
            if not name.endswith('.json'):
                continue
            try:
                data = self.load(name[: -len('.json')])
            except (KeyError, ValueError):
                continue
            items.append(describe(data))
        items.sort(key=lambda item: item['recorded_at'] or 0, reverse=True)
        return items

    def _path(self, recording_id):
        return os.path.join(self._directory, f'{recording_id}.json')


def describe(recording):
    """What the recordings list shows for one recording."""
    summary = {}
    for event in recording.get('events') or []:
        if event.get('type') == SUMMARY:
            summary = event.get('data') or {}
    task = recording.get('task') or {}
    return {
        'id': recording.get('id'),
        'recorded_at': recording.get('recorded_at'),
        'task_id': task.get('id'),
        'title': task.get('title') or task.get('question', '')[:40],
        'routed_model': summary.get('routed_model'),
        'saving_pct': summary.get('saving_pct'),
        'routed_correct': summary.get('routed_correct'),
        'opus_correct': summary.get('opus_correct'),
    }
