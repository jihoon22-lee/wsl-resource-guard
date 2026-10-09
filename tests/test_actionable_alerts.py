import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wsl_resource_guard.actions import Actions
from wsl_resource_guard.config import Settings
from wsl_resource_guard.delivery import Delivery
from wsl_resource_guard.identity import describe_target, process_identity
from wsl_resource_guard.incidents import atomic_write, read_store, reconcile, incident_message
from wsl_resource_guard.metrics import SystemMetrics
from wsl_resource_guard.notifications import NotificationResult
from wsl_resource_guard.processes import ProcessInfo, ProcessSnapshot, SessionUsage, ProjectUsage, is_stale_session, _persistent_provider


def fixture(shared=False):
    p = ProcessInfo(100, 1, os.getuid(), 'codex', 'S', 500, 0, 200000, '/work/demo', '/init.scope',
                    '/usr/bin/codex app-server' if shared else '/usr/bin/codex', start_ticks=10)
    child = replace(p, pid=101, ppid=100, name='node', command='node tool', start_ticks=11)
    usage = SessionUsage(100, 'codex', 'codex', 'demo', 200000, youngest_process_age_seconds=200000,
                         rss_kib=1000, process_count=2, mcp_rss_kib=500, persistent=shared,
                         projects=[ProjectUsage(100,'codex','demo',rss_kib=1000,process_count=2)])
    return ProcessSnapshot({100:p,101:child},[usage],[],{100:100,101:100},{100:'codex'})


def metrics(now=1000, available=8):
    return SystemMetrics(timestamp=now,mem_total_kib=16*1048576,mem_available_kib=available*1048576,
                         swap_total_kib=0,swap_free_kib=0,psi_some_avg60=0,psi_full_avg60=0,pswpout_pages=0,oom_kills=0)


def condition(key='warning.mcp_total',severity='warning'):
    return {'key':key,'severity':severity,'reason':'MCP memory threshold exceeded'}


class IdentityTests(unittest.TestCase):
    def test_shared_runtime_is_not_an_idle_conversation_or_kill_candidate(self):
        snap=fixture(True)
        row=describe_target(snap,100)
        self.assertEqual(row['kind'],'shared_runtime')
        self.assertFalse(row['killable'])
        self.assertFalse(is_stale_session(snap.sessions[0],48))
        self.assertTrue(row['kill_block_reason'])

    def test_program_argument_cannot_impersonate_a_persistent_server(self):
        self.assertIsNone(_persistent_provider('codex','/usr/bin/codex exec echo app-server'))
        self.assertIsNone(_persistent_provider('bash','echo codex app-server'))
        self.assertEqual(_persistent_provider('codex', '/path with spaces/codex app-server',
                          ('/path with spaces/codex', 'app-server')), 'codex')
        self.assertEqual(_persistent_provider('codex', '', ('/path/codex','-c','setting=value','app-server')), 'codex')
        self.assertIsNone(_persistent_provider('codex', '', ('codex','-c','app-server','exec','echo')))

    def test_identity_changes_on_pid_reuse_or_reboot(self):
        p=fixture().processes[100]
        self.assertNotEqual(process_identity(p,'boot-a'),process_identity(replace(p,start_ticks=20),'boot-a'))
        self.assertNotEqual(process_identity(p,'boot-a'),process_identity(p,'boot-b'))

    def test_matching_explicit_id_is_required_for_a_task_title(self):
        snap=fixture()
        metadata={'101':{'start_ticks':11,'thread_id':'thread-a','title':'Task A','source':'explicit_environment'}}
        self.assertEqual(describe_target(snap,100,metadata)['tasks'][0]['title'],'Task A')
        metadata['101']['start_ticks']=12
        self.assertEqual(describe_target(snap,100,metadata)['task_identity'],'unavailable')

    def test_multiple_task_ids_prevent_whole_tree_termination(self):
        snap=fixture()
        metadata={str(pid):{'start_ticks':p.start_ticks,'thread_id':str(pid),'title':'same project'} for pid,p in snap.processes.items()}
        self.assertFalse(describe_target(snap,100,metadata)['killable'])


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings(recovery_sustain_seconds=30)
        self.snap=fixture(True)

    def update(self,previous,conditions,now,**kwargs):
        return reconcile(previous,conditions,self.snap,metrics(now),self.settings,boot=kwargs.get('boot','boot-a'))

    def test_different_conditions_same_severity_have_separate_stable_ids(self):
        one=self.update({},[condition()],1000)
        two=self.update(one,[condition(),condition('warning.session.100')],1015)
        self.assertEqual(len(two['incidents']),2)
        self.assertIn(one['incidents'][0]['id'],[r['id'] for r in two['incidents']])
        self.assertEqual(len({r['id'] for r in two['incidents']}),2)

    def test_partial_recovery_retains_other_incidents(self):
        start=self.update({},[condition(),condition('warning.session.100')],1000)
        first=self.update(start,[condition()],1015)
        final=self.update(first,[condition()],1045)
        bykey={r['key'].split(':')[0]:r['state'] for r in final['incidents']}
        self.assertEqual(bykey['mcp_total'],'active')
        self.assertEqual(bykey['session'],'resolved')

    def test_collection_gap_does_not_mean_recovered(self):
        before=self.update({},[condition()],1000)
        after=self.update(before,[],2000)
        self.assertEqual(after['incidents'][0]['state'],'unknown')

    def test_unobserved_psi_does_not_recover(self):
        before=self.update({},[condition('warning.psi_some')],1000)
        m=replace(metrics(1015),psi_some_avg60=None)
        after=reconcile(before,[],self.snap,m,self.settings,boot='boot-a')
        self.assertEqual(after['incidents'][0]['state'],'unknown')

    def test_mcp_volume_and_memory_pressure_are_different(self):
        row=self.update({},[condition()],1000)['incidents'][0]
        self.assertEqual(row['priority'],'review')
        pressured=reconcile({},[condition()],self.snap,metrics(1000,1),self.settings,boot='boot-a')['incidents'][0]
        self.assertEqual(pressured['priority'],'pressure')

    def test_recurrence_uses_a_new_id_after_confirmed_recovery(self):
        first=self.update({},[condition()],1000)
        done=self.update(self.update(first,[],1015),[],1045)
        again=self.update(done,[condition()],1060)
        active=next(r for r in again['incidents'] if r['state']=='active')
        self.assertNotEqual(first['incidents'][0]['id'],active['id'])

    def test_link_has_incident_identity_and_html_is_escaped(self):
        row=self.update({},[condition()],1000)['incidents'][0]
        row['reason']='<script>marker</script>'
        title,text,html=incident_message(row,'https://demo.example:9443')
        self.assertIn('/#incident?id='+row['id'],text)
        self.assertNotIn('<script>',html)
        self.assertIn('공용 실행기',text)
        self.assertNotIn('href=',incident_message(row,'javascript:alert(1)')[2])

    def test_malformed_store_is_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'incidents.json';path.write_text('{broken')
            with self.assertRaises(ValueError):read_store(path)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.actions=Actions(Path(self.temp.name)/'actions.json')
        self.snap=fixture();self.actor='owner@example.com'

    def preview(self):
        return self.actions.preview(self.snap,100,{},self.actor)

    def test_shared_runtime_cannot_be_previewed(self):
        with self.assertRaisesRegex(ValueError,'상시'):
            self.actions.preview(fixture(True),100,{},self.actor)

    def test_same_token_sends_once_even_after_lost_response(self):
        preview=self.preview()
        with patch('wsl_resource_guard.processes.signal_verified_process',return_value=True) as send:
            self.actions.execute(preview['id'],self.snap,{},self.actor,os.getuid())
            self.actions.execute(preview['id'],self.snap,{},self.actor,os.getuid())
        self.assertEqual(send.call_count,2)
        self.assertEqual(self.actions.load()[preview['id']]['status'],'verifying')

    def test_replaced_root_and_changed_scope_send_nothing(self):
        for change in ('reuse','new-child','exec'):
            with self.subTest(change=change):
                p=self.preview();new=copy.deepcopy(self.snap)
                if change=='reuse':new.processes[100].start_ticks=99
                elif change=='exec':new.processes[101].command='node different-task'
                else:new.processes[102]=replace(new.processes[101],pid=102)
                with patch('wsl_resource_guard.processes.signal_verified_process') as send:
                    result=self.actions.execute(p['id'],new,{},self.actor,os.getuid())
                self.assertEqual(result['status'],'rejected');send.assert_not_called()

    def test_actor_binding_and_expiry(self):
        p=self.preview()
        with self.assertRaises(ValueError):self.actions.execute(p['id'],self.snap,{},'other',os.getuid())
        with patch('wsl_resource_guard.actions.time.time',return_value=p['expires_at']+1),patch('wsl_resource_guard.processes.signal_verified_process') as send:
            self.assertEqual(self.actions.execute(p['id'],self.snap,{},self.actor,os.getuid())['status'],'rejected')
            send.assert_not_called()

    def test_partial_signal_failure_is_preserved_and_not_retried(self):
        p=self.preview()
        with patch('wsl_resource_guard.processes.signal_verified_process',side_effect=[True,PermissionError('private')]) as send:
            result=self.actions.execute(p['id'],self.snap,{},self.actor,os.getuid())
            again=self.actions.execute(p['id'],self.snap,{},self.actor,os.getuid())
        self.assertEqual(send.call_count,2);self.assertEqual(result['status'],'partial')
        self.assertEqual(result['signalled'],[101]);self.assertEqual(again['status'],'partial')
        self.assertNotIn('private',json.dumps(result))

    def test_parent_exit_does_not_hide_remaining_child(self):
        p=self.preview()
        with patch('wsl_resource_guard.processes.signal_verified_process',return_value=True):
            self.actions.execute(p['id'],self.snap,{},self.actor,os.getuid())
        new=copy.deepcopy(self.snap);new.processes.pop(100);new.processes[101].ppid=1
        with patch('wsl_resource_guard.processes.process_start_ticks',return_value=None):
            result=self.actions.status(p['id'],new,self.actor)
        self.assertEqual(result['status'],'remaining');self.assertEqual(result['remaining'],[101])

    def test_unobservable_member_does_not_count_as_completed(self):
        p=self.preview()
        with patch('wsl_resource_guard.processes.signal_verified_process',return_value=True):
            self.actions.execute(p['id'],self.snap,{},self.actor,os.getuid())
        new=copy.deepcopy(self.snap);new.processes={}
        with patch('wsl_resource_guard.processes.process_start_ticks',side_effect=lambda pid:self.snap.processes[pid].start_ticks):
            self.assertEqual(self.actions.status(p['id'],new,self.actor)['status'],'unknown')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.shared=self.root/'shared';self.shared.mkdir()
        self.settings=Settings(state_dir=str(self.root),secrets_path=self.root/'secrets.json',windows_toast_enabled=False,discord_enabled=False,webhook_enabled=False,push_enabled=False,reminder_cooldown_seconds=3600)
        self.settings.secrets_path.write_text(json.dumps({'gmail_to':'one@example.com,two@example.com','gmail_user':'fixture','gmail_app_password':'fixture'}))
        self.now=1000
        self.engine=Delivery(self.settings,self.shared,clock=lambda:self.now)
        self.store=reconcile({},[condition()],fixture(True),metrics(),self.settings,boot='test')
        self.row=self.store['incidents'][0]
        atomic_write(self.root/'incidents.json',self.store)
        self.calls=[]

    def send(self,destination,row,notifier,origin):
        self.calls.append(destination['recipient'])
        return NotificationResult('gmail',destination['recipient'].startswith('one'),'fixture')

    def ledger(self):return json.loads((self.root/'delivery.json').read_text())

    def test_partial_delivery_preserved_and_only_failed_destination_retries(self):
        self.engine.tick(send=self.send)
        states=[d['status'] for d in self.ledger()['events'][self.row['id']]['destinations'].values()]
        self.assertEqual(sorted(states),['accepted','retry'])
        self.now=1060;self.store['updated_at']=1060;atomic_write(self.root/'incidents.json',self.store)
        self.engine.tick(send=self.send)
        self.assertEqual(self.calls.count('one@example.com'),1)
        self.assertEqual(self.calls.count('two@example.com'),2)

    def test_new_same_severity_incident_is_not_suppressed_by_defer(self):
        self.engine.tick(send=self.send)
        atomic_write(self.shared/'incident-decisions.json',{self.row['id']:{'choice':'defer','until':9000,'revision':self.row['revision']}})
        self.now=1015
        other=reconcile(self.store,[condition(),condition('warning.session.100')],fixture(True),metrics(1015),self.settings,boot='test')
        atomic_write(self.root/'incidents.json',other)
        before=len(self.calls);self.engine.tick(send=self.send)
        self.assertEqual(len(self.calls)-before,2)

    def test_ambiguous_timeout_is_not_automatically_retried(self):
        self.engine.tick(send=lambda *args:NotificationResult('gmail',False,'TimeoutError'))
        self.now=1030
        self.engine.tick(send=self.send)
        self.assertEqual(self.calls,[])
        self.assertEqual({d['status'] for d in self.ledger()['events'][self.row['id']]['destinations'].values()},{'unknown'})

    def test_interrupted_send_is_unknown_after_restart(self):
        self.engine.tick(send=self.send)
        ledger=self.ledger()
        for d in ledger['events'][self.row['id']]['destinations'].values():d['status']='sending'
        atomic_write(self.root/'delivery.json',ledger)
        self.calls=[];self.engine.tick(send=self.send)
        self.assertEqual(self.calls,[])
        self.assertEqual({d['status'] for d in self.ledger()['events'][self.row['id']]['destinations'].values()},{'unknown'})

    def test_stale_collection_sends_nothing(self):
        self.now=2000
        self.assertEqual(self.engine.tick(send=self.send),0)
        self.assertEqual(self.calls,[])

    def test_recovery_supersedes_failed_old_alert(self):
        self.engine.tick(send=self.send)
        resolved=self.row.copy();resolved.update(state='resolved',revision=2,resolved_at=1015,observed_at=1015)
        self.store.update(updated_at=1015,incidents=[resolved]);atomic_write(self.root/'incidents.json',self.store)
        self.now=1015;seen=[]
        self.engine.tick(send=lambda destination,row,*rest:(seen.append(row['state']) or NotificationResult('gmail',True,'accepted')))
        self.assertEqual(seen,['resolved','resolved'])



class ProtocolTests(unittest.TestCase):
    def setUp(self):
        import pwd,socket
        from types import SimpleNamespace
        from wsl_resource_guard.service_control import Controller,ControlError
        from wsl_resource_guard.services import ServiceError
        from wsl_resource_guard.webapp import create_app
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);registry=self.path/'registry.json'
        registry.write_text(json.dumps({'owner':pwd.getpwuid(os.getuid()).pw_name,'origin':'https://fixture.example','services':[]}))
        self.controller=Controller(registry,shared=self.path/'shared')
        self.controller._owner_settings=lambda:Settings(state_dir=str(self.path))
        self.controller._owner_read=lambda operation,args=None: {} if operation=='task-metadata' else read_store(self.path/'incidents.json')
        def control(request):
            try:return self.controller.dispatch(request,uid=999,web_uid=999)
            except ControlError as exc:raise ServiceError('fixture controlled error',code=exc.code) from None
        self.app=create_app({'origin':'https://fixture.example','allowed_logins':['owner@example.com'],'secret_key':'fixture'},control=control)
        self.client=self.app.test_client()
        self.env={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        self.headers={'Tailscale-User-Login':'owner@example.com'}
        self.token=self.client.get('/api/bootstrap',base_url='https://fixture.example',headers=self.headers,environ_overrides=self.env).json['csrf']
        self.snap=fixture()
        self.patcher=patch('wsl_resource_guard.processes.build_snapshot',return_value=self.snap);self.patcher.start();self.addCleanup(self.patcher.stop)

    def post(self,path,body,token=True):
        headers=dict(self.headers,Origin='https://fixture.example')
        if token:headers['X-CSRF-Token']=self.token
        return self.client.post(path,base_url='https://fixture.example',headers=headers,json=body,environ_overrides=self.env)

    def get(self,path):
        return self.client.get(path,base_url='https://fixture.example',headers=self.headers,environ_overrides=self.env)

    def test_legacy_web_kills_cannot_bypass_preview(self):
        with patch('wsl_resource_guard.processes.signal_verified_process') as send:
            for path,body in (('/api/sessions/100/kill',{'confirmed':True}),('/api/sessions/kill-stale',{'confirmed':True,'pids':[100]})):
                self.assertEqual(self.post(path,body).status_code,409)
            send.assert_not_called()

    def test_real_controller_preview_execute_replay_and_result_protocol(self):
        identifier=process_identity(self.snap.processes[100])
        detail=self.get('/api/targets/'+identifier)
        self.assertEqual(detail.status_code,200)
        preview=self.post('/api/actions/preview',{'target_id':identifier})
        self.assertEqual(preview.status_code,200)
        token=preview.json['id']
        with patch('wsl_resource_guard.processes.signal_verified_process',return_value=True) as send:
            first=self.post('/api/actions/'+token+'/execute',{'confirmed':True})
            again=self.post('/api/actions/'+token+'/execute',{'confirmed':True})
        self.assertEqual(first.status_code,200);self.assertEqual(again.status_code,200)
        self.assertEqual(send.call_count,2)
        status=self.get('/api/actions/'+token)
        self.assertEqual(status.json['status'],'remaining')
        self.assertNotIn('members',status.json)
        self.assertNotIn('signature',status.json)

    def test_csrf_and_confirmation_are_required_for_new_actions(self):
        identifier=process_identity(self.snap.processes[100])
        self.assertEqual(self.post('/api/actions/preview',{'target_id':identifier},token=False).status_code,403)
        preview=self.post('/api/actions/preview',{'target_id':identifier}).json
        for body in ({},[],{'confirmed':'true'}):
            self.assertEqual(self.post('/api/actions/'+preview['id']+'/execute',body).status_code,400)

    def test_unknown_and_reused_targets_are_not_connected(self):
        identifier=process_identity(self.snap.processes[100])
        self.snap.processes[100].start_ticks+=1
        self.assertEqual(self.get('/api/targets/'+identifier).status_code,404)
        self.assertEqual(self.get('/api/incidents?id='+'a'*32).status_code,404)

    def test_incident_decision_is_scoped_and_does_not_resolve(self):
        store=reconcile({},[condition(),condition('warning.session.100')],self.snap,metrics(),Settings(),boot='fixture')
        atomic_write(self.path/'incidents.json',store)
        identifier=store['incidents'][0]['id']
        result=self.post('/api/incidents/'+identifier+'/decision',{'choice':'defer','minutes':30})
        self.assertEqual(result.status_code,200)
        self.assertEqual(read_store(self.path/'incidents.json')['incidents'][0]['state'],'active')
        decisions=json.loads((self.path/'shared/incident-decisions.json').read_text())
        self.assertEqual(list(decisions),[identifier])


if __name__ == '__main__':
    unittest.main()
