"""Review-only IAM fragments, not Terraform changes or attached policies.

Must be merged with BOTH approved role policy and permissions boundary.
No OIDC trust change, SG write, RunCommand, default shell or IAM administration.
"""
from .ssm import Target, need


def caller_statements(target, project, environment):
    need(type(target) is Target, 'TARGET_REQUIRED')
    import re
    need(all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,62}', v)
             for v in (project, environment)), 'POLICY_SCOPE_INVALID')
    regional = f'arn:aws:ssm:{target.region}:{target.account}'
    return [
        {'Sid': 'BackupSSHToTaggedTarget', 'Effect': 'Allow', 'Action': ['ssm:StartSession'],
         'Resource': [f'arn:aws:ec2:{target.region}:{target.account}:instance/{target.instance}'],
         'Condition': {'StringEquals': {'ssm:resourceTag/Project': project,
                                       'ssm:resourceTag/Environment': environment},
                       'Bool': {'ssm:SessionDocumentAccessCheck': 'true'}}},
        {'Sid': 'BackupSSHDocumentOnly', 'Effect': 'Allow', 'Action': ['ssm:StartSession'],
         'Resource': [f'arn:aws:ssm:{target.region}::document/AWS-StartSSHSession']},
        {'Sid': 'OwnBackupSessionsOnly', 'Effect': 'Allow', 'Action': ['ssm:TerminateSession'],
         'Resource': [regional + ':session/${aws:userid}-*']},
        {'Sid': 'BackupNodeDiscovery', 'Effect': 'Allow', 'Action': ['ssm:DescribeInstanceInformation'],
         'Resource': ['*'], 'Condition': {'StringEquals': {'aws:RequestedRegion': target.region}}},
        {'Sid': 'OwnBackupDataChannel', 'Effect': 'Allow', 'Action': ['ssmmessages:OpenDataChannel'],
         'Resource': [regional + ':session/${aws:userid}-*']},
    ]


def agent_statements(region):
    # Channel APIs do not support resource-level ARN restriction; no ec2messages
    # fallback. Agent >=3.3.40.0 is required and must be tested on the actual host.
    import re
    need(isinstance(region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region), 'POLICY_SCOPE_INVALID')
    return [{'Sid': 'MinimalSSMAgentChannels', 'Effect': 'Allow',
             'Action': ['ssmmessages:CreateControlChannel', 'ssmmessages:CreateDataChannel',
                        'ssmmessages:OpenControlChannel', 'ssmmessages:OpenDataChannel',
                        'ssm:UpdateInstanceInformation'],
             'Resource': ['*'], 'Condition': {'StringEquals': {'aws:RequestedRegion': region}}}]
