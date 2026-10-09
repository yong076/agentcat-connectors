#!/usr/bin/env python3
"""Manual TRA-780 benchmark; never reads real CLI homes or calls providers.

Run: python3 scripts/benchmark_codex_ticks.py --baseline-ref f3b4e3c
Each revision gets the same synthetic store, a cold scan outside the timer,
then 60 idle and 60 active-gap ticks. Only session polling is measured: discovery
and sqlite are injected so results isolate session I/O, cursor parsing and rollups.
"""
import argparse
from contextlib import ExitStack
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def run(source, root, size_mib, count, ticks):
    home = root / 'home'
    store = home / '.codex' / 'sessions'
    store.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('AGENTCAT_', 'CODEX_', 'CLAUDE_', 'GROK_', 'KIMI_', 'GEMINI_'))}
    env.update(HOME=str(home), USERPROFILE=str(home), AGENTCAT_HOME=str(home / '.agentcat'),
               APPDATA=str(home / 'AppData/Roaming'), LOCALAPPDATA=str(home / 'AppData/Local'))
    sys.path.insert(0, str(ROOT / 'lib'))
    module_path = root / 'agentcat.py'
    module_path.write_bytes(source)
    with patch.dict(os.environ, env, clear=True):
        loader = SourceFileLoader('codex_benchmark', str(module_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        event = {'timestamp': '2026-10-10T00:00:00Z', 'type': 'event_msg',
                 'payload': {'type': 'token_count', 'info': {
                     'last_token_usage': {'input_tokens': 10, 'output_tokens': 10}}}}
        line = json.dumps(event) + '\n'
        padding = json.dumps({'type': 'fixture_padding', 'payload': 'x' * max(0, int(size_mib * 1024**2 / count) - len(line) - 50)}) + '\n'
        files = [store / f'rollout-{i:05d}.jsonl' for i in range(count)]
        for path in files:
            path.write_text(padding + line)
        sqlite = {'status': 'ok', 'tokens': {'all': 50_000_000}}
        counts = {'cursorReads': 0, 'sessionOpens': 0}
        original_read = module.read_json
        original_open = Path.open
        def read(path, *args, **kwargs):
            if path == module.CODEX_SESSIONS_CURSOR_FILE:
                counts['cursorReads'] += 1
            return original_read(path, *args, **kwargs)
        def opened(path, *args, **kwargs):
            if path.parent == store and args and args[0] == 'rb':
                counts['sessionOpens'] += 1
            return original_open(path, *args, **kwargs)
        with ExitStack() as stack:
            for name, value in (
                ('codex_session_files', lambda: files),
                ('codex_session_roots', lambda: [store]),
                ('_usage_home_id', lambda *args: 'fixture-home'),
                ('_discover_provider_homes', lambda: {'codex': [
                    {'id': 'fixture-home', 'path': store.parent, 'state': 'tracked', 'exists': True}]}),
                ('_prepare_home_accounts', lambda *args: False),
                ('codex_sqlite_snapshot', lambda: sqlite),
                ('codexbar_cost_cache_snapshot', lambda *args: {}),
            ):
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(patch.object(module.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden')))
            module.codex_snapshot()  # Cold initialization is deliberately untimed.
            stack.enter_context(patch.object(module, 'read_json', read))
            stack.enter_context(patch.object(Path, 'open', opened))
            results = {}
            for mode in ('idle', 'active_gap'):
                counts.update(cursorReads=0, sessionOpens=0)
                start = time.perf_counter()
                cpu = time.process_time()
                for _ in range(ticks):
                    if mode == 'active_gap':
                        with files[0].open('a') as handle:
                            handle.write(line)
                        sqlite['tokens']['all'] += 20
                    module.codex_snapshot()
                results[mode] = {'seconds': round(time.perf_counter() - start, 4),
                                 'cpuSeconds': round(time.process_time() - cpu, 4), **counts}
            results['storeMiB'] = round(sum(p.stat().st_size for p in files) / 1024**2, 2)
            results['cursorMiB'] = round(module.CODEX_SESSIONS_CURSOR_FILE.stat().st_size / 1024**2, 2)
            return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='f3b4e3c')
    parser.add_argument('--size-mib', type=int, default=100)
    parser.add_argument('--files', type=int, default=10000)
    parser.add_argument('--ticks', type=int, default=60)
    parser.add_argument('--revision', choices=('before', 'after', 'both'), default='both')
    args = parser.parse_args()
    if min(args.size_mib, args.files, args.ticks) <= 0:
        parser.error('size, files, and ticks must be positive')
    before = subprocess.check_output(['git', 'show', f'{args.baseline_ref}:bin/agentcat'], cwd=ROOT)
    after = (ROOT / 'bin/agentcat').read_bytes()
    with tempfile.TemporaryDirectory(prefix='agentcat-codex-bench-') as temp:
        for name, source in [('before', before), ('after', after)]:
            if args.revision != 'both' and name != args.revision:
                continue
            result = run(source, Path(temp) / name, args.size_mib, args.files, args.ticks)
            print(json.dumps({'revision': name, 'ticksPerMode': args.ticks, **result}), flush=True)


if __name__ == '__main__':
    main()
