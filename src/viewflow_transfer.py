"""Test Viewflow's public zero-state regression through a declared approval workflow.

The author-written workflow runs on Viewflow and Django/SQLite. Workflow code
checks request bindings; Viewflow checks lifecycle transitions. Pinned original,
fixed, and single-line contrast sources provide the regression treatments.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--treatment',required=True);p.add_argument('--approved',type=int,required=True)
    a=p.parse_args()
    sys.path.insert(0,str(a.source.resolve()))
    from django.conf import settings
    database=Path(tempfile.mkdtemp(prefix='viewflow_contract_'))/'state.sqlite3'
    settings.configure(SECRET_KEY='local-test-only',INSTALLED_APPS=[],MIDDLEWARE=(),
        DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':str(database)}},
        VIEWFLOW={'AUTOREGISTER':False},USE_TZ=True)
    import django
    django.setup()
    from django.db import connection,models,transaction
    from viewflow import fsm
    import viewflow
    from approval_contract import Contract,OriginRegistry,check_transition,SAFETY
    from contract_model import Declaration,synthesize,initial,step
    APPROVED=a.approved;NEW=1;REJECTED=2;PUBLISHED=3
    assert APPROVED not in (NEW,REJECTED,PUBLISHED)
    code_to_state={NEW:'new',APPROVED:'approved',REJECTED:'rejected',PUBLISHED:'published'}

    class Report(models.Model):
        body=models.TextField(default='a')
        stage=models.IntegerField(default=NEW)
        approval_origin=models.CharField(max_length=80,null=True)
        positive=models.BooleanField(default=False)
        class Meta:app_label='approval_probe';db_table='report'

    class ReportFlow:
        stage=fsm.State(int,default=NEW)
        def __init__(self,report):self.report=report
        @stage.getter()
        def get_stage(self):return self.report.stage
        @stage.setter()
        def set_stage(self,value):self.report.stage=value
        @stage.on_success()
        def persist(self,descriptor,source,target):self.report.save()
        @stage.transition(source=NEW,target=APPROVED)
        def approve(self,origin):
            self.report.approval_origin=origin;self.report.positive=True
        @stage.transition(source=NEW,target=REJECTED)
        def reject(self,origin):
            self.report.approval_origin=origin;self.report.positive=False
        @stage.transition(source=APPROVED,target=PUBLISHED)
        def publish(self):pass

    with connection.schema_editor() as schema:schema.create_model(Report)
    declaration=Declaration(dimensions=('object','content'),states=('new','approved','rejected','published'),
        reset_state='new',completion_states=('new',),activation_states=('approved',),batch_states=(),
        consumed_state='published',perturb=(('content','edit'),),lifecycle=(),
        positive_completion_state='approved',negative_completion_state='rejected')
    contract=Contract(('object','content'),activation_states=('approved',),batch_states=(),
        completion_states=('new',),consumed_state='published',reset_state='new',
        invalidations=(('edit_',()),))
    witnesses,inventory=synthesize(declaration)
    plans=[{'target':target,'events':events,'witness':i} for target,options in witnesses.items()
           for i,events in enumerate(options)]
    # Report the registered normal-path and invalidation controls separately
    # from compiled target histories.
    controls=[('fresh_negative',['request_a','complete_a_neg']),
              ('unapproved_use',['activate']),
              ('edit_invalidates',['request_a','complete_a_pos','edit_b','activate'])]
    plans += [dict(target=name,events=events,witness=0,control=True) for name,events in controls]

    def observe():
        # Read persisted lifecycle state directly through SQL.
        with connection.cursor() as cursor:
            cursor.execute('SELECT id,body,stage,approval_origin,positive FROM report')
            identifier,body,stage,origin,positive=cursor.fetchone()
        return {'context':{'object':identifier,'content':body},'state':code_to_state[stage],
                'approval':None if origin is None else {'origin':origin,'positive':bool(positive),'local_valid':True}}

    def reference(model,actual):
        obj=model.current;expected=obj.approval
        mismatches=[]
        if obj.state!=actual['state']:mismatches.append('state')
        if obj.content!=actual['context']['content']:mismatches.append('content')
        if bool(expected and expected.positive)!=bool(actual['approval'] and actual['approval']['positive']):
            mismatches.append('positive')
        if expected and expected.positive and actual['approval'] and actual['approval']['positive']:
            if actual['approval']['origin']!=str(expected.receipt.identifier):mismatches.append('origin')
        return mismatches

    rows=[]
    for plan in plans:
        Report.objects.all().delete();Report.objects.create(id=1,body='a',stage=NEW)
        registry=OriginRegistry();slots={};next_id=0;model=initial(declaration);records=[]
        for event in plan['events']:
            before=observe();record={'event':event,'kind':'other','before':before}
            native_state=None;outcome='completed'
            if event.startswith('request_'):
                next_id+=1;origin=str(next_id);slots[event.split('_')[1]]=origin
                registry.register(origin,before['context'])
                record.update(kind='capture',capture={'id':origin,'context':before['context']})
            elif event.startswith('complete_'):
                _,channel,sign=event.split('_');origin=slots[channel]
                record.update(kind='complete',incoming_origin=origin,incoming_positive=sign=='pos',incoming_valid=True)
                obj=Report.objects.get(id=1);flow=ReportFlow(obj);native_state=flow.stage
                # Binding is independently declared workflow behavior, unchanged
                # across source treatments. Lifecycle authority comes from fsm.
                if registry[origin]!=before['context']:outcome='refused_context'
                else:
                    try:
                        with transaction.atomic():
                            (flow.approve if sign=='pos' else flow.reject)(origin)
                    except fsm.TransitionNotAllowed:outcome='refused_transition'
            elif event=='activate':
                record.update(kind='activate',entry='single')
                obj=Report.objects.get(id=1);flow=ReportFlow(obj);native_state=flow.stage
                # An approval is required independently of the FSM lifecycle gate.
                if not obj.positive or obj.approval_origin is None:outcome='refused_no_approval'
                elif registry[obj.approval_origin]!=before['context']:outcome='refused_context'
                else:
                    try:
                        with transaction.atomic():flow.publish()
                    except fsm.TransitionNotAllowed:outcome='refused_transition'
            elif event.startswith('edit_'):
                Report.objects.filter(id=1).update(body=event.split('_')[1],stage=NEW,
                                                  approval_origin=None,positive=False)
            else:raise ValueError(event)
            record['after']=observe();record['reported']=outcome
            record['native_guard_state']=native_state
            if record['kind']=='activate':record['released']=record['after']['state']=='published'
            findings=check_transition(record,registry,contract)
            result=step(model,'edit' if event.startswith('edit_') else event,declaration)
            assert result is not None,event
            model=result[0]
            records.append({'record':record,'findings':findings,'reference':reference(model,record['after'])})
        all_findings=sorted({f for r in records for f in r['findings']})
        admission={'unqualified_admission','active_state_validity'}
        rows.append({**plan,'findings':all_findings,'admission_findings':sorted(set(all_findings)&admission),
                     'v2_findings':sorted(set(all_findings)&SAFETY),'records':records,
                     'reference_mismatches':sum(bool(r['reference']) for r in records),
                     'final':observe()})
    result={'treatment':a.treatment,'library_version':viewflow.__version__,'approved_encoding':APPROVED,
            'declaration':asdict(declaration),'contract':asdict(contract),'inventory':inventory,
            'generated_histories':len(rows)-len(controls),'control_histories':len(controls),
            'rows':rows,'database':'SQLite; fresh SQL projections',
            'detected_histories':sum(bool(r['findings']) for r in rows if not r.get('control')),
            'admission_detected_histories':sum(bool(r['admission_findings']) for r in rows if not r.get('control')),
            'control_findings':sum(bool(r['findings']) for r in rows if r.get('control')),
            'finding_types':sorted({f for r in rows for f in r['findings']})}
    out=ROOT/'results/transfer';out.mkdir(exist_ok=True)
    (out/f'{a.treatment}-{APPROVED}.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('treatment','library_version','approved_encoding',
          'generated_histories','control_histories','detected_histories','admission_detected_histories','control_findings','finding_types')}))


if __name__=='__main__':main()
