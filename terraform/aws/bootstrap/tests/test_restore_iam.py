"""Tests of actual Terraform mock-rendered policy; not AWS Policy Simulator."""
import importlib.util
import json
import os
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('iam_evaluator',Path(__file__).with_name('test_backup_iam.py'))
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)

class RestoreIAM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Path(os.environ['RESTORE_IAM_TEST_PLAN']).open(encoding='utf-8-sig') as source:
            events=[json.loads(line) for line in source if line.strip()]
        plan=next(e['test_plan'] for e in events if e.get('type')=='test_plan' and e.get('@testrun')=='scoped_restore')
        cls.resources={c['address']:c['change']['after'] for c in plan['resource_changes']}
        cls.policy=json.loads(cls.resources['aws_iam_role_policy.restore[0]']['policy'])
        cls.boundary=json.loads(cls.resources['aws_iam_policy.restore_boundary[0]']['policy'])
        cls.role=cls.resources['aws_iam_role.restore[0]']
        cls.trust=json.loads(cls.role['assume_role_policy'])
        cls.context={'aws:SecureTransport':'true','s3:ResourceAccount':'000000000000','aws:RequestedRegion':'us-east-1'}
    def check(self,expected,action,resource,**ctx):
        for policy in [self.policy,self.boundary]:
            self.assertEqual(helper.decision(policy,action,resource,dict(self.context,**ctx)),expected)
    def test_destination_runtime_snapshot_matches_source(self):
        root=Path(__file__).resolve().parents[2]
        self.assertEqual((root/'restore/iam.tf').read_text(),(root/'runtime/iam.tf').read_text())
        snapshot=(root/'restore/runtime.tf').read_text().split('\n',1)[1]
        self.assertEqual(snapshot,(root/'runtime/main.tf').read_text())
    def test_policy_equals_boundary(self):self.assertEqual(self.policy,self.boundary)
    def test_boundary_mandatory(self):self.assertEqual(self.role['permissions_boundary'],self.resources['aws_iam_policy.restore_boundary[0]']['arn'])
    def test_read_source_version(self):self.check('allow','s3:GetObjectVersion','arn:aws:s3:::fixture-source-data/file')
    def test_read_backup_version(self):self.check('allow','s3:GetObjectVersion','arn:aws:s3:::fixture-backups/backups/id/database.dump')
    def test_unversioned_read_not_granted(self):self.check('explicit_deny','s3:GetObject','arn:aws:s3:::fixture-source-data/file')
    def test_source_write_denied(self):self.check('explicit_deny','s3:PutObject','arn:aws:s3:::fixture-source-data/file')
    def test_backup_write_denied(self):self.check('explicit_deny','s3:PutObject','arn:aws:s3:::fixture-backups/backups/id/file')
    def test_destination_delete_denied(self):self.check('explicit_deny','s3:DeleteObject','arn:aws:s3:::fixture-destination/file')
    def test_version_delete_denied(self):self.check('explicit_deny','s3:DeleteObjectVersion','arn:aws:s3:::fixture-destination/file')
    def test_bucket_policy_write_denied(self):self.check('explicit_deny','s3:PutBucketPolicy','arn:aws:s3:::fixture-destination')
    def test_state_denied(self):self.check('explicit_deny','s3:GetObjectVersion','arn:aws:s3:::fixture-state/file')
    def test_other_bucket_denied(self):self.check('explicit_deny','s3:GetObjectVersion','arn:aws:s3:::other-bucket/file')
    def test_destination_copy_allowed(self):self.check('allow','s3:PutObject','arn:aws:s3:::fixture-destination/file',**{'s3:x-amz-server-side-encryption':'AES256','s3:x-amz-acl':'private'})
    def test_encryption_missing_denied(self):self.check('implicit_deny','s3:PutObject','arn:aws:s3:::fixture-destination/file',**{'s3:x-amz-acl':'private'})
    def test_public_acl_denied(self):self.check('implicit_deny','s3:PutObjectAcl','arn:aws:s3:::fixture-destination/file',**{'s3:x-amz-acl':'public-read'})
    def test_insecure_transport_denied(self):self.check('implicit_deny','s3:GetObjectVersion','arn:aws:s3:::fixture-source-data/file',**{'aws:SecureTransport':'false'})
    def test_ssm_exact_node(self):self.check('allow','ssm:StartSession','arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111',**{'ssm:resourceTag/Project':'fixture','ssm:resourceTag/Environment':'restore'})
    def test_wrong_ssm_node_denied(self):self.check('explicit_deny','ssm:StartSession','arn:aws:ec2:us-east-1:000000000000:instance/i-22222222222222222')
    def test_wrong_ssm_tags_denied(self):self.check('implicit_deny','ssm:StartSession','arn:aws:ec2:us-east-1:000000000000:instance/i-11111111111111111',**{'ssm:resourceTag/Project':'other','ssm:resourceTag/Environment':'restore'})
    def test_generic_shell_denied(self):self.check('explicit_deny','ssm:StartSession','arn:aws:ssm:us-east-1::document/SSM-SessionManagerRunShell')
    def test_run_command_denied(self):self.check('explicit_deny','ssm:SendCommand','*')
    def test_own_session_allowed(self):self.check('allow','ssm:TerminateSession','arn:aws:ssm:us-east-1:000000000000:session/synthetic',**{'aws:userid':'role:run','ssm:resourceTag/aws:ssmmessages:session-id':'role:run','ssm:resourceTag/aws:ssmmessages:target-id':'i-11111111111111111'})
    def test_other_session_denied(self):self.check('implicit_deny','ssm:TerminateSession','arn:aws:ssm:us-east-1:000000000000:session/synthetic',**{'aws:userid':'role:run','ssm:resourceTag/aws:ssmmessages:session-id':'other:run','ssm:resourceTag/aws:ssmmessages:target-id':'i-11111111111111111'})
    def test_iam_admin_denied(self):self.check('explicit_deny','iam:PutRolePolicy','*')
    def test_sg_write_denied(self):self.check('explicit_deny','ec2:AuthorizeSecurityGroupIngress','*')
    def test_trust_exact_workflow_branch_audience(self):
        c=self.trust['Statement'][0]['Condition']['StringEquals'];prefix='token.actions.githubusercontent.com:'
        self.assertEqual(c[prefix+'workflow'],'Restore platform (manual)')
        self.assertEqual(c[prefix+'ref'],'refs/heads/main');self.assertEqual(c[prefix+'aud'],'sts.amazonaws.com')
        self.assertEqual(c[prefix+'sub'],'repo:fixture@123/platform@456:environment:test')
        self.assertEqual(c[prefix+'repository_id'],'456');self.assertEqual(c[prefix+'repository_owner_id'],'123')

if __name__=='__main__':unittest.main()
