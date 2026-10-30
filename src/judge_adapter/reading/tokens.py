# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""A fixed token estimate for the size limits of ``davi-read``.

UTF-8 bytes / 4: close to the cl100k count for English text and a little
under it for Korean. It needs no tokenizer download, so a page gets the
same estimate everywhere; the judge's own input limit is enforced by the
server.
"""

from __future__ import annotations

BYTES_PER_TOKEN = 4


def estimate(text):
    """Estimated tokens of ``text`` (at least 1 for non-empty text)."""
    if not text:
        return 0
    return max(1, -(-len(text.encode('utf-8')) // BYTES_PER_TOKEN))
