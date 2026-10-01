from dataclasses import asdict
import copy
import unittest
from causal_schedule import compile_schedule,LoweringRejected
from certificate_verifier import verify,CertificateRejected
from test_causal_schedule import NATIVE


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.events=['request_a','complete_a_pos','request_b','complete_b_neg']
        self.cert=compile_schedule(self.events,NATIVE)

    def test_independent_valid_certificate(self):
        self.assertTrue(verify(self.cert,asdict(NATIVE),self.events))

    def test_payload_equality_cannot_merge_origins(self):
        c=copy.deepcopy(self.cert)
        c['semantic_projection']['effects'][-1][1]['incoming_capture']='request_a'
        with self.assertRaisesRegex(CertificateRejected,'semantic projection'):
            verify(c,asdict(NATIVE),self.events)

    def test_certificate_cannot_choose_its_own_native_gate(self):
        c=copy.deepcopy(self.cert);c['native_capture_states'].append('approved')
        with self.assertRaisesRegex(CertificateRejected,'native gate'):
            verify(c,asdict(NATIVE),self.events)

    def test_false_safe_boolean_does_not_override_replay(self):
        c=copy.deepcopy(self.cert);c['schedule']=list(self.events);c['swaps']=[]
        with self.assertRaisesRegex(CertificateRejected,'native capture disabled'):
            verify(c,asdict(NATIVE),self.events)

    def test_undeclared_channel_is_not_an_enabled_operation(self):
        with self.assertRaisesRegex(LoweringRejected,'operation alphabet'):
            compile_schedule(['request_z','complete_z_pos'],NATIVE)

    def test_unregistered_verdict_is_rejected(self):
        with self.assertRaisesRegex(LoweringRejected,'operation alphabet'):
            compile_schedule(['request_a','complete_a_typo'],NATIVE)


if __name__=='__main__':unittest.main()
