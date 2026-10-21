#!/usr/bin/env python3
# Copyright 2026 hundredeuk2
# SPDX-License-Identifier: Apache-2.0
"""Write the davi-route options file (``/davi-route:setup``).

Standard library only. Usage::

    python3 configure.py http://<davi-host>:<port>   # turn routing on
    python3 configure.py --mode post --judge claude   # change options
    python3 configure.py --set log_file=/tmp/route.jsonl
    python3 configure.py --off                        # routing off
    python3 configure.py --show

Writes ``~/.claude/davi-route.json`` (``DAVI_ROUTE_CONFIG`` or
``CLAUDE_CONFIG_DIR`` move it). The hooks read it on every call, so a
change applies from the next subagent call. ``DAVI_ROUTE_<KEY>``
environment variables still win over the file. With a URL, it also sends
one small request to the Davi server and reports the answer.
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog='/davi-route:setup', description=__doc__.split('\n')[0]
    )
    parser.add_argument(
        'base_url', nargs='?', help='Davi server, http://host:port'
    )
    parser.add_argument('--mode', choices=MODES)
    parser.add_argument('--judge', choices=JUDGES)
    parser.add_argument('--off', action='store_true', help='routing off')
    parser.add_argument('--show', action='store_true', help='print, no write')
    parser.add_argument(
        '--set',
        action='append',
        default=[],
        metavar='KEY=VALUE',
        help='any plugin option, e.g. log_file=/tmp/route.jsonl',
    )
    parser.add_argument(
        '--no-check', action='store_true', help='skip the server check'
    )
    return parser


def apply(options, args):
    """The new option dict from the old one and the command line."""
    options = dict(options)
    if args.base_url:
        url = args.base_url.strip().strip('"\'')
        if not url.startswith(('http://', 'https://')):
            raise ValueError('the URL must start with http:// or https://')
        options['davi_base_url'] = url.rstrip('/')
        options['route_enabled'] = True
        options.setdefault('route_mode', 'tiers')
        options.setdefault('route_judge', 'davi')
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


def _shown(options):
    hidden = {
        key: ('***' if key.endswith(('_key', '_token')) else value)
        for key, value in options.items()
    }
    return json.dumps(hidden, indent=2, sort_keys=True)


def main(argv=None, check=check_server, out=sys.stdout):
    args = build_parser().parse_args(argv)
    path = route_hook.config_path()
    options = route_hook.read_config(path)
    if not args.show:
        try:
            options = apply(options, args)
        except ValueError as error:
            print(f'error: {error}', file=out)
            return 2
        write(path, options)
        print(f'saved {path}', file=out)
    print(_shown(options), file=out)
    overridden = sorted(
        key
        for key in options
        if os.environ.get('DAVI_ROUTE_' + key.upper(), '').strip()
    )
    if overridden:
        print(
            'note: DAVI_ROUTE_* environment variables override '
            + ', '.join(overridden),
            file=out,
        )
    url = options.get('davi_base_url')
    if url and not args.no_check and (args.base_url or args.show):
        ok, message = check(url, key=options.get('davi_api_key'))
        print(f'Davi server {url}: {message}', file=out)
        if not ok:
            print(
                'routing stays on and fails open (no change) until the '
                'server answers',
                file=out,
            )
    if not args.show:
        print(
            'applies from the next subagent call; httpx is installed at the '
            'next session start if python3 lacks it',
            file=out,
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
