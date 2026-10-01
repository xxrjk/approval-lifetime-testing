"""Fixtures for concurrent moderation requests through Wagtail's admin routes.

Wagtail 7.4.3 / Django 5.2.17 handle approve and reject requests for one TaskState.
A barrier pauses one request after its read while the other completes and commits.
Sequential and reverse-order controls isolate the effect of the schedule.

The repair treatment replaces approve/reject at test time with a conditional
update requiring status=in_progress. Each condition then approves the current
workflow task to check its downstream effect. Approval contracts check the
persisted state before and after completion.
"""
import json, os, sys, tempfile, threading
from pathlib import Path

W = Path(__file__).resolve().parents[1]
DB = Path(tempfile.mkdtemp(prefix='wagtail_race_')) / 'race.sqlite3'
os.environ['DJANGO_SETTINGS_MODULE'] = 'wagtail.test.settings'
os.environ['DATABASE_NAME'] = str(DB)
os.environ.setdefault('no_proxy', '127.0.0.1,localhost')
import django  # noqa: E402
django.setup()
from django.conf import settings  # noqa: E402
from django.contrib.auth.models import Group  # noqa: E402
from django.core.exceptions import PermissionDenied  # noqa: E402
from django.db import transaction, connection  # noqa: E402
from django.test import TransactionTestCase, Client  # noqa: E402
from django.test.runner import DiscoverRunner  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.contrib.admin.utils import quote  # noqa: E402
import wagtail  # noqa: E402
from wagtail.models import (Page, Workflow, WorkflowTask, GroupApprovalTask, WorkflowPage,  # noqa: E402
                            TaskState, WorkflowState)
from wagtail.admin.views.generic import workflow as generic_workflow  # noqa: E402
from wagtail.test.testapp.models import SimplePage  # noqa: E402
from wagtail.test.utils import WagtailTestUtils  # noqa: E402

sys.path.insert(0, str(W/'src'))
from approval_contract import Contract, OriginRegistry, check_transition  # noqa: E402

ROWS = []


def receipt_id(ts):
    return f'task-{ts.id}-revision-{ts.revision_id}'


def ctx(ts):
    return {'object': str(ts.workflow_state.object_id), 'content': {'revision_id': ts.revision_id},
            'purpose': str(ts.task_id)}


def project(ts):
    state = 'pending' if ts.status == TaskState.STATUS_IN_PROGRESS else ts.status
    return {'context': ctx(ts), 'state': state,
            'approval': {'origin': receipt_id(ts), 'positive': True, 'local_valid': True}
            if ts.status == TaskState.STATUS_APPROVED else None}


def cas_transition(self, new_status, user, update, comment, action):
    """Compare-and-set repair: the write itself requires the row to still be in progress."""
    changed = TaskState.objects.filter(pk=self.pk, status=TaskState.STATUS_IN_PROGRESS).update(
        status=new_status, finished_at=timezone.now(), finished_by=user, comment=comment)
    if changed != 1:
        raise PermissionDenied
    self.refresh_from_db()
    self.log_state_change_action(user, action)
    if update:
        self.workflow_state.update(user=user)
    return self


class RaceTests(WagtailTestUtils, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        Workflow.objects.all().delete()
        self.submitter = self.create_user(username='submitter', password='password')
        Group.objects.get(name='Editors').user_set.add(self.submitter)
        self.m1 = self.create_user(username='moderator1', password='password')
        self.m2 = self.create_user(username='moderator2', password='password')
        for u in (self.m1, self.m2):
            Group.objects.get(name='Moderators').user_set.add(u)
        self.workflow = Workflow.objects.create(name='two_stage')
        self.task_1 = GroupApprovalTask.objects.create(name='stage_1')
        self.task_2 = GroupApprovalTask.objects.create(name='stage_2')
        for t in (self.task_1, self.task_2):
            t.groups.set(Group.objects.filter(name='Moderators'))
        WorkflowTask.objects.create(workflow=self.workflow, task=self.task_1, sort_order=1)
        if self._testMethodName != 'test_single_stage_race':
            WorkflowTask.objects.create(workflow=self.workflow, task=self.task_2, sort_order=2)
        root = Page.objects.get(id=2)
        self.page = SimplePage(title='Hello', slug='hello', content='hello', live=False,
                               has_unpublished_changes=True)
        root.add_child(instance=self.page)
        WorkflowPage.objects.create(workflow=self.workflow, page=self.page)
        c = Client(); c.force_login(self.submitter)
        r = c.post(reverse('wagtailadmin_pages:edit', args=(self.page.id,)),
                   {'title': 'Hello', 'slug': 'hello', 'content': 'hello', 'action-submit': 'True'})
        assert r.status_code in (200, 302), r.status_code
        self.page.refresh_from_db()
        self.ts1 = self.page.current_workflow_task_state
        assert self.ts1.task_id == self.task_1.id and self.ts1.status == TaskState.STATUS_IN_PROGRESS

    def url(self, action, ts):
        return reverse('wagtailadmin_pages:workflow_action', args=(quote(self.page.pk), action, ts.id))

    def post(self, user, action, ts, result):
        c = Client(); c.force_login(user)
        try:
            r = c.post(self.url(action, ts), {'comment': action})
            result[action] = r.status_code
        except Exception as exc:  # retain the exception as the request outcome
            result[action] = f'{type(exc).__name__}: {exc}'
        finally:
            connection.close()

    def run_condition(self, mode, repaired=False):
        original = (TaskState.approve, TaskState.reject)
        if repaired:
            TaskState.approve = transaction.atomic(lambda self, user=None, update=True, comment='':
                                                   cas_transition(self, TaskState.STATUS_APPROVED, user, update, comment, 'approve'))
            TaskState.reject = transaction.atomic(lambda self, user=None, update=True, comment='':
                                                  cas_transition(self, TaskState.STATUS_REJECTED, user, update, comment, 'reject'))
        first, second = ('approve', 'reject') if mode != 'reverse_race' else ('reject', 'approve')
        u_first, u_second = self.m1, self.m2
        origins = OriginRegistry()
        captured = TaskState.objects.get(pk=self.ts1.pk)
        origins.register(receipt_id(captured), ctx(captured))
        loaded = threading.Event(); release = threading.Event(); result = {}
        real_get = generic_workflow.BaseWorkflowFormView.get_task_state

        def paused_get(view):
            ts = real_get(view)
            if view.action_name == second and threading.current_thread().name == 'late':
                loaded.set(); release.wait(30)
            return ts
        try:
            if mode in ('race', 'race_repaired', 'reverse_race', 'race_single_stage'):
                generic_workflow.BaseWorkflowFormView.get_task_state = paused_get
                late = threading.Thread(target=self.post, args=(u_second, second, self.ts1, result), name='late')
                late.start(); assert loaded.wait(30)
                self.post(u_first, first, self.ts1, result)          # completes and commits
                before = project(TaskState.objects.get(pk=self.ts1.pk))
                release.set(); late.join(30)
            else:                                                   # sequential control
                self.post(u_first, first, self.ts1, result)
                before = project(TaskState.objects.get(pk=self.ts1.pk))
                self.post(u_second, second, self.ts1, result)
        finally:
            generic_workflow.BaseWorkflowFormView.get_task_state = real_get
            TaskState.approve, TaskState.reject = original
        after_ts = TaskState.objects.get(pk=self.ts1.pk)
        after = project(after_ts)
        record = {'kind': 'complete', 'incoming_origin': receipt_id(captured), 'incoming_valid': True,
                  'incoming_positive': second == 'approve', 'http_status': result.get(second),
                  'before': before, 'after': after}
        findings = check_transition(record, origins, Contract(('object', 'content', 'purpose')))
        ws = WorkflowState.objects.get(pk=self.ts1.workflow_state_id)
        state_after_race = {'task1': after_ts.status, 'workflow': ws.status,
                            'current_task': ws.current_task_state.task.name if ws.current_task_state else None}
        # downstream: approve whatever task is now current (stage 2 if the approval survived)
        downstream = {}
        page = Page.objects.get(pk=self.page.pk)
        if page.workflow_in_progress and ws.current_task_state and ws.current_task_state.task_id == self.task_2.id:
            self.post(self.m1, 'approve', ws.current_task_state, downstream)
        ws.refresh_from_db(); page = Page.objects.get(pk=self.page.pk)
        final = {'workflow': ws.status, 'current_task': ws.current_task_state.task.name if ws.current_task_state else None,
                 'current_task_status': ws.current_task_state.status if ws.current_task_state else None,
                 'page_live': page.live,
                 'task1_states': list(TaskState.objects.filter(workflow_state=ws, task=self.task_1)
                                      .order_by('id').values_list('status', flat=True))}
        ROWS.append({'mode': mode, 'repaired': repaired, 'http_status': result, 'record': record,
                     'findings': findings, 'state_after_race': state_after_race,
                     'downstream_stage2_approve': downstream, 'final': final})
        return findings, state_after_race, final, result

    def test_race(self):
        findings, s, final, result = self.run_condition('race')
        print('RACE', result, s, final, findings, flush=True)

    def test_sequential_control(self):
        findings, s, final, result = self.run_condition('sequential')
        print('SEQUENTIAL', result, s, final, findings, flush=True)

    def test_race_repaired(self):
        findings, s, final, result = self.run_condition('race_repaired', repaired=True)
        print('REPAIRED', result, s, final, findings, flush=True)

    def test_single_stage_race(self):
        findings, s, final, result = self.run_condition('race_single_stage')
        print('SINGLE-STAGE RACE', result, s, final, findings, flush=True)

    def test_reverse_race(self):
        findings, s, final, result = self.run_condition('reverse_race')
        print('REVERSE', result, s, final, findings, flush=True)


if __name__ == '__main__':
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    labels = ['__main__.RaceTests.' + n for n in ['test_race', 'test_sequential_control', 'test_race_repaired', 'test_reverse_race', 'test_single_stage_race']]
    failures = DiscoverRunner(verbosity=1, interactive=False).run_tests(labels)
    out = {'wagtail': wagtail.__version__, 'django': django.get_version(), 'database': 'file-backed SQLite',
           'failures': failures, 'rows': ROWS,
           'scope': 'full HTTP admin route with Django test clients; a pause is inserted after the late request loads its TaskState; no frequency, PostgreSQL behavior or upstream confirmation is claimed',
           'checker': 'frozen core v3', 'model_calls': 0, 'new_human_annotations': 0}
    (W/'results/independent/wagtail_http_race.json').write_text(json.dumps(out, indent=2, default=str))
    raise SystemExit(bool(failures))
