from contextlib import contextmanager
from types import SimpleNamespace
import json
import unittest
from scripts.backup.operational.ssm import Target, SSMChannel, ssh_options
from scripts.backup.operational.ssm_policy import caller_statements, agent_statements
from scripts.backup.capture.storage import CaptureFailure


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(region_name='us-east-1', endpoint_url='https://ssm.us-east-1.amazonaws.com')
        self.calls = []; self.token = 'synthetic-not-a-real-session-token'; self.online = True
        self.role = 'synthetic-backup'; self.version = '3.3.40.0'
    def get_caller_identity(self): return {'Account': '123456789012', 'Arn': 'arn:aws:sts::123456789012:assumed-role/' + self.role + '/run-123'}
    def describe_instance_information(self, **kwargs):
        self.calls.append(('describe', kwargs))
        return {'InstanceInformationList': [{'InstanceId': 'i-0123456789abcdef0',
            'PingStatus': 'Online' if self.online else 'Offline', 'PlatformType': 'Linux',
            'PlatformName': 'Ubuntu', 'PlatformVersion': '24.04', 'AgentVersion': self.version}]}
    def start_session(self, **kwargs):
        self.calls.append(('start', kwargs))
        return {'SessionId': 'synthetic-session', 'TokenValue': self.token,
                'StreamUrl': 'wss://ssmmessages.us-east-1.amazonaws.com/v1/data-channel/synthetic'}
    def terminate_session(self, **kwargs):
        self.calls.append(('terminate', kwargs)); return {'SessionId': kwargs['SessionId']}


class SSMTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.target = Target('123456789012', 'us-east-1', 'i-0123456789abcdef0', 'synthetic-backup')
        self.channel = SSMChannel(self.client, self.client, self.target)
    def code(self, expected, action):
        with self.assertRaises(CaptureFailure) as error: action()
        self.assertEqual(error.exception.code, expected)
        self.assertNotIn(self.client.token, str(error.exception))
    def test_constructor_no_io(self): self.assertFalse(self.client.calls)
    def test_preflight_exact_filtered_target(self):
        self.assertEqual(self.channel.preflight()['code'], 'SSM_NODE_READY')
        self.assertEqual(self.client.calls[0][1]['Filters'][0]['Values'], [self.target.instance])
    def test_deploy_role_rejected(self):
        self.client.role = 'synthetic-deploy'; self.code('BACKUP_ROLE_MISMATCH', self.channel.preflight)
    def test_offline_agent(self):
        self.client.online = False; self.code('SSM_TARGET_NOT_READY', self.channel.preflight)
    def test_old_agent(self):
        self.client.version = '3.3.39.0'; self.code('SSM_AGENT_TOO_OLD', self.channel.preflight)
    def test_wrong_endpoint(self):
        self.client.meta.endpoint_url = 'https://evil.invalid'
        self.code('SSM_ENDPOINT_REJECTED', lambda: SSMChannel(self.client, self.client, self.target))
    def test_token_never_in_arguments_and_cleanup(self):
        reference = []
        @contextmanager
        def bridge(argv, env):
            self.assertNotIn(self.client.token, json.dumps(argv))
            self.assertEqual(json.loads(env[argv[0]])['TokenValue'], self.client.token)
            reference.append(env); yield 'private-pipe'
        with self.channel.session(bridge) as stream: self.assertEqual(stream, 'private-pipe')
        self.assertEqual(reference, [{}])
        self.assertEqual(self.client.calls[-1][0], 'terminate')
        self.assertEqual(self.client.calls[-2][1]['DocumentName'], 'AWS-StartSSHSession')
        self.assertEqual(self.client.calls[-2][1]['Parameters'], {'portNumber': ['22']})
    def test_bridge_error_sanitized_and_terminated(self):
        @contextmanager
        def bridge(argv, env):
            raise RuntimeError(self.client.token)
            yield
        def invoke():
            with self.channel.session(bridge): pass
        self.code('SSM_CHANNEL_FAILED', invoke)
        self.assertEqual(self.client.calls[-1][0], 'terminate')
    def test_host_key_no_auto_accept(self):
        args = ssh_options('/private/known_hosts', '/private/key', 'ncbackup-synthetic')
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('ForwardAgent=no', args)
        self.assertNotIn('accept-new', ' '.join(args))
    def test_target_injection_rejected(self):
        self.code('TARGET_INVALID', lambda: Target('123456789012', 'us-east-1', 'i-evil;command', 'synthetic-backup'))
    def test_policy_not_administration(self):
        statements = caller_statements(self.target, 'nextcloud-platform', 'lab')
        actions = {a for s in statements for a in s['Action']}
        self.assertEqual(actions, {'ssm:StartSession', 'ssm:TerminateSession', 'ssm:DescribeInstanceInformation', 'ssmmessages:OpenDataChannel'})
        start = statements[0]
        self.assertEqual(start['Resource'], ['arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0'])
        self.assertTrue(start['Condition']['Bool']['ssm:SessionDocumentAccessCheck'])
    def test_termination_bound_to_federated_owner(self):
        terminate = caller_statements(self.target, 'nextcloud-platform', 'lab')[2]
        self.assertEqual(terminate['Resource'], ['arn:aws:ssm:us-east-1:123456789012:session/${aws:userid}-*'])
    def test_instance_policy_only_channels(self):
        policy = agent_statements('us-east-1')
        self.assertEqual(len(policy[0]['Action']), 5)
        self.assertFalse(any(a.startswith(('s3:', 'ec2messages:', 'iam:')) for a in policy[0]['Action']))
