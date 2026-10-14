"""Parse HCL and render operational policy for longest supported inputs, offline.

This checks structure/size, not AWS authorization semantics or Access Analyzer.
"""
import json
from pathlib import Path
import re
import hcl2

ROOT=Path(__file__).resolve().parents[3]
account='000000000000'
project='p'*25
environment='e'*15
name=f'{project}-{environment}'
region='us-east-1'
state='s'*63
prefix=f'states/{account}/{project}/{environment}'
keys=[f'{prefix}/{root}/terraform.tfstate' for root in ('persistent','runtime')]
values={
    'var.project_name':project,'var.environment':environment,'var.aws_region':region,
    'local.state_bucket_arn':f'arn:aws:s3:::{state}',
    'local.data_bucket_arn':'arn:aws:s3:::'+'d'*63,
    'local.backups_bucket_arn':'arn:aws:s3:::'+'b'*63,
    'local.ec2_arn':f'arn:aws:ec2:{region}:{account}',
    'local.iam_host_arn':f'arn:aws:iam::{account}:role/{name}-ec2',
    'local.profile_arn':f'arn:aws:iam::{account}:instance-profile/{name}-ec2',
    'aws_iam_policy.ec2_boundary.arn':f'arn:aws:iam::{account}:policy/{name}-ec2-boundary',
    'data.aws_partition.current.partition':'aws',
    'local.operational_state_keys':keys,
}

def evaluate(value):
    if isinstance(value,list): return [evaluate(item) for item in value]
    if not isinstance(value,str): return value
    if value=='${local.operational_state_keys}': return keys
    if value.startswith('${[for key in local.operational_state_keys : '):
        template=value.split(' : ',1)[1][1:-3]
        return [re.sub(r'\$\{([^}]+)\}',lambda m:key if m[1]=='key' else values[m[1]],template) for key in keys]
    return re.sub(r'\$\{([^}]+)\}',lambda m:values[m[1]],value)

source=hcl2.loads((ROOT/'terraform/aws/bootstrap/operational-iam.tf').read_text(encoding='utf-8'))
document=source['data'][1]['aws_iam_policy_document']['operational']
statements=[]
for statement in document['statement']:
    rendered=dict(Effect='Allow',Action=evaluate(statement['actions']),Resource=evaluate(statement['resources']))
    if 'sid' in statement: rendered['Sid']=statement['sid']
    if 'condition' in statement:
        conditions={}
        for condition in statement['condition']:
            conditions.setdefault(condition['test'],{})[condition['variable']]=evaluate(condition['values'])
        rendered['Condition']=conditions
    statements.append(rendered)
policy={'Version':'2012-10-17','Statement':statements}
serialized=json.dumps(policy,separators=(',',':'))
assert '${' not in serialized, 'Unevaluated reference'
assert len(serialized)<10240, 'IAM role aggregate inline-policy limit exceeded'
locks=next(s for s in statements if s.get('Sid')=='OperationalLocksOnly')
assert all(resource.endswith('.tflock') for resource in locks['Resource'])
assert not any('/bootstrap/' in resource for s in statements for resource in s['Resource'])
backend=next(s for s in statements if s.get('Sid')=='OperationalStateOnly')
assert 's3:DeleteObject' not in backend['Action']
assert len(backend['Resource'])==2
boundary=next(s for s in statements if s.get('Sid')=='CreateHostRoleWithBoundary')
assert boundary['Condition']['StringEquals']['iam:PermissionsBoundary']==[values['aws_iam_policy.ec2_boundary.arn']]
print(f'PASS: operational IAM HCL/JSON, {len(statements)} statements, {len(serialized)} characters (max-name fixture), state/lock scope and boundary; no AWS authorization claimed.')
