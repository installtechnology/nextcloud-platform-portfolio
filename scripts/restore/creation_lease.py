"""Versioned S3 creation authority; no expiry, deletion or implicit recovery.

Used only before a recovery host exists. It does not replace the existing host
lease and never grants backup access to Terraform state. Clients are injected;
the operational constructor in reprovision uses pinned, explicit AWS credentials.
"""
from contextlib import contextmanager
import hashlib
import json
import re
import time
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import require

LIMIT=131072


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def digest(value):return hashlib.sha256(encoded(value)).hexdigest()


class CreationLease:
    def __init__(self,s3,account,bucket,prefix,owner,commit,configuration):
        require(re.fullmatch('[0-9]{12}',account) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',owner)
                and re.fullmatch('[a-f0-9]{40}',commit),'CREATION_CONTEXT_REJECTED')
        require(prefix==f"control/reprovision/{account}/{configuration['project']}/{configuration['github_environment']}/",
                'CREATION_PREFIX_REJECTED')
        self.s3,self.account,self.bucket,self.prefix=s3,account,bucket,prefix
        self.owner,self.commit,self.configuration=owner,commit,configuration
        self.key=prefix+'lease.json';self.grant_key=prefix+'grants/'+owner.replace(':','-')+'.json'
        self.record=None;self.etag=None;self.version=None;self.grant=None
        self.reconciliation_receipt=None
        self.resume=None

    def call(self,name,**kwargs):
        try:return getattr(self.s3,name)(**kwargs,ExpectedBucketOwner=self.account)
        except Exception:raise CaptureFailure('CREATION_S3_OUTCOME_UNKNOWN') from None

    def versions(self,key):
        result=[];seen=set();cursor={}
        for _ in range(100):
            page=self.call('list_object_versions',Bucket=self.bucket,Prefix=key,MaxKeys=100,**cursor)
            require(isinstance(page,dict) and type(page.get('IsTruncated')) is bool,'CREATION_HISTORY_INVALID')
            require(page.get('DeleteMarkers',[])==[],'CREATION_HISTORY_DELETED')
            rows=page.get('Versions',[]);require(isinstance(rows,list),'CREATION_HISTORY_INVALID')
            for row in rows:
                require(isinstance(row,dict) and row.get('Key')==key and type(row.get('IsLatest')) is bool
                        and isinstance(row.get('VersionId'),str) and row['VersionId'] not in ('','null')
                        and row['VersionId'] not in seen,'CREATION_HISTORY_INVALID')
                seen.add(row['VersionId']);result.append(row)
                require(len(result)<=1000,'CREATION_HISTORY_LIMIT')
            if not page['IsTruncated']:
                require(sum(r['IsLatest'] for r in result)==(1 if result else 0),'CREATION_HISTORY_INVALID')
                return result
            next_cursor={k:page.get(v) for k,v in [('KeyMarker','NextKeyMarker'),('VersionIdMarker','NextVersionIdMarker')]}
            require(all(isinstance(v,str) and v for v in next_cursor.values()) and next_cursor!=cursor,
                    'CREATION_HISTORY_INVALID')
            cursor=next_cursor
        raise CaptureFailure('CREATION_HISTORY_LIMIT')

    def read(self,key,version):
        value=self.call('get_object',Bucket=self.bucket,Key=key,VersionId=version)
        body=value.get('Body');require(body is not None,'CREATION_OBJECT_INVALID')
        try:
            raw=body.read(LIMIT+1)
            require(isinstance(raw,bytes) and len(raw)<=LIMIT and value.get('VersionId')==version
                    and type(value.get('ContentLength')) is int and value['ContentLength']==len(raw)
                    and value.get('ServerSideEncryption')=='AES256','CREATION_OBJECT_INVALID')
            doc=parse_json(raw,Limits(json_bytes=LIMIT))
            require(isinstance(doc,dict) and isinstance(value.get('ETag'),str) and value['ETag'],
                    'CREATION_OBJECT_INVALID')
            return doc,value['ETag']
        finally:body.close()

    def verify_authority(self):
        raw=self.call('get_bucket_policy',Bucket=self.bucket).get('Policy')
        from scripts.backup.operational.fence_policy import policy_document
        policy=policy_document(raw)
        expected={'Sid':'ExclusiveRecoveryGrantIssuer','Effect':'Deny','Principal':'*',
                  'Action':['s3:PutObject','s3:DeleteObject','s3:DeleteObjectVersion'],
                  'Resource':f'arn:aws:s3:::{self.bucket}/{self.prefix}grants/*',
                  'Condition':{'ArnNotEquals':{'aws:PrincipalArn':self.configuration['grant_issuer_arn']}}}
        actual=[s for s in policy['Statement'] if s.get('Sid')=='ExclusiveRecoveryGrantIssuer']
        if len(actual)==1:
            actual=[dict(actual[0])]
            actions=actual[0].get('Action');actual[0]['Action']=sorted(actions) if isinstance(actions,list) else actions
            expected['Action']=sorted(expected['Action'])
        require(actual==[expected],
                'CREATION_GRANT_ISSUER_NOT_EXCLUSIVE')
        expected={'Sid':'ExclusiveRecoveryCreationJournalWriter','Effect':'Deny','Principal':'*',
                  'Action':sorted(['s3:PutObject','s3:DeleteObject','s3:DeleteObjectVersion']),
                  'Resource':f'arn:aws:s3:::{self.bucket}/{self.key}',
                  'Condition':{'ArnNotEquals':{'aws:PrincipalArn':sorted([
                      self.configuration['grant_issuer_arn'],
                      f"arn:aws:iam::{self.account}:role/{self.configuration['project']}-{self.configuration['github_environment']}-reprovision"])}}}
        actual=[dict(s) for s in policy['Statement'] if s.get('Sid')==expected['Sid']]
        if len(actual)==1:
            actions=actual[0].get('Action')
            actual[0]['Action']=sorted(actions) if isinstance(actions,list) else actions
            condition=actual[0].get('Condition',{}).get('ArnNotEquals',{}).get('aws:PrincipalArn')
            if isinstance(condition,list):actual[0]['Condition']={'ArnNotEquals':{'aws:PrincipalArn':sorted(condition)}}
        require(actual==[expected],'CREATION_JOURNAL_WRITER_NOT_EXCLUSIVE')
        status=self.call('get_bucket_versioning',Bucket=self.bucket)
        require(status.get('Status')=='Enabled','CREATION_VERSIONING_REQUIRED')

    def authorize(self,wait_seconds=0,clock=time.monotonic,sleep=time.sleep):
        require(type(wait_seconds) is int and 0<=wait_seconds<=300,'CREATION_GRANT_WAIT_INVALID')
        self.verify_authority()
        history=self.versions(self.grant_key)
        deadline=clock()+wait_seconds
        # Wait only for absence. Invalid grants, network failures and historical
        # collisions never trigger retries or any write before authorization.
        while not history and clock()<deadline:
            sleep(min(5,deadline-clock()));history=self.versions(self.grant_key)
        require(len(history)==1 and history[0]['IsLatest'],'CREATION_GRANT_NOT_UNIQUE')
        grant,_=self.read(self.grant_key,history[0]['VersionId'])
        expected={'version':1,'kind':'recovery-host-creation','owner':self.owner,'commit':self.commit,
                  'configuration_sha256':digest(self.configuration),'configuration':self.configuration}
        if isinstance(grant,dict) and 'resume' in grant:
            require({k:v for k,v in grant.items() if k!='resume'}==expected,'CREATION_GRANT_MISMATCH')
            from scripts.restore.reprovision import validate_resume_binding
            self.resume=validate_resume_binding(grant['resume'],self.configuration)
        else:require(grant==expected,'CREATION_GRANT_MISMATCH')
        self.grant=(history[0]['VersionId'],digest(grant));return grant

    def grant_unchanged(self):
        require(self.grant is not None,'CREATION_GRANT_REQUIRED')
        rows=self.versions(self.grant_key)
        require(len(rows)==1 and rows[0]['VersionId']==self.grant[0] and rows[0]['IsLatest'],
                'CREATION_GRANT_CHANGED')
        value,_=self.read(self.grant_key,self.grant[0])
        require(digest(value)==self.grant[1],'CREATION_GRANT_CHANGED')

    def put(self,record,first=False):
        self.grant_unchanged()
        raw=encoded(record);require(len(raw)<=LIMIT,'CREATION_OBJECT_LIMIT')
        result=self.call('put_object',Bucket=self.bucket,Key=self.key,Body=raw,ServerSideEncryption='AES256',
                         **({'IfNoneMatch':'*'} if first else {'IfMatch':self.etag}))
        vid=result.get('VersionId');require(isinstance(vid,str) and vid not in ('','null'),
                                            'CREATION_WRITE_UNCONFIRMED')
        value,etag=self.read(self.key,vid)
        rows=self.versions(self.key)
        require(value==record and any(r['VersionId']==vid and r['IsLatest'] for r in rows),
                'CREATION_WRITE_UNCONFIRMED')
        self.record,self.etag,self.version=record,etag,vid

    def assert_held(self):
        require(self.record is not None and self.record['phase'] in ('held','host_bound','handoff'),
                'CREATION_LEASE_REQUIRED')
        self.grant_unchanged();rows=self.versions(self.key)
        require(any(r['VersionId']==self.version and r['IsLatest'] for r in rows),'CREATION_AUTHORITY_LOST')
        value,etag=self.read(self.key,self.version)
        require(value==self.record and etag==self.etag,'CREATION_AUTHORITY_LOST')

    def previous_resolution(self,row,old):
        if old.get('phase')=='released' and old.get('handoff_verified') is True:
            return None
        require(old.get('phase')=='intervention_required' and old.get('host') is None
                and old.get('handoff_verified') is False
                and isinstance(old.get('owner'),str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',old['owner']),
                'CREATION_RECONCILIATION_REQUIRED')
        key=self.prefix+'grants/reconciliations/'+old['owner'].replace(':','-')+'.json'
        history=self.versions(key)
        require(len(history)==1 and history[0]['IsLatest'],'CREATION_RECONCILIATION_REQUIRED')
        receipt,_=self.read(key,history[0]['VersionId'])
        require(set(receipt)=={'version','kind','binding','state','workflow','absence_sha256'}
                and type(receipt.get('version')) is int and receipt['version']==1
                and receipt.get('kind')=='administrative-creation-abort-before-apply'
                and receipt.get('binding')=={'owner':old['owner'],'commit':old['commit'],
                    'lease_version_id':row['VersionId'],'lease_sha256':digest(old),
                    'configuration_sha256':digest(self.configuration)}
                and receipt.get('workflow')=={'repository':self.configuration['repository'],
                    'run_id':int(old['owner'].split(':')[0]),'attempt':int(old['owner'].split(':')[1]),
                    'commit':old['commit'],'status':'completed','conclusion':'failure'}
                and receipt.get('absence_sha256')==digest({k:[] for k in (
                    'instances','vpcs','subnets','internet_gateways','route_tables','security_groups',
                    'key_pairs','network_interfaces','volumes','host_role','host_profile')}),
                'CREATION_RECONCILIATION_INVALID')
        snapshot=receipt.get('state')
        require(isinstance(snapshot,dict) and set(snapshot)=={'version_id','sha256','lineage','serial'}
                and isinstance(snapshot['version_id'],str) and snapshot['version_id'] not in ('','null')
                and isinstance(snapshot['sha256'],str) and re.fullmatch('[a-f0-9]{64}',snapshot['sha256'])
                and isinstance(snapshot['lineage'],str) and snapshot['lineage']
                and type(snapshot['serial']) is int and snapshot['serial']>=0,
                'CREATION_RECONCILIATION_INVALID')
        return receipt

    def assert_reconciled_state(self,receipt):
        # The creation role can read its own Terraform state. The issuer cannot;
        # it attests independent administrative reads, verified again here.
        from scripts.restore.reprovision import state_key,retained_state
        value=self.call('get_object',Bucket=self.bucket,Key=state_key(self.configuration))
        body=value.get('Body');require(body is not None,'CREATION_RECONCILIATION_STATE_CHANGED')
        try:
            raw=body.read(1048577);snapshot=receipt['state']
            require(len(raw)<=1048576 and value.get('VersionId')==snapshot['version_id']
                    and value.get('ServerSideEncryption')=='AES256'
                    and value.get('ContentLength')==len(raw)
                    and hashlib.sha256(raw).hexdigest()==snapshot['sha256'],
                    'CREATION_RECONCILIATION_STATE_CHANGED')
            document=parse_json(raw,Limits(json_bytes=1048576))
            require(document.get('lineage')==snapshot['lineage'] and document.get('serial')==snapshot['serial']
                    and retained_state(document,self.configuration) is not None,
                    'CREATION_RECONCILIATION_STATE_CHANGED')
        finally:body.close()

    def acquire(self,wait_seconds=0):
        require(self.record is None,'CREATION_ALREADY_ACQUIRED');self.authorize(wait_seconds)
        rows=self.versions(self.key)
        for row in rows:
            old,etag=self.read(self.key,row['VersionId'])
            require(set(old)=={'version','kind','owner','commit','grant_sha256','phase','host','handoff_verified'}
                    and type(old.get('version')) is int and old['version']==1 and old.get('kind')=='recovery-creation-lease'
                    and isinstance(old['owner'],str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',old['owner'])
                    and isinstance(old['commit'],str) and re.fullmatch('[a-f0-9]{40}',old['commit'])
                    and isinstance(old['grant_sha256'],str) and re.fullmatch('[a-f0-9]{64}',old['grant_sha256'])
                    and type(old['handoff_verified']) is bool
                    and old.get('phase') in ('held','host_bound','handoff','released','intervention_required'),
                    'CREATION_PRIOR_STATE_INVALID')
            host=old['host']
            require(host is None or isinstance(host,dict) and set(host)=={'account','region','instance'}
                    and host['account']==self.account and host['region']==self.configuration['region']
                    and isinstance(host['instance'],str) and re.fullmatch('i-[a-f0-9]{17}',host['instance'])
                    and host['instance']!=self.configuration['source_instance'],'CREATION_PRIOR_STATE_INVALID')
            require(not old['handoff_verified'] or host is not None,'CREATION_PRIOR_STATE_INVALID')
            require(old.get('owner')!=self.owner and old.get('grant_sha256')!=self.grant[1],
                    'CREATION_GRANT_ALREADY_USED')
            if row['IsLatest']:
                if self.resume is not None:
                    require(self.resume['previous']=={'version_id':row['VersionId'],'record':old}
                            and old['owner']!=self.owner,'CREATION_RESUME_BINDING_CHANGED')
                    prior=CreationLease(self.s3,self.account,self.bucket,self.prefix,
                                        old['owner'],old['commit'],self.configuration)
                    prior.authorize()
                    require(prior.grant[1]==old['grant_sha256'],'CREATION_RESUME_BINDING_CHANGED')
                    require(self.resume['creation_grant_sha256']==(
                        prior.resume['creation_grant_sha256'] if prior.resume else prior.grant[1]),
                        'CREATION_RESUME_BINDING_CHANGED')
                else:
                    receipt=self.previous_resolution(row,old)
                    if receipt is not None:
                        self.assert_reconciled_state(receipt);self.reconciliation_receipt=receipt
                self.etag=etag
        require(self.resume is None or rows,'CREATION_RESUME_BINDING_CHANGED')
        record={'version':1,'kind':'recovery-creation-lease','owner':self.owner,'commit':self.commit,
                'grant_sha256':self.grant[1],'phase':'held','host':None,'handoff_verified':False}
        if self.resume is not None:
            record.update(phase='host_bound',host=self.resume['previous']['record']['host'])
        self.put(record,first=not rows)

    def bind(self,identity):
        self.assert_held();require(self.record['phase']=='held' and isinstance(identity,dict)
                                  and set(identity)=={'account','region','instance'},'CREATION_HOST_BINDING_INVALID')
        require(identity['account']==self.account and identity['region']==self.configuration['region']
                and re.fullmatch('i-[a-f0-9]{17}',identity['instance'])
                and identity['instance']!=self.configuration['source_instance'],'CREATION_HOST_BINDING_INVALID')
        self.put({**self.record,'phase':'host_bound','host':identity})

    def handoff(self,remote):
        from scripts.backup.operational.remote_lease import RemoteDeployLease
        require(type(remote) is RemoteDeployLease and remote.owner==self.owner,'CREATION_REMOTE_LEASE_REQUIRED')
        self.assert_held();remote.assert_held()
        require(self.record['phase']=='host_bound' and self.record['host']=={
            'account':remote.target.account,'region':remote.target.region,'instance':remote.target.instance},
            'CREATION_HANDOFF_MISMATCH')
        self.put({**self.record,'phase':'handoff','handoff_verified':True})

    def release_to_host(self,remote):
        self.assert_held();remote.assert_held()
        require(self.record['phase']=='handoff' and self.record['handoff_verified'] is True
                and remote.owner==self.owner and remote.target.instance==self.record['host']['instance'],
                'CREATION_HANDOFF_REQUIRED')
        # Release the external authority while the host still holds its flock.
        # A subsequent channel failure leaves the host intervention_required.
        self.put({**self.record,'phase':'released'})

    @contextmanager
    def hold(self,wait_seconds=0):
        self.acquire(wait_seconds)
        try:
            yield self
            require(self.record['phase']=='released' and self.record['handoff_verified'],
                    'CREATION_HANDOFF_REQUIRED')
            self.grant_unchanged()
            rows=self.versions(self.key);require(any(r['IsLatest'] and r['VersionId']==self.version for r in rows),
                                                'CREATION_AUTHORITY_LOST')
        except BaseException:
            # If communication is ambiguous, retain whichever version AWS holds.
            # No deletion, unlock or release is attempted during unwind.
            if self.record is not None:
                try:
                    self.assert_held();self.put({**self.record,'phase':'intervention_required'})
                except Exception:pass
            raise
