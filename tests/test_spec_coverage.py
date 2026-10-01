import unittest
from collections import deque
from approval_contract import check_transition
from fair_comparison import APPLICATION,DECLARATION
from contract_model import initial,enabled,key
from specification_coverage import monitor,model_record,features


class SpecificationTests(unittest.TestCase):
    def test_model_all_obligations_and_corrupted_effects(self):
        queue=deque([(initial(DECLARATION),0)]);seen=set();count=0
        while queue:
            before,depth=queue.popleft()
            if depth==5:continue
            for op,(after,event,info) in enabled(before,DECLARATION):
                record,origins=model_record(before,after,event,info,DECLARATION)
                self.assertEqual(monitor(record,origins,APPLICATION),sorted(set(check_transition(record,origins,APPLICATION))))
                # Independently corrupt effects while retaining their precondition;
                # exercise preservation, admission, liveness and invalidation cases.
                for state in DECLARATION.states:
                    bad=dict(record,after=dict(record['after'],state=state,approval=None))
                    self.assertEqual(monitor(bad,origins,APPLICATION),sorted(set(check_transition(bad,origins,APPLICATION))))
                count+=1
                marker=key(after,DECLARATION)
                if marker not in seen:seen.add(marker);queue.append((after,depth+1))
        self.assertGreater(count,1000)

    def test_preservation_antecedent_visible(self):
        from contract_model import step
        s=initial(DECLARATION)
        for op in ('request_a','edit','request_b','complete_b_pos'):
            s=step(s,op,DECLARATION)[0]
        after,event,info=step(s,'complete_a_neg',DECLARATION)
        record,origins=model_record(s,after,event,info,DECLARATION)
        f=features(record,origins,APPLICATION)
        self.assertTrue(any('old_bind:content' in repr(x) and 'incoming_bind:content' in repr(x)
                            for x in f['spec_cubes']))


if __name__=='__main__':unittest.main()
