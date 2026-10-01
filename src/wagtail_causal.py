"""Execute generated approval histories through controlled Wagtail HTTP schedules.

The compiler receives pending, approved, and rejected lifecycle states. Capture
starts a request paused at a registered boundary; completion releases it. Both
verdict signs use the same read and pre-write hooks. A separate observer reads
committed state before and after each released operation while other target
writers remain paused.
"""
from dataclasses import asdict
import json
from pathlib import Path
import threading

import wagtail_http_race as native
from approval_contract import Contract, OriginRegistry, check_transition
from contract_model import Declaration, initial, step, synthesize
from fair_comparison import events_to_operation

from django.conf import settings
from django.db import connection, transaction
from django.test import Client
from django.test.runner import DiscoverRunner
from wagtail.models import TaskState, WorkflowState, Page, GroupApprovalTask
from wagtail.admin.views.generic import workflow as generic

ROOT = Path(__file__).resolve().parents[1]
DECLARATION = Declaration(dimensions=('object', 'content', 'purpose'),
    states=('pending', 'approved', 'rejected'), completion_states=('pending',),
    activation_states=(), batch_states=(), consumed_state='approved', perturb=(), lifecycle=(),
    positive_completion_state='approved', negative_completion_state='rejected',
    consume_receipt_on_completion=True)
WITNESSES, INVENTORY = synthesize(DECLARATION, families=('F1',), max_depth=6)
from causal_schedule import compile_schedule


PROGRAMS = []
for name, options in WITNESSES.items():
    if options:
        # Each channel names one HTTP request. Overlapping captures use the
        # compiler's equivalent witness with distinct channel names.
        raw = next(history for history in options
                   if len({e for e in history if e.startswith('request_')}) ==
                      sum(e.startswith('request_') for e in history))
        PROGRAMS.append({'target': name, 'abstract_events': raw,
                         'events': compile_schedule(raw, DECLARATION)['schedule'],
                         'certificate': compile_schedule(raw, DECLARATION)})
PROGRAMS += [{'target': 'positive_control', 'events': ['request_a', 'complete_a_pos']},
             {'target': 'negative_control', 'events': ['request_a', 'complete_a_neg']}]
ROWS = []


class GeneratedWorkflow(native.RaceTests):
    def post(self, user, action, task, result):
        # Authentication setup is outside the scheduled task effect. Creating a
        # session inside an outstanding SQLite transaction adds an unrelated
        # writer to the schedule and can prevent the target request from starting.
        try:
            response = self.clients[user.pk].post(self.url(action, task), {'comment': action})
            result[action] = response.status_code
        except Exception as error:
            result[action] = f'{type(error).__name__}: {error}'
        finally:
            connection.close()

    def execute_program(self, program, boundary, treatment):
        original_get = generic.BaseWorkflowFormView.get_task_state
        original_methods = TaskState.approve, TaskState.reject
        original_action = GroupApprovalTask.on_action
        self.clients = {}
        for actor in (self.m1, self.m2):
            client = Client(); client.force_login(actor); self.clients[actor.pk] = client
        jobs = {}; records = []; origins = OriginRegistry(); stage2_id = None
        contract = Contract(('object', 'content', 'purpose'))
        completion_sign = {event.split('_')[1]: event.endswith('_pos')
                           for event in program['events'] if event.startswith('complete_')}

        def current_job():
            return jobs.get(threading.current_thread().name)

        def barrier(job, task, point):
            # Observe on a separate ORM instance: dereferencing workflow_state
            # on the SUT's cached task would populate its relation cache and
            # change the execution being measured.
            job['loaded_context'] = native.ctx(TaskState.objects.get(pk=task.pk))
            job['loaded_status'] = task.status; job['hook'] = point
            job['ready'].set()
            if not job['release'].wait(30):
                raise TimeoutError('controlled scheduler did not release its operation')

        def get_task(view):
            task = original_get(view); job = current_job()
            if job is not None and boundary in ('read', 'read_after_fresh'):
                if boundary == 'read_after_fresh' and treatment == 'fresh':
                    task = original_get(view)
                barrier(job, task, 'after_fresh_read' if boundary == 'read_after_fresh' else 'after_task_read')
                if treatment == 'fresh' and boundary == 'read':
                    task = original_get(view)
            return task

        def transition(positive):
            original = original_methods[0 if positive else 1]
            def call(task, user=None, update=True, comment=''):
                job = current_job()
                if job is not None and boundary == 'action' and treatment == 'fresh':
                    task.refresh_from_db()
                if treatment == 'cas':
                    with transaction.atomic():
                        return native.cas_transition(task,
                            TaskState.STATUS_APPROVED if positive else TaskState.STATUS_REJECTED,
                            user, update, comment, 'approve' if positive else 'reject')
                return original(task, user=user, update=update, comment=comment)
            return call

        def on_action(task, task_state, user, action_name, **kwargs):
            job = current_job()
            if job is not None and boundary in ('action', 'action_after_fresh'):
                if boundary == 'action_after_fresh' and treatment == 'fresh':
                    task_state.refresh_from_db()
                # Pause before the task-action transaction, after the read has completed.
                barrier(job, task_state, 'after_fresh_before_task_transaction' if boundary == 'action_after_fresh' else 'before_task_action_transaction')
            return original_action(task, task_state, user, action_name, **kwargs)

        generic.BaseWorkflowFormView.get_task_state = get_task
        TaskState.approve, TaskState.reject = transition(True), transition(False)
        GroupApprovalTask.on_action = on_action
        try:
            for index, event in enumerate(program['events']):
                if event.startswith('request_'):
                    channel = event.split('_')[1]
                    captured = TaskState.objects.get(pk=self.ts1.pk)
                    origin = native.receipt_id(captured); context = native.ctx(captured)
                    if origin not in origins:
                        origins.register(origin, context)
                    else:
                        assert origins[origin] == context
                    action = 'approve' if completion_sign[channel] else 'reject'
                    job = {'ready': threading.Event(), 'release': threading.Event(), 'result': {},
                           'origin': origin, 'capture_context': context, 'action': action}
                    jobs[channel] = job
                    actor = self.m1 if channel == 'a' else self.m2
                    job['thread'] = threading.Thread(target=self.post,
                        args=(actor, action, self.ts1, job['result']), name=channel)
                    job['thread'].start()
                    assert job['ready'].wait(30), (program, boundary, treatment, channel, job['result'])
                    assert job['loaded_context'] == context
                    records.append({'kind': 'capture', 'event': event, 'origin': origin,
                                    'context': context, 'loaded_status': job['loaded_status'],
                                    'hook': job['hook'], 'schedule_index': index})
                elif event.startswith('complete_'):
                    channel = event.split('_')[1]; job = jobs[channel]
                    before = native.project(TaskState.objects.get(pk=self.ts1.pk))
                    job['release'].set(); job['thread'].join(30)
                    assert not job['thread'].is_alive(), 'target request did not complete'
                    after = native.project(TaskState.objects.get(pk=self.ts1.pk))
                    record = {'kind': 'complete', 'event': event, 'incoming_origin': job['origin'],
                              'incoming_valid': True, 'incoming_positive': event.endswith('_pos'),
                              'before': before, 'after': after, 'http_result': job['result'],
                              'observation_boundary': 'fresh committed read immediately before release and after request completion',
                              'other_target_writers_inside_effect': 0, 'schedule_index': index}
                    record['findings'] = check_transition(record, origins, contract)
                    records.append(record)
                    # Retain the stage-2 request target created by an early
                    # approval. The downstream probe uses that real target even
                    # if the late completion subsequently changes workflow state.
                    if stage2_id is None:
                        stage2 = TaskState.objects.filter(workflow_state_id=self.ts1.workflow_state_id,
                            task_id=self.task_2.pk, status=TaskState.STATUS_IN_PROGRESS).first()
                        if stage2 is not None:
                            stage2_id = stage2.pk
                else:
                    raise ValueError(event)
        finally:
            for job in jobs.values():
                job['release'].set()
            for job in jobs.values():
                job['thread'].join(30)
            generic.BaseWorkflowFormView.get_task_state = original_get
            TaskState.approve, TaskState.reject = original_methods
            GroupApprovalTask.on_action = original_action
            connection.close()
        workflow = WorkflowState.objects.get(pk=self.ts1.workflow_state_id)
        downstream = {}
        if stage2_id is not None:
            self.post(self.m1, 'approve', TaskState.objects.get(pk=stage2_id), downstream)
        workflow.refresh_from_db(); page = Page.objects.get(pk=self.page.pk)
        findings = sorted({f for record in records for f in record.get('findings', [])})
        if treatment == 'cas' or (treatment == 'fresh' and 'after_fresh' not in boundary) or program['target'].endswith('_control'):
            self.assertEqual(findings, [])
        ROWS.append({'target': program['target'], 'events': program['events'], 'boundary': boundary,
                     'treatment': treatment, 'origins': dict(origins), 'records': records,
                     'findings': findings, 'downstream_http': downstream,
                     'downstream_target': stage2_id,
                     'final': {'page_live': page.live, 'workflow': workflow.status,
                               'current_task': workflow.current_task_state.task_id if workflow.current_task_state else None,
                               'stage1_states': list(TaskState.objects.filter(workflow_state=workflow, task=self.task_1)
                                                    .order_by('id').values_list('status', flat=True))}})


LABELS = []
for pindex, program in enumerate(PROGRAMS):
    for boundary in ('read', 'action', 'read_after_fresh', 'action_after_fresh'):
        for treatment in ('cached', 'fresh', 'cas'):
            name = f'test_generated_{pindex}_{boundary}_{treatment}'
            def test(self, program=program, boundary=boundary, treatment=treatment):
                self.execute_program(program, boundary, treatment)
            setattr(GeneratedWorkflow, name, test)
            LABELS.append('__main__.GeneratedWorkflow.' + name)


if __name__ == '__main__':
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    failures = DiscoverRunner(verbosity=1, interactive=False).run_tests(LABELS)
    result = {'success': failures == 0, 'declaration': asdict(DECLARATION), 'inventory': INVENTORY,
              'programs': PROGRAMS, 'cases': len(ROWS), 'rows': ROWS,
              'reuse': 'frozen checker and relation compiler; checked dependency schedule; fresh read before release controls',
              'scheduling_hooks': ['after_task_read', 'before_task_action_transaction', 'after_fresh_read', 'after_fresh_before_task_transaction'],
              'not_automatic_window_discovery': True, 'known_diagnostic_case': True,
              'upstream_contact_performed': False, 'model_calls': 0, 'new_human_annotations': 0}
    out = ROOT/'results/causal'; out.mkdir(parents=True, exist_ok=True)
    (out/'native.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({'cases': len(ROWS), 'failures': failures,
                      'flagged': sum(bool(r['findings']) for r in ROWS)}), flush=True)
    raise SystemExit(bool(failures))
