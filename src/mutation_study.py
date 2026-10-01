"""Mechanical mutants of the real persistence code under every strategy and oracle level.

Each mutant replaces db.dart or audit_binding.dart inside an isolated copy of the
bridge app. The bridge variant is `digest_control`: the real application path
without harness-level fault injection. Every run records the first discovery
operation of every finding type and continues to the end of its budget, so kill
status under each oracle level is computed from the same executions.
"""
from __future__ import annotations
import argparse, io, json, shutil, socket, subprocess, time, traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import psutil
from hypothesis import HealthCheck, Phase, settings, seed as hypothesis_seed
from hypothesis.stateful import run_state_machine_as_test
from approval_contract import histories
from strong_baseline import BudgetExhausted, ApprovalMachine, fixed_histories
from extension_baseline import ApplicationController, FlatApprovalMachine, rows_for
import mutants as MUT

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ['answer', 'question', 'evidence', 'source']
STOCHASTIC = ['contract', 'interleaved', 'enabled_random', 'fixed_then_interleaved',
              'compiled', 'compiled_full', 'hypothesis', 'hypothesis_flat']
SEEDS = range(15000, 15005)


class Bridge:
    """Same launch protocol as rpc_backend.Backend, with mutated sources written into lib/."""
    def __init__(self, name, sources, flutter, cache):
        self.app = ROOT/'tmp'/name
        first = not (self.app/'.dart_tool').exists()
        self.app.mkdir(parents=True, exist_ok=True)
        template = ROOT/'artifact_templates/pubspec.yaml'
        shutil.copy2(template if template.exists() else ROOT/'app/pubspec.yaml', self.app/'pubspec.yaml')
        shutil.copy2(ROOT/'app/pubspec.lock', self.app/'pubspec.lock')
        for folder, names in [('lib', ['db.dart', 'models.dart', 'audit_binding.dart', 'fsrs.dart', 'stats.dart']),
                              ('test', ['approval_rpc_test.dart', 'effects_test.dart',
                                        'lifecycle_test.dart', 'admission_test.dart'])]:
            (self.app/folder).mkdir(exist_ok=True)
            for n in names:
                shutil.copy2(ROOT/'app'/folder/n, self.app/folder/n)
        for fname, text in sources.items():
            (self.app/'lib'/fname).write_text(text)
        if first:
            if cache:
                target = self.app/'.dart_tool/hooks_runner/shared/sqlite3/build'
                for p in Path(cache).glob('download-*'):
                    if p.is_dir():
                        shutil.copytree(p, target/p.name, dirs_exist_ok=True)
            subprocess.run([flutter, 'pub', 'get', '--offline'], cwd=self.app, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.log = (self.app/'bridge.log').open('w')
        ready = self.app/'ready.json'
        if ready.exists():
            ready.unlink()
        started = time.perf_counter()
        self.process = subprocess.Popen([flutter, 'test', '--no-pub', '--reporter', 'expanded',
                                         '--dart-define=RPC_READY=' + str(ready), 'test/approval_rpc_test.dart'],
                                        cwd=self.app, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 240
        port = None
        while port is None:
            if self.process.poll() is not None:
                raise RuntimeError('STILLBORN: ' + (self.app/'bridge.log').read_text()[-3000:])
            if time.monotonic() > deadline:
                self.process.kill(); raise TimeoutError('bridge did not start')
            if ready.exists():
                try:
                    port = json.loads(ready.read_text())['port']
                except (json.JSONDecodeError, KeyError):
                    pass
            time.sleep(.05)
        self.startup = time.perf_counter() - started
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=30)
        self.stream = self.socket.makefile('rwb'); self.sut_exceptions = 0

    def call(self, command):
        self.stream.write((json.dumps(command) + '\n').encode()); self.stream.flush()
        line = self.stream.readline()
        if not line:
            raise RuntimeError('bridge closed')
        result = json.loads(line)
        if not result['ok']:
            self.sut_exceptions += 1
            raise RuntimeError('SUT_EXCEPTION: ' + str(result.get('error'))[:300])
        return result

    def rss_bytes(self):
        return psutil.Process().memory_info().rss

    def close(self):
        try:
            self.call({'op': 'shutdown'})
        except Exception:
            pass
        for closer in (lambda: self.stream.close(), lambda: self.socket.close()):
            try:
                closer()
            except Exception:
                pass
        try:
            self.process.wait(timeout=20)
        except Exception:
            self.process.kill()
        self.log.close()


class RecordingController(ApplicationController):
    """Record each finding's first operation and continue to the execution budget."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs); self.first_by_type = {}

    def assert_invariants(self):
        if self.last is None:
            return
        _, findings = self.last
        for item in findings:
            self.first_by_type.setdefault(item, self.spent)


def run_one(bridge, strategy, field, seed, budget=300):
    c = RecordingController(bridge, 'digest_control', field, budget, io.StringIO(), f'{strategy}-{field}-{seed}', shrink_cap=0)
    before = bridge.sut_exceptions; aborted_histories = 0
    try:
        if strategy in ('hypothesis', 'hypothesis_flat'):
            machine = ApprovalMachine if strategy == 'hypothesis' else FlatApprovalMachine
            config = settings(max_examples=100000, stateful_step_count=29, database=None, deadline=None,
                              phases=(Phase.generate,), suppress_health_check=list(HealthCheck),
                              report_multiple_bugs=False)
            attempt = 0
            while True:   # a SUT exception ends one Hypothesis test; continue until the budget ends
                factory = hypothesis_seed(seed * 1000 + attempt)(lambda: machine(c))
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        run_state_machine_as_test(factory, settings=config)
                    break
                except Exception:   # SUT exception or a Hypothesis error it causes; BudgetExhausted still ends the run
                    aborted_histories += 1; attempt += 1
        else:
            if strategy == 'fixed_regression':
                rows = list(fixed_histories(budget))
            elif strategy in ('contract', 'interleaved', 'enabled_random'):
                rows = [r['events'] for r in histories(strategy, seed, budget)]
            else:
                rows = rows_for(strategy, seed, budget)
            for events in rows:
                c.reset()
                try:
                    for e in events:
                        c.step(e); c.assert_invariants()
                except RuntimeError:
                    aborted_histories += 1; c.last = None
    except BudgetExhausted:
        pass
    return {'strategy': strategy, 'field': field, 'seed': seed, 'first_by_type': c.first_by_type,
            'operations': c.spent, 'valid_uses': c.valid_uses, 'releases': c.releases,
            'sut_exceptions': bridge.sut_exceptions - before, 'aborted_histories': aborted_histories}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--set', choices=['A', 'B'], required=True)
    ap.add_argument('--worker', type=int, required=True); ap.add_argument('--workers', type=int, required=True)
    ap.add_argument('--include-original', action='store_true')
    ap.add_argument('--flutter', default='flutter'); ap.add_argument('--sqlite-cache')
    ap.add_argument('--only', nargs='*', default=None)
    a = ap.parse_args()
    base = {'db.dart': (ROOT/'app/lib/db.dart').read_text(),
            'audit_binding.dart': (ROOT/'app/lib/audit_binding.dart').read_text()}
    mutants = MUT.set_a() if a.set == 'A' else MUT.statement_deletion_mutants()
    todo = [m for k, m in enumerate(mutants) if k % a.workers == a.worker]
    if a.only:
        todo = [m for m in mutants if m['id'] in a.only]
    if a.include_original:
        todo = [{'id': 'ORIG', 'file': None, 'op': 'none', 'function': None, 'line': None, 'edits': {}}] + todo
    out = ROOT/'results/mutation'; out.mkdir(parents=True, exist_ok=True)
    path = out/f'set{a.set}_worker{a.worker}.jsonl'
    done = set()
    if path.exists():
        done = {json.loads(l)['id'] for l in path.open() if json.loads(l).get('outcome') == 'ran'}
    with path.open('a') as f:
        for m in todo:
            if m['id'] in done:
                continue
            sources = dict(base)
            if m['file']:
                sources[m['file']] = MUT.apply(m, base[m['file']])
            t0 = time.perf_counter(); rec = {k: m[k] for k in ['id', 'file', 'op', 'function', 'line']}
            try:
                b = Bridge(f'mutation_{a.set}{a.worker}', sources, a.flutter, a.sqlite_cache)
            except Exception as exc:
                rec.update(outcome='stillborn', error=str(exc)[-600:], seconds=time.perf_counter() - t0)
                f.write(json.dumps(rec) + '\n'); f.flush(); print(m['id'], 'stillborn', flush=True); continue
            runs = []
            try:
                for field in FIELDS:
                    runs.append(run_one(b, 'fixed_regression', field, 15000))
                    for s in STOCHASTIC:
                        for seed in SEEDS:
                            runs.append(run_one(b, s, field, seed))
            except Exception:
                rec['runner_error'] = traceback.format_exc()[-800:]
            finally:
                b.close()
            rec.update(outcome='ran', startup_seconds=b.startup, runs=runs, seconds=time.perf_counter() - t0)
            f.write(json.dumps(rec) + '\n'); f.flush()
            found = sorted({t for r in runs for t in r['first_by_type']})
            print(m['id'], m['op'], m['function'], found, round(rec['seconds'], 1), flush=True)


if __name__ == '__main__':
    main()
