import unittest
from dataclasses import replace
from causal_schedule import compile_schedule, certify_reordering, LoweringRejected
from contract_model import Declaration

NATIVE = Declaration(dimensions=('object', 'content', 'purpose'),
    states=('pending', 'approved', 'rejected'), completion_states=('pending',),
    activation_states=(), batch_states=(), consumed_state='approved', perturb=(), lifecycle=(),
    positive_completion_state='approved', negative_completion_state='rejected',
    consume_receipt_on_completion=True)


class SchedulingTests(unittest.TestCase):
    def test_pending_route_needs_checked_hoist(self):
        raw = ['request_a', 'complete_a_pos', 'request_b', 'complete_b_neg']
        cert = compile_schedule(raw, NATIVE)
        self.assertEqual(cert['schedule'], ['request_a', 'request_b', 'complete_a_pos', 'complete_b_neg'])
        self.assertEqual(len(cert['swaps']), 1)
        self.assertEqual(cert['semantic_projection']['effects'][-1][1]['why'], ('state:approved',))

    def test_latest_identity_cannot_be_hoisted(self):
        # Both abstract histories are enabled and payloads agree. Starting B
        # changes A's authority, which the old event-string check missed.
        decl = replace(Declaration(), identity='latest', dimensions=('object', 'content', 'latest'))
        with self.assertRaisesRegex(LoweringRejected, 'authority or protected effect'):
            certify_reordering(['request_a', 'complete_a_pos', 'request_b', 'complete_b_neg'],
                ['request_a', 'request_b', 'complete_a_pos', 'complete_b_neg'], decl)

    def test_same_channel_cannot_overwrite_live_http(self):
        with self.assertRaisesRegex(LoweringRejected, 'exactly one'):
            compile_schedule(['request_a', 'request_a', 'complete_a_pos'], NATIVE)

    def test_completion_order_is_part_of_target(self):
        with self.assertRaisesRegex(LoweringRejected, 'completion order'):
            certify_reordering(['request_a', 'request_b', 'complete_a_pos', 'complete_b_neg'],
                ['request_a', 'request_b', 'complete_b_neg', 'complete_a_pos'], NATIVE)

    def test_unsupported_operation_refused(self):
        with self.assertRaisesRegex(LoweringRejected, 'registered'):
            compile_schedule(['request_a', 'revoke', 'complete_a_pos'], NATIVE)


if __name__ == '__main__':
    unittest.main()
