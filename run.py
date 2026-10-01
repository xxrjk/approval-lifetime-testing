"""Run the executable method and experiments from source."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def run(script, *args):
    subprocess.run([sys.executable, str(ROOT/'src'/script), *args], cwd=ROOT, check=True)


def option_value(options, name, default):
    for i, option in enumerate(options):
        if option == name:
            return options[i+1]
        if option.startswith(name+'='):
            return option.split('=', 1)[1]
    return default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', choices=['check', 'smoke', 'native', 'spec',
                                        'matched', 'fair', 'mutants', 'transfer'])
    parser.add_argument('options', nargs=argparse.REMAINDER,
                        help='Options are passed to the selected experiment; use TASK --help.')
    args = parser.parse_args()
    options = args.options
    if args.task == 'check':
        if options:
            parser.error('check takes no additional options')
        env = dict(os.environ, PYTHONPATH=str(ROOT/'src'))
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                        '-p', 'test_*.py'], cwd=ROOT, env=env, check=True)
        return
    for folder in ('fair', 'spec', 'matched', 'certificates', 'causal', 'mutation', 'transfer'):
        (ROOT/'results'/folder).mkdir(parents=True, exist_ok=True)
    if args.task == 'native':
        if options:
            parser.error('native takes no additional options')
        for script in ('wagtail_causal.py', 'verify_native_targets.py',
                       'validate_causal.py', 'certificate_faults.py'):
            run(script)
    elif args.task in ('fair', 'matched', 'spec'):
        if not any(x == '--phase' or x.startswith('--phase=') for x in options):
            options = ['--phase', 'confirmation', *options]
        # The selected runner validates the full option set before analysis.
        runner = {'fair': 'fair_comparison.py', 'matched': 'run_matched.py',
                  'spec': 'run_spec.py'}[args.task]
        run(runner, *options)
        if '--help' in options or '-h' in options:
            return
        phase = option_value(options, '--phase', 'confirmation')
        prefix = (args.task+'_' if args.task != 'fair' else '') + phase
        if args.task == 'fair':
            prefix = option_value(options, '--output', prefix)
        run('analyze_runs.py', '--prefix', prefix)
        if args.task == 'spec':
            run('check_spec.py', '--prefix', prefix)
    else:
        run({'smoke': 'smoke.py', 'mutants': 'mutation_study.py',
             'transfer': 'run_transfer.py'}[args.task], *options)


if __name__ == '__main__':
    main()
