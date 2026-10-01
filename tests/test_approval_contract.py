import copy
import unittest
from approval_contract import (Contract, OriginRegistry, valid_approval,
                               check_transition, alarm, histories, may_replace, eligible)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract=Contract(('content','authority','purpose'))
        self.origins={'p':{'content':'A','authority':0,'purpose':'review'}}
        self.good={'context':dict(self.origins['p']),'state':'pending',
                   'approval':{'origin':'p','positive':True,'local_valid':True}}

    def test_valid_and_independent_dimensions(self):
        self.assertTrue(valid_approval(self.good,self.origins,self.contract))
        for field,value in [('content','B'),('authority',1),('purpose','publish')]:
            row=copy.deepcopy(self.good); row['context'][field]=value
            self.assertFalse(valid_approval(row,self.origins,self.contract))

    def test_refusal_preservation_and_mask(self):
        after=copy.deepcopy(self.good);after['approval']=None
        record={'kind':'complete','incoming_origin':'missing','applied':False,'before':self.good,'after':after}
        findings=check_transition(record,self.origins,self.contract)
        self.assertEqual(findings,['approval_preservation'])
        self.assertTrue(alarm(findings,True));self.assertFalse(alarm(findings,False))

    def test_log_writes_allowed_and_applied_negative_exempt(self):
        after=copy.deepcopy(self.good);after['log']=['refused obsolete work']
        record={'kind':'complete','incoming_origin':'missing','applied':False,'before':self.good,'after':after}
        self.assertEqual(check_transition(record,self.origins,self.contract),[])
        after['approval']=None;record['applied']=True;record['incoming_origin']='p'
        self.assertEqual(check_transition(record,self.origins,self.contract),[])

    def test_destructive_completion_lying_about_applied(self):
        after=copy.deepcopy(self.good);after['approval']=None
        for applied in [False, True, None]:
            record={'kind':'complete','incoming_origin':'unknown','applied':applied,
                    'before':self.good,'after':after}
            self.assertEqual(check_transition(record,self.origins,self.contract),
                             ['approval_preservation'])

    def test_may_replace_truth_table_not_return_flag(self):
        # Expected values come from the independently declared truth table.
        cases=[('pending','p',True,True),('pending','unknown',True,False),
               ('suspended','p',True,False),('active','p',True,False),
               ('pending','p',False,False)]
        for state,origin,incoming_valid,expected in cases:
            before=copy.deepcopy(self.good);before['state']=state
            for applied in [False,True]:
                record={'kind':'complete','before':before,'after':before,
                        'incoming_origin':origin,'incoming_valid':incoming_valid,'applied':applied}
                self.assertEqual(bool(may_replace(record,self.origins,self.contract)),expected)

    def test_no_current_approval_is_not_a_clobber(self):
        before=copy.deepcopy(self.good);before['approval']=None
        record={'kind':'complete','incoming_origin':'missing','applied':True,
                'before':before,'after':copy.deepcopy(before)}
        self.assertEqual(check_transition(record,self.origins,self.contract),[])

    def test_suspended_approval_is_valid_but_not_batch_eligible(self):
        row=copy.deepcopy(self.good);row['state']='suspended'
        self.assertTrue(valid_approval(row,self.origins,self.contract))
        self.assertFalse(eligible(row,self.origins,self.contract,'batch'))
        self.assertTrue(eligible(row,self.origins,self.contract,'single'))

    def test_preserve_declared_lifecycle_state(self):
        after=copy.deepcopy(self.good);after['state']='suspended'
        record={'kind':'complete','incoming_origin':'missing','applied':True,
                'before':self.good,'after':after}
        self.assertEqual(check_transition(record,self.origins,self.contract),['refusal_state_preservation'])
        self.assertFalse(alarm(['refusal_state_preservation'],False))

    def test_immutable_origin_registry_deep_copy(self):
        registry=OriginRegistry();value={'content':{'evidence':['A']},'authority':0}
        registry.register('q',value)
        value['content']['evidence'].append('B');value['authority']=1
        exposed=registry['q'];exposed['content']['evidence'].append('C')
        self.assertEqual(registry['q'],{'content':{'evidence':['A']},'authority':0})
        with self.assertRaises(ValueError):registry.register('q',value)

    def test_restored_values_do_not_restore_authority(self):
        before=copy.deepcopy(self.good);before['context']['authority']=1
        record={'before':before,'incoming_origin':'p'}
        self.assertFalse(may_replace(record,self.origins,self.contract))
        self.assertFalse(valid_approval(before,self.origins,self.contract))

    def test_purpose_is_checked_only_when_declared(self):
        before=copy.deepcopy(self.good);before['context']['purpose']='publish'
        self.assertFalse(valid_approval(before,self.origins,self.contract))
        self.assertTrue(valid_approval(before,self.origins,Contract(('content','authority'))))

    def test_admission_and_batch_state(self):
        before=copy.deepcopy(self.good);before['state']='suspended'
        after=copy.deepcopy(before);after['state']='active'
        record={'kind':'activate','released':True,'entry':'batch','before':before,'after':after}
        self.assertIn('unqualified_admission',check_transition(record,self.origins,self.contract))
        record['entry']='single'
        self.assertEqual(check_transition(record,self.origins,self.contract),[])

    def test_generator_budget_and_captured_completions(self):
        for strategy in ['enabled_random','interleaved','contract','action_matched']:
            for seed in range(8):
                rows=histories(strategy,seed,300)
                self.assertEqual(rows,histories(strategy,seed,300))
                self.assertIn(sum(1+len(r['events']) for r in rows),[299,300])
                for row in rows:
                    captured=set()
                    for event in row['events']:
                        if event.startswith('request_'):captured.add(event.split('_')[1])
                        if event.startswith('complete_'):
                            self.assertIn(event.split('_')[1],captured)

    def test_action_matched_histories(self):
        from collections import Counter
        for seed in range(8):
            directed=histories('contract',seed,300)
            shuffled=histories('action_matched',seed,300)
            self.assertEqual(len(directed),len(shuffled))
            for a,b in zip(directed,shuffled):
                self.assertEqual(a['offset'],b['offset'])
                self.assertEqual(Counter(a['events']),Counter(b['events']))


class EffectObligationTests(unittest.TestCase):
    """Truth tables for qualified-use, completion, and invalidation obligations."""
    def setUp(self):
        self.c=Contract(('content','authority'),invalidations=(('edit_',()),('revoke',('authority',))))
        self.origins={'p':{'content':'A','authority':0},'q':{'content':'B','authority':0}}
        self.good={'context':{'content':'A','authority':0},'state':'pending',
                   'approval':{'origin':'p','positive':True,'local_valid':True}}

    def activation(self,before_state,after_state,released_flag,entry='single'):
        before=copy.deepcopy(self.good);before['state']=before_state
        after=copy.deepcopy(before);after['state']=after_state
        return {'kind':'activate','entry':entry,'released':released_flag,'before':before,'after':after}

    def test_admission_uses_persisted_transition_not_flag(self):
        record=self.activation('active','active',True)          # lying flag, no transition
        record['before']['approval']=None;record['after']['approval']=None
        self.assertEqual(check_transition(record,self.origins,self.c),['active_state_validity'])
        record=self.activation('pending','active',False)        # hidden release
        record['before']['approval']=None;record['after']['approval']=None
        self.assertIn('unqualified_admission',check_transition(record,self.origins,self.c))

    def test_qualified_use_truth_table(self):
        cases=[('pending','pending','single',['qualified_refusal']),
               ('suspended','suspended','single',['qualified_refusal']),
               ('suspended','suspended','batch',[]),               # batch not eligible
               ('pending','active','batch',[]),
               ('pending','active','single',[])]
        for before,after,entry,expected in cases:
            record=self.activation(before,after,after=='active',entry)
            self.assertEqual(check_transition(record,self.origins,self.c),expected,(before,after,entry))
        record=self.activation('pending','pending',False)
        record['before']['approval']=None;record['after']['approval']=None
        self.assertEqual(check_transition(record,self.origins,self.c),[])  # nothing to consume

    def test_applicable_completion_truth_table(self):
        before=copy.deepcopy(self.good);before['approval']=None
        applied=copy.deepcopy(self.good)
        for event,after,expected in [('complete_a_pos',applied,[]),
                                     ('complete_a_pos',before,['applicable_completion']),
                                     ('complete_a_neg',applied,['applicable_completion']),
                                     ('complete_a_neg',before,[])]:
            record={'kind':'complete','event':event,'incoming_origin':'p','applied':None,
                    'before':before,'after':after}
            self.assertEqual(check_transition(record,self.origins,self.c),expected,event)
        # A positive whose stored origin is another packet did not take effect.
        other=copy.deepcopy(self.good);other['approval']['origin']='q';other['context']['content']='A'
        record={'kind':'complete','event':'complete_a_pos','incoming_origin':'p','before':before,'after':other}
        self.assertEqual(check_transition(record,self.origins,self.c),['applicable_completion'])
        # Unknown sign: obligation not evaluated.
        record={'kind':'complete','incoming_origin':'p','before':before,'after':before}
        self.assertEqual(check_transition(record,self.origins,self.c),[])
        # Declared incoming_positive overrides a missing event name.
        record['incoming_positive']=True
        self.assertEqual(check_transition(record,self.origins,self.c),['applicable_completion'])

    def test_liveness_disabled_restores_v2(self):
        c=Contract(('content','authority'),liveness=False)
        self.assertEqual(check_transition(self.activation('pending','pending',False),self.origins,c),[])

    def test_invalidation_effects(self):
        before=copy.deepcopy(self.good);before['state']='active'
        reset=copy.deepcopy(before);reset['state']='pending';reset['approval']=None
        stay=copy.deepcopy(before);stay['approval']=None
        for event,after,expected in [('edit_b',reset,[]),('edit_b',stay,['invalidation_effect','active_state_validity']),
                                     ('suspend',before,[])]:
            record={'kind':'other','event':event,'before':before,'after':after}
            self.assertEqual(check_transition(record,self.origins,self.c),expected,event)
        advanced=copy.deepcopy(reset);advanced['context']['authority']=1
        record={'kind':'other','event':'revoke','before':before,'after':advanced}
        self.assertEqual(check_transition(record,self.origins,self.c),[])
        record['after']=reset                                   # epoch not advanced
        self.assertEqual(check_transition(record,self.origins,self.c),['invalidation_effect'])

    def test_masks(self):
        self.assertFalse(alarm(['qualified_refusal'],True,liveness=False))
        self.assertFalse(alarm(['invalidation_effect'],True,effects=False))
        self.assertTrue(alarm(['unqualified_admission'],False,liveness=False,effects=False))


if __name__=='__main__':unittest.main()
