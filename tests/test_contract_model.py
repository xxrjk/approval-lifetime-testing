"""Truth and abstraction tests independent of fault outcomes."""
from dataclasses import replace
import unittest

from contract_model import Declaration, initial, step, key, synthesize, valid
from model_baselines import histories


def run(events, declaration=Declaration()):
    state = initial(declaration)
    for event in events:
        operation = 'edit' if event.startswith('edit_') else event
        result = step(state, operation, declaration)
        assert result is not None, event
        state = result[0]
    return state


class ModelTests(unittest.TestCase):
    def test_default_matches_existing_reached_scope(self):
        _, inventory = synthesize(Declaration())
        self.assertEqual(23, sum(t['status'] == 'reached' for t in inventory['targets']))
        self.assertTrue(all(t['status'] == 'capability_filtered' for t in inventory['targets']
                            if 'dim:object' in t['name'] or 'input' in t['name']))

    def test_malformed_targets_are_really_emitted(self):
        declaration = Declaration(malformed_inputs=True)
        witnesses, inventory = synthesize(declaration)
        for sign in ('pos', 'neg'):
            name = f'preserve[input|{sign}]'
            self.assertTrue(witnesses[name])
            self.assertTrue(any('bad' in event for event in witnesses[name][0]))
        self.assertFalse(any(t['status'] == 'bounded_unreached' for t in inventory['targets']))

    def test_object_identity_is_not_payload_identity(self):
        declaration = Declaration(object_count=2)
        witnesses, _ = synthesize(declaration)
        self.assertTrue(witnesses['preserve[dim:object|neg]'])
        self.assertTrue(any(event.startswith('select_')
                            for event in witnesses['preserve[dim:object|neg]'][0]))

    def test_old_and_current_receipts_do_not_merge(self):
        a = run(['request_a', 'complete_a_pos', 'request_b'])
        b = run(['request_a', 'request_b', 'complete_b_pos'])
        self.assertNotEqual(key(a, Declaration()), key(b, Declaration()))

    def test_consistent_counter_renaming_preserves_state(self):
        state = run(['request_a', 'revoke', 'request_b', 'complete_b_pos'])
        objects = tuple(replace(o, authority=o.authority + 100,
                                approval=None if o.approval is None else replace(o.approval,
                                receipt=replace(o.approval.receipt, authority=o.approval.receipt.authority + 100,
                                                identifier=o.approval.receipt.identifier + 20)))
                        for o in state.objects)
        renamed = replace(state, objects=objects, next_identifier=state.next_identifier + 20,
                          slots=tuple((ch, replace(r, authority=r.authority + 100,
                                                   identifier=r.identifier + 20)) for ch, r in state.slots))
        self.assertEqual(key(state, Declaration()), key(renamed, Declaration()))

    def test_value_restore_and_revision_differ(self):
        events = ['request_a', 'edit_b', 'edit_a', 'complete_a_pos']
        value = Declaration()
        revision = Declaration(dimensions=('object', 'content', 'authority', 'revision'), identity='revision')
        self.assertTrue(valid(run(events, value), value))
        self.assertFalse(valid(run(events, revision), revision))

    def test_latest_request_supersedes_equal_value_origin(self):
        latest = Declaration(dimensions=('object', 'content', 'authority', 'latest'), identity='latest')
        self.assertFalse(valid(run(['request_a', 'request_b', 'complete_a_pos'], latest), latest))
        self.assertTrue(valid(run(['request_a', 'request_b', 'complete_b_pos'], latest), latest))

    def test_native_http_requests_complete_once(self):
        declaration = Declaration(dimensions=('object', 'content', 'purpose'),
            states=('pending', 'approved', 'rejected'), consumed_state='approved',
            activation_states=(), batch_states=(), perturb=(), lifecycle=(),
            positive_completion_state='approved', negative_completion_state='rejected',
            consume_receipt_on_completion=True)
        witnesses, _ = synthesize(declaration, families=('F1',), max_depth=6)
        events = witnesses['preserve[state:approved|neg]'][0]
        self.assertEqual(4, len(events))
        self.assertEqual(2, sum(e.startswith('request_') for e in events))

    def test_all_general_generators_cover_both_entries(self):
        for strategy in ('balanced', 'uniform_model', 'state_coverage', 'edge_coverage', 'pair_coverage'):
            events = [e for row in histories(strategy, Declaration(), 16000, 300) for e in row['events']]
            self.assertIn('activate', events, strategy)
            self.assertIn('activate_batch', events, strategy)
            self.assertIn('complete_a_pos', events, strategy)
            self.assertLessEqual(sum(1 + len(row['events']) for row in
                                      histories(strategy, Declaration(), 16000, 300)), 300)

    def test_policy_change_changes_target_manifest(self):
        _, pending = synthesize(Declaration())
        _, suspended = synthesize(Declaration(completion_states=('pending', 'suspended')))
        self.assertTrue(any('state:suspended' in t['name'] for t in pending['targets']))
        self.assertFalse(any('state:suspended' in t['name'] for t in suspended['targets']))


if __name__ == '__main__':
    unittest.main()
