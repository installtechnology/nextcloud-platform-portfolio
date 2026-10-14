"""Real adapter rejection paths with injected clients; no API/subprocess IO."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from scripts.restore.reprovision_operations import ReprovisionOperations
from scripts.restore.tests.test_reprovision import config,preserved_state
from scripts.restore.reprovision import retained_state
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import SealFailure
from scripts.backup.operational.fence_policy import digest


class APIError(Exception):
    def __init__(self,code,status):self.response={'Error':{'Code':code},'ResponseMetadata':{'HTTPStatusCode':status}}


def lease():return SimpleNamespace(assert_held=Mock())


class Operations(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.c=config();self.sdk={k:Mock() for k in ('s3','ec2','ssm','sts')}
        self.ci=Mock();self.ci.paths.return_value=self.root
        self.ops=ReprovisionOperations(self.sdk,self.ci,self.c,'123:1','a'*40)
        self.sdk['sts'].get_caller_identity.return_value={'Account':self.c['account']}
        self.sdk['ec2'].describe_instances.return_value={'Reservations':[]}
        self.sdk['s3'].get_object.side_effect=APIError('NoSuchKey',404)
        self.sdk['s3'].head_bucket.side_effect=APIError('404',404)
        self.sdk['s3'].list_object_versions.return_value={'IsTruncated':False,'Versions':[],'DeleteMarkers':[]}
        policy={'Version':'2012-10-17','Statement':[{'Sid':'DenyInsecureTransport','Effect':'Deny','Principal':'*','Action':'s3:*','Resource':'arn:aws:s3:::fixture-source/*'}]}
        self.c['source_policy_sha256']=digest(policy)
        self.sdk['s3'].get_bucket_policy.return_value={'Policy':json.dumps(policy)}
    def code(self,code,fn):
        with self.assertRaises((CaptureFailure,SealFailure)) as caught:fn()
        self.assertEqual(caught.exception.code,code)
    def test_absence_is_observed_without_writes(self):
        self.ops.assert_absent(self.c)
        self.assertFalse(self.sdk['s3'].put_object.called);self.assertFalse(self.ci.run.called)
    def test_state_access_denied_not_absence(self):
        self.sdk['s3'].get_object.side_effect=APIError('AccessDenied',403)
        self.code('CREATION_STATE_UNVERIFIED',lambda:self.ops.assert_absent(self.c))
    def test_deleted_state_not_absence(self):
        self.sdk['s3'].list_object_versions.return_value['DeleteMarkers']=[{}]
        self.code('CREATION_STATE_HISTORY_PRESENT',lambda:self.ops.assert_absent(self.c))
    def test_stopped_host_never_replaced(self):
        self.sdk['ec2'].describe_instances.return_value={'Reservations':[{'Instances':[{'State':{'Name':'stopped'}}]}]}
        self.code('CREATION_RUNTIME_ALREADY_EXISTS',lambda:self.ops.assert_absent(self.c))
        self.sdk['s3'].get_object.assert_not_called()
    def test_account_mismatch(self):
        self.sdk['sts'].get_caller_identity.return_value={'Account':'111111111111'}
        self.code('CREATION_ACCOUNT_MISMATCH',lambda:self.ops.assert_absent(self.c))
        self.sdk['ec2'].describe_instances.assert_not_called()
    def test_partial_state_blocks(self):
        self.sdk['s3'].get_object.side_effect=None
        self.sdk['s3'].get_object.return_value={'Body':io.BytesIO(json.dumps({'version':4,'serial':1,'lineage':'fixture','resources':[{}],'outputs':{}}).encode())}
        self.code('CREATION_RETAINED_STATE_INVALID',lambda:self.ops.assert_absent(self.c))
    def retained(self):
        state=preserved_state(self.c)
        self.sdk['s3'].get_object.side_effect=None
        def state_response(**kw):return {'Body':io.BytesIO(json.dumps(state).encode()),
                'VersionId':'retained-v1','ServerSideEncryption':'AES256'}
        self.sdk['s3'].get_object.side_effect=state_response
        self.sdk['s3'].head_bucket.side_effect=None
        self.sdk['s3'].meta=SimpleNamespace(endpoint_url='https://s3.us-east-1.amazonaws.com',region_name='us-east-1')
        attrs={r['type']:r['instances'][0]['attributes'] for r in state['resources']}
        original=self.sdk['s3'].get_bucket_policy.return_value['Policy']
        self.sdk['s3'].get_bucket_policy.side_effect=lambda **kw:{'ResponseMetadata':{'HTTPStatusCode':200},
            'Policy':attrs['aws_s3_bucket_policy']['policy'] if kw['Bucket']==self.c['data_bucket'] else original}
        def response(**values):return {'ResponseMetadata':{'HTTPStatusCode':200},**values}
        self.sdk['s3'].get_bucket_versioning.return_value=response(Status='Enabled')
        self.sdk['s3'].get_bucket_tagging.return_value=response(TagSet=[
            {'Key':k,'Value':v} for k,v in attrs['aws_s3_bucket']['tags_all'].items()])
        self.sdk['s3'].get_bucket_encryption.return_value=response(ServerSideEncryptionConfiguration={
            'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})
        self.sdk['s3'].get_public_access_block.return_value=response(PublicAccessBlockConfiguration={
            k:True for k in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')})
        self.sdk['s3'].get_bucket_ownership_controls.return_value=response(OwnershipControls={
            'Rules':[{'ObjectOwnership':'BucketOwnerPreferred'}]})
        return state
    def test_retained_bucket_observed_without_writes(self):
        self.retained();self.ops.assert_absent(self.c);self.ops.assert_absent(self.c)
        self.assertIsNotNone(self.ops.retained)
        self.sdk['s3'].put_object.assert_not_called();self.ci.run.assert_not_called()
    def test_retained_bucket_missing_blocks(self):
        self.retained();self.sdk['s3'].head_bucket.side_effect=APIError('404',404)
        self.code('CREATION_RETAINED_BUCKET_MISSING',lambda:self.ops.assert_absent(self.c))
    def test_retained_state_requires_version_and_sse(self):
        for field,value in [('VersionId','null'),('ServerSideEncryption','none')]:
            with self.subTest(field=field):
                self.retained();original=self.sdk['s3'].get_object.side_effect
                self.sdk['s3'].get_object.side_effect=lambda **kw:{**original(**kw),field:value}
                self.code('CREATION_RETAINED_STATE_UNVERIFIED',lambda:self.ops.assert_absent(self.c))
    def test_retained_state_changes_before_apply_block(self):
        state=self.retained();self.ops.assert_absent(self.c);state['serial']+=1
        self.code('CREATION_STATE_CHANGED',lambda:self.ops.assert_absent(self.c))
    def test_retained_policy_drift_blocks(self):
        self.retained();original=self.sdk['s3'].get_bucket_policy.side_effect
        def changed(**kw):
            r=original(**kw)
            if kw['Bucket']==self.c['data_bucket']:
                p=json.loads(r['Policy']);p['Statement'].append({'Sid':'ExternalChange','Effect':'Deny',
                    'Principal':'*','Action':'s3:PutObject','Resource':'arn:aws:s3:::'+self.c['data_bucket']+'/*'})
                r['Policy']=json.dumps(p)
            return r
        self.sdk['s3'].get_bucket_policy.side_effect=changed
        self.code('CREATION_RETAINED_POLICY_DRIFT',lambda:self.ops.assert_absent(self.c))
    def test_retained_protection_failure_blocks(self):
        self.retained();self.sdk['s3'].get_bucket_versioning.return_value['Status']='Suspended'
        self.code('RESTORE_TARGET_VERSIONING_REQUIRED',lambda:self.ops.assert_absent(self.c))
    def test_actual_retained_tags_change_blocks(self):
        self.retained();self.sdk['s3'].get_bucket_tagging.return_value['TagSet'][0]['Value']='changed'
        self.code('CREATION_RETAINED_TAGS_DRIFT',lambda:self.ops.assert_absent(self.c))
    def test_duplicate_actual_tags_block(self):
        self.retained();tags=self.sdk['s3'].get_bucket_tagging.return_value['TagSet'];tags.append(tags[0])
        self.code('CREATION_RETAINED_TAGS_DRIFT',lambda:self.ops.assert_absent(self.c))
    def test_tag_lookup_failure_blocks(self):
        self.retained();self.sdk['s3'].get_bucket_tagging.side_effect=APIError('AccessDenied',403)
        self.code('CREATION_RETAINED_METADATA_UNVERIFIED',lambda:self.ops.assert_absent(self.c))
    def test_actual_mfa_change_blocks(self):
        self.retained();self.sdk['s3'].get_bucket_versioning.return_value['MFADelete']='Enabled'
        self.code('CREATION_RETAINED_VERSIONING_DRIFT',lambda:self.ops.assert_absent(self.c))
    def test_existing_instance_still_blocks_with_preserved_bucket(self):
        self.retained();self.sdk['ec2'].describe_instances.return_value={'Reservations':[
            {'Instances':[{'State':{'Name':'running'}}]}]}
        self.code('CREATION_RUNTIME_ALREADY_EXISTS',lambda:self.ops.assert_absent(self.c))
        self.sdk['s3'].get_object.assert_not_called()
    def test_plan_preserves_default_tags_and_grants_only_new_host(self):
        state=preserved_state(self.c)
        self.ops.retained=retained_state(state,self.c)
        self.ops.retained['aws_s3_bucket.data']['tags']=None
        repo=self.root/'repository';tf=repo/'terraform/aws/restore';tf.mkdir(parents=True)
        (tf/'backend.tf.example').write_text('terraform { backend "s3" {} }')
        self.ci.ROOT=repo
        self.ci.required.side_effect=lambda name:json.dumps(['192.0.2.1/32']) if name.endswith('_CIDRS') else 'synthetic-public-key'
        def run(argv,**kw):
            if 'plan' in argv:self.ops.plan_path.write_bytes(b'synthetic-plan')
            return '{"resource_changes":[]}' if 'show' in argv else ''
        self.ci.run.side_effect=run
        active=lease();active.grant=('synthetic-document','new-grant-version')
        self.ops.plan(self.c,active)
        call=next(args for args,kw in self.ci.private_file.call_args_list if args[0].name=='restore-creation.tfvars.json')
        values=json.loads(call[1])
        self.assertEqual(values['tags'],{'Portfolio':'synthetic'})
        self.assertEqual(values['creation_grant'],'new-grant-version')
        self.assertNotIn('ReprovisionGrant',values['tags'])
    def test_bucket_access_denied_not_absence(self):
        self.sdk['s3'].head_bucket.side_effect=APIError('AccessDenied',403)
        self.code('CREATION_DESTINATION_BUCKET_UNVERIFIED',lambda:self.ops.assert_absent(self.c))
    def test_existing_destination_is_not_adopted(self):
        self.sdk['s3'].head_bucket.side_effect=None
        self.code('CREATION_DESTINATION_BUCKET_EXISTS',lambda:self.ops.assert_absent(self.c))
    def test_source_policy_change_blocks(self):
        self.c['source_policy_sha256']='0'*64
        self.code('CREATION_SOURCE_FENCING_UNVERIFIED',lambda:self.ops.assert_absent(self.c))
    def test_truncated_discovery_blocks(self):
        self.sdk['ec2'].describe_instances.return_value['NextToken']='fixture'
        self.code('CREATION_DISCOVERY_TRUNCATED',lambda:self.ops.assert_absent(self.c))
    def test_modified_saved_plan_not_applied(self):
        self.ops.plan_path.write_bytes(b'synthetic-changed');self.ops.plan_sha=hashlib.sha256(b'synthetic-original').hexdigest()
        self.code('CREATION_PLAN_CHANGED',lambda:self.ops.apply(self.c,lease()))
        self.ci.run.assert_not_called()
    def test_apply_confirmation_mandatory(self):
        self.ops.plan_path.write_bytes(b'synthetic');self.ops.plan_sha=hashlib.sha256(b'synthetic').hexdigest()
        with patch.dict(os.environ,{'CONFIRMATION':'NO'}):
            self.code('CREATION_APPLY_NOT_AUTHORIZED',lambda:self.ops.apply(self.c,lease()))
        self.ci.run.assert_not_called()
    def test_lost_lease_blocks_before_terraform(self):
        active=lease();active.assert_held.side_effect=CaptureFailure('CREATION_AUTHORITY_LOST')
        self.code('CREATION_AUTHORITY_LOST',lambda:self.ops.apply(self.c,active));self.ci.run.assert_not_called()
    def test_ssm_foreign_instance_does_not_mark_ready(self):
        self.ops.bound=SimpleNamespace(instance='i-'+'3'*17)
        self.sdk['ssm'].describe_instance_information.return_value={'InstanceInformationList':[{'InstanceId':'i-'+'4'*17,'PingStatus':'Online'}]}
        self.code('CREATION_SSM_AMBIGUOUS',lambda:self.ops.verify_prepared(self.c,{},lease()))
        self.sdk['ec2'].create_tags.assert_not_called()


if __name__=='__main__':unittest.main()
