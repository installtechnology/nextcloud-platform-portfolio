"""Evaluate Terraform-rendered mock plans offline; NOT AWS IAM Policy Simulator.

BACKUP_IAM_TEST_PLAN points to `terraform test -json -verbose` output generated
in a backend-free copy using tests/backup.tftest.hcl. No AWS SDK or network calls.
"""
import fnmatch
import json
import os
from pathlib import Path
import unittest


def items(value):
    return value if isinstance(value, list) else [value]


def matches(patterns, value, case_insensitive=False):
    if case_insensitive:
        return any(fnmatch.fnmatchcase(value.lower(), p.lower()) for p in items(patterns))
    return any(fnmatch.fnmatchcase(value, p) for p in items(patterns))


def conditions_match(conditions, context):
    for operator, entries in conditions.items():
        for key, expected in entries.items():
            present = key in context
            actual = str(context.get(key, '')).lower() if operator in ('Bool', 'BoolIfExists', 'Null') else context.get(key, '')
            expected = [v.replace("${aws:userid}", context.get("aws:userid", "UNRESOLVED_PRINCIPAL")) for v in items(expected)]
            if operator == 'Null':
                ok = str(not present).lower() in expected
            elif operator in ('StringEqualsIfExists', 'BoolIfExists') and not present:
                ok = True
            elif operator in ('StringEquals', 'StringEqualsIfExists', 'Bool', 'BoolIfExists'):
                ok = present and actual in expected
            elif operator == 'StringLike':
                ok = present and matches(expected, actual)
            elif operator == 'StringNotLike':
                ok = not present or not matches(expected, actual)
            else:
                raise AssertionError('Unsupported test condition operator')
            if not ok:
                return False
    return True


def decision(document, action, resource, context):
    allowed = False
    for statement in document['Statement']:
        action_match = (matches(statement['Action'], action, True) if 'Action' in statement
                        else not matches(statement['NotAction'], action, True))
        resource_match = (matches(statement['Resource'], resource) if 'Resource' in statement
                          else not matches(statement['NotResource'], resource))
        if action_match and resource_match and conditions_match(statement.get('Condition', {}), context):
            if statement['Effect'] == 'Deny':
                return 'explicit_deny'
            allowed = True
    return 'allow' if allowed else 'implicit_deny'


def load_run(name):
    filename = os.environ.get('BACKUP_IAM_TEST_PLAN')
    if not filename:
        raise RuntimeError('Generate offline Terraform mock JSON first; no embedded policy substitute')
    with Path(filename).open(encoding='utf-8') as source:
        for line in source:
            if name not in line:
                continue
            event = json.loads(line)
            if event.get('type') == 'test_plan' and event.get('@testrun') == name:
                return {change['address']: change['change']['after']
                        for change in event['test_plan']['resource_changes']}
    raise RuntimeError('Expected mock test plan absent')


class BackupIAMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resources = load_run('backup_security_contract')
        cls.role = resources['aws_iam_role.backup[0]']
        cls.trust = json.loads(cls.role['assume_role_policy'])
        cls.policy = json.loads(resources['aws_iam_role_policy.backup[0]']['policy'])
        cls.boundary = json.loads(resources['aws_iam_policy.backup_boundary[0]']['policy'])
        cls.resources = resources
        cls.data = 'arn:aws:s3:::fixture-nextcloud-data-test'
        cls.backups = 'arn:aws:s3:::fixture-nextcloud-backups-test'
        cls.state = 'arn:aws:s3:::fixture-terraform-state-test'
        cls.ctx = {'aws:SecureTransport': 'true', 's3:ResourceAccount': '000000000000', 'aws:RequestedRegion': 'us-east-1'}

    def expect(self, expected, action, resource, **context):
        ctx = dict(self.ctx, **context)
        self.assertEqual(decision(self.policy, action, resource, ctx), expected)
        self.assertEqual(decision(self.boundary, action, resource, ctx), expected)

    def test_boundary_and_identity_policy_are_identical(self):
        self.assertEqual(self.policy, self.boundary)
        self.assertEqual(self.role['permissions_boundary'], self.resources['aws_iam_policy.backup_boundary[0]']['arn'])
        self.assertEqual(self.role['max_session_duration'], 3600)

    def test_policy_schema_and_size(self):
        self.assertEqual(self.policy['Version'], '2012-10-17')
        sids = [s['Sid'] for s in self.policy['Statement']]
        self.assertEqual(len(sids), len(set(sids)))
        self.assertLess(len(json.dumps(self.policy, separators=(',', ':'))), 6144)
        maximum = load_run('maximum_names_policy_size')
        self.assertLess(len(maximum['aws_iam_policy.backup_boundary[0]']['policy']), 6144)

    def test_only_exact_allow_actions(self):
        actions = {a for s in self.policy['Statement'] if s['Effect'] == 'Allow' for a in items(s['Action'])}
        self.assertEqual(actions, {'ec2:DescribeInstances', 'ec2:DescribeInstanceStatus', 's3:GetBucketLocation',
                                  's3:GetBucketVersioning', 's3:GetEncryptionConfiguration', 's3:ListBucket',
                                  's3:ListBucketVersions', 's3:GetObject', 's3:GetObjectVersion', 's3:PutObject',
                                  's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'})
        self.assertFalse(any('*' in a for a in actions))

    def test_wildcard_allow_only_for_regional_describe(self):
        for statement in self.policy['Statement']:
            if statement['Effect'] == 'Allow' and '*' in items(statement['Resource']):
                self.assertEqual(set(statement['Action']), {'ec2:DescribeInstances', 'ec2:DescribeInstanceStatus'})
                self.assertEqual(statement['Condition'], {'StringEquals': {'aws:RequestedRegion': 'us-east-1'}})

    def test_trust_exact_claims_and_principal(self):
        statement = self.trust['Statement'][0]
        self.assertEqual(statement['Action'], 'sts:AssumeRoleWithWebIdentity')
        self.assertEqual(statement['Principal'], {'Federated': 'arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com'})
        self.assertEqual(statement['Condition']['StringEquals'], {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
            'token.actions.githubusercontent.com:sub': 'repo:fixture@123/platform@456:environment:test',
            'token.actions.githubusercontent.com:ref': 'refs/heads/main',
            'token.actions.githubusercontent.com:workflow': 'Backup platform (manual)',
            'token.actions.githubusercontent.com:repository_id': '456',
            'token.actions.githubusercontent.com:repository_owner_id': '123'})

    def test_trust_rejects_missing_wrong_and_branch_subject_claims(self):
        condition = self.trust['Statement'][0]['Condition']
        context = dict(condition['StringEquals'])
        self.assertTrue(conditions_match(condition, context))
        for key in context:
            for variant in ('absent', 'mismatch'):
                with self.subTest(key=key, variant=variant):
                    altered = dict(context)
                    if variant == 'absent':
                        del altered[key]
                    else:
                        altered[key] = 'different'
                    self.assertFalse(conditions_match(condition, altered))
        context['token.actions.githubusercontent.com:sub'] = 'repo:fixture@123/platform@456:ref:refs/heads/main'
        self.assertFalse(conditions_match(condition, context))
        for ref in ('refs/heads/release', 'refs/tags/main', 'refs/pull/1/merge'):
            with self.subTest(ref=ref):
                altered = dict(condition['StringEquals'], **{'token.actions.githubusercontent.com:ref': ref})
                self.assertFalse(conditions_match(condition, altered))

    def test_discovery_is_regional_not_tag_authorized(self):
        for action in ('ec2:DescribeInstances', 'ec2:DescribeInstanceStatus'):
            self.expect('allow', action, '*')
            self.expect('implicit_deny', action, '*', **{'aws:RequestedRegion': 'eu-west-1'})

    def test_metadata_and_data_inventory_allowed(self):
        for action in ('s3:GetBucketLocation', 's3:GetBucketVersioning', 's3:GetEncryptionConfiguration'):
            for bucket in (self.data, self.backups):
                self.expect('allow', action, bucket)
        for action in ('s3:ListBucket', 's3:ListBucketVersions'):
            self.expect('allow', action, self.data)
        for action in ('s3:GetObject', 's3:GetObjectVersion'):
            self.expect('allow', action, self.data + '/urn:oid:synthetic')

    def test_backup_prefix_positive_and_negative(self):
        for action in ('s3:ListBucket', 's3:ListBucketVersions'):
            for prefix in ('backups/', 'backups/synthetic/manifest.json.age'):
                self.expect('allow', action, self.backups, **{'s3:prefix': prefix})
            for prefix in ('', 'other/', 'backups-other/', '/backups/'):
                self.expect('explicit_deny', action, self.backups, **{'s3:prefix': prefix})
            self.expect('explicit_deny', action, self.backups)

    def test_backup_artifact_actions(self):
        for action in ('s3:GetObject', 's3:GetObjectVersion', 's3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'):
            self.expect('allow', action, self.backups + '/backups/synthetic/database.dump.age')
            self.expect('explicit_deny', action, self.backups + '/other/secret')
            self.expect('explicit_deny', action, self.backups + '/backups-other/secret')

    def test_state_and_other_buckets_explicitly_denied(self):
        for bucket in (self.state, 'arn:aws:s3:::other-bucket'):
            for action, resource in [('s3:ListBucket', bucket), ('s3:GetObject', bucket+'/key'), ('s3:PutObject', bucket+'/key')]:
                self.expect('explicit_deny', action, resource)

    def test_data_writes_and_all_object_deletes_denied(self):
        for action in ('s3:PutObject', 's3:PutObjectAcl', 's3:AbortMultipartUpload'):
            self.expect('explicit_deny', action, self.data + '/object')
        for action in ('s3:DeleteObject', 's3:DeleteObjectVersion'):
            for resource in (self.data+'/object', self.backups+'/backups/synthetic/object'):
                self.expect('explicit_deny', action, resource)

    def test_transport_and_owner_fail_closed(self):
        for action, resource in [('s3:ListBucket', self.data), ('s3:GetObjectVersion', self.data+'/object'),
                                 ('s3:PutObject', self.backups+'/backups/id/object')]:
            self.expect('explicit_deny', action, resource, **{'aws:SecureTransport': 'false'})
            self.expect('implicit_deny', action, resource, **{'s3:ResourceAccount': '111111111111'})
        context = dict(self.ctx)
        del context['s3:ResourceAccount']
        self.assertEqual(decision(self.policy, 's3:GetObject', self.data+'/object', context), 'implicit_deny')

    def test_encryption_header_policy(self):
        target = self.backups+'/backups/id/object'
        self.expect('allow', 's3:PutObject', target)
        self.expect('allow', 's3:PutObject', target, **{'s3:x-amz-server-side-encryption': 'AES256'})
        self.expect('implicit_deny', 's3:PutObject', target, **{'s3:x-amz-server-side-encryption': 'aws:kms'})
        self.expect('implicit_deny', 's3:PutObject', target, **{'s3:x-amz-server-side-encryption-customer-algorithm': 'AES256'})

    def test_no_iam_provisioning_destroy_ssh_or_remote_commands(self):
        for action in ('iam:PassRole', 'iam:CreateRole', 'iam:PutRolePolicy', 'sts:AssumeRole', 'ec2:RunInstances',
                       'ec2:TerminateInstances', 'ec2:AuthorizeSecurityGroupIngress', 'ec2:RevokeSecurityGroupIngress',
                       'ec2:CreateTags', 'ssm:SendCommand', 'ec2-instance-connect:SendSSHPublicKey', 'kms:Decrypt',
                       's3:DeleteBucket', 's3:PutBucketPolicy', 's3:PutLifecycleConfiguration', 's3:PutBucketVersioning',
                       's3:ListBucketMultipartUploads', 's3:PutObjectAcl'):
            self.expect('explicit_deny', action, '*')

    def test_simulated_extra_admin_policy_cannot_exceed_boundary(self):
        admin = {'Statement': [{'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
        for action, resource in [('iam:CreateRole', '*'), ('s3:DeleteObjectVersion', self.backups+'/backups/id/object'),
                                 ('s3:GetObject', self.state+'/key'), ('ec2:AuthorizeSecurityGroupIngress', '*')]:
            self.assertEqual(decision(admin, action, resource, self.ctx), 'allow')
            self.assertEqual(decision(self.boundary, action, resource, self.ctx), 'explicit_deny')

    def test_legacy_subject_and_wrong_repository_cannot_assume_backup(self):
        condition = self.trust['Statement'][0]['Condition']
        for subject in ('repo:fixture/platform:environment:test',
                        'repo:fixture@123/other@456:environment:test',
                        'repo:fixture@123/platform@456:environment:other'):
            with self.subTest(subject=subject):
                context = dict(condition['StringEquals'], **{'token.actions.githubusercontent.com:sub': subject})
                self.assertFalse(conditions_match(condition, context))

    def test_duplicate_workflow_name_is_not_a_file_identity(self):
        # A second file emitting identical claims is indistinguishable to this trust.
        # GitHub branch/Environment/review controls must prevent unauthorized edits.
        condition = self.trust['Statement'][0]['Condition']
        self.assertTrue(conditions_match(condition, dict(condition['StringEquals'])))
        self.assertNotIn('token.actions.githubusercontent.com:job_workflow_ref', condition['StringEquals'])

    def test_bucket_and_object_resources_are_not_interchangeable(self):
        for action in ('s3:GetBucketLocation', 's3:GetBucketVersioning', 's3:GetEncryptionConfiguration',
                       's3:ListBucket', 's3:ListBucketVersions'):
            self.expect('implicit_deny', action, self.data+'/object')
        for action in ('s3:GetObject', 's3:GetObjectVersion', 's3:PutObject'):
            self.expect('implicit_deny', action, self.backups)

    def test_session_resource_grant_risk_is_not_hidden_by_boundary(self):
        # Synthetic direct session Allow: AWS ignores implicit boundary denial here,
        # but explicit denial still overrides it. Not a full resource-policy simulator.
        grant = {'Statement': [{'Effect': 'Allow', 'Action': 's3:PutObject',
                                'Resource': self.backups+'/backups/id/object'}]}
        context = dict(self.ctx, **{'s3:x-amz-server-side-encryption-customer-algorithm': 'AES256'})
        resource = self.backups+'/backups/id/object'
        self.assertEqual(decision(grant, 's3:PutObject', resource, context), 'allow')
        self.assertEqual(decision(self.boundary, 's3:PutObject', resource, context), 'implicit_deny')
        for action, target in [('s3:DeleteObjectVersion', resource), ('s3:GetObject', self.state+'/key'),
                               ('s3:PutObject', self.data+'/key')]:
            self.assertEqual(decision(self.boundary, action, target, context), 'explicit_deny')

    def test_disabled_opt_in_has_no_backup_resources(self):
        resources = load_run('backup_disabled_by_default')
        self.assertFalse(any(name.startswith(('aws_iam_role.backup[', 'aws_iam_role_policy.backup[',
                                             'aws_iam_policy.backup_boundary[')) for name in resources))


class BackupSSMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resources = load_run('explicit_recovery_scope')
        cls.policy = json.loads(resources['aws_iam_role_policy.backup[0]']['policy'])
        cls.boundary = json.loads(resources['aws_iam_policy.backup_boundary[0]']['policy'])
        cls.target = 'arn:aws:ec2:us-east-1:000000000000:instance/i-aaaaaaaaaaaaaaaaa'
        cls.doc = 'arn:aws:ssm:us-east-1::document/AWS-StartSSHSession'
        cls.tags = {'ssm:resourceTag/Project': 'fixture', 'ssm:resourceTag/Environment': 'test'}

    def expect(self, expected, action, resource, context):
        for document in (self.policy, self.boundary):
            self.assertEqual(decision(document, action, resource, context), expected)

    def test_target_allowed_without_optional_service_key(self):
        self.expect('allow', 'ssm:StartSession', self.target, self.tags)

    def test_target_allowed_with_true_service_key(self):
        self.expect('allow', 'ssm:StartSession', self.target,
                    {**self.tags, 'ssm:SessionDocumentAccessCheck': 'true'})

    def test_false_document_check_rejected(self):
        self.expect('implicit_deny', 'ssm:StartSession', self.target,
                    {**self.tags, 'ssm:SessionDocumentAccessCheck': 'false'})

    def test_missing_project_tag_rejected(self):
        self.expect('implicit_deny', 'ssm:StartSession', self.target,
                    {'ssm:resourceTag/Environment': 'test'})

    def test_wrong_environment_rejected(self):
        self.expect('implicit_deny', 'ssm:StartSession', self.target,
                    {**self.tags, 'ssm:resourceTag/Environment': 'foreign'})

    def test_other_instance_rejected(self):
        self.expect('implicit_deny', 'ssm:StartSession', self.target.replace('i-aaaaaaaaaaaaaaaaa', 'i-bbbbbbbbbbbbbbbbb'), self.tags)

    def test_other_region_rejected(self):
        self.expect('implicit_deny', 'ssm:StartSession', self.target.replace('us-east-1', 'us-west-2'), self.tags)

    def test_ssh_document_allowed(self):
        self.expect('allow', 'ssm:StartSession', self.doc, {})

    def test_shell_and_port_forwarding_documents_rejected(self):
        for name in ('SSM-SessionManagerRunShell', 'AWS-StartPortForwardingSession'):
            self.expect('implicit_deny', 'ssm:StartSession', self.doc.replace('AWS-StartSSHSession', name), self.tags)

    def test_dangerous_apis_remain_explicitly_denied(self):
        for action in ('ssm:SendCommand', 'ssm:StartAutomationExecution', 'iam:PassRole', 'iam:PutRolePolicy'):
            self.expect('explicit_deny', action, self.target, self.tags)

    def test_state_access_remains_explicitly_denied(self):
        self.expect('explicit_deny', 's3:GetObject', 'arn:aws:s3:::fixture-state/key', {'aws:SecureTransport': 'true'})

    def test_federated_own_session_termination(self):
        self.expect('allow', 'ssm:TerminateSession', 'arn:aws:ssm:us-east-1:000000000000:session/fixture-run-123', self.session_tags())

    def session_tags(self):
        return {'aws:userid': 'AROA_FIXTURE:fixture-run', 'ssm:resourceTag/aws:ssmmessages:session-id': 'AROA_FIXTURE:fixture-run', 'ssm:resourceTag/aws:ssmmessages:target-id': 'i-aaaaaaaaaaaaaaaaa'}

    def test_other_owner_or_target_or_missing_tags_rejected(self):
        for key in self.session_tags():
            for missing in (True, False):
                ctx = self.session_tags()
                if missing: ctx.pop(key)
                else: ctx[key] = 'foreign'
                self.expect('implicit_deny', 'ssm:TerminateSession', 'arn:aws:ssm:us-east-1:000000000000:session/fixture-run-123', ctx)

    def test_session_prefix_collision_rejected(self):
        ctx = self.session_tags()
        ctx['ssm:resourceTag/aws:ssmmessages:session-id'] += '-other'
        self.expect('implicit_deny', 'ssm:TerminateSession', 'arn:aws:ssm:us-east-1:000000000000:session/fixture-run-123', ctx)

    def test_other_session_account_region_rejected(self):
        for resource in ['arn:aws:ssm:us-west-2:000000000000:session/x', 'arn:aws:ssm:us-east-1:111111111111:session/x']:
            self.expect('implicit_deny', 'ssm:TerminateSession', resource, self.session_tags())

    def test_no_signed_channel_or_resume_permission(self):
        for action in ('ssmmessages:OpenDataChannel', 'ssm:ResumeSession'):
            self.expect('explicit_deny', action, 'arn:aws:ssm:us-east-1:000000000000:session/fixture-run-123', self.session_tags())

    def test_inline_and_boundary_match(self):
        self.assertEqual(self.policy, self.boundary)


if __name__ == '__main__':
    unittest.main()
