"""Explicit, pinned clients. No profiles, IMDS fallback, retries or import IO."""
from dataclasses import dataclass
import importlib.metadata
import re
from .ssm import Target, need
from ..capture.storage import CaptureFailure

VERSIONS = {'boto3': '1.40.76', 'botocore': '1.40.76', 'docker': '7.1.0'}


@dataclass(frozen=True, repr=False)
class TemporaryCredentials:
    access_key: str
    secret_key: str
    token: str

    def __post_init__(self):
        need(isinstance(self.access_key, str) and re.fullmatch(r'ASIA[A-Z0-9]{16}', self.access_key)
             and isinstance(self.secret_key, str) and len(self.secret_key) == 40
             and isinstance(self.token, str) and 16 <= len(self.token) <= 16384,
             'TEMPORARY_CREDENTIALS_REQUIRED')


def dependencies():
    try:
        need(all(importlib.metadata.version(name) == version for name, version in VERSIONS.items()),
             'SDK_VERSION_MISMATCH')
    except importlib.metadata.PackageNotFoundError:
        raise CaptureFailure('PINNED_SDK_UNAVAILABLE') from None


def clients(credentials, target):
    need(type(credentials) is TemporaryCredentials and type(target) is Target, 'SDK_SCOPE_REQUIRED')
    dependencies()
    import boto3
    from botocore.config import Config
    # Explicit keys prevent the default provider chain from loading application
    # Instance Profile credentials. TLS and regional endpoints are not configurable.
    session = boto3.Session(aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key, aws_session_token=credentials.token,
        region_name=target.region)
    config = Config(connect_timeout=10, read_timeout=30,
                    retries={'total_max_attempts': 1, 'mode': 'standard'},
                    proxies={}, signature_version='v4', s3={'addressing_style': 'path'})
    return {service: session.client(service, region_name=target.region,
                endpoint_url=f'https://{service}.{target.region}.amazonaws.com',
                verify=True, config=config) for service in ('sts', 'ssm', 'ec2', 's3')}


def validate_ec2(ec2, target, project, environment):
    """Account/tags/profile/IMDS and root device type checked before the tunnel.

    Does not claim DescribeVolumes verification of encryption or capacity.
    """
    try:
        result = ec2.describe_instances(InstanceIds=[target.instance])['Reservations']
        need(len(result) == 1 and result[0]['OwnerId'] == target.account
             and len(result[0]['Instances']) == 1, 'EC2_TARGET_AMBIGUOUS')
        node = result[0]['Instances'][0]
        tags = {item['Key']: item['Value'] for item in node.get('Tags', [])}
        need(node['InstanceId'] == target.instance and node['State']['Name'] == 'running'
             and tags.get('Project') == project and tags.get('Environment') == environment
             and node.get('MetadataOptions', {}).get('HttpTokens') == 'required'
             and node['MetadataOptions'].get('HttpEndpoint') == 'enabled'
             and node['MetadataOptions'].get('HttpPutResponseHopLimit') == 2,
             'EC2_TARGET_MISMATCH')
        profile = node.get('IamInstanceProfile', {}).get('Arn', '')
        need(profile == f'arn:aws:iam::{target.account}:instance-profile/{project}-{environment}-ec2',
             'EC2_PROFILE_MISMATCH')
        need(node.get('RootDeviceType') == 'ebs', 'EC2_ROOT_VOLUME_MISMATCH')
        return {'account': target.account, 'region': target.region, 'instance': target.instance}
    except CaptureFailure:
        raise
    except Exception:
        raise CaptureFailure('EC2_PREFLIGHT_FAILED') from None
