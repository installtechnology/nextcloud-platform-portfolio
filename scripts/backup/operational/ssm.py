"""Explicit SSM SSH channel primitives. No CLI, discovery or import-time IO.

Injected SDK/plugin bridge must be independently pinned and verified before use.
Session tokens are passed only through a private child environment, never argv.
No shell session, remote command, SG change or credentials-file fallback.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit
from ..capture.storage import CaptureFailure


def need(condition, code):
    if not condition:
        raise CaptureFailure(code)


@dataclass(frozen=True, repr=False)
class Target:
    account: str
    region: str
    instance: str
    role_name: str

    def __post_init__(self):
        need(isinstance(self.account, str) and re.fullmatch(r'[0-9]{12}', self.account), 'TARGET_INVALID')
        need(isinstance(self.region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', self.region), 'TARGET_INVALID')
        need(isinstance(self.instance, str) and re.fullmatch(r'i-[a-f0-9]{17}', self.instance), 'TARGET_INVALID')
        need(isinstance(self.role_name, str) and re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,64}', self.role_name), 'TARGET_INVALID')


class SSMChannel:
    document = 'AWS-StartSSHSession'
    token_variable = 'AWS_SSM_START_SESSION_RESPONSE_NEXTCLOUD'

    def __init__(self, ssm, sts, target):
        need(type(target) is Target, 'TARGET_REQUIRED')
        try:
            endpoint = urlsplit(ssm.meta.endpoint_url)
            need(ssm.meta.region_name == target.region and endpoint.scheme == 'https'
                 and endpoint.hostname == f'ssm.{target.region}.amazonaws.com'
                 and endpoint.port in (None, 443) and endpoint.path in ('', '/')
                 and not endpoint.username and not endpoint.password and not endpoint.query
                 and not endpoint.fragment, 'SSM_ENDPOINT_REJECTED')
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure('SSM_ENDPOINT_REJECTED') from None
        self.ssm, self.sts, self.target = ssm, sts, target

    def preflight(self):
        try:
            identity = self.sts.get_caller_identity()
            arn = identity['Arn']
            need(identity['Account'] == self.target.account and isinstance(arn, str)
                 and re.fullmatch('arn:aws:sts::' + re.escape(self.target.account)
                     + ':assumed-role/' + re.escape(self.target.role_name) + '/[A-Za-z0-9+=,.@_-]{2,64}', arn),
                 'BACKUP_ROLE_MISMATCH')
            response = self.ssm.describe_instance_information(
                Filters=[{'Key': 'InstanceIds', 'Values': [self.target.instance]}], MaxResults=5)
            entries = response.get('InstanceInformationList')
            need(isinstance(entries, list) and len(entries) == 1 and not response.get('NextToken'), 'SSM_TARGET_AMBIGUOUS')
            node = entries[0]
            need(node.get('InstanceId') == self.target.instance and node.get('PingStatus') == 'Online'
                 and node.get('PlatformType') == 'Linux' and node.get('PlatformName') == 'Ubuntu'
                 and node.get('PlatformVersion') == '24.04', 'SSM_TARGET_NOT_READY')
            version = node.get('AgentVersion', '')
            need(isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', version), 'SSM_AGENT_VERSION_INVALID')
            need(tuple(map(int, version.split('.'))) >= (3, 3, 40, 0), 'SSM_AGENT_TOO_OLD')
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure('SSM_PREFLIGHT_FAILED') from None
        return {'code': 'SSM_NODE_READY', 'agent_version': version}

    @contextmanager
    def session(self, plugin_bridge):
        """Bridge owns a pinned subprocess and pipes; result never includes token.

        Bridge must discard stderr, keep diagnostics off, prohibit profile/IMDS
        credential discovery and terminate its process when this context exits.
        No actual subprocess factory is supplied by this module.
        """
        self.preflight()
        request = {'Target': self.target.instance, 'DocumentName': self.document,
                   'Parameters': {'portNumber': ['22']}}
        sid = None
        try:
            response = self.ssm.start_session(**request)
            candidate = response.get('SessionId')
            need(isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,96}', candidate), 'SSM_SESSION_INVALID')
            sid = candidate
            token = response.get('TokenValue')
            need(isinstance(token, str) and 1 <= len(token) <= 65536, 'SSM_SESSION_INVALID')
            url = urlsplit(response.get('StreamUrl', ''))
            need(url.scheme == 'wss' and url.hostname == f'ssmmessages.{self.target.region}.amazonaws.com'
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.fragment, 'SSM_STREAM_REJECTED')
            payload = json.dumps({k: response[k] for k in ('SessionId', 'StreamUrl', 'TokenValue')})
            argv = [self.token_variable, self.target.region, 'StartSession', '',
                    json.dumps(request), f'https://ssm.{self.target.region}.amazonaws.com']
            environment = {self.token_variable: payload, 'AWS_EC2_METADATA_DISABLED': 'true'}
            try:
                with plugin_bridge(argv, environment) as stream:
                    yield stream
            finally:
                environment.clear()
        except CaptureFailure:
            raise
        except BaseException:
            raise CaptureFailure('SSM_CHANNEL_FAILED') from None
        finally:
            if sid is not None:
                try:
                    result = self.ssm.terminate_session(SessionId=sid)
                    need(result.get('SessionId') == sid, 'SSM_CLEANUP_FAILED')
                except BaseException:
                    # A stale session is not silently ignored and is not capture recovery.
                    raise CaptureFailure('SSM_CLEANUP_FAILED') from None


def ssh_options(known_hosts, identity_file, alias):
    """No ssh-keyscan/accept-new. Files must be verified private inputs by launcher.

    Returned argv is data, never interpolated into a shell command. SSM bridge
    must implement a fixed ProxyCommand executable, not a caller-supplied string.
    """
    need(isinstance(alias, str) and re.fullmatch(r'ncbackup-[a-z0-9-]{1,50}', alias), 'SSH_ALIAS_INVALID')
    need(all(isinstance(p, str) and p and not any(c in p for c in '\r\n\0')
             for p in (known_hosts, identity_file)), 'SSH_PATH_INVALID')
    return ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + known_hosts, '-o', 'HostKeyAlias=' + alias,
            '-o', 'IdentitiesOnly=yes', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'ConnectTimeout=20', '-o', 'ControlMaster=no', '-i', identity_file]
