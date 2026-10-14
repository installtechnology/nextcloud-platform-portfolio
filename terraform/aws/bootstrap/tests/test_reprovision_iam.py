"""Effective inline/boundary intersection of actual Terraform mock output.

The mock input IAM document was generated offline by the installed AWS provider;
these evaluations are not an AWS policy simulator or an operational validation.
"""
import importlib.util
import json
import os
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('evaluator',Path(__file__).with_name('test_backup_iam.py'))
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)


class ReprovisionIAM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        events=[json.loads(line) for line in Path(os.environ['REPROVISION_IAM_TEST_PLAN']).read_text(encoding='utf-8-sig').splitlines()]
        state=next(e['test_state'] for e in events if e.get('type')=='test_state' and e.get('@testrun')=='scoped_creation')
        cls.resources={r['address']:r['values'] for r in state['root_module']['resources']}
        cls.inline=json.loads(cls.resources['aws_iam_role_policy.reprovision[0]']['policy'])
        cls.boundary=json.loads(cls.resources['aws_iam_policy.reprovision_boundary[0]']['policy'])
        cls.trust=json.loads(cls.resources['aws_iam_role.reprovision[0]']['assume_role_policy'])
        cls.context={'aws:RequestedRegion':'us-east-1','s3:x-amz-server-side-encryption':'AES256',
                     'aws:ResourceTag/Project':'fixture','aws:ResourceTag/Environment':'restore',
                     'aws:RequestTag/Project':'fixture','aws:RequestTag/Environment':'restore'}
    def effective(self,action,resource,**context):
        results=[helper.decision(p,action,resource,dict(self.context,**context)) for p in (self.inline,self.boundary)]
        return all(r=='allow' for r in results)
    def test_sizes(self):
        self.assertLessEqual(len(json.dumps(self.boundary,separators=(',',':'))),6144)
        self.assertLessEqual(len(json.dumps(self.inline,separators=(',',':'))),10240)
    def test_size_compaction_removes_only_optional_statement_labels(self):
        self.assertTrue(all('Sid' not in s for s in self.inline['Statement']))
        self.assertTrue(all('Sid' not in s for s in self.boundary['Statement']))
    def test_boundary_mandatory(self):
        self.assertEqual(self.resources['aws_iam_role.reprovision[0]']['permissions_boundary'],self.resources['aws_iam_policy.reprovision_boundary[0]']['arn'])
    def test_exact_native_lock_delete(self):
        key='arn:aws:s3:::fixture-state/states/000000000000/fixture/restore/restore/terraform.tfstate.tflock'
        self.assertTrue(self.effective('s3:DeleteObject',key))
        self.assertFalse(self.effective('s3:DeleteObjectVersion',key))
    def test_other_state_delete_denied(self):
        self.assertFalse(self.effective('s3:DeleteObject','arn:aws:s3:::fixture-state/states/000000000000/fixture/test/runtime/terraform.tfstate'))
    def test_control_never_deleted(self):
        self.assertFalse(self.effective('s3:DeleteObject','arn:aws:s3:::fixture-state/control/reprovision/000000000000/fixture/test/lease.json'))
    def test_grant_write_denied(self):
        self.assertFalse(self.effective('s3:PutObject','arn:aws:s3:::fixture-state/control/reprovision/000000000000/fixture/test/grants/123-1.json'))
    def test_journal_write_encrypted(self):
        key='arn:aws:s3:::fixture-state/control/reprovision/000000000000/fixture/test/lease.json'
        self.assertTrue(self.effective('s3:PutObject',key))
        self.assertFalse(self.effective('s3:PutObject',key,**{'s3:x-amz-server-side-encryption':'aws:kms'}))
    def test_source_and_backup_writes_denied(self):
        for bucket in ('fixture-source-data','fixture-backups'):
            for action in ('s3:PutBucketPolicy','s3:PutObject','s3:DeleteBucket','s3:DeleteObjectVersion'):
                with self.subTest(bucket=bucket,action=action):
                    self.assertFalse(self.effective(action,'arn:aws:s3:::'+bucket+('/file' if 'Object' in action else '')))
    def test_state_policy_administration_denied(self):
        self.assertFalse(self.effective('s3:PutBucketPolicy','arn:aws:s3:::fixture-state'))
    def test_only_new_destination_policy_allowed(self):
        self.assertTrue(self.effective('s3:PutBucketPolicy','arn:aws:s3:::fixture-destination'))
        self.assertFalse(self.effective('s3:DeleteBucketPolicy','arn:aws:s3:::fixture-destination'))
    def test_destination_object_writes_not_granted(self):
        self.assertFalse(self.effective('s3:PutObject','arn:aws:s3:::fixture-destination/file'))
    def test_other_bucket_denied(self):self.assertFalse(self.effective('s3:CreateBucket','arn:aws:s3:::other'))
    def test_no_ssm_sessions_or_commands(self):
        for action in ('ssm:StartSession','ssm:SendCommand'):
            self.assertFalse(self.effective(action,'*'))
    def test_no_terminate_stop(self):
        for action in ('ec2:TerminateInstances','ec2:StopInstances'):
            self.assertFalse(self.effective(action,'arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111'))
    def test_other_project_or_environment_blocked(self):
        arn='arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111'
        self.assertTrue(self.effective('ec2:CreateTags',arn))
        self.assertFalse(self.effective('ec2:CreateTags',arn,**{'aws:ResourceTag/Environment':'test'}))
        self.assertFalse(self.effective('ec2:CreateTags',arn,**{'aws:ResourceTag/Project':'other'}))
    def test_other_region_blocked(self):self.assertFalse(self.effective('ec2:DescribeInstances','*',**{'aws:RequestedRegion':'eu-west-1'}))
    def test_role_creation_requires_exact_boundary(self):
        arn='arn:aws:iam::000000000000:role/fixture-restore-ec2'
        self.assertTrue(self.effective('iam:CreateRole',arn,**{'iam:PermissionsBoundary':'arn:aws:iam::000000000000:policy/fixture-restore-ec2-boundary'}))
        self.assertFalse(self.effective('iam:CreateRole',arn))
    def test_cannot_modify_own_role(self):self.assertFalse(self.effective('iam:PutRolePolicy','arn:aws:iam::000000000000:role/fixture-test-reprovision'))
    def test_pass_role_only_ec2(self):
        arn='arn:aws:iam::000000000000:role/fixture-restore-ec2'
        self.assertTrue(self.effective('iam:PassRole',arn,**{'iam:PassedToService':'ec2.amazonaws.com'}))
        self.assertFalse(self.effective('iam:PassRole',arn,**{'iam:PassedToService':'lambda.amazonaws.com'}))
    def test_oidc_exact_manual_context(self):
        c=self.trust['Statement'][0]['Condition']['StringEquals'];p='token.actions.githubusercontent.com:'
        self.assertEqual(c[p+'workflow'],'Deploy platform (manual)');self.assertEqual(c[p+'ref'],'refs/heads/main')
        self.assertEqual(c[p+'aud'],'sts.amazonaws.com');self.assertEqual(c[p+'sub'],'repo:fixture@123/platform@456:environment:test')
    def test_restore_remains_without_state_access(self):
        doc=json.loads(self.resources['aws_iam_role_policy.restore[0]']['policy'])
        self.assertEqual(helper.decision(doc,'s3:GetObject','arn:aws:s3:::fixture-state/file',self.context),'explicit_deny')
    def test_restore_wrong_destination_tags_blocked(self):
        doc=json.loads(self.resources['aws_iam_role_policy.restore[0]']['policy'])
        ctx=dict(self.context,**{'ssm:resourceTag/Project':'fixture','ssm:resourceTag/Environment':'test'})
        self.assertEqual(helper.decision(doc,'ssm:StartSession','arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111',ctx),'implicit_deny')


if __name__=='__main__':unittest.main()
