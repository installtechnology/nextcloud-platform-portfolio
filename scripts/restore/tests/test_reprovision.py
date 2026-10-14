"""Synthetic S3/EC2/lease fixtures only; no AWS, SSH, Docker or installation."""
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch,Mock
from scripts.restore.creation_lease import CreationLease,encoded,digest
from scripts.restore.reprovision import configuration,execute,validate_plan,assert_empty_state,retained_state,PERSISTENT
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import SealFailure
from scripts.backup.errors import ValidationFailure
from scripts.backup.operational.remote_lease import RemoteDeployLease,AuthenticatedHostChannel
from scripts.backup.operational.ssm import Target
from scripts.restore.tests.test_host_preparation import config as host_fixture,ROOT


def config():
    h=host_fixture();account=h['account'];project=h['project'];destination='restore'
    source={'account_id':account,'region':h['region'],'project':project,'environment':'lab',
            'data_bucket':h['source_data_bucket'],'installation_identity_sha256':'a'*64}
    base=f'arn:aws:iam::{account}:role/{project}-{destination}'
    return {'version':1,'account':account,'region':h['region'],'project':project,'github_environment':'lab',
        'destination_environment':destination,'source_instance':h['source_instance'],
        'source_data_bucket':h['source_data_bucket'],'data_bucket':h['data_bucket'],
        'backups_bucket':h['backups_bucket'],'state_bucket':h['state_bucket'],'images':h['images'],
        'repository':'fixture/platform','source':source,'source_policy_sha256':'b'*64,
        'terraform_inputs_sha256':'c'*64,'grant_issuer_arn':f'arn:aws:iam::{account}:role/fixture-issuer',
        'guard_template':{'version':2,'account':account,'region':h['region'],'project':project,
            'environment':destination,'data_bucket':h['data_bucket'],'backups_bucket':h['backups_bucket'],
            'state_bucket':h['state_bucket'],'installation_identity':'a'*64,'deployment_root':'/opt/nextcloud-platform',
            'staging_root':'/var/lib/nextcloud-recovery','images':h['images'],'admin_role':base+'-data-policy-admin',
            'emergency_role':f'arn:aws:iam::{account}:role/fixture-emergency','backup_role':base+'-backup',
            'guard_role':base+'-deploy-guard','storage_encryption':'SSE-S3','repository':'fixture/platform'}}


def plan(c):
    texts='\n'.join(p.read_text() for p in (ROOT/'terraform/aws/restore').glob('*.tf'))
    rows=[]
    for kind,name in re.findall(r'resource "([^"]+)" "([^"]+)"',texts):
        address=kind+'.'+name;after={}
        if address=='aws_instance.host':after={'instance_type':'t3.medium',
            'metadata_options':[{'http_endpoint':'enabled','http_tokens':'required','http_put_response_hop_limit':2}],
            'root_block_device':[{'volume_size':30,'volume_type':'gp3','encrypted':True}],
            'tags_all':{'Project':c['project'],'Environment':c['destination_environment']}}
        if address=='aws_s3_bucket.data':after={'bucket':c['data_bucket'],'force_destroy':False}
        if address=='aws_iam_role.host':after={'permissions_boundary':f"arn:aws:iam::{c['account']}:policy/{c['project']}-{c['destination_environment']}-ec2-boundary"}
        if address=='aws_s3_bucket_public_access_block.data':after={k:True for k in ('block_public_acls','block_public_policy','ignore_public_acls','restrict_public_buckets')}
        if address=='aws_s3_bucket_versioning.data':after={'versioning_configuration':[{'status':'Enabled'}]}
        if address=='aws_s3_bucket_server_side_encryption_configuration.data':after={'rule':[{'apply_server_side_encryption_by_default':[{'sse_algorithm':'AES256'}]}]}
        if kind=='aws_vpc_security_group_ingress_rule':
            port={'ssh':22,'http':80,'grafana':3000}[name]
            after={'ip_protocol':'tcp','from_port':port,'to_port':port,'cidr_ipv4':'192.0.2.1/32'}
            address+='["192.0.2.1/32"]'
        rows.append({'mode':'managed','address':address,'change':{'actions':['create'],'before':None,'after':after}})
    return {'resource_changes':rows,'resource_drift':[],'errored':False}


def preserved_state(c):
    rows={r['address']:copy.deepcopy(r['change']['after']) for r in plan(c)['resource_changes']
          if r['address'] in PERSISTENT}
    rows['terraform_data.isolation']={'id':'fixture-isolation','input':{'value':c['data_bucket'],'type':'string'}}
    rows['aws_s3_bucket.data'].update(id=c['data_bucket'],arn='arn:aws:s3:::'+c['data_bucket'],
        region=c['region'],tags={'Portfolio':'synthetic'},tags_all={'Project':c['project'],
            'Environment':c['destination_environment'],'Portfolio':'synthetic'})
    for address in PERSISTENT-{'terraform_data.isolation','aws_s3_bucket.data'}:
        rows[address].update(id=c['data_bucket'],bucket=c['data_bucket'])
    rows['aws_s3_bucket_ownership_controls.data']['rule']=[{'object_ownership':'BucketOwnerPreferred'}]
    arn='arn:aws:s3:::'+c['data_bucket']
    rows['aws_s3_bucket_policy.tls']['policy']=json.dumps({'Version':'2012-10-17','Statement':[
        {'Sid':'DenyInsecureTransport','Effect':'Deny','Principal':'*','Action':'s3:*',
         'Resource':[arn,arn+'/*'],'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]})
    return {'version':4,'serial':7,'lineage':'synthetic-preserved','outputs':{},'resources':[
        {'mode':'managed','type':a.split('.')[0],'name':a.split('.')[1],
         'instances':[{'attributes':v}]} for a,v in sorted(rows.items())]}


def preserved_plan(c,document):
    retained=retained_state(document,c);p=plan(c)
    for row in p['resource_changes']:
        if row['address'] in PERSISTENT:
            values=copy.deepcopy(retained[row['address']])
            if row['address']=='terraform_data.isolation':values['input']=values['input']['value']
            row['change']={'actions':['no-op'],'before':values,'after':copy.deepcopy(values)}
    return p


class S3:
    def __init__(self,c):self.data={};self.c=c;self.calls=[];self.fail=None
    def add(self,key,value):
        history=self.data.setdefault(key,[]);raw=encoded(value);vid='v'+str(sum(len(v) for v in self.data.values())+1)
        history.append((vid,raw));return vid
    def get_bucket_policy(self,**kw):
        return {'Policy':json.dumps({'Version':'2012-10-17','Statement':[{
            'Sid':'ExclusiveRecoveryGrantIssuer','Effect':'Deny','Principal':'*',
            'Action':['s3:PutObject','s3:DeleteObject','s3:DeleteObjectVersion'],
            'Resource':f"arn:aws:s3:::{self.c['state_bucket']}/control/reprovision/{self.c['account']}/{self.c['project']}/lab/grants/*",
            'Condition':{'ArnNotEquals':{'aws:PrincipalArn':self.c['grant_issuer_arn']}}},
            {'Sid':'ExclusiveRecoveryCreationJournalWriter','Effect':'Deny','Principal':'*',
             'Action':['s3:PutObject','s3:DeleteObject','s3:DeleteObjectVersion'],
             'Resource':f"arn:aws:s3:::{self.c['state_bucket']}/control/reprovision/{self.c['account']}/{self.c['project']}/lab/lease.json",
             'Condition':{'ArnNotEquals':{'aws:PrincipalArn':[self.c['grant_issuer_arn'],
                 f"arn:aws:iam::{self.c['account']}:role/{self.c['project']}-lab-reprovision"]}}}]})}
    def get_bucket_versioning(self,**kw):return {'Status':'Enabled'}
    def list_object_versions(self,**kw):
        rows=self.data.get(kw['Prefix'],[])
        return {'IsTruncated':False,'Versions':[{'Key':kw['Prefix'],'VersionId':v,'IsLatest':i==len(rows)-1}
                                               for i,(v,raw) in enumerate(rows)],'DeleteMarkers':[]}
    def get_object(self,**kw):
        for vid,raw in self.data[kw['Key']]:
            if vid==kw['VersionId']:
                return {'VersionId':vid,'Body':io.BytesIO(raw),'ContentLength':len(raw),
                        'ServerSideEncryption':'AES256','ETag':'"'+hashlib.sha256(raw).hexdigest()+'"'}
        raise RuntimeError('synthetic-secret')
    def put_object(self,**kw):
        self.calls.append(kw)
        old=self.data.get(kw['Key'],[])
        if kw.get('IfNoneMatch')=='*' and old:raise RuntimeError('collision')
        if 'IfMatch' in kw and (not old or kw['IfMatch']!='"'+hashlib.sha256(old[-1][1]).hexdigest()+'"'):
            raise RuntimeError('collision')
        if self.fail=='before':raise TimeoutError('synthetic-secret')
        vid=self.add(kw['Key'],json.loads(kw['Body']))
        if self.fail=='after':raise TimeoutError('synthetic-secret')
        return {'VersionId':vid}


class Channel(AuthenticatedHostChannel):
    def request(self,r):return {**r,'phase':'released' if r['command']=='release' else 'held'}


class Operations:
    def __init__(self,c,owner):self.c,self.owner=c,owner;self.calls=[];self.failure=None
    def stage(self,name):
        self.calls.append(name)
        if self.failure==name:raise CaptureFailure('SYNTHETIC_'+name.upper())
    def assert_absent(self,c):self.stage('absence')
    def plan(self,c,lease):self.stage('plan');return plan(c)
    def apply(self,c,lease):self.stage('apply');return {}
    def verify_created(self,c,outputs):
        self.stage('identity');return {'account':c['account'],'region':c['region'],'instance':'i-'+'3'*17}
    def install(self,c,outputs,lease):self.stage('install')
    @contextmanager
    def remote_lease(self,c,outputs,lease):
        self.stage('remote')
        target=Target(c['account'],c['region'],'i-'+'3'*17,'fixture-lab-reprovision')
        with RemoteDeployLease(Channel(),target,self.owner).hold() as remote:yield remote
    def verify_prepared(self,c,outputs,lease):self.stage('prepared')


class Creation(unittest.TestCase):
    def setUp(self):
        self.c=config();self.s3=S3(self.c);self.owner='123:1';self.lease=self.make(self.owner)
        self.grant(self.lease);self.ops=Operations(self.c,self.owner)
    def make(self,owner):
        c=self.c;return CreationLease(self.s3,c['account'],c['state_bucket'],
            f"control/reprovision/{c['account']}/{c['project']}/lab/",owner,'a'*40,c)
    def grant(self,l):
        self.s3.add(l.grant_key,{'version':1,'kind':'recovery-host-creation','owner':l.owner,'commit':l.commit,
            'configuration_sha256':digest(self.c),'configuration':self.c})
    def code(self,code,fn):
        with self.assertRaises((CaptureFailure,SealFailure,ValidationFailure)) as caught:fn()
        self.assertEqual(caught.exception.code,code);self.assertNotIn('synthetic-secret',str(caught.exception))
    def test_configuration_valid(self):self.assertEqual(configuration(json.dumps(self.c)),self.c)
    def test_plan_from_existing_root_allowlist(self):validate_plan(plan(self.c),self.c)
    def test_preserved_state_and_create_only_runtime(self):
        state=preserved_state(self.c);keep=retained_state(state,self.c)
        validate_plan(preserved_plan(self.c,state),self.c,keep)
        self.assertEqual(set(keep),PERSISTENT)
    def test_preserved_runtime_in_state_rejected(self):
        state=preserved_state(self.c);state['resources'].append({'mode':'managed','type':'aws_instance',
            'name':'host','instances':[{'attributes':{}}]})
        self.code('CREATION_RETAINED_STATE_INVALID',lambda:retained_state(state,self.c))
    def test_preserved_incomplete_set_rejected(self):
        state=preserved_state(self.c);state['resources'].pop()
        self.code('CREATION_RETAINED_STATE_INCOMPLETE',lambda:retained_state(state,self.c))
    def test_preserved_foreign_bucket_rejected(self):
        state=preserved_state(self.c)
        row=next(r for r in state['resources'] if r['type']=='aws_s3_bucket')
        row['instances'][0]['attributes']['bucket']=self.c['source_data_bucket']
        self.code('CREATION_RETAINED_BUCKET_MISMATCH',lambda:retained_state(state,self.c))
    def test_preserved_unknown_module_duplicate_and_deposed_rejected(self):
        for mutation in ('module','duplicate','deposed','tainted'):
            with self.subTest(mutation=mutation):
                state=preserved_state(self.c);row=state['resources'][0]
                if mutation=='module':row['module']='module.foreign'
                elif mutation=='duplicate':state['resources'].append(copy.deepcopy(row))
                elif mutation=='deposed':row['instances'][0]['deposed']='old'
                else:row['instances'][0]['status']='tainted'
                self.code('CREATION_RETAINED_STATE_INVALID',lambda:retained_state(state,self.c))
    def test_preserved_storage_guards(self):
        for kind,key,value,code in [
            ('aws_s3_bucket','region','eu-west-1','CREATION_RETAINED_BUCKET_MISMATCH'),
            ('aws_s3_bucket_public_access_block','block_public_acls',False,'CREATION_PLAN_STORAGE_PROTECTION_REJECTED'),
            ('aws_s3_bucket_versioning','versioning_configuration',[{'status':'Suspended'}],'CREATION_PLAN_VERSIONING_REJECTED'),
            ('aws_s3_bucket_ownership_controls','rule',[{'object_ownership':'BucketOwnerEnforced'}],'CREATION_RETAINED_OWNERSHIP_REJECTED'),
            ('aws_s3_bucket_policy','policy','{"Version":"2012-10-17","Statement":[]}','CREATION_RETAINED_TLS_REJECTED')]:
            with self.subTest(kind=kind):
                state=preserved_state(self.c)
                next(r for r in state['resources'] if r['type']==kind)['instances'][0]['attributes'][key]=value
                self.code(code,lambda:retained_state(state,self.c))
    def test_preserved_policy_is_never_updated_replaced_deleted_or_created(self):
        state=preserved_state(self.c);keep=retained_state(state,self.c)
        for actions in (['update'],['delete'],['delete','create'],['create'],['forget']):
            with self.subTest(actions=actions):
                p=preserved_plan(self.c,state)
                next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket_policy.tls')['change']['actions']=actions
                self.code('CREATION_PERSISTENT_CHANGE_REJECTED',lambda:validate_plan(p,self.c,keep))
    def test_noop_requires_observed_state(self):
        self.code('CREATION_PLAN_NOT_CREATE_ONLY',lambda:validate_plan(preserved_plan(self.c,preserved_state(self.c)),self.c))
    def test_preserved_plan_cannot_substitute_identity(self):
        state=preserved_state(self.c);p=preserved_plan(self.c,state)
        row=next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket.data')
        row['change']['before']['id']='foreign';row['change']['after']['id']='foreign'
        self.code('CREATION_PERSISTENT_STATE_MISMATCH',lambda:validate_plan(p,self.c,retained_state(state,self.c)))
    def test_preserved_runtime_noop_remains_blocked(self):
        state=preserved_state(self.c);p=preserved_plan(self.c,state)
        next(r for r in p['resource_changes'] if r['address']=='aws_instance.host')['change']['actions']=['no-op']
        self.code('CREATION_PLAN_NOT_CREATE_ONLY',lambda:validate_plan(p,self.c,retained_state(state,self.c)))
    def test_import_into_preserved_state_blocked(self):
        state=preserved_state(self.c);p=preserved_plan(self.c,state)
        next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket.data')['change']['importing']={'id':'foreign'}
        self.code('CREATION_PERSISTENT_CHANGE_REJECTED',lambda:validate_plan(p,self.c,retained_state(state,self.c)))
    def test_owned_policy_computed_mirror_refresh_only(self):
        state=preserved_state(self.c);keep=retained_state(state,self.c);p=preserved_plan(self.c,state)
        before=copy.deepcopy(keep['aws_s3_bucket.data']);after=copy.deepcopy(before)
        after['policy']=keep['aws_s3_bucket_policy.tls']['policy']
        p['resource_drift']=[{'mode':'managed','address':'aws_s3_bucket.data',
            'change':{'actions':['update'],'before':before,'after':after}}]
        row=next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket.data')
        row['change']['before']=after;row['change']['after']=copy.deepcopy(after)
        validate_plan(p,self.c,keep)
        self.assertNotIn('policy',keep['aws_s3_bucket.data'])
    def test_policy_mirror_cannot_mask_unrelated_drift(self):
        for mutation in ('policy','tag','second_resource','before_identity'):
            with self.subTest(mutation=mutation):
                state=preserved_state(self.c);keep=retained_state(state,self.c);p=preserved_plan(self.c,state)
                before=copy.deepcopy(keep['aws_s3_bucket.data']);after=copy.deepcopy(before)
                after['policy']=keep['aws_s3_bucket_policy.tls']['policy']
                if mutation=='policy':after['policy']=after['policy'].replace('false','true')
                elif mutation=='tag':after['tags_all']['Unexpected']='external'
                elif mutation=='before_identity':before['id']='other'
                drift={'mode':'managed','address':'aws_s3_bucket.data','change':{
                    'actions':['update'],'before':before,'after':after}}
                p['resource_drift']=[drift,copy.deepcopy(drift)] if mutation=='second_resource' else [drift]
                self.code('CREATION_PLAN_DRIFT',lambda:validate_plan(p,self.c,keep))
    def refreshed_mirrors(self):
        state=preserved_state(self.c)
        attrs=next(r for r in state['resources'] if r['type']=='aws_s3_bucket')['instances'][0]['attributes']
        attrs.update(tags=None,policy='',versioning=[{'enabled':False,'mfa_delete':False}])
        keep=retained_state(state,self.c);p=preserved_plan(self.c,state)
        before=copy.deepcopy(keep['aws_s3_bucket.data']);after=copy.deepcopy(before)
        after.update(tags={},policy=keep['aws_s3_bucket_policy.tls']['policy'],
                     versioning=[{'enabled':True,'mfa_delete':False}])
        p['resource_drift']=[{'mode':'managed','address':'aws_s3_bucket.data',
                             'change':{'actions':['update'],'before':before,'after':after}}]
        row=next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket.data')
        row['change']['before']=after;row['change']['after']=copy.deepcopy(after)
        return keep,p
    def test_three_computed_mirrors_refresh_without_mutation(self):
        keep,p=self.refreshed_mirrors();before=copy.deepcopy(keep)
        validate_plan(p,self.c,keep);self.assertEqual(keep,before)
    def test_empty_tags_and_versioning_mirrors_refresh_individually(self):
        for field in ('tags','versioning'):
            with self.subTest(field=field):
                keep,p=self.refreshed_mirrors();change=p['resource_drift'][0]['change']
                for other in {'policy','tags','versioning'}-{field}:change['after'][other]=change['before'][other]
                row=next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket.data')
                row['change']['after']=copy.deepcopy(change['after'])
                validate_plan(p,self.c,keep)
    def test_mirrors_cannot_mask_tag_mutation(self):
        for field,value in [('tags',{'Unexpected':'value'}),('tags_all',{'Unexpected':'value'})]:
            with self.subTest(field=field):
                keep,p=self.refreshed_mirrors();p['resource_drift'][0]['change']['after'][field]=value
                self.code('CREATION_PLAN_DRIFT',lambda:validate_plan(p,self.c,keep))
    def test_mirrors_cannot_erase_preexisting_explicit_tags(self):
        keep,p=self.refreshed_mirrors();keep['aws_s3_bucket.data']['tags']={'Portfolio':'synthetic'}
        p['resource_drift'][0]['change']['before']['tags']={'Portfolio':'synthetic'}
        self.code('CREATION_PLAN_DRIFT',lambda:validate_plan(p,self.c,keep))
    def test_mirrors_cannot_mask_versioning_or_mfa_mutation(self):
        for value in ([{'enabled':False,'mfa_delete':False}],[{'enabled':True,'mfa_delete':True}],
                      [{'enabled':1,'mfa_delete':False}],[{'enabled':True,'mfa_delete':0}],[]):
            with self.subTest(value=value):
                keep,p=self.refreshed_mirrors();p['resource_drift'][0]['change']['after']['versioning']=value
                if value==[{'enabled':False,'mfa_delete':False}]:
                    p['resource_drift'][0]['change']['before']['versioning']=[{'enabled':True,'mfa_delete':False}]
                    keep['aws_s3_bucket.data']['versioning']=[{'enabled':True,'mfa_delete':False}]
                self.code('CREATION_PLAN_DRIFT',lambda:validate_plan(p,self.c,keep))
    def test_mirrors_do_not_authorize_persistent_update(self):
        keep,p=self.refreshed_mirrors()
        next(r for r in p['resource_changes'] if r['address']=='aws_s3_bucket_versioning.data')['change']['actions']=['update']
        self.code('CREATION_PERSISTENT_CHANGE_REJECTED',lambda:validate_plan(p,self.c,keep))
    def test_creation_and_handoff(self):
        result=execute(self.lease,self.ops)
        self.assertEqual(self.lease.record['phase'],'released');self.assertFalse(result['restore_proven'])
        self.assertFalse(result['backup_complete']);self.assertFalse(result['destroy_authorized'])
        self.assertLess(self.ops.calls.index('apply'),self.ops.calls.index('install'))
        self.assertLess(self.ops.calls.index('install'),self.ops.calls.index('remote'))
    def test_no_grant(self):
        self.s3.data.pop(self.lease.grant_key);self.code('CREATION_GRANT_NOT_UNIQUE',self.lease.acquire)
        self.assertFalse(self.s3.calls)
    def test_grant_duplicate_versions_rejected(self):
        self.grant(self.lease);self.code('CREATION_GRANT_NOT_UNIQUE',self.lease.acquire)
    def test_wrong_commit_rejected(self):
        self.lease.commit='b'*40;self.code('CREATION_GRANT_MISMATCH',self.lease.acquire)
    def test_wrong_run_rejected(self):
        l=self.make('123:2');self.code('CREATION_GRANT_NOT_UNIQUE',l.acquire)
    def test_wrong_configuration_rejected(self):
        self.c['data_bucket']='fixture-other-data';self.code('CREATION_GRANT_MISMATCH',self.lease.acquire)
    def test_incomplete_capture_does_not_authorize_creation(self):
        self.s3.data[self.lease.grant_key]=[]
        self.s3.add(self.lease.grant_key,{'backup_complete':True,'restore_proven':True})
        self.code('CREATION_GRANT_MISMATCH',self.lease.acquire)
    def test_concurrent_runs_blocked(self):
        self.lease.acquire();l=self.make('124:1');self.grant(l)
        self.code('CREATION_RECONCILIATION_REQUIRED',l.acquire)
    def test_same_grant_never_reused(self):
        execute(self.lease,self.ops);self.code('CREATION_GRANT_ALREADY_USED',self.make(self.owner).acquire)
    def test_new_grant_after_verified_handoff(self):
        execute(self.lease,self.ops);l=self.make('124:1');self.grant(l);l.acquire()
        self.assertEqual(l.record['owner'],'124:1')
    def test_failure_each_transition_preserves_authority(self):
        for step in ('absence','plan','apply','identity','install','remote','prepared'):
            with self.subTest(step=step):
                self.setUp();self.ops.failure=step
                self.code('SYNTHETIC_'+step.upper(),lambda:execute(self.lease,self.ops))
                self.assertEqual(self.lease.record['phase'],'intervention_required')
                self.assertNotEqual(self.lease.record['phase'],'released')
    def test_ambiguous_put_after_server_commit_retained(self):
        self.s3.fail='after';self.code('CREATION_S3_OUTCOME_UNKNOWN',self.lease.acquire)
        self.s3.fail=None;l=self.make('124:1');self.grant(l)
        self.code('CREATION_RECONCILIATION_REQUIRED',l.acquire)
    def test_unknown_put_before_commit_not_retried(self):
        self.s3.fail='before';self.code('CREATION_S3_OUTCOME_UNKNOWN',self.lease.acquire)
        self.assertEqual(len(self.s3.calls),1)
    def test_authority_changed_blocks(self):
        self.lease.acquire();self.s3.add(self.lease.key,{**self.lease.record,'owner':'124:1'})
        self.code('CREATION_AUTHORITY_LOST',self.lease.assert_held)
    def test_grant_changed_blocks(self):
        self.lease.acquire();self.grant(self.lease);self.code('CREATION_GRANT_CHANGED',self.lease.assert_held)
    def test_no_timeout_release(self):
        self.lease.acquire();l=self.make('124:1');self.grant(l)
        self.code('CREATION_RECONCILIATION_REQUIRED',l.acquire)
    def test_without_handoff_cannot_release(self):
        def fn():
            with self.lease.hold():pass
        self.code('CREATION_HANDOFF_REQUIRED',fn)
        self.assertEqual(self.lease.record['phase'],'intervention_required')
    def test_delete_markers_rejected(self):
        with patch.object(self.s3,'list_object_versions',return_value={'IsTruncated':False,'Versions':[],
            'DeleteMarkers':[{'Key':self.lease.key}]}):self.code('CREATION_HISTORY_DELETED',self.lease.acquire)
    def test_wrong_latest_flags_rejected(self):
        with patch.object(self.s3,'list_object_versions',return_value={'IsTruncated':False,'Versions':[
            {'Key':self.lease.grant_key,'VersionId':'v1','IsLatest':False}]}):
            self.code('CREATION_HISTORY_INVALID',self.lease.acquire)
    def test_invalid_pagination_rejected(self):
        with patch.object(self.s3,'list_object_versions',return_value={'IsTruncated':True,'Versions':[]}):
            self.code('CREATION_HISTORY_INVALID',self.lease.acquire)
    def test_unexpected_prefix_row_rejected(self):
        with patch.object(self.s3,'list_object_versions',return_value={'IsTruncated':False,'Versions':[
            {'Key':'other','VersionId':'v1','IsLatest':True}]}):self.code('CREATION_HISTORY_INVALID',self.lease.acquire)
    def test_plaintext_at_rest_rejected(self):
        old=self.s3.get_object
        with patch.object(self.s3,'get_object',side_effect=lambda **kw:{**old(**kw),'ServerSideEncryption':'none'}):
            self.code('CREATION_OBJECT_INVALID',self.lease.acquire)
    def test_duplicate_json_keys_rejected(self):
        self.s3.data[self.lease.grant_key][0]=('v1',b'{"version":1,"version":1}')
        self.code('DUPLICATE_JSON_KEY',self.lease.acquire)
        self.assertFalse(self.s3.calls)
    def test_exclusive_issuer_policy_required(self):
        with patch.object(self.s3,'get_bucket_policy',return_value={'Policy':json.dumps({'Version':'2012-10-17',
            'Statement':[{'Sid':'TLS','Effect':'Deny','Principal':'*','Action':'s3:*','Resource':'arn:aws:s3:::fixture-state/*'}]})}):
            self.code('CREATION_GRANT_ISSUER_NOT_EXCLUSIVE',self.lease.acquire)
    def test_versioning_required(self):
        with patch.object(self.s3,'get_bucket_versioning',return_value={'Status':'Suspended'}):
            self.code('CREATION_VERSIONING_REQUIRED',self.lease.acquire)
    def test_exclusive_journal_writer_required(self):
        policy=json.loads(self.s3.get_bucket_policy()['Policy']);policy['Statement'].pop()
        with patch.object(self.s3,'get_bucket_policy',return_value={'Policy':json.dumps(policy)}):
            self.code('CREATION_JOURNAL_WRITER_NOT_EXCLUSIVE',self.lease.acquire)
    def test_grant_wait_only_for_absence(self):
        self.s3.data.pop(self.lease.grant_key);now=[0]
        def tick(seconds):now[0]+=seconds;self.grant(self.lease)
        self.lease.authorize(5,clock=lambda:now[0],sleep=tick)
        self.assertEqual(now[0],5);self.assertFalse(self.s3.calls)
    def test_grant_wait_expires_without_writing(self):
        self.s3.data.pop(self.lease.grant_key);now=[0]
        self.code('CREATION_GRANT_NOT_UNIQUE',lambda:self.lease.authorize(
            10,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds)))
        self.assertEqual(now[0],10);self.assertFalse(self.s3.calls)
    def test_wait_never_retries_invalid_grant(self):
        sleep=Mock();self.grant(self.lease)
        self.code('CREATION_GRANT_NOT_UNIQUE',lambda:self.lease.authorize(300,sleep=sleep))
        sleep.assert_not_called();self.assertFalse(self.s3.calls)
    def test_wait_limit(self):self.code('CREATION_GRANT_WAIT_INVALID',lambda:self.lease.authorize(301))
    def test_prior_state_unknown_properties_rejected(self):
        execute(self.lease,self.ops);self.s3.add(self.lease.key,{**self.lease.record,'unlock':True})
        l=self.make('124:1');self.grant(l);self.code('CREATION_PRIOR_STATE_INVALID',l.acquire)
    def test_administrative_grant(self):
        from scripts.restore.reprovision_grant import issue
        self.s3.data.pop(self.lease.grant_key)
        sts=Mock();sts.get_caller_identity.return_value={'Account':self.c['account'],
            'Arn':f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-issuer/review"}
        result=issue(self.s3,sts,json.dumps(self.c),self.owner,'a'*40)
        self.assertEqual(result['code'],'CREATION_GRANT_ISSUED')
        self.lease.acquire()
    def test_backup_role_cannot_issue_grant(self):
        from scripts.restore.reprovision_grant import issue
        sts=Mock();sts.get_caller_identity.return_value={'Account':self.c['account'],
            'Arn':f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-lab-backup/review"}
        self.code('CREATION_ADMINISTRATIVE_ISSUER_REQUIRED',lambda:issue(self.s3,sts,json.dumps(self.c),self.owner,'a'*40))
        self.assertFalse(self.s3.calls)
    def test_administrative_grant_never_overwrites(self):
        from scripts.restore.reprovision_grant import issue
        sts=Mock();sts.get_caller_identity.return_value={'Account':self.c['account'],
            'Arn':f"arn:aws:sts::{self.c['account']}:assumed-role/fixture-issuer/review"}
        self.code('CREATION_GRANT_ALREADY_EXISTS',lambda:issue(self.s3,sts,json.dumps(self.c),self.owner,'a'*40))
        self.assertFalse(self.s3.calls)
    def test_never_calls_delete(self):
        execute(self.lease,self.ops)
        self.assertTrue(all('IfMatch' in r or r.get('IfNoneMatch')=='*' for r in self.s3.calls))
        self.assertTrue(all(r['ServerSideEncryption']=='AES256' for r in self.s3.calls))
    def test_plan_delete_update_replace_import_rejected(self):
        for actions in (['delete'],['update'],['delete','create'],['no-op']):
            with self.subTest(actions=actions):
                p=plan(self.c);p['resource_changes'][0]['change']['actions']=actions
                self.code('CREATION_PLAN_NOT_CREATE_ONLY',lambda:validate_plan(p,self.c))
    def test_plan_drift_rejected(self):
        p=plan(self.c);p['resource_drift']=[{}];self.code('CREATION_PLAN_DRIFT',lambda:validate_plan(p,self.c))
    def test_plan_unknown_resource_rejected(self):
        p=plan(self.c);p['resource_changes'].append({'mode':'managed','address':'aws_s3_bucket.source'})
        self.code('CREATION_PLAN_SCOPE_REJECTED',lambda:validate_plan(p,self.c))
    def test_plan_missing_protections_rejected(self):
        p=plan(self.c);p['resource_changes']=[r for r in p['resource_changes'] if r['address']!='aws_s3_bucket_policy.tls']
        self.code('CREATION_PLAN_INCOMPLETE',lambda:validate_plan(p,self.c))
    def test_plan_wrong_bucket_rejected(self):
        p=plan(self.c)
        for r in p['resource_changes']:
            if r['address']=='aws_s3_bucket.data':r['change']['after']['bucket']=self.c['source_data_bucket']
        self.code('CREATION_PLAN_TARGET_MISSING',lambda:validate_plan(p,self.c))
    def test_plan_mandatory_storage_controls(self):
        for address,key,value,code in [
                ('aws_s3_bucket.data','force_destroy',True,'CREATION_PLAN_STORAGE_PROTECTION_REJECTED'),
                ('aws_iam_role.host','permissions_boundary','other','CREATION_PLAN_HOST_BOUNDARY_REJECTED'),
                ('aws_s3_bucket_public_access_block.data','block_public_acls',False,'CREATION_PLAN_STORAGE_PROTECTION_REJECTED'),
                ('aws_s3_bucket_versioning.data','versioning_configuration',[{'status':'Suspended'}],'CREATION_PLAN_VERSIONING_REJECTED'),
                ('aws_instance.host','metadata_options',[],'CREATION_PLAN_IMDS_REJECTED'),
                ('aws_s3_bucket_server_side_encryption_configuration.data','rule',[],'CREATION_PLAN_STORAGE_ENCRYPTION_REJECTED')]:
            with self.subTest(address=address):
                p=plan(self.c);next(r for r in p['resource_changes'] if r['address']==address)['change']['after'][key]=value
                self.code(code,lambda:validate_plan(p,self.c))
    def test_plan_open_ingress_rejected(self):
        p=plan(self.c)
        for r in p['resource_changes']:
            if r['address'].startswith('aws_vpc_security_group_ingress_rule.ssh'):r['change']['after']['cidr_ipv4']='0.0.0.0/0'
        self.code('CREATION_PLAN_CIDR_REJECTED',lambda:validate_plan(p,self.c))
    def test_existing_state_resources_never_discarded(self):
        self.code('CREATION_STATE_NOT_EMPTY',lambda:assert_empty_state({'version':4,'serial':1,'lineage':'fixture',
                  'resources':[{'type':'aws_instance'}],'outputs':{}}))
    def test_empty_state_accepted(self):assert_empty_state({'version':4,'serial':0,'lineage':'fixture','resources':[],'outputs':{}})
    def test_bad_configuration_rejected(self):
        for key,value in [('version',2),('account','*'),('destination_environment','lab'),('data_bucket',self.c['source_data_bucket'])]:
            with self.subTest(key=key),self.assertRaises((CaptureFailure,SealFailure)):
                configuration(json.dumps({**self.c,key:value}))
    def test_unknown_config_key_rejected(self):
        self.code('CREATION_CONFIGURATION_INVALID',lambda:configuration(json.dumps({**self.c,'force':True})))
    def test_recovery_runtime_source_unchanged(self):
        text=(ROOT/'.github/workflows/deploy.yml').read_text()
        self.assertIn('cancel-in-progress: false',text);self.assertIn('create_restore_target',text)
        self.assertIn('scripts.backup.operational.runner deploy',text)
    def test_workflow_authentication_and_lease_routes_exclusive(self):
        import yaml
        doc=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())
        steps=doc['jobs']['deploy']['steps']
        auth=next(s for s in steps if s['name']=='Authenticate AWS using OIDC')
        self.assertEqual(auth['if'],"inputs.purpose != 'create_restore_target'")
        normal=next(s for s in steps if 'scripts.backup.operational.runner deploy' in s.get('run',''))
        cold=next(s for s in steps if 'scripts.restore.reprovision_runner' in s.get('run',''))
        self.assertEqual(normal['if'],"inputs.mode == 'apply' && inputs.purpose != 'create_restore_target'")
        self.assertEqual(cold['if'],"inputs.mode == 'apply' && inputs.purpose == 'create_restore_target'")
        self.assertEqual(set(cold['env']),{'SSH_PRIVATE_KEY'})
        self.assertEqual(doc['jobs']['deploy']['concurrency']['cancel-in-progress'],False)
    def test_defaults_do_not_discover_or_create(self):
        import yaml
        deploy=yaml.safe_load((ROOT/'.github/workflows/deploy.yml').read_text())
        restore=yaml.safe_load((ROOT/'.github/workflows/restore.yml').read_text())
        self.assertEqual(deploy[True]['workflow_dispatch']['inputs']['purpose']['default'],'new_installation')
        self.assertEqual(restore[True]['workflow_dispatch']['inputs']['target_selection']['default'],'configured')
    def test_issuer_cannot_be_operational(self):
        c={**self.c,'grant_issuer_arn':f"arn:aws:iam::{self.c['account']}:role/{self.c['project']}-lab-reprovision"}
        self.code('CREATION_ISSUER_NOT_INDEPENDENT',lambda:configuration(json.dumps(c)))


if __name__=='__main__':unittest.main()
