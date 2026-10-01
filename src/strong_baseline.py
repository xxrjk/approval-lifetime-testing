"""Budget-matched live stateful testing, including actual Hypothesis shrinking."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout, redirect_stderr
import gzip
import importlib.metadata
import io
import json
import random
from pathlib import Path
import time
from hypothesis import HealthCheck, Phase, settings, seed as hypothesis_seed
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, precondition, rule, run_state_machine_as_test
from approval_contract import Contract, OriginRegistry, check_transition, histories, eligible
from rpc_backend import Backend

ROOT=Path(__file__).resolve().parents[1]
STRATEGIES=['enabled_random','interleaved','action_matched','contract','hypothesis','fixed_regression']
FIXED=[['request_b','edit_b','complete_b_pos','activate_batch'],
       ['request_b','edit_b','request_a','complete_a_pos','complete_b_pos'],
       ['request_b','revoke','complete_b_pos','activate']]


class BudgetExhausted(KeyboardInterrupt):
    """Signal that the discovery or post-discovery resource budget is exhausted."""


class ContractViolation(AssertionError):
    pass


class Controller:
    def __init__(self,backend,variant,field,budget,writer,run_id,shrink_cap=5000):
        self.backend=backend;self.variant=variant;self.field=field;self.budget=budget
        self.writer=writer;self.run_id=run_id;self.shrink_cap=shrink_cap
        self.contract=Contract(('object','content','authority'))
        self.spent=0;self.post_spent=0;self.first=None;self.target=None
        self.trace=-1;self.events=[];self.origins=OriginRegistry();self.last=None
        self.started=time.perf_counter();self.first_wall=None
        self.call_seconds=0.;self.target_seconds=0.;self.observe_seconds=0.
        self.reset_seconds=0.;self.check_seconds=0.;self.peak_rss=backend.rss_bytes()
        self.valid_uses=0;self.releases=0;self.failing_histories=[]
        self.last_failing_events=None;self.discover_elapsed=None

    def charge(self):
        if self.first is None:
            if self.spent>=self.budget:raise BudgetExhausted('discovery')
            self.spent+=1
        else:
            if self.post_spent>=self.shrink_cap:raise BudgetExhausted('post-discovery')
            self.post_spent+=1

    def call(self,request):
        started=time.perf_counter();result=self.backend.call(request)
        self.call_seconds+=time.perf_counter()-started
        self.target_seconds+=result.get('target_us',0)/1e6
        self.observe_seconds+=result.get('observation_us',0)/1e6
        if request['op']=='reset':self.reset_seconds+=result.get('target_us',0)/1e6
        if (self.spent+self.post_spent)%30==0:self.peak_rss=max(self.peak_rss,self.backend.rss_bytes())
        return result

    def reset(self):
        self.charge();result=self.call({'op':'reset','variant':self.variant,'field':self.field})
        self.trace+=1;self.events=[];self.origins=OriginRegistry();self.last=None
        self.observation=result['observation']
        self.writer.write(json.dumps({'run':self.run_id,'trace':self.trace,'reset':True,
                                     'phase':'discovery' if self.first is None else 'post_discovery',
                                     'observation':self.observation,'target_us':result['target_us']})+'\n')

    def step(self,event):
        self.charge();phase='discovery' if self.first is None else 'post_discovery'
        result=self.call({'op':'step','event':event});record=result['record']
        if self.last is not None:assert record['before']==self.observation
        self.events.append(event)
        if 'capture' in record:
            self.origins.register(record['capture']['id'],record['capture']['context'])
        started=time.perf_counter();findings=check_transition(record,self.origins,self.contract)
        if record['kind']=='activate' and record['released']:
            self.releases+=1
            self.valid_uses+=eligible(record['before'],self.origins,self.contract,record['entry'])
        self.check_seconds+=time.perf_counter()-started
        self.observation=record['after'];self.last=(record,findings)
        self.writer.write(json.dumps({'run':self.run_id,'trace':self.trace,'phase':phase,
            'operation':self.spent if phase=='discovery' else self.post_spent,
            'target_us':result['target_us'],'observation_us':result['observation_us'],
            'record':record,'findings':findings})+'\n')
        return findings

    def assert_invariants(self):
        if self.last is None:return
        record,findings=self.last
        if findings and self.first is None:
            self.first=self.spent;self.first_wall=time.perf_counter()-self.started
            self.target=findings[0];self.discover_elapsed=self.first_wall
            self.first_events=list(self.events)
        if self.target is not None and self.target in findings:
            self.last_failing_events=list(self.events)
            self.failing_histories.append(list(self.events))
            raise ContractViolation(self.target)


class ApprovalMachine(RuleBasedStateMachine):
    """Normal rule state machine; Bundles contain the same two channel names."""
    receipts=Bundle('receipts')
    def __init__(self,controller):
        super().__init__();self.c=controller;self.value='a';self.c.reset()

    @rule(target=receipts,channel=st.sampled_from(['a','b']))
    def capture(self,channel):
        self.c.step('request_'+channel);return channel

    @rule(channel=receipts,positive=st.booleans())
    def complete(self,channel,positive):
        self.c.step(f'complete_{channel}_'+('pos' if positive else 'neg'))

    @rule()
    def edit(self):
        self.value='b' if self.value=='a' else 'a';self.c.step('edit_'+self.value)

    @rule()
    def revoke(self):self.c.step('revoke')

    @precondition(lambda self:self.c.observation['state']!='suspended')
    @rule()
    def suspend(self):self.c.step('suspend')

    @rule(batch=st.booleans())
    def activate(self,batch):self.c.step('activate_batch' if batch else 'activate')

    @invariant()
    def declared_contract(self):self.c.assert_invariants()


def fixed_histories(budget):
    spent=0;index=0
    while spent<budget:
        events=FIXED[index%len(FIXED)][:budget-spent-1]
        yield events;spent+=1+len(events);index+=1


def execute(backend,strategy,variant,field,seed,budget,writer,run_id):
    controller=Controller(backend,variant,field,budget,writer,run_id)
    engine_output=io.StringIO();status='exhausted_examples';native_shrink_complete=False
    try:
        if strategy=='hypothesis':
            factory=hypothesis_seed(seed)(lambda:ApprovalMachine(controller))
            config=settings(max_examples=1000,stateful_step_count=29,database=None,
                            deadline=None,phases=(Phase.generate,Phase.shrink),
                            suppress_health_check=[HealthCheck.too_slow],
                            report_multiple_bugs=False)
            with redirect_stdout(engine_output),redirect_stderr(engine_output):
                run_state_machine_as_test(factory,settings=config)
        else:
            rows=(fixed_histories(budget) if strategy=='fixed_regression' else
                  (row['events'] for row in histories(strategy,seed,budget)))
            for events in rows:
                controller.reset()
                for event in events:
                    controller.step(event);controller.assert_invariants()
            status='budget_complete'
    except BudgetExhausted as stop:
        status='budget_censored' if controller.first is None else 'shrink_budget_censored'
    except ContractViolation:
        status='detected';native_shrink_complete=strategy=='hypothesis'
    elapsed=time.perf_counter()-controller.started
    if variant.endswith('_control'):
        assert controller.first is None,(strategy,variant,seed,controller.target)
        assert controller.valid_uses==controller.releases
    return {'run':run_id,'variant':variant,'field':field,'strategy':strategy,'seed':seed,
            'budget':budget,'discovery_operations':controller.spent,
            'first_operation':controller.first,'first_seconds':controller.first_wall,
            'first_events':getattr(controller,'first_events',None),
            'target_property':controller.target,'status':status,
            'post_discovery_operations':controller.post_spent,
            'native_shrink_complete':native_shrink_complete,
            'native_final_failing_history':controller.last_failing_events if strategy=='hypothesis' else None,
            'total_seconds':elapsed,'target_seconds':controller.target_seconds,
            'observation_seconds':controller.observe_seconds,'reset_seconds':controller.reset_seconds,
            'check_seconds':controller.check_seconds,'bridge_call_seconds':controller.call_seconds,
            'generation_and_harness_residual_seconds':max(0.,elapsed-controller.call_seconds-controller.check_seconds),
            'sampled_peak_combined_rss_bytes':controller.peak_rss,
            'valid_uses':controller.valid_uses,'reported_releases':controller.releases,
            'engine_output':engine_output.getvalue()[-10000:]}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=['pilot','confirmation'],default='pilot')
    parser.add_argument('--flutter',default='flutter');parser.add_argument('--sqlite-cache')
    parser.add_argument('--offline',action='store_true');parser.add_argument('--strategies',nargs='+',default=STRATEGIES)
    parser.add_argument('--seeds',type=int,default=None);parser.add_argument('--manifest',type=Path)
    parser.add_argument('--budget',type=int,default=None)
    parser.add_argument('--output',default=None)
    args=parser.parse_args()
    cases=json.loads(args.manifest.read_text())['included'] if args.manifest else [
        {'variant':'callback_regression','field':'answer'},
        {'variant':'epoch_rebinding','field':'answer'},
        {'variant':'digest_control','field':'answer'}]
    phase=args.phase;budget=args.budget or (150 if phase=='pilot' else 300)
    seed_start=14000 if phase=='pilot' else 15000
    schedule=[]
    for case in cases:
        count=args.seeds or case.get('seeds',3 if phase=='pilot' else 30)
        schedule.extend((case,strategy,seed) for strategy in args.strategies
                        for seed in range(seed_start,seed_start+count))
    random.Random(20260930).shuffle(schedule)
    out=ROOT/'results/strong';out.mkdir(parents=True,exist_ok=True)
    prefix=args.output or phase;report_path=out/(prefix+'.runs.jsonl')
    records=[]
    with Backend(args.flutter,args.sqlite_cache,args.offline) as backend, \
            gzip.open(out/(prefix+'.observations.jsonl.gz'),'wt',compresslevel=3) as writer, \
            report_path.open('w') as results:
        for case,strategy,seed in schedule:
            run_id=f'{case["variant"]}-{case["field"]}-{strategy}-{seed}'
            result=execute(backend,strategy,case['variant'],case['field'],seed,budget,writer,run_id)
            results.write(json.dumps(result)+'\n');results.flush();records.append(result)
            print(json.dumps({k:result[k] for k in ['run','status','first_operation','post_discovery_operations','total_seconds']}),flush=True)
        startup=backend.startup_seconds
    summary={'success':True,'phase':phase,'runs':len(records),'hypothesis_version':importlib.metadata.version('hypothesis'),
             'bridge_startup_seconds':startup,'all_controls_clean':True,'protocol':'INDEPENDENT_BASELINE_PROTOCOL.md',
             'experimental_model_calls':0,'new_human_annotations':0}
    (out/(prefix+'.status.json')).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
