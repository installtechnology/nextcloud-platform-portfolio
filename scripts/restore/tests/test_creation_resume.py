"""Interrupted host preparation: synthetic clients only, no AWS or SSH IO."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from scripts.restore.tests.test_reprovision import config,S3,Operations,preserved_state,plan
from scripts.restore.creation_lease import CreationLease,encoded,digest
from scripts.restore.reprovision import validate_resume_binding,validate_resume_plan,execute
from scripts.restore.reprovision_grant import issue,ResumeObserver
from scripts.restore.prepare_identity import check_idle_preparation
from scripts.restore.reprovision_operations import ReprovisionOperations
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import SealFailure


class Resume(unittest.TestCase):
    def setUp(self):
        self.c=config();self.s3=S3(self.c);self.old=self.lease('123:1','a'*40)
        grant={'version':1,'kind':'recovery-host-creation','owner':self.old.owner,
               'commit':self.old.commit,'configuration_sha256':digest(self.c),'configuration':self.c}
        self.s3.add(self.old.grant_key,grant);self.old.acquire()
        self.host={'account':self.c['account'],'region':self.c['region'],'instance':'i-'+'3'*17}
        self.old.bind(self.host);self.history=copy.deepcopy(self.s3.data[self.old.key])
        self.sts=Mock();self.sts.get_caller_identity.return_value={
            'Account':self.c['account'],'Arn':f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-issuer/admin"}
        self.observer=Mock();self.observer.collect.return_value={
            'workflow':{'repository':self.c['repository'],'run_id':123,'attempt':1,'commit':'a'*40,
                        'status':'completed','conclusion':'cancelled'},
            'state':{'version_id':'state-v2','sha256':'f'*64}}
        self.new=self.lease('124:1','b'*40)

    def lease(self,owner,commit):
        return CreationLease(self.s3,self.c['account'],self.c['state_bucket'],
            f"control/reprovision/{self.c['account']}/{self.c['project']}/lab/",owner,commit,self.c)

    def issue(self,observer=True):
        return issue(self.s3,self.sts,json.dumps(self.c),self.new.owner,self.new.commit,
                     self.observer if observer else None)

    def code(self,code,fn):
        with self.assertRaises((CaptureFailure,SealFailure)) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def test_resume_requires_explicit_admin_observation(self):
        self.code('CREATION_RESUME_OBSERVATION_REQUIRED',lambda:self.issue(False))
        self.assertEqual(self.s3.data[self.old.key],self.history)

    def test_admin_grant_preserves_original_history(self):
        self.issue();self.new.authorize();self.assertEqual(self.new.resume['creation_grant_sha256'],self.old.grant[1])
        self.assertEqual(self.s3.data[self.old.key],self.history)

    def test_resume_acquires_same_host_with_new_owner_not_new_runtime(self):
        self.issue();self.new.acquire()
        self.assertEqual(self.new.record['phase'],'host_bound')
        self.assertEqual(self.new.record['host'],self.host)
        self.assertEqual(self.new.record['owner'],'124:1')
        self.assertFalse(self.new.record['handoff_verified'])
        self.assertEqual(self.s3.data[self.old.key][:-1],self.history)

    def test_changed_previous_version_blocks(self):
        self.issue();self.s3.add(self.old.key,self.old.record)
        self.code('CREATION_RESUME_BINDING_CHANGED',self.new.acquire)

    def test_original_configuration_cannot_change(self):
        self.c['terraform_inputs_sha256']='e'*64
        self.code('CREATION_GRANT_MISMATCH',self.issue)

    def test_observer_exception_sanitized_without_write(self):
        self.observer.collect.side_effect=RuntimeError('synthetic-private-data')
        self.code('CREATION_RESUME_OBSERVATION_FAILED',self.issue)
        self.assertNotIn(self.new.grant_key,self.s3.data)

    def test_unfinished_workflow_blocks(self):
        self.observer.collect.return_value['workflow']['status']='in_progress'
        self.code('CREATION_RESUME_WORKFLOW_UNVERIFIED',self.issue)

    def test_wrong_workflow_commit_blocks(self):
        self.observer.collect.return_value['workflow']['commit']='c'*40
        self.code('CREATION_RESUME_WORKFLOW_UNVERIFIED',self.issue)

    def test_successful_workflow_not_interrupted(self):
        self.observer.collect.return_value['workflow']['conclusion']='success'
        self.code('CREATION_RESUME_WORKFLOW_UNVERIFIED',self.issue)

    def test_missing_state_digest_blocks(self):
        self.observer.collect.return_value['state']['sha256']=''
        self.code('CREATION_RESUME_STATE_UNVERIFIED',self.issue)

    def test_source_instance_blocks(self):
        self.old.put({**self.old.record,'host':{**self.host,'instance':self.c['source_instance']}})
        self.code('CREATION_RESUME_TARGET_REJECTED',self.issue)

    def test_prior_handoff_is_not_overwritten(self):
        self.old.put({**self.old.record,'handoff_verified':True,'phase':'handoff'})
        self.code('CREATION_RECONCILIATION_REQUIRED',self.issue)

    def test_grant_timeout_never_releases_old_lease(self):
        self.s3.fail='after';self.code('CREATION_S3_OUTCOME_UNKNOWN',self.issue)
        self.assertEqual(self.s3.data[self.old.key],self.history)

    def test_repeated_interruption_keeps_original_resource_grant(self):
        self.issue();self.new.acquire();self.new.put({**self.new.record,'phase':'intervention_required'})
        next_lease=self.lease('125:1','c'*40)
        observation=copy.deepcopy(self.observer.collect.return_value)
        observation['workflow'].update(run_id=124,commit='b'*40)
        self.observer.collect.return_value=observation
        issue(self.s3,self.sts,json.dumps(self.c),next_lease.owner,next_lease.commit,self.observer)
        next_lease.acquire()
        self.assertEqual(next_lease.resume['creation_grant_sha256'],self.old.grant[1])
        self.assertEqual(next_lease.record['host'],self.host)

    def test_resume_execution_never_calls_apply_or_absence_check(self):
        self.issue();ops=Mock(spec=['inspect_resume','apply','assert_absent','install'])
        ops.assert_absent=Mock()
        ops.inspect_resume.side_effect=RuntimeError('inspection-failed')
        with self.assertRaisesRegex(RuntimeError,'inspection-failed'):execute(self.new,ops)
        ops.apply.assert_not_called();ops.assert_absent.assert_not_called();ops.install.assert_not_called()
        self.assertEqual(self.s3.data[self.old.key],self.history)

    def test_resume_completes_handoff_without_provisioning(self):
        self.issue();ops=Operations(self.c,self.new.owner)
        ops.inspect_resume=lambda c,l:ops.stage('inspect') or {}
        result=execute(self.new,ops)
        self.assertEqual(ops.calls,['inspect','inspect','install','remote','prepared'])
        self.assertEqual(self.new.record['phase'],'released')
        self.assertTrue(self.new.record['handoff_verified'])
        self.assertFalse(result['backup_complete']);self.assertFalse(result['restore_proven'])

    def test_failed_install_keeps_bound_host_blocked(self):
        self.issue();ops=Operations(self.c,self.new.owner);ops.failure='install'
        ops.inspect_resume=lambda c,l:{}
        self.code('SYNTHETIC_INSTALL',lambda:execute(self.new,ops))
        self.assertEqual(self.new.record['phase'],'intervention_required')
        self.assertEqual(self.new.record['host'],self.host)
        self.assertNotIn('apply',ops.calls)

    def test_wrong_original_grant_cannot_change_resource_binding(self):
        self.issue();vid,raw=self.s3.data[self.new.grant_key][0]
        grant=json.loads(raw);grant['resume']['creation_grant_sha256']='0'*64
        self.s3.data[self.new.grant_key][0]=(vid,encoded(grant))
        self.code('CREATION_RESUME_BINDING_CHANGED',self.new.acquire)


class ResumeAdapter(unittest.TestCase):
    # Reuse setup only; tests below exercise the real adapter with injected SDK.
    lease=Resume.lease
    issue=Resume.issue
    code=Resume.code
    def setUp(self):
        Resume.setUp(self);self.issue();self.new.authorize()
        keep=preserved_state(self.c);rows=plan(self.c)['resource_changes']
        existing={r['type']+'.'+r['name'] for r in keep['resources']}
        for row in rows:
            address=row['address'].split('[',1)[0]
            if address in existing:continue
            kind,name=address.split('.')
            instance={'attributes':copy.deepcopy(row['change']['after'])}
            if '[' in row['address']:instance['index_key']='192.0.2.1/32'
            keep['resources'].append({'mode':'managed','type':kind,'name':name,'instances':[instance]})
        host=next(r for r in keep['resources'] if r['type']=='aws_instance')['instances'][0]['attributes']
        host.update(id=self.host['instance'],tags={'ReprovisionGrant':self.old.grant[1]},
                    vpc_security_group_ids=['sg-fixture'],subnet_id='subnet-fixture')
        keep['outputs']={'instance_id':{'value':self.host['instance']},'public_ip':{'value':'9.9.9.9'}}
        self.state=keep;self.sdk={k:Mock() for k in ('s3','sts','ec2')}
        self.sdk['sts'].get_caller_identity.return_value={'Account':self.c['account']}
        def read(**kw):
            raw=encoded(self.state)
            return {'VersionId':'state-v2','ServerSideEncryption':'AES256','Body':io.BytesIO(raw)}
        self.sdk['s3'].get_object.side_effect=read
        node={'InstanceId':self.host['instance'],'State':{'Name':'running'},
              'PublicIpAddress':'8.8.8.8','PublicDnsName':'ec2-8-8-8-8.example',
              'SecurityGroups':[{'GroupId':'sg-fixture'}],'SubnetId':'subnet-fixture',
              'Tags':[{'Key':'ReprovisionGrant','Value':self.old.grant[1]}]}
        self.sdk['ec2'].describe_instances.return_value={'Reservations':[{'Instances':[node]}]}
        self.ci=Mock();self.ci.paths.return_value=Path('.')
        self.adapter=ReprovisionOperations(self.sdk,self.ci,self.c,self.new.owner,self.new.commit)
        self.adapter.verify_created=Mock()
        self.new.resume['state']['sha256']=hashlib.sha256(encoded(self.state)).hexdigest()

    def test_adapter_reuses_exact_state_and_host(self):
        output=self.adapter.inspect_resume(self.c,self.new)
        self.assertEqual(output['instance_id']['value'],self.host['instance'])
        self.ci.run.assert_not_called();self.sdk['s3'].put_object.assert_not_called()

    def test_adapter_resolves_rebooted_endpoint_without_changing_state(self):
        output=self.adapter.inspect_resume(self.c,self.new)
        self.assertEqual(output['public_ip']['value'],'8.8.8.8')
        self.assertEqual(self.state['outputs']['public_ip']['value'],'9.9.9.9')
        self.adapter.verify_created.assert_called_once_with(self.c,output)
        order=[]
        self.ci.revoke_owned.side_effect=lambda *args,**kw:order.append('cleanup')
        self.adapter.plan=Mock(side_effect=lambda *args:order.append('plan') or {'resource_changes':[]})
        with patch.object(Path,'read_text',return_value=json.dumps({'security_group_id':'sg-fixture'})):
            self.code('CREATION_RESUME_PLAN_CHANGED',lambda:self.adapter.install(self.c,output,self.new))
        self.assertEqual(order,['cleanup','plan'])
        self.ci.connect_created_host.assert_not_called();self.ci.run.assert_not_called()

    def test_adapter_private_endpoint_blocks(self):
        node=self.sdk['ec2'].describe_instances.return_value['Reservations'][0]['Instances'][0]
        node['PublicIpAddress']='10.0.0.1'
        self.code('CREATION_RESUME_ENDPOINT_UNVERIFIED',lambda:self.adapter.inspect_resume(self.c,self.new))

    def test_adapter_network_rebinding_blocks(self):
        node=self.sdk['ec2'].describe_instances.return_value['Reservations'][0]['Instances'][0]
        node['SecurityGroups']=[{'GroupId':'sg-other'}]
        self.code('CREATION_RESUME_TARGET_REJECTED',lambda:self.adapter.inspect_resume(self.c,self.new))

    def test_adapter_changed_state_blocks(self):
        self.state['serial']+=1
        self.code('CREATION_RESUME_STATE_CHANGED',lambda:self.adapter.inspect_resume(self.c,self.new))

    def test_adapter_changed_instance_blocks(self):
        self.state['outputs']['instance_id']['value']='i-'+'4'*17
        self.new.resume['state']['sha256']=hashlib.sha256(encoded(self.state)).hexdigest()
        self.code('CREATION_RESUME_TARGET_REJECTED',lambda:self.adapter.inspect_resume(self.c,self.new))

    def test_adapter_missing_resource_blocks(self):
        self.state['resources'].pop()
        self.new.resume['state']['sha256']=hashlib.sha256(encoded(self.state)).hexdigest()
        self.code('CREATION_RESUME_STATE_INVALID',lambda:self.adapter.inspect_resume(self.c,self.new))

    def test_adapter_another_live_instance_blocks(self):
        node=self.sdk['ec2'].describe_instances.return_value['Reservations'][0]['Instances'][0]
        self.sdk['ec2'].describe_instances.side_effect=lambda **kw:{'Reservations':[{'Instances':
            [node] if 'InstanceIds' in kw else [node,dict(node,InstanceId='i-'+'4'*17)]}]}
        self.code('CREATION_RESUME_TARGET_AMBIGUOUS',lambda:self.adapter.inspect_resume(self.c,self.new))


class ResumePlan(unittest.TestCase):
    def setUp(self):
        self.addresses={'aws_instance.host','aws_s3_bucket.data'}
        self.plan={'resource_changes':[{'mode':'managed','address':a,
            'change':{'actions':['no-op'],'before':{'id':a},'after':{'id':a}}} for a in sorted(self.addresses)],
            'resource_drift':[],'output_changes':{}}
    def reject(self):
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses)
    def test_exact_noop_plan_passes(self):validate_resume_plan(self.plan,self.addresses)
    def test_empty_plan_rejected(self):self.plan['resource_changes']=[];self.reject()
    def test_update_rejected(self):self.plan['resource_changes'][0]['change']['actions']=['update'];self.reject()
    def test_delete_rejected(self):self.plan['resource_changes'][0]['change']['actions']=['delete'];self.reject()
    def test_create_rejected(self):self.plan['resource_changes'][0]['change']['actions']=['create'];self.reject()
    def test_resource_drift_rejected(self):self.plan['resource_drift']=[{}];self.reject()
    def test_duplicate_row_rejected(self):self.plan['resource_changes'].append(self.plan['resource_changes'][0]);self.reject()
    def test_extra_resource_rejected(self):self.plan['resource_changes'][0]['address']='aws_instance.other';self.reject()
    def test_changed_values_under_noop_rejected(self):self.plan['resource_changes'][0]['change']['after']={};self.reject()
    def test_import_rejected(self):self.plan['resource_changes'][0]['change']['importing']={'id':'other'};self.reject()
    def test_changed_output_rejected(self):self.plan['output_changes']={'instance':{'actions':['update']}};self.reject()

    def mirror(self,address,before,after,extra=()):
        self.addresses.add(address)
        self.plan['resource_changes'].append({'mode':'managed','address':address,
            'change':{'actions':['no-op'],'before':after,'after':copy.deepcopy(after)}})
        self.plan['resource_drift']=[{'mode':'managed','address':address,
            'change':{'actions':['update'],'before':before,'after':after}}]
        kind,name=address.split('.')
        return {'resources':[{'mode':'managed','type':kind,'name':name,'instances':[{'attributes':before}]},*extra]}

    def test_empty_tags_refresh_never_changes_real_tags(self):
        before={'id':'fixture','tags':None,'tags_all':{'Project':'fixture'}};after={**before,'tags':{}}
        state=self.mirror('aws_key_pair.operator',before,after)
        validate_resume_plan(self.plan,self.addresses,state,{})
        after['tags']={'Project':'unexpected'}
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,{})

    def test_inline_policy_mirror_requires_exact_owned_permissions(self):
        body={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:GetObject','Resource':'*'}]}
        before={'id':'fixture','name':'fixture','inline_policy':[],'tags':None,'permissions_boundary':'unchanged'}
        after={**before,'tags':{},'inline_policy':[{'name':'owned','policy':json.dumps(body)}]}
        owned={'mode':'managed','type':'aws_iam_role_policy','name':'storage','instances':[
            {'attributes':{'role':'fixture','name':'owned','policy':json.dumps(body)}}]}
        state=self.mirror('aws_iam_role.host',before,after,[owned])
        validate_resume_plan(self.plan,self.addresses,state,{})
        body['Statement'][0]['Action']='s3:DeleteObject';after['inline_policy'][0]['policy']=json.dumps(body)
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,{})

    def test_route_mirror_rejects_other_gateway(self):
        fields=('carrier_gateway_id','cidr_block','core_network_arn','destination_prefix_list_id',
            'egress_only_gateway_id','gateway_id','ipv6_cidr_block','local_gateway_id','nat_gateway_id',
            'network_interface_id','odb_network_arn','transit_gateway_id','vpc_endpoint_id','vpc_peering_connection_id')
        route={**dict.fromkeys(fields,''),'cidr_block':'0.0.0.0/0','gateway_id':'igw-owned'}
        before={'id':'rtb-owned','route':[]};after={**before,'route':[route]}
        owned={'mode':'managed','type':'aws_route','name':'internet','instances':[{'attributes':{
            'route_table_id':'rtb-owned','destination_cidr_block':'0.0.0.0/0','gateway_id':'igw-owned'}}]}
        state=self.mirror('aws_route_table.public',before,after,[owned])
        validate_resume_plan(self.plan,self.addresses,state,{})
        route['gateway_id']='igw-other'
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,{})

    def test_default_acl_refresh_requires_independent_vpc_binding(self):
        before={'id':'vpc-owned','default_network_acl_id':''}
        after={**before,'default_network_acl_id':'acl-'+'a'*17}
        state=self.mirror('aws_vpc.main',before,after)
        node={'VpcId':'vpc-owned','_observed_vpc_id':'vpc-owned'}
        validate_resume_plan(self.plan,self.addresses,state,node)
        node['_observed_vpc_id']='vpc-other'
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,node)

    def test_only_observed_reboot_endpoint_refresh_is_accepted(self):
        before={'id':'i-'+'3'*17,'public_ip':'9.9.9.9','public_dns':'old.example','ami':'unchanged'}
        after={**before,'public_ip':'8.8.8.8','public_dns':'new.example'}
        state={'outputs':{'public_ip':{'value':'9.9.9.9'}},'resources':[
            {'mode':'managed','type':'aws_instance','name':'host','instances':[{'attributes':before}]}]}
        node={'InstanceId':before['id'],'PublicIpAddress':'8.8.8.8','PublicDnsName':'new.example'}
        self.plan['resource_drift']=[{'mode':'managed','address':'aws_instance.host',
                                    'change':{'actions':['update'],'before':before,'after':after}}]
        self.plan['output_changes']={'public_ip':{'actions':['update'],'before':'9.9.9.9','after':'8.8.8.8'}}
        validate_resume_plan(self.plan,self.addresses,state,node)
        after['ami']='changed'
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,node)

    def test_security_group_mirror_requires_exact_owned_rules(self):
        self.addresses.add('aws_security_group.host')
        before={'id':'sg-fixture','ingress':[],'egress':[]}
        rule={'security_group_id':'sg-fixture','cidr_ipv4':'192.0.2.1/32','description':'administrative',
              'ip_protocol':'tcp','from_port':22,'to_port':22}
        mirror={'cidr_blocks':['192.0.2.1/32'],'ipv6_cidr_blocks':[],'prefix_list_ids':[],
                'security_groups':[],'self':False,'description':'administrative',
                'protocol':'tcp','from_port':22,'to_port':22}
        after={**before,'ingress':[mirror]}
        self.plan['resource_changes'].append({'mode':'managed','address':'aws_security_group.host',
            'change':{'actions':['no-op'],'before':after,'after':copy.deepcopy(after)}})
        state={'resources':[{'mode':'managed','type':'aws_security_group','name':'host','instances':[{'attributes':before}]},
            {'mode':'managed','type':'aws_vpc_security_group_ingress_rule','name':'ssh','instances':[{'attributes':rule}]}]}
        self.plan['resource_drift']=[{'mode':'managed','address':'aws_security_group.host',
                                    'change':{'actions':['update'],'before':before,'after':after}}]
        validate_resume_plan(self.plan,self.addresses,state,{})
        mirror['cidr_blocks']=['0.0.0.0/0']
        with self.assertRaisesRegex(SealFailure,'^CREATION_RESUME_PLAN_CHANGED$'):
            validate_resume_plan(self.plan,self.addresses,state,{})


class Observer(unittest.TestCase):
    def setUp(self):
        self.c=config();self.s3=Mock();self.raw=b'{"synthetic":true}'
        self.s3.get_object.side_effect=lambda **kw:{'VersionId':'state-v2','ServerSideEncryption':'AES256',
            'ContentLength':len(self.raw),'Body':io.BytesIO(self.raw)}
        self.observer=ResumeObserver(self.s3)
        self.latest={'id':123,'head_sha':'a'*40,'status':'completed','conclusion':'failure','run_attempt':2}
        self.prior={**self.latest,'conclusion':'cancelled','run_attempt':1}
        self.jobs={'total_count':1,'jobs':[{'status':'completed'}]}
        self.observer.github=Mock(side_effect=lambda path:self.jobs if '/jobs?' in path
            else self.prior if '/attempts/' in path else self.latest)
    def collect(self):return self.observer.collect(self.c,'123:1','a'*40)
    def reject(self,code='CREATION_RESUME_WORKFLOW_UNVERIFIED'):
        with self.assertRaisesRegex(SealFailure,'^'+code+'$'):self.collect()
    def test_real_collector_pins_state_digest(self):
        result=self.collect()
        self.assertEqual(result['state']['sha256'],hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result['workflow']['conclusion'],'cancelled')
        self.s3.put_object.assert_not_called()
    def test_later_queued_attempt_blocks(self):self.latest['status']='queued';self.reject()
    def test_previous_unfinished_attempt_blocks(self):self.prior['status']='in_progress';self.reject()
    def test_partial_jobs_response_blocks(self):self.jobs['total_count']=2;self.reject()
    def test_running_job_blocks(self):self.jobs['jobs'][0]['status']='in_progress';self.reject()
    def test_wrong_commit_blocks(self):self.latest['head_sha']='b'*40;self.reject()
    def test_state_without_sse_blocks(self):
        self.s3.get_object.side_effect=None
        self.s3.get_object.return_value={'VersionId':'state-v2','ContentLength':len(self.raw),
                                       'Body':io.BytesIO(self.raw),'ServerSideEncryption':'none'}
        self.reject('CREATION_RESUME_STATE_UNVERIFIED')
    def test_state_reader_error_is_not_absence(self):
        self.s3.get_object.side_effect=PermissionError('synthetic')
        with self.assertRaises(PermissionError):self.collect()


class IdleProcesses(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.proc=Path(self.temp.name)
        own=self.proc/str(os.getpid());own.mkdir();(own/'status').write_text('PPid:\t0\n')
        self.fcntl=patch.dict('sys.modules',{'fcntl':SimpleNamespace(flock=Mock(),LOCK_EX=2,LOCK_NB=4)})
        self.fcntl.start();self.addCleanup(self.fcntl.stop)
    def process(self,text):
        node=self.proc/'999999';node.mkdir();(node/'cmdline').write_bytes(text)
    def test_idle_host_passes(self):check_idle_preparation(self.proc,())
    def test_orphan_ansible_blocks(self):
        self.process(b'python3\0/tmp/AnsiballZ_copy.py\0')
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_PROCESS_BUSY$'):check_idle_preparation(self.proc,())
    def test_postgres_cli_writer_blocks(self):
        self.process(b'/usr/bin/psql\0')
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_PROCESS_BUSY$'):check_idle_preparation(self.proc,())
    def test_package_installation_blocks(self):
        self.process(b'/usr/bin/dpkg\0--configure\0')
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_PROCESS_BUSY$'):check_idle_preparation(self.proc,())
    def test_unknown_ancestry_blocks(self):
        (self.proc/str(os.getpid())/'status').write_text('unverified')
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_PROCESS_UNVERIFIED$'):check_idle_preparation(self.proc,())

    def root(self,entry=None):
        root=Mock();root.is_symlink.return_value=False;root.exists.return_value=True
        root.is_dir.return_value=True;root.stat.return_value=SimpleNamespace(st_uid=0,st_mode=0o40700)
        root.iterdir.return_value=[] if entry is None else [entry]
        return root
    def lock(self):
        lock=Mock();lock.name='capture.lock'
        lock.lstat.return_value=SimpleNamespace(st_mode=0o100600,st_nlink=1,st_uid=0)
        return lock
    def test_journal_is_never_removed_or_ignored(self):
        entry=self.lock();entry.name='deploy-lease.json'
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_JOURNAL_PRESENT$'):
            check_idle_preparation(self.proc,(self.root(entry),))
        entry.unlink.assert_not_called()
    def test_nonprivate_root_blocks(self):
        root=self.root();root.stat.return_value.st_mode=0o40755
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_JOURNAL_UNSAFE$'):
            check_idle_preparation(self.proc,(root,))
    def test_symlink_root_blocks(self):
        root=self.root();root.is_symlink.return_value=True
        with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_JOURNAL_UNSAFE$'):
            check_idle_preparation(self.proc,(root,))
    def test_active_kernel_lock_blocks(self):
        import fcntl
        fcntl.flock.side_effect=BlockingIOError()
        with patch('scripts.restore.prepare_identity.os.open',return_value=123),\
             patch('scripts.restore.prepare_identity.os.close') as close,\
             patch('scripts.restore.prepare_identity.os.O_NOFOLLOW',131072,create=True):
            with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_LOCK_BUSY$'):
                check_idle_preparation(self.proc,(self.root(self.lock()),))
            close.assert_called_once_with(123)
    def test_handoff_receipt_blocks_preparation(self):
        with patch.object(Path,'exists',lambda p:p.name=='creation-receipt.json'):
            with self.assertRaisesRegex(ValueError,'^RESTORE_PREPARE_HANDOFF_PRESENT$'):
                check_idle_preparation(self.proc,())


if __name__=='__main__':unittest.main()
