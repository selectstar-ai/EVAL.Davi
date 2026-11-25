# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Reference input: local files read into the state by the MCP layer.

A file is read only when it lies inside one of the allowed roots (by
default the server's working directory) after symbolic links are
resolved. A path with a ``..`` part, an absolute path outside the roots,
a key-like or ``.git`` path, a missing, non-regular, non-UTF-8 or too
large file is refused. Errors name the file by its number and a reason;
they never carry the path or the content.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePath

from ..decision import DecisionError

MAX_FILES = 16
MAX_FILE_BYTES = 256 * 1024
MAX_STATE_BYTES = 256 * 1024
DENIED_NAMES = ('.env*', '*.pem', '*.key', 'id_*')
DENIED_DIRS = ('.git',)
HEADER = '[파일 {number}: {path}]'


def _invalid(message):
    return DecisionError('invalid_request', message)


def _denied(parts):
    for part in parts:
        name = part.lower()
        if name in DENIED_DIRS:
            return True
        if any(fnmatch.fnmatchcase(name, rule) for rule in DENIED_NAMES):
            return True
    return False


def _inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class StateFiles:
    """Reads ``state_files`` for one server; the roots are fixed at start."""

    def __init__(
        self,
        roots=None,
        *,
        max_files=MAX_FILES,
        max_file_bytes=MAX_FILE_BYTES,
        max_state_bytes=MAX_STATE_BYTES,
    ):
        roots = list(roots or [os.getcwd()])
        self._roots = [os.path.realpath(os.fspath(root)) for root in roots]
        for root in self._roots:
            if not os.path.isdir(root):
                raise ValueError('an allowed root is not a directory')
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_state_bytes = max_state_bytes

    @property
    def roots(self):
        return tuple(self._roots)

    @property
    def max_files(self):
        return self._max_files

    def combine(self, state, paths):
        """``state`` followed by each file under a ``[파일 N: path]``
        header. ``state`` may be None or empty when files are given."""
        paths = list(paths or [])
        state = state if isinstance(state, str) and state.strip() else ''
        if not state and not paths:
            raise _invalid('give state, state_files or both')
        if len(paths) > self._max_files:
            raise _invalid(f'state_files has more than {self._max_files}')
        parts = [state] if state else []
        for number, path in enumerate(paths, start=1):
            shown, text = self._read(number, path)
            header = HEADER.format(number=number, path=shown)
            parts.append(f'{header}\n{text}')
        combined = '\n\n'.join(parts)
        if len(combined.encode('utf-8')) > self._max_state_bytes:
            raise _invalid(
                f'the state with its files is over '
                f'{self._max_state_bytes // 1024} KiB'
            )
        return combined

    # --- one file -----------------------------------------------------------
    def _read(self, number, raw):
        """(path shown to the judge, text) of file ``number``."""
        what = f'state_files[{number}]'
        if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
            raise _invalid(f'{what}: not a usable path')
        given = PurePath(raw)
        if '..' in given.parts:
            raise _invalid(f"{what}: '..' is not allowed")
        if given.is_absolute():
            lexical = os.path.normpath(raw)
        else:
            lexical = os.path.normpath(os.path.join(self._roots[0], raw))
        real = os.path.realpath(lexical)
        root = next((r for r in self._roots if _inside(real, r)), None)
        if root is None:
            raise _invalid(f'{what}: outside the allowed roots')
        relative = Path(os.path.relpath(real, root))
        lexical_root = next(
            (r for r in self._roots if _inside(lexical, r)), root
        )
        lexical_parts = Path(os.path.relpath(lexical, lexical_root)).parts
        if _denied(relative.parts) or _denied(lexical_parts):
            raise _invalid(f'{what}: a key, environment or .git file')
        if not os.path.isfile(real):
            raise _invalid(f'{what}: not found or not a regular file')
        limit = self._max_file_bytes
        try:
            if os.path.getsize(real) > limit:
                raise _invalid(f'{what}: over {limit // 1024} KiB')
            with open(real, 'rb') as file:
                data = file.read(limit + 1)
        except OSError:
            raise _invalid(f'{what}: not readable') from None
        if len(data) > limit:
            raise _invalid(f'{what}: over {limit // 1024} KiB')
        if b'\0' in data:
            raise _invalid(f'{what}: not UTF-8 text')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise _invalid(f'{what}: not UTF-8 text') from None
        return relative.as_posix(), text
