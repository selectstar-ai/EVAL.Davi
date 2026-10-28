"""davi-read: blocks, fetching limits, two-pass judging, choosing, the CLI.

Model-free: the judge is an in-process MockTransport that rates a passage
by a marker word; pages come from another MockTransport.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from judge_adapter.decision import DaviClient
from judge_adapter.reading import (
    FetchError,
    HtmlBlockParser,
    PageFetcher,
    ReadConfig,
    Reader,
    TextBlockParser,
    Thresholds,
    format_result,
    tokens,
)
from judge_adapter.reading import cli as read_cli
from judge_adapter.reading.blocks import MAX_BLOCK_CHARS, Block
from judge_adapter.reading.reader import choose

MARKER = 'maxsize'
# Pushes a paragraph past the length below which short fragments are merged.
PAD = (
    ' This sentence makes the paragraph long enough to stand on its own,'
    ' past the limit below which fragments are merged.'
)
FILLER = 'This sentence is about something else entirely. ' * 4


def public(host, port):
    return [(None, None, None, '', ('93.184.216.34', port))]


def private(host, port):
    return [(None, None, None, '', ('10.0.0.5', port))]


class Judge:
    """A fake Davi: P(true) is 0.95 when the passage holds MARKER."""

    def __init__(self, fail=False):
        self._fail = fail
        self._bodies = []

    @property
    def bodies(self):
        return list(self._bodies)

    def transport(self):
        def handle(request):
            body = json.loads(request.content)
            self._bodies.append(body)
            if self._fail:
                return httpx.Response(500, text='down')
            state = body['state']
            answers = {}
            for key in body['questions']:
                if 'passages' in state:
                    text = next(
                        p['text'] for p in state['passages'] if p['id'] == key
                    )
                else:
                    text = state['passage']
                p = 0.95 if MARKER in text else 0.02
                answers[key] = {'probabilities': {'true': p, 'false': 1 - p}}
            return httpx.Response(200, json={'answers': answers})

        return httpx.MockTransport(handle)

    def client(self, **kwargs):
        return DaviClient(
            'http://davi.test', transport=self.transport(), **kwargs
        )


def long_page():
    sections = []
    for index in range(80):
        sections.append(f'<h2>Part {index}</h2><p>{FILLER}</p>')
    sections.insert(
        12,
        f'<h2>lru_cache</h2><p>Wraps a function.{PAD}</p>'
        f'<p>If {MARKER} is None the cache grows without bound.</p>'
        f'<p>Typed caching is separate.{PAD}</p>',
    )
    table = (
        '<table><thead><tr><th>Option</th><th>Default</th></tr></thead>'
        f'<tr><td>other</td><td>1</td></tr><tr><td>{MARKER}</td>'
        '<td>128</td></tr></table>'
    )
    return (
        '<html><head><title>T</title></head><body><nav>menu</nav><main>'
        '<h1>functools</h1><p>Intro to the module.</p>'
        + ''.join(sections)
        + table
        + '</main><footer>foot</footer></body></html>'
    )


# --- blocks ------------------------------------------------------------------
def test_html_blocks_keep_headings_tables_code_and_links():
    html = (
        '<html><head><title>Doc</title><script>x</script></head><body>'
        '<nav>menu</nav><main><h1>Top<a href="#top">¶</a></h1>'
        '<p>See <a href="/x">the page</a>.</p>'
        '<dl><dt>f(maxsize)</dt><dd><p>About f.</p></dd></dl>'
        '<h2>Sub</h2><ul><li>one</li><li>two</li></ul>'
        '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>'
        '</table><pre>x = 1\n  y = 2</pre></main><footer>f</footer>'
        '</body></html>'
    )
    blocks = HtmlBlockParser().parse(html, 'https://docs.test/a/b')
    rows = [(b.kind, b.path, b.text, b.head) for b in blocks]
    assert rows == [
        ('text', ('Top',), 'See [the page](https://docs.test/x).', False),
        ('text', ('Top', 'f(maxsize)'), 'About f.', False),
        ('text', ('Top', 'Sub'), '- one\n- two', False),
        ('row', ('Top', 'Sub'), 'A | B', True),
        ('row', ('Top', 'Sub'), '1 | 2', False),
        ('code', ('Top', 'Sub'), 'x = 1\n  y = 2', False),
    ]
    assert [b.id for b in blocks] == list(range(len(blocks)))


def test_page_title_is_the_path_when_there_is_no_heading():
    blocks = HtmlBlockParser().parse(
        '<html><head><title>Only title</title></head><body><p>text</p>'
        '</body></html>'
    )
    assert blocks[0].path == ('Only title',)


def test_long_preformatted_text_is_split_into_paragraphs():
    paragraph = '   JSON text exchanged between systems MUST be\n   UTF-8.'
    pre = '\n\n'.join([paragraph] + ['   filler line ' * 20] * 10)
    blocks = HtmlBlockParser().parse(f'<pre>{pre}</pre>')
    assert len(blocks) == 11
    assert blocks[0].text == paragraph
    assert all(b.kind == 'code' for b in blocks)
    short = HtmlBlockParser().parse('<pre>a = 1\n\nb = 2</pre>')
    assert [b.text for b in short] == ['a = 1\n\nb = 2']


def test_wiki_citation_marks_and_edit_links_are_dropped():
    html = (
        '<h2>History<span class="mw-editsection">[edit]</span></h2>'
        '<p>The Allies formed the United Nations,<sup class="reference">'
        '<a href="#cite-1">[1]</a></sup> which came into existence.</p>'
        '<div class="navbox"><ul><li>Related</li></ul></div>'
    )
    blocks = HtmlBlockParser().parse(html)
    assert [(b.path, b.text) for b in blocks] == [
        (
            ('History',),
            'The Allies formed the United Nations, which came into existence.',
        )
    ]


def test_note_asides_are_kept_and_sidebars_dropped():
    html = (
        f'<main><h1>Doc</h1><p>Body text.{PAD}</p>'
        '<aside class="admonition warning"><p>Do not use in production.</p>'
        '</aside><aside class="sidebar"><p>Related links</p></aside></main>'
    )
    texts = [b.text for b in HtmlBlockParser().parse(html)]
    assert texts == [f'Body text.{PAD}', 'Do not use in production.']


def test_a_lead_in_sentence_is_joined_with_its_list():
    html = (
        '<h1>Limits</h1><p>The value must be one of:</p>'
        f'<ul><li>small</li><li>large</li></ul><p>Afterwards.{PAD}</p>'
        '<ul><li>separate</li></ul>'
    )
    texts = [b.text for b in HtmlBlockParser().parse(html)]
    assert texts == [
        'The value must be one of:\n- small\n- large',
        f'Afterwards.{PAD}',
        '- separate',
    ]


def test_short_fragments_under_one_heading_are_merged():
    long_item = 'x' * 120
    html = (
        '<h1>Steps</h1><ul><li>one</li><li>two</li><li>three</li></ul>'
        f'<ul><li>{long_item}</li><li>after the long one</li></ul>'
        '<h2>Other</h2><p>short</p>'
        '<table><tr><td>a</td></tr><tr><td>b</td></tr></table>'
        '<pre>x = 1</pre><pre>y = 2</pre>'
    )
    texts = [b.text for b in HtmlBlockParser().parse(html)]
    assert texts == [
        '- one\n- two\n- three',
        f'- {long_item}',
        '- after the long one',
        'short',
        'a',
        'b',
        'x = 1',
        'y = 2',
    ]


def test_fragment_merging_stops_at_the_length_cap():
    items = ''.join(f'<li>{"y" * 90} {i}</li>' for i in range(12))
    blocks = HtmlBlockParser().parse(f'<h1>Many</h1><ul>{items}</ul>')
    assert len(blocks) > 1
    assert all(len(b.text) < 900 for b in blocks)


def test_menus_inside_main_content_are_dropped():
    html = (
        '<main id="content"><header><h1>Python</h1>'
        '<div class="vector-dropdown mw-portlet-lang"><ul>'
        '<li><a href="https://af.test/">Afrikaans</a></li></ul></div>'
        '<ul role="menu"><li>Edit</li></ul></header>'
        '<p>Python is a programming language.</p></main>'
    )
    blocks = HtmlBlockParser().parse(html)
    assert [(b.path, b.text) for b in blocks] == [
        (('Python',), 'Python is a programming language.')
    ]


def test_markdown_blocks():
    text = (
        '# Title\n\nIntro\nline two.\n\n## Sub\n- a\n1. b\n\n'
        '| h | v |\n|---|---|\n| x | y |\n\n```\ncode\n```\n'
    )
    blocks = TextBlockParser().parse(text)
    assert [(b.kind, b.text, b.head) for b in blocks] == [
        ('text', 'Intro line two.', False),
        ('text', '- a\n- b', False),
        ('row', 'h | v', True),
        ('row', 'x | y', False),
        ('code', 'code', False),
    ]
    assert blocks[-1].path == ('Title', 'Sub')


def test_long_blocks_are_split_without_losing_text():
    sentence = 'A sentence that goes on. '
    text = sentence * (MAX_BLOCK_CHARS // len(sentence) * 2)
    blocks = TextBlockParser().parse(text)
    assert len(blocks) > 1
    assert all(len(b.text) <= MAX_BLOCK_CHARS for b in blocks)
    assert ' '.join(b.text for b in blocks) == text.strip()


# --- fetching ----------------------------------------------------------------
def pages(routes):
    def handle(request):
        status, headers, body = routes[str(request.url)]
        return httpx.Response(status, headers=headers, content=body)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    'url', ['http://localhost/x', 'http://10.0.0.5/x', 'file:///etc/hosts']
)
def test_private_and_non_http_addresses_are_blocked(url):
    def resolver(host, port):
        return (
            private(host, port)
            if host != 'localhost'
            else [(None, None, None, '', ('127.0.0.1', port))]
        )

    with pytest.raises(FetchError) as caught:
        PageFetcher(resolver=resolver).check(url)
    assert caught.value.code == 'blocked'


def test_allowed_host_may_be_private():
    PageFetcher(['wiki.corp'], resolver=private).check('http://wiki.corp/x')


def test_redirect_to_a_private_host_is_blocked():
    def resolver(host, port):
        return private(host, port) if host == 'inner' else public(host, port)

    routes = {
        'https://docs.test/a': (302, {'location': 'http://inner/k'}, b''),
    }
    fetcher = PageFetcher(transport=pages(routes), resolver=resolver)
    with pytest.raises(FetchError) as caught:
        fetcher.fetch('https://docs.test/a')
    assert caught.value.code == 'blocked'


def test_fetch_decodes_a_legacy_charset_and_follows_redirects():
    korean = (
        '<html><head><meta charset="euc-kr"></head><body><p>한국어 문서</p>'
    )
    routes = {
        'https://docs.test/a': (301, {'location': '/b'}, b''),
        'https://docs.test/b': (
            200,
            {'content-type': 'text/html'},
            korean.encode('euc-kr'),
        ),
    }
    page = PageFetcher(transport=pages(routes), resolver=public).fetch(
        'https://docs.test/a'
    )
    assert page.final_url == 'https://docs.test/b'
    assert '한국어 문서' in page.text


def test_refused_files_are_saved_for_a_file_reader(tmp_path):
    routes = {
        'https://docs.test/a.pdf': (
            200,
            {'content-type': 'application/pdf'},
            b'%PDF-1.7 body',
        )
    }
    fetcher = PageFetcher(
        transport=pages(routes), resolver=public, files_dir=str(tmp_path)
    )
    with pytest.raises(FetchError) as caught:
        fetcher.fetch('https://docs.test/a.pdf')
    assert caught.value.code == 'unsupported'
    assert caught.value.saved.endswith('.pdf')
    with open(caught.value.saved, 'rb') as file:
        assert file.read() == b'%PDF-1.7 body'


def test_script_built_pages_are_rendered_when_the_html_is_empty():
    class Renderer:
        def __init__(self):
            self.urls = []

        def render(self, url):
            self.urls.append(url)
            return '<main><h1>App</h1><p>Rendered text.</p></main>'

    routes = {
        'https://app.test/': (
            200,
            {'content-type': 'text/html'},
            b'<html><body><div id="root"></div></body></html>',
        )
    }
    renderer = Renderer()
    reader = Reader(
        Judge().client(),
        fetcher=PageFetcher(transport=pages(routes), resolver=public),
        renderer=renderer,
    )
    result = reader.read_url('https://app.test/', 'q?')
    assert renderer.urls == ['https://app.test/']
    assert result.rendered is True
    assert result.text == '## App\nRendered text.'


@pytest.mark.parametrize(
    'response, code',
    [
        ((404, {}, b'no'), 'http'),
        (
            (200, {'content-type': 'application/pdf'}, b'%PDF-1.7'),
            'unsupported',
        ),
        ((200, {'content-type': 'image/png'}, b'png'), 'unsupported'),
        ((200, {'content-type': 'text/html'}, b'x' * 2048), 'too_large'),
    ],
)
def test_fetch_failures(response, code):
    routes = {'https://docs.test/a': response}
    fetcher = PageFetcher(
        transport=pages(routes), resolver=public, max_bytes=1024
    )
    with pytest.raises(FetchError) as caught:
        fetcher.fetch('https://docs.test/a')
    assert caught.value.code == code


# --- reading -----------------------------------------------------------------
def test_short_pages_pass_through_without_a_judge_call():
    judge = Judge()
    result = Reader(judge.client()).read_text('<p>short page</p>', 'question')
    assert result.status == 'passthrough'
    assert result.text == 'short page'
    assert not judge.bodies


def test_long_page_keeps_the_answer_its_heading_and_table_header():
    judge = Judge()
    result = Reader(judge.client()).read_text(long_page(), 'q?')
    assert result.status == 'filtered'
    text = result.text
    assert f'If {MARKER} is None the cache grows without bound.' in text
    assert '## functools > lru_cache' in text
    assert 'Option | Default' in text
    assert f'{MARKER} | 128' in text
    assert 'Intro to the module.' in text  # the first block
    assert 'menu' not in text and 'foot' not in text
    assert result.blocks_kept < result.blocks_total
    # pass 1 windows put the query first and one noul per block
    first = judge.bodies[0]
    assert list(first['state']) == ['query', 'passages']
    assert all(q['type'] == 'choice' for q in first['questions'].values())


def test_native_mode_sends_server_noul_questions():
    class Native(Judge):
        def transport(self):
            def handle(request):
                body = json.loads(request.content)
                self._bodies.append(body)
                answers = {key: {'noul': 0.01} for key in body['questions']}
                return httpx.Response(200, json={'answers': answers})

            return httpx.MockTransport(handle)

    judge = Native()
    result = Reader(judge.client(noul_mode='native')).read_text(
        long_page(), 'q?'
    )
    assert result.status == 'no_match'
    assert result.noul_mode == 'native'
    question = next(iter(judge.bodies[0]['questions'].values()))
    assert question == {
        'type': 'noul',
        'instructions': question['instructions'],
    }


def test_no_match_still_returns_the_closest_blocks():
    judge = Judge()
    page = long_page().replace(MARKER, 'nothing')
    result = Reader(judge.client()).read_text(page, 'q?')
    assert result.status == 'no_match'
    assert tokens.estimate(result.text) >= ReadConfig().min_output_tokens


def test_judge_failure_returns_the_page_unjudged():
    judge = Judge(fail=True)
    config = ReadConfig(failopen_chars=500)
    result = Reader(judge.client(), config).read_text(long_page(), 'q?')
    assert result.status == 'judge_failed'
    assert result.error == 'provider_error'
    assert len(result.text) == 500
    assert result.page_tokens > tokens.estimate(result.text)
    assert '# cut at about' in format_result(result)


def test_each_window_is_rated_and_its_candidates_asked_alone():
    judge = Judge()
    result = Reader(judge.client()).read_text(long_page(), 'q?')
    windows = [b for b in judge.bodies if 'passages' in b['state']]
    alone = [b for b in judge.bodies if 'passage' in b['state']]
    assert len(windows) >= 2
    assert sum(len(b['questions']) for b in windows) == result.blocks_total
    assert {b['state']['passage'] for b in alone} == {
        f'If {MARKER} is None the cache grows without bound.',
        f'{MARKER} | 128',
    }
    assert result.judge_requests == len(judge.bodies)


# --- choosing ---------------------------------------------------------------
def blocks_of(*paths, table=0):
    return [
        Block(i, f'block {i} ' + 'x' * 100, (path,), table=table)
        for i, path in enumerate(paths)
    ]


def test_choose_keeps_first_block_even_when_the_body_is_long_enough():
    blocks = blocks_of('a', 'b', 'b')
    keep = choose(blocks, {}, {2: 0.9}, Thresholds(), min_output_tokens=10)
    assert keep == {0, 2}


def test_choose_adds_neighbours_only_under_the_same_heading():
    blocks = blocks_of('a', 'b', 'b', 'c')
    second = {1: 0.7, 2: 0.9, 3: 0.7}
    keep = choose(blocks, {}, second, Thresholds(), min_output_tokens=10)
    assert keep == {0, 1, 2}


def test_choose_neighbour_needs_its_own_pass_two_score():
    blocks = blocks_of('a', 'b', 'b', 'b', 'b')
    second = {1: 0.5, 2: 0.9, 3: 0.65}
    keep = choose(blocks, {4: 0.99}, second, Thresholds(), 10)
    assert keep == {0, 2, 3}


def test_choose_tops_up_by_pass_two_then_pass_one_score():
    blocks = blocks_of('a', 'b', 'c', 'd', 'e')
    first = {1: 0.3, 2: 0.5, 3: 0.4}
    second = {4: 0.7}
    keep = choose(blocks, first, second, Thresholds(), min_output_tokens=50)
    assert keep == {0, 4, 2}


def test_choose_adds_the_header_row_of_a_kept_table_row():
    blocks = [
        Block(0, 'intro', ('a',)),
        Block(1, 'Option | Default', ('a',), 'row', 1, True),
        Block(2, 'x | 1', ('a',), 'row', 1),
        Block(3, 'y | 2', ('a',), 'row', 1),
    ]
    keep = choose(blocks, {}, {3: 0.9}, Thresholds(), min_output_tokens=1)
    assert keep == {0, 1, 3}


# --- command line ------------------------------------------------------------
def run_cli(argv, tmp_path, transport=None, fetcher=None):
    env = {
        'DAVI_BASE_URL': 'http://davi.test',
        'DAVI_ROUTE_CONFIG': str(tmp_path / 'none.json'),
    }
    out = io.StringIO()
    code = read_cli.run(argv, env, transport, fetcher, out)
    return code, out.getvalue()


def test_cli_reads_a_local_file_as_json(tmp_path):
    page = tmp_path / 'page.html'
    page.write_text(long_page())
    code, out = run_cli(
        ['--file', str(page), 'q?', '--json'], tmp_path, Judge().transport()
    )
    data = json.loads(out)
    assert code == 0
    assert data['status'] == 'filtered'
    assert data['provider'] == 'davi'


@pytest.mark.parametrize('name', ['.env', 'server.pem', 'id_rsa', 'a.key'])
def test_cli_refuses_key_files(tmp_path, name):
    secret = tmp_path / name
    secret.write_text('SECRET=1')
    code, out = run_cli(['--file', str(secret), 'q?'], tmp_path)
    assert code == 2
    assert out == ''


def test_cli_fetch_error_exits_1(tmp_path):
    fetcher = PageFetcher(resolver=private)
    code, out = run_cli(['http://inner/x', 'q?'], tmp_path, None, fetcher)
    assert code == 1
    assert '# status: fetch_error' in out
    assert 'blocked' in out


def test_cli_rejects_bad_thresholds(tmp_path):
    code, _ = run_cli(['http://x.test', 'q?', '--keep', '2'], tmp_path)
    assert code == 2
