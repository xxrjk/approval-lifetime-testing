"""Regression, interleaving, compiled-target, and stateful baseline strategies.

fixed_then_interleaved runs three fixed regressions before generic interleaving.
directed_plus_interleaved alternates directed and interleaved histories.
compiled uses F1-F3 targets; compiled_full adds F4. hypothesis_flat draws an
enabled action through one rule with a 29-step sequence limit.

The strategies share the application declaration and seeded history generators.
Discovery stops at the first finding. Reported costs cover discovery; the added
Hypothesis configuration excludes post-discovery shrinking from measurement.
"""
from __future__ import annotations
import argparse, gzip, importlib.metadata, io, json, random, time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from hypothesis import HealthCheck, Phase, settings, seed as hypothesis_seed
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from approval_contract import Contract, histories
from contract_compiler import Declaration, synthesize, compiled_histories
from strong_baseline import Controller, BudgetExhausted, ContractViolation, FIXED
from rpc_backend import Backend

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ['fixed_then_interleaved', 'directed_plus_interleaved', 'compiled', 'compiled_full', 'hypothesis_flat']
APPLICATION = Contract(('object', 'content', 'authority'),
                       invalidations=(('edit_', ()), ('revoke', ('authority',))))
DECLARATION = Declaration()
WITNESSES = {'compiled': synthesize(DECLARATION, families=('F1', 'F2', 'F3'))[0],
             'compiled_full': synthesize(DECLARATION)[0]}


class ApplicationController(Controller):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contract = APPLICATION


def alternate(*streams):
    iters = [iter(s) for s in streams]
    while iters:
        for it in list(iters):
            try:
                yield next(it)
            except StopIteration:
                iters.remove(it)


def rows_for(strategy, seed, budget):
    generic = [r['events'] for r in histories('interleaved', seed, budget)]
    if strategy == 'fixed_then_interleaved':
        return [list(h) for h in FIXED] + generic
    if strategy == 'directed_plus_interleaved':
        return list(alternate([r['events'] for r in histories('contract', seed, budget)], generic))
    if strategy in WITNESSES:
        return [r['events'] for r in compiled_histories(DECLARATION, WITNESSES[strategy], seed, budget)]
    raise ValueError(strategy)


class FlatApprovalMachine(RuleBasedStateMachine):
    """Draw enabled operations through one rule using ApprovalMachine's action set."""
    def __init__(self, controller):
        super().__init__(); self.c = controller; self.value = 'a'; self.captured = set(); self.c.reset()

    @rule(data=st.data())
    def act(self, data):
        enabled = ['request_a', 'request_b', 'edit', 'revoke', 'activate', 'activate_batch']
        enabled += [f'complete_{ch}_{s}' for ch in sorted(self.captured) for s in ('pos', 'neg')]
        if self.c.observation['state'] != 'suspended':
            enabled.append('suspend')
        action = data.draw(st.sampled_from(enabled))
        if action == 'edit':
            self.value = 'b' if self.value == 'a' else 'a'; action = 'edit_' + self.value
        if action.startswith('request_'):
            self.captured.add(action.split('_')[1])
        self.c.step(action)

    @invariant()
    def declared_contract(self):
        self.c.assert_invariants()


def execute(backend, strategy, variant, field, seed, budget, writer, run_id):
    controller = ApplicationController(backend, variant, field, budget, writer, run_id, shrink_cap=0)
    status = 'budget_complete'
    try:
        if strategy == 'hypothesis_flat':
            factory = hypothesis_seed(seed)(lambda: FlatApprovalMachine(controller))
            config = settings(max_examples=1000, stateful_step_count=29, database=None, deadline=None,
                              phases=(Phase.generate, Phase.shrink),
                              suppress_health_check=list(HealthCheck), report_multiple_bugs=False)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                run_state_machine_as_test(factory, settings=config)
            status = 'exhausted_examples'
        else:
            for events in rows_for(strategy, seed, budget):
                controller.reset()
                for event in events:
                    controller.step(event); controller.assert_invariants()
    except BudgetExhausted:
        status = 'budget_censored' if controller.first is None else 'shrink_budget_censored'
    except ContractViolation:
        status = 'detected'
    elapsed = time.perf_counter() - controller.started
    if variant.endswith('_control'):
        assert controller.first is None, (strategy, variant, seed, controller.target)
        assert controller.valid_uses == controller.releases
    return {'run': run_id, 'variant': variant, 'field': field, 'strategy': strategy, 'seed': seed,
            'budget': budget, 'discovery_operations': controller.spent, 'first_operation': controller.first,
            'first_seconds': controller.first_wall, 'first_events': getattr(controller, 'first_events', None),
            'target_property': controller.target, 'status': status,
            'post_discovery_operations': controller.post_spent, 'total_seconds': elapsed,
            'target_seconds': controller.target_seconds, 'observation_seconds': controller.observe_seconds,
            'reset_seconds': controller.reset_seconds, 'check_seconds': controller.check_seconds,
            'bridge_call_seconds': controller.call_seconds,
            'sampled_peak_combined_rss_bytes': controller.peak_rss,
            'valid_uses': controller.valid_uses, 'reported_releases': controller.releases}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--flutter', default='flutter'); p.add_argument('--sqlite-cache')
    p.add_argument('--offline', action='store_true'); p.add_argument('--strategies', nargs='+', default=STRATEGIES)
    p.add_argument('--manifest', type=Path, default=ROOT/'cases.json')
    p.add_argument('--budget', type=int, default=300); p.add_argument('--output', default='extension')
    p.add_argument('--app', default='extension_app')
    a = p.parse_args()
    cases = json.loads(a.manifest.read_text())['included']
    schedule = [(case, s, seed) for case in cases for s in a.strategies
                for seed in range(15000, 15000 + case.get('seeds', 30))]
    random.Random(20261001).shuffle(schedule)
    out = ROOT/'results/extension'; out.mkdir(parents=True, exist_ok=True)
    with Backend(a.flutter, a.sqlite_cache, a.offline, name=a.app) as backend, \
            gzip.open(out/f'{a.output}.observations.jsonl.gz', 'wt', compresslevel=3) as writer, \
            (out/f'{a.output}.runs.jsonl').open('w') as results:
        for case, strategy, seed in schedule:
            run_id = f'{case["variant"]}-{case["field"]}-{strategy}-{seed}'
            r = execute(backend, strategy, case['variant'], case['field'], seed, a.budget, writer, run_id)
            results.write(json.dumps(r) + '\n'); results.flush()
        startup = backend.startup_seconds
    status = {'success': True, 'runs': len(schedule), 'strategies': a.strategies,
              'hypothesis_version': importlib.metadata.version('hypothesis'), 'core': 'core_v3',
              'bridge_startup_seconds': startup, 'all_controls_clean': True,
              'protocol': 'EXTENSION_PROTOCOL.md', 'experimental_model_calls': 0, 'new_human_annotations': 0}
    (out/f'{a.output}.status.json').write_text(json.dumps(status, indent=2) + '\n')
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    main()
