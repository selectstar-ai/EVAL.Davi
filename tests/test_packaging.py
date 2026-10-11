"""Source distribution contents: every relative link in README.md must point into a path the sdist includes, so the
README's links (docs/, examples/, hooks/, ...) still resolve in an unpacked sdist. Reads pyproject.toml only; no build."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sdist_includes():
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    block = re.search(r'^\[tool\.hatch\.build\.targets\.sdist\]\s*\ninclude\s*=\s*\[(.*?)\]', text, re.S | re.M)
    assert block, 'sdist include list not found in pyproject.toml'
    return re.findall(r'"([^"]+)"', block.group(1))


def test_sdist_includes_docs():
    assert '/docs' in sdist_includes()


def test_every_readme_relative_link_is_inside_the_sdist():
    inc = {p.strip('/') for p in sdist_includes()}
    links = re.findall(r'\]\(([^)\s]+)\)', (ROOT / 'README.md').read_text(encoding='utf-8'))
    rel = [t.partition('#')[0] for t in links if not re.match(r'[a-z]+:', t) and not t.startswith('#')]
    assert any(t.startswith('docs/') for t in rel)
    for t in rel:
        assert (ROOT / t).exists(), t
        assert t.split('/')[0] in inc, f'{t} is linked from README.md but not in the sdist include list'
