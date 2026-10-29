#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Turn davi-route's features on or off (``/davi-route:setup``).

Standard library only. Usage::

    python3 configure.py                        # show the settings
    python3 configure.py on                     # routing on (tiers, davi)
    python3 configure.py off                    # routing off
    python3 configure.py stop on                # completion check on
    python3 configure.py --mode post --judge claude
    python3 configure.py http://<davi-host>:<port>  # where there is no
                                                     # settings dialog
    python3 configure.py --set log_file=/tmp/route.jsonl  # advanced

The features (on/off, mode, judge) are set here. The connection (Davi URL
and keys) belongs in the plugin's settings dialog, which wins over this
file; a URL given here is used when the dialog has none (claude.ai has no
dialog). Commands run through the Bash tool get no plugin options, so the
skill passes the dialog's URL with ``--dialog-url``.

Writes ``~/.claude/davi-route.json`` (``DAVI_ROUTE_CONFIG`` or
``CLAUDE_CONFIG_DIR`` move it). The hooks read it on every call, so a
change applies from the next subagent call. ``DAVI_ROUTE_<KEY>``
environment variables win over both. With a Davi URL, showing the settings
or turning routing on also sends one small request to the Davi server and
reports the answer.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import route_hook  # noqa: E402 - the launcher's option file helpers

MODES = ('tiers', 'post', 'pre', 'both')
JUDGES = ('davi', 'jev', 'claude', 'fake')
SWITCHES = ('on', 'off')
# what this command shows by name; any other saved key is "advanced"
FEATURE_DEFAULTS = {
    'route_enabled': False,
    'route_mode': 'tiers',
    'route_judge': 'davi',
    'stop_verify_enabled': False,
}
CHECK_TIMEOUT_SECONDS = 20
CHECK_BODY = {
    'state': {'task': 'What is 2 + 2?', 'candidate': '4'},
    'questions': {
        'route': {
            'type': 'choice',
            'instructions': 'Is the candidate answer correct?',
            'criteria': {
                'accept': 'Correct.',
                'escalate': 'Wrong or unsure.',
            },
        }
    },
}


def check_server(base_url, timeout=CHECK_TIMEOUT_SECONDS, key=None):
    """POST one tiny question to ``/v1/systemone``. -> (ok, message)."""
    request = urllib.request.Request(
        base_url.rstrip('/') + '/v1/systemone',
        data=json.dumps(CHECK_BODY).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    if key:
        request.add_header('Authorization', f'Bearer {key}')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        return False, f'HTTP {error.code}'
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, 'reason', error)
        return False, f'{type(error).__name__}: {reason}'
    answer = (body.get('answers') or {}).get('route') or {}
    accept = (answer.get('probabilities') or {}).get('accept')
    if not isinstance(accept, (int, float)):
        return False, 'reply has no answers.route.probabilities.accept'
    return True, f'ok, P(accept)={accept:.3f} for "2 + 2 = 4"'


def _value(text):
    lowered = text.strip().lower()
    if lowered in ('true', 'false'):
        return lowered == 'true'
    try:
        return float(text) if '.' in text else int(text)
    except ValueError:
        return text.strip()


def _flag(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in route_hook.TRUE_WORDS


def build_parser():
    parser = argparse.ArgumentParser(
        prog='/davi-route:setup', description=__doc__.split('\n')[0]
    )
    parser.add_argument(
        'words',
        nargs='*',
        metavar='on|off|stop on|stop off|URL',
        help='routing on/off, the completion check on/off, a Davi URL',
    )
    parser.add_argument('--mode', choices=MODES)
    parser.add_argument('--judge', choices=JUDGES)
    parser.add_argument(
        '--off', action='store_true', help='routing and the check off'
    )
    parser.add_argument('--show', action='store_true', help='print, no write')
    parser.add_argument(
        '--set',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='an advanced option, e.g. log_file=/tmp/route.jsonl',
    )
    parser.add_argument('--dialog-url', default='', help=argparse.SUPPRESS)
    parser.add_argument(
        '--no-check', action='store_true', help='skip the server check'
    )
    return parser


def read_words(words):
    """(url, routing, check) from the words; the switches are 'on', 'off'
    or None."""
    url = routing = check = None
    queue = [word.strip().strip('"\'') for word in words if word.strip()]
    while queue:
        word = queue.pop(0)
        lowered = word.lower()
        if lowered == 'stop':
            if not queue or queue[0].lower() not in SWITCHES:
                raise ValueError('"stop" needs on or off after it')
            check = queue.pop(0).lower()
        elif lowered in SWITCHES:
            routing = lowered
        elif lowered.startswith(('http://', 'https://')):
            url = word.rstrip('/')
        elif '.' in word or ':' in word:
            raise ValueError('the URL must start with http:// or https://')
        else:
            raise ValueError(f'unknown word {word!r}')
    return url, routing, check


def apply(options, args):
    """The new option dict from the old one and the command line."""
    options = dict(options)
    url, routing, check = read_words(args.words)
    if url:
        options['davi_base_url'] = url
    if routing == 'on':
        options['route_enabled'] = True
        options.setdefault('route_mode', FEATURE_DEFAULTS['route_mode'])
        options.setdefault('route_judge', FEATURE_DEFAULTS['route_judge'])
    elif routing == 'off':
        options['route_enabled'] = False
    if check:
        options['stop_verify_enabled'] = check == 'on'
    if args.mode:
        options['route_mode'] = args.mode
    if args.judge:
        options['route_judge'] = args.judge
    for item in args.set:
        key, sep, value = item.partition('=')
        key = key.strip().lower()
        if not sep or not route_hook.CONFIG_KEY.match(key):
            raise ValueError(f'--set needs KEY=VALUE, got {item!r}')
        options[key] = _value(value)
    if args.off:
        options['route_enabled'] = False
        options['stop_verify_enabled'] = False
    return options


def write(path, options):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump(options, file, indent=2, sort_keys=True)
        file.write('\n')


def _env(key):
    return os.environ.get('DAVI_ROUTE_' + key.upper(), '').strip()


def effective(options, key):
    """(value, where) of a feature option: variable, file or default."""
    if _env(key):
        return _env(key), 'DAVI_ROUTE_' + key.upper()
    if key in options:
        return options[key], 'setup'
    return FEATURE_DEFAULTS[key], 'default'


def davi_url(options, dialog_url):
    """(url, where) of the Davi server the hooks use; url None if unset."""
    if _env('davi_base_url'):
        return _env('davi_base_url'), 'DAVI_ROUTE_DAVI_BASE_URL'
    if dialog_url:
        return dialog_url, 'settings dialog'
    if options.get('davi_base_url'):
        return options['davi_base_url'], 'setup'
    return None, 'not set'


def _shown(value, key):
    if key.endswith(('_key', '_token')):
        return '***'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        return ','.join(str(item) for item in value)
    return str(value)


def describe(options, dialog_url):
    """The settings as the lines the skill reports."""
    routing, _ = effective(options, 'route_enabled')
    mode, _ = effective(options, 'route_mode')
    judge, _ = effective(options, 'route_judge')
    check, _ = effective(options, 'stop_verify_enabled')
    url, where = davi_url(options, dialog_url)
    lines = [
        f'routing: {"on" if _flag(routing) else "off"}'
        f' (mode {mode}, judge {judge})',
        f'completion check: {"on" if _flag(check) else "off"}',
        f'Davi server: {url} ({where})' if url else 'Davi server: not set',
    ]
    advanced = sorted(
        key
        for key in options
        if key not in FEATURE_DEFAULTS and key != 'davi_base_url'
    )
    if advanced:
        lines.append(
            'advanced: '
            + ', '.join(
                f'{key}={_shown(options[key], key)}' for key in advanced
            )
        )
    overridden = sorted(key for key in FEATURE_DEFAULTS if _env(key))
    if overridden:
        lines.append(
            'note: DAVI_ROUTE_* environment variables override '
            + ', '.join(overridden)
        )
    return lines


def _server_key(options):
    return (
        _env('davi_api_key')
        or options.get('davi_api_key')
        or os.environ.get('DAVI_API_KEY', '').strip()
        or None
    )


def main(argv=None, check=check_server, out=sys.stdout):
    args = build_parser().parse_args(argv)
    dialog_url = route_hook.dialog_value(args.dialog_url)
    path = route_hook.config_path()
    options = route_hook.read_config(path)
    try:
        url, routing, _ = read_words(args.words)
    except ValueError as error:
        print(f'error: {error}', file=out)
        return 2
    writes = bool(
        args.words or args.mode or args.judge or args.set or args.off
    )
    if writes and not args.show:
        try:
            options = apply(options, args)
        except ValueError as error:
            print(f'error: {error}', file=out)
            return 2
        write(path, options)
        print(f'saved {path}', file=out)
    for line in describe(options, dialog_url):
        print(line, file=out)
    if url and dialog_url:
        print(
            'note: the settings dialog has a Davi URL, which is used; the '
            'URL given here is kept for when the dialog has none',
            file=out,
        )
    server, _ = davi_url(options, dialog_url)
    routing_on = _flag(effective(options, 'route_enabled')[0])
    judge = effective(options, 'route_judge')[0]
    if server is None and routing_on and judge == 'davi':
        print(
            'note: no Davi server URL: set it in the plugin settings '
            '(/plugin > davi-route > Configure options) or give it here; '
            'until then routing changes nothing',
            file=out,
        )
    wants_check = url or routing == 'on' or not writes or args.show
    if server and wants_check and not args.no_check:
        ok, message = check(server, key=_server_key(options))
        print(f'Davi server check: {message}', file=out)
        if not ok:
            print(
                'the hooks fail open (no change) until the server answers',
                file=out,
            )
    if writes and not args.show:
        print(
            'applies from the next subagent call; httpx is installed at the '
            'next session start if python3 lacks it',
            file=out,
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
