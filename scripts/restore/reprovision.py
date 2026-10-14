"""Recovery-target creation, separate from existing-host deployment.

No import IO, no fallback from an existing/unknown target, no application start.
AWS/Ansible operations are supplied by ReprovisionOperations, not test flags.
"""
import re
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.json_input import parse_json
from scripts.backup.limits import Limits
from scripts.backup.sealing.common import require
from scripts.restore.creation_lease import CreationLease,digest

FIELDS={'version','account','region','project','github_environment','destination_environment',
        'source_instance','source_data_bucket','data_bucket','backups_bucket','state_bucket',
        'repository','images','source','guard_template','source_policy_sha256','terraform_inputs_sha256',
        'grant_issuer_arn'}

# The existing root remains authoritative; no import, state removal or second
# backend is introduced. A future authorized runtime teardown must retain this
# entire set. Incomplete sets and residual runtime resources are never adopted.
PERSISTENT=frozenset({'terraform_data.isolation','aws_s3_bucket.data',
    'aws_s3_bucket_public_access_block.data','aws_s3_bucket_ownership_controls.data',
    'aws_s3_bucket_versioning.data','aws_s3_bucket_server_side_encryption_configuration.data',
    'aws_s3_bucket_policy.tls'})


def retained_state(document,c):
    require(isinstance(document,dict) and document.get('version')==4
            and type(document.get('serial')) is int and document['serial']>=0
            and isinstance(document.get('lineage'),str) and document['lineage']
            and isinstance(document.get('resources'),list)
            and isinstance(document.get('outputs'),dict),'CREATION_STATE_NOT_EMPTY')
    if not document['resources']:
        assert_empty_state(document);return None
    resources={}
    for row in document['resources']:
        require(isinstance(row,dict) and not row.get('module'),'CREATION_RETAINED_STATE_INVALID')
        address=str(row.get('type'))+'.'+str(row.get('name'))
        if row.get('mode')=='data':
            require(address in {'aws_partition.current','aws_availability_zones.available',
                    'aws_ssm_parameter.ubuntu','aws_iam_policy_document.ec2_trust',
                    'aws_iam_policy_document.storage'},'CREATION_RETAINED_STATE_INVALID')
            continue
        instances=row.get('instances')
        require(row.get('mode')=='managed' and address in PERSISTENT and address not in resources
                and isinstance(instances,list) and len(instances)==1
                and isinstance(instances[0],dict)
                and not any(k in instances[0] for k in ('index_key','deposed'))
                and instances[0].get('status') is None
                and isinstance(instances[0].get('attributes'),dict),
                'CREATION_RETAINED_STATE_INVALID')
        resources[address]=instances[0]['attributes']
    require(set(resources)==PERSISTENT,'CREATION_RETAINED_STATE_INCOMPLETE')
    bucket=resources['aws_s3_bucket.data']
    require(bucket.get('id')==bucket.get('bucket')==c['data_bucket']
            and bucket.get('arn')=='arn:aws:s3:::'+c['data_bucket']
            and bucket.get('force_destroy') is False
            and bucket.get('region')==c['region']
            and bucket.get('tags_all',{}).get('Project')==c['project']
            and bucket.get('tags_all',{}).get('Environment')==c['destination_environment'],
            'CREATION_RETAINED_BUCKET_MISMATCH')
    for address in PERSISTENT-{'terraform_data.isolation','aws_s3_bucket.data'}:
        require(resources[address].get('bucket')==c['data_bucket'],
                'CREATION_RETAINED_BUCKET_MISMATCH')
    isolation=resources['terraform_data.isolation'].get('input')
    if isinstance(isolation,dict) and set(isolation)=={'value','type'}:isolation=isolation['value']
    require(isolation==c['data_bucket'],'CREATION_RETAINED_BUCKET_MISMATCH')
    # Reuse the exact storage checks applied to fresh plans below.
    _storage_controls(resources,c)
    return resources


def _storage_controls(resources,c):
    public=resources['aws_s3_bucket_public_access_block.data']
    require(all(public.get(k) is True for k in ('block_public_acls','block_public_policy','ignore_public_acls','restrict_public_buckets')),
            'CREATION_PLAN_STORAGE_PROTECTION_REJECTED')
    versions=resources['aws_s3_bucket_versioning.data'].get('versioning_configuration')
    require(isinstance(versions,list) and len(versions)==1 and isinstance(versions[0],dict)
            and set(versions[0])<={'status','mfa_delete'} and versions[0].get('status')=='Enabled'
            and versions[0].get('mfa_delete','') in ('','Disabled','Enabled'),
            'CREATION_PLAN_VERSIONING_REJECTED')
    rules=resources['aws_s3_bucket_server_side_encryption_configuration.data'].get('rule',[])
    require(len(rules)==1 and len(rules[0].get('apply_server_side_encryption_by_default',[]))==1
            and rules[0]['apply_server_side_encryption_by_default'][0].get('sse_algorithm')=='AES256',
            'CREATION_PLAN_STORAGE_ENCRYPTION_REJECTED')
    require(resources['aws_s3_bucket_ownership_controls.data'].get('rule')==[{'object_ownership':'BucketOwnerPreferred'}],
            'CREATION_RETAINED_OWNERSHIP_REJECTED')
    from scripts.backup.operational.fence_policy import policy_document
    try:policy=policy_document(resources['aws_s3_bucket_policy.tls'].get('policy',''))
    except CaptureFailure:raise CaptureFailure('CREATION_RETAINED_TLS_REJECTED') from None
    arn='arn:aws:s3:::'+c['data_bucket']
    require(any(s.get('Effect')=='Deny' and s.get('Principal')=='*'
                and s.get('Action') in ('s3:*',['s3:*'])
                and set(s.get('Resource',[]))=={arn,arn+'/*'}
                and s.get('Condition')=={'Bool':{'aws:SecureTransport':'false'}}
                for s in policy['Statement']),'CREATION_RETAINED_TLS_REJECTED')


def configuration(raw):
    c=parse_json(raw.encode(),Limits(json_bytes=65536))
    require(isinstance(c,dict) and set(c)==FIELDS and type(c['version']) is int and c['version']==1,
            'CREATION_CONFIGURATION_INVALID')
    for key,pattern in [('account','[0-9]{12}'),('region','[a-z]{2}(?:-[a-z]+)+-[0-9]+'),
                        ('project','[a-z][a-z0-9-]{1,24}'),('github_environment','[a-z][a-z0-9-]{1,14}'),
                        ('destination_environment','[a-z][a-z0-9-]{1,14}'),
                        ('source_instance','i-[a-f0-9]{17}'),('repository','[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')]:
        require(isinstance(c[key],str) and re.fullmatch(pattern,c[key]),'CREATION_CONFIGURATION_INVALID')
    require(c['github_environment']!=c['destination_environment'],'CREATION_ENVIRONMENT_NOT_ISOLATED')
    require(c['grant_issuer_arn'] not in [f"arn:aws:iam::{c['account']}:role/{c['project']}-{env}-{suffix}"
            for env in (c['github_environment'],c['destination_environment'])
            for suffix in ('github','reprovision','backup','restore','deploy-guard','ec2')],
            'CREATION_ISSUER_NOT_INDEPENDENT')
    require(all(isinstance(c[k],str) and re.fullmatch('[a-f0-9]{64}',c[k]) for k in
                ('source_policy_sha256','terraform_inputs_sha256')) and isinstance(c['grant_issuer_arn'],str)
            and re.fullmatch(f"arn:aws:iam::{c['account']}:role/[A-Za-z0-9+=,.@_/-]+",c['grant_issuer_arn']),
            'CREATION_CONFIGURATION_INVALID')
    buckets=[c[k] for k in ('source_data_bucket','data_bucket','backups_bucket','state_bucket')]
    require(len(set(buckets))==4 and all(isinstance(b,str) and re.fullmatch('[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',b)
                                       for b in buckets),'CREATION_STORAGE_NOT_ISOLATED')
    host=host_configuration(c,'i-'+'0'*17)
    from scripts.restore.host_scope import validate,validate_shared_preparation
    from scripts.restore.access import validate_access
    validate(host);validate_access(access_configuration(c),host)
    guard=guard_configuration(c,host['instance']);validate_shared_preparation(host,guard)
    require(guard['environment']==c['destination_environment'] and guard['repository']==c['repository']
            and guard['installation_identity']==c['source']['installation_identity_sha256'],
            'CREATION_CONFIGURATION_INVALID')
    return c


def host_configuration(c,instance):
    return {**{k:c[k] for k in ('account','region','project','source_instance','source_data_bucket','data_bucket',
                              'backups_bucket','state_bucket','images')},'version':1,'instance':instance,
            'staging_root':'/var/lib/nextcloud-restore','deployment_root':'/opt/nextcloud-platform'}


def guard_configuration(c,instance):
    value=dict(c['guard_template']);value['instance']=instance;return value


def access_configuration(c):
    return {'version':1,'repository':c['repository'],'branch':'main','github_environment':c['github_environment'],
            'resource_environment':c['destination_environment'],
            'role_arn':f"arn:aws:iam::{c['account']}:role/{c['project']}-{c['github_environment']}-restore",'source':c['source']}


def state_key(c):
    return f"states/{c['account']}/{c['project']}/{c['destination_environment']}/restore/terraform.tfstate"


def assert_empty_state(document):
    require(isinstance(document,dict) and type(document.get('version')) is int and document['version']==4
            and type(document.get('serial')) is int and document['serial']>=0
            and isinstance(document.get('lineage'),str) and document['lineage']
            and document.get('resources')==[] and document.get('outputs')=={},'CREATION_STATE_NOT_EMPTY')


def validate_plan(document,c,retained=None):
    require(isinstance(document,dict) and document.get('errored') is not True,'CREATION_PLAN_DRIFT')
    drift=document.get('resource_drift',[])
    if drift:
        # Deprecated bucket mirrors may refresh from the separately owned
        # policy/versioning resources. The preflight verifies those resources
        # and the actual tags; no persistent mutation or arbitrary drift passes.
        require(retained is not None and isinstance(drift,list) and len(drift)==1,
                'CREATION_PLAN_DRIFT')
        row=drift[0];change=row.get('change',{})
        before=change.get('before');after=change.get('after')
        require(row.get('address')=='aws_s3_bucket.data' and row.get('mode')=='managed'
                and change.get('actions')==['update'] and isinstance(before,dict)
                and not change.get('importing') and not change.get('replace_paths')
                and isinstance(after,dict) and before==retained['aws_s3_bucket.data'],
                'CREATION_PLAN_DRIFT')
        changed={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
        require(changed and changed<={'policy','tags','versioning'},'CREATION_PLAN_DRIFT')
        from scripts.backup.operational.fence_policy import policy_document,digest as policy_digest
        if 'policy' in changed:
            try:mirror=policy_document(after.get('policy'))
            except CaptureFailure:raise CaptureFailure('CREATION_PLAN_DRIFT') from None
            expected=policy_document(retained['aws_s3_bucket_policy.tls']['policy'])
            require(policy_digest(mirror)==policy_digest(expected),'CREATION_PLAN_DRIFT')
        if 'tags' in changed:
            require('tags' in before and before['tags'] is None and after['tags']=={},'CREATION_PLAN_DRIFT')
        if 'versioning' in changed:
            old,new=before.get('versioning'),after.get('versioning')
            require(isinstance(old,list) and len(old)==1 and isinstance(old[0],dict)
                    and set(old[0])=={'enabled','mfa_delete'} and old[0]['enabled'] is False
                    and type(old[0]['mfa_delete']) is bool,'CREATION_PLAN_DRIFT')
            owned=retained['aws_s3_bucket_versioning.data']['versioning_configuration'][0]
            expected={'enabled':True,'mfa_delete':owned.get('mfa_delete','')=='Enabled'}
            require(owned['status']=='Enabled' and new==[expected]
                    and all(type(new[0].get(k)) is bool for k in expected)
                    and old[0]['mfa_delete']==expected['mfa_delete'],'CREATION_PLAN_DRIFT')
        retained={**retained,'aws_s3_bucket.data':after}
    else:require(drift==[],'CREATION_PLAN_DRIFT')
    rows=document.get('resource_changes');require(isinstance(rows,list),'CREATION_PLAN_INVALID')
    allowed={'terraform_data.isolation','aws_s3_bucket.data','aws_s3_bucket_public_access_block.data',
             'aws_s3_bucket_ownership_controls.data','aws_s3_bucket_versioning.data',
             'aws_s3_bucket_server_side_encryption_configuration.data','aws_s3_bucket_policy.tls',
             'aws_iam_role_policy.ssm_agent','aws_vpc.main','aws_subnet.public','aws_internet_gateway.main',
             'aws_route_table.public','aws_route.internet','aws_route_table_association.public',
             'aws_security_group.host','aws_vpc_security_group_ingress_rule.ssh',
             'aws_vpc_security_group_ingress_rule.http','aws_vpc_security_group_ingress_rule.grafana',
             'aws_vpc_security_group_egress_rule.outbound','aws_key_pair.operator','aws_iam_role.host',
             'aws_iam_role_policy.storage','aws_iam_instance_profile.host','aws_instance.host'}
    managed=[r for r in rows if r.get('mode')=='managed']
    require(len(managed)==len({r.get('address') for r in managed}) and
            all(isinstance(r.get('address'),str) and r['address'].split('[',1)[0] in allowed for r in managed),
            'CREATION_PLAN_SCOPE_REJECTED')
    require({r['address'].split('[',1)[0] for r in managed}==allowed,'CREATION_PLAN_INCOMPLETE')
    for r in rows:
        change=r.get('change',{});actions=change.get('actions')
        if r.get('mode')=='data':require(actions in (['read'],['no-op']),'CREATION_PLAN_SCOPE_REJECTED');continue
        if retained is not None and r['address'] in PERSISTENT:
            require(r.get('mode')=='managed' and actions==['no-op']
                    and change.get('before')==change.get('after')
                    and not change.get('importing') and not change.get('replace_paths'),
                    'CREATION_PERSISTENT_CHANGE_REJECTED')
            before=change.get('before')
            expected=retained[r['address']]
            if r['address']=='terraform_data.isolation':
                value=expected.get('input')
                if isinstance(value,dict):value=value.get('value')
                require(before.get('id')==expected.get('id') and before.get('input')==value,
                        'CREATION_PERSISTENT_STATE_MISMATCH')
            else:
                require(before==expected,'CREATION_PERSISTENT_STATE_MISMATCH')
            continue
        require(r.get('mode')=='managed' and actions==['create'] and change.get('before') is None
                and not change.get('importing'),
                'CREATION_PLAN_NOT_CREATE_ONLY')
    hosts=[r for r in managed if r['address']=='aws_instance.host']
    buckets=[r for r in managed if r['address']=='aws_s3_bucket.data']
    require(len(hosts)==len(buckets)==1 and buckets[0]['change']['after'].get('bucket')==c['data_bucket'],
            'CREATION_PLAN_TARGET_MISSING')
    after=hosts[0]['change']['after'];root=after.get('root_block_device',[])
    require(after.get('instance_type')=='t3.medium' and len(root)==1 and root[0].get('encrypted') is True
            and root[0].get('volume_type')=='gp3' and type(root[0].get('volume_size')) is int
            and 8<=root[0]['volume_size']<=30,'CREATION_PLAN_CAPACITY_REJECTED')
    tags=after.get('tags_all',{})
    require(tags.get('Project')==c['project'] and tags.get('Environment')==c['destination_environment'],
            'CREATION_PLAN_IDENTITY_REJECTED')
    metadata=after.get('metadata_options',[])
    require(len(metadata)==1 and metadata[0].get('http_tokens')=='required'
            and metadata[0].get('http_endpoint')=='enabled'
            and metadata[0].get('http_put_response_hop_limit')==2,'CREATION_PLAN_IMDS_REJECTED')
    by_address={r['address']:r['change']['after'] for r in managed}
    require(buckets[0]['change']['after'].get('force_destroy') is False,'CREATION_PLAN_STORAGE_PROTECTION_REJECTED')
    role=by_address['aws_iam_role.host']
    require(role.get('permissions_boundary')==f"arn:aws:iam::{c['account']}:policy/{c['project']}-{c['destination_environment']}-ec2-boundary",
            'CREATION_PLAN_HOST_BOUNDARY_REJECTED')
    public=by_address['aws_s3_bucket_public_access_block.data']
    require(all(public.get(k) is True for k in ('block_public_acls','block_public_policy','ignore_public_acls','restrict_public_buckets')),
            'CREATION_PLAN_STORAGE_PROTECTION_REJECTED')
    versioning=by_address['aws_s3_bucket_versioning.data'].get('versioning_configuration')
    require(isinstance(versioning,list) and len(versioning)==1 and isinstance(versioning[0],dict)
            and set(versioning[0])<={'status','mfa_delete'} and versioning[0].get('status')=='Enabled'
            and versioning[0].get('mfa_delete','') in ('','Disabled','Enabled'),
            'CREATION_PLAN_VERSIONING_REJECTED')
    rules=by_address['aws_s3_bucket_server_side_encryption_configuration.data'].get('rule',[])
    require(len(rules)==1 and len(rules[0].get('apply_server_side_encryption_by_default',[]))==1
            and rules[0]['apply_server_side_encryption_by_default'][0].get('sse_algorithm')=='AES256',
            'CREATION_PLAN_STORAGE_ENCRYPTION_REJECTED')
    for r in managed:
        if r['address'].split('[',1)[0].startswith('aws_vpc_security_group_ingress_rule.'):
            import ipaddress
            rule=r['change']['after']
            try:net=ipaddress.IPv4Network(rule['cidr_ipv4'],strict=True)
            except Exception:raise CaptureFailure('CREATION_PLAN_CIDR_REJECTED') from None
            require(rule.get('ip_protocol')=='tcp' and rule.get('from_port')==rule.get('to_port')
                    and rule.get('from_port') in (22,80,3000) and net.prefixlen>0
                    and (rule['from_port']!=22 or net.prefixlen>=24),'CREATION_PLAN_CIDR_REJECTED')


def validate_resume_host_key(proof,instance):
    """Public Ed25519 key observed through administrative SSM, bound by the grant."""
    import base64
    require(isinstance(proof,dict) and set(proof)=={'instance','public_key','ssm_command_id'}
            and proof.get('instance')==instance
            and isinstance(proof.get('ssm_command_id'),str)
            and re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',proof['ssm_command_id'])
            and isinstance(proof.get('public_key'),str)
            and re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}',proof['public_key']),
            'CREATION_RESUME_HOST_KEY_INVALID')
    try:blob=base64.b64decode(proof['public_key'].split()[1],validate=True)
    except ValueError:raise CaptureFailure('CREATION_RESUME_HOST_KEY_INVALID') from None
    require(len(blob)==51 and blob[:19]==b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20',
            'CREATION_RESUME_HOST_KEY_INVALID')
    return proof


def validate_resume_binding(binding,c):
    fields={'previous','workflow','state','creation_grant_sha256'}
    require(isinstance(binding,dict) and set(binding) in (fields,fields|{'host_key'})
            and isinstance(binding['creation_grant_sha256'],str)
            and re.fullmatch('[a-f0-9]{64}',binding['creation_grant_sha256']),
            'CREATION_RESUME_INVALID')
    prior=binding['previous'];snapshot=binding['state'];workflow=binding['workflow']
    require(isinstance(prior,dict) and set(prior)=={'version_id','record'}
            and isinstance(prior['version_id'],str) and prior['version_id'] not in ('','null'),
            'CREATION_RESUME_INVALID')
    old=prior['record']
    require(isinstance(old,dict) and set(old)=={'version','kind','owner','commit','grant_sha256','phase','host','handoff_verified'}
            and type(old['version']) is int and old['version']==1 and old['kind']=='recovery-creation-lease'
            and old['phase'] in ('host_bound','intervention_required') and old['handoff_verified'] is False
            and isinstance(old['owner'],str) and re.fullmatch('[0-9]{1,20}:[0-9]{1,4}',old['owner'])
            and isinstance(old['commit'],str) and re.fullmatch('[a-f0-9]{40}',old['commit'])
            and isinstance(old['grant_sha256'],str) and re.fullmatch('[a-f0-9]{64}',old['grant_sha256']),
            'CREATION_RESUME_INVALID')
    host=old['host']
    require(isinstance(host,dict) and set(host)=={'account','region','instance'}
            and host['account']==c['account'] and host['region']==c['region']
            and isinstance(host['instance'],str) and re.fullmatch('i-[a-f0-9]{17}',host['instance'])
            and host['instance']!=c['source_instance'],'CREATION_RESUME_TARGET_REJECTED')
    if 'host_key' in binding:validate_resume_host_key(binding['host_key'],host['instance'])
    require(isinstance(workflow,dict) and workflow=={
        'repository':c['repository'],'run_id':int(old['owner'].split(':')[0]),
        'attempt':int(old['owner'].split(':')[1]),'commit':old['commit'],
        'status':'completed','conclusion':workflow.get('conclusion')}
        and workflow['conclusion'] in ('failure','cancelled'),'CREATION_RESUME_WORKFLOW_UNVERIFIED')
    require(isinstance(snapshot,dict) and set(snapshot)=={'version_id','sha256'}
            and isinstance(snapshot['version_id'],str) and snapshot['version_id'] not in ('','null')
            and isinstance(snapshot['sha256'],str) and re.fullmatch('[a-f0-9]{64}',snapshot['sha256']),
            'CREATION_RESUME_STATE_UNVERIFIED')
    return binding


def validate_resume_plan(plan,addresses,state=None,node=None):
    require(isinstance(plan,dict) and plan.get('errored') is not True
            and isinstance(plan.get('resource_drift',[]),list) and isinstance(plan.get('resource_changes'),list),
            'CREATION_RESUME_PLAN_CHANGED')
    drift=plan.get('resource_drift',[])
    seen=set()
    for row in drift:
        address=row.get('address');change=row.get('change',{})
        require(state is not None and node is not None and address not in seen and address in addresses
                and address in ('aws_instance.host','aws_security_group.host','aws_iam_role.host',
                    'aws_iam_instance_profile.host','aws_key_pair.operator','aws_route_table.public','aws_vpc.main')
                and row.get('mode')=='managed' and change.get('actions')==['update']
                and not change.get('importing') and not change.get('replace_paths'),
                'CREATION_RESUME_PLAN_CHANGED')
        seen.add(address)
        resources={r['type']+'.'+r['name']:r for r in state['resources'] if r.get('mode')=='managed'}
        expected=resources[address]['instances'][0]['attributes']
        before,after=change.get('before'),change.get('after')
        require(before==expected and isinstance(after,dict),'CREATION_RESUME_PLAN_CHANGED')
        changed={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
        if address in ('aws_iam_role.host','aws_iam_instance_profile.host','aws_key_pair.operator'):
            require(changed and changed<=({'tags','inline_policy'} if address=='aws_iam_role.host' else {'tags'}),
                    'CREATION_RESUME_PLAN_CHANGED')
            if 'tags' in changed:
                require(before.get('tags') is None and after.get('tags')=={},'CREATION_RESUME_PLAN_CHANGED')
            if 'inline_policy' in changed:
                owned=[]
                for key,value in resources.items():
                    if not key.startswith('aws_iam_role_policy.'):continue
                    for instance in value['instances']:
                        policy=instance['attributes']
                        require(policy.get('role')==before.get('name'),'CREATION_RESUME_PLAN_CHANGED')
                        owned.append({'name':policy['name'],'policy':parse_json(policy['policy'].encode(),Limits(json_bytes=131072))})
                actual=after.get('inline_policy')
                require(isinstance(actual,list) and all(isinstance(p,dict) and set(p)=={'name','policy'} for p in actual)
                        and len(actual)==len(owned) and len({p['name'] for p in actual})==len(actual),
                        'CREATION_RESUME_PLAN_CHANGED')
                parsed=[{'name':p['name'],'policy':parse_json(p['policy'].encode(),Limits(json_bytes=131072))} for p in actual]
                require(sorted(map(digest,parsed))==sorted(map(digest,owned)),'CREATION_RESUME_PLAN_CHANGED')
        elif address=='aws_route_table.public':
            require(changed=={'route'} and isinstance(after.get('route'),list),'CREATION_RESUME_PLAN_CHANGED')
            fields={'carrier_gateway_id','cidr_block','core_network_arn','destination_prefix_list_id',
                    'egress_only_gateway_id','gateway_id','ipv6_cidr_block','local_gateway_id',
                    'nat_gateway_id','network_interface_id','odb_network_arn','transit_gateway_id',
                    'vpc_endpoint_id','vpc_peering_connection_id'}
            routes=[]
            for key,value in resources.items():
                if not key.startswith('aws_route.'):continue
                for instance in value['instances']:
                    route=instance['attributes']
                    require(route.get('route_table_id')==before['id'] and route.get('destination_cidr_block')
                            and route.get('gateway_id'),'CREATION_RESUME_PLAN_CHANGED')
                    routes.append({**{k:'' for k in fields},'cidr_block':route['destination_cidr_block'],
                                   'gateway_id':route['gateway_id']})
            require(all(set(r)==fields for r in after['route'])
                    and sorted(map(digest,after['route']))==sorted(map(digest,routes)),
                    'CREATION_RESUME_PLAN_CHANGED')
        elif address=='aws_vpc.main':
            require(changed=={'default_network_acl_id'} and not before.get('default_network_acl_id')
                    and before['id']==node.get('VpcId')
                    and isinstance(after.get('default_network_acl_id'),str)
                    and re.fullmatch('acl-[a-f0-9]{8,17}',after['default_network_acl_id'])
                    and before['id']==node.get('_observed_vpc_id'),
                    'CREATION_RESUME_PLAN_CHANGED')
        elif address=='aws_instance.host':
            require(changed and changed<={'public_ip','public_dns'}
                    and before['id']==node.get('InstanceId')
                    and after.get('public_ip')==node.get('PublicIpAddress')
                    and after.get('public_dns')==node.get('PublicDnsName'),
                    'CREATION_RESUME_PLAN_CHANGED')
        elif address=='aws_security_group.host':
            require(changed and changed<={'ingress','egress'},'CREATION_RESUME_PLAN_CHANGED')
            for direction in changed:
                rules=[]
                for key,value in resources.items():
                    if not key.startswith('aws_vpc_security_group_'+direction+'_rule.'):continue
                    for instance in value['instances']:
                        rule=instance['attributes']
                        require(rule.get('security_group_id')==before['id']
                                and isinstance(rule.get('cidr_ipv4'),str)
                                and not rule.get('cidr_ipv6') and not rule.get('prefix_list_id')
                                and not rule.get('referenced_security_group_id'),'CREATION_RESUME_PLAN_CHANGED')
                        rules.append({'cidr_blocks':[rule['cidr_ipv4']],'ipv6_cidr_blocks':[],
                            'prefix_list_ids':[],'security_groups':[],'self':False,
                            'description':rule.get('description') or '',
                            'protocol':rule['ip_protocol'],'from_port':rule.get('from_port') or 0,
                            'to_port':rule.get('to_port') or 0})
                require(isinstance(after.get(direction),list)
                        and sorted(map(digest,after[direction]))==sorted(map(digest,rules)),
                        'CREATION_RESUME_PLAN_CHANGED')
    found=set()
    for row in plan['resource_changes']:
        change=row.get('change',{})
        if row.get('mode')=='data':
            require(change.get('actions') in (['no-op'],['read']),'CREATION_RESUME_PLAN_CHANGED')
            continue
        require(row.get('mode')=='managed' and row.get('address') in addresses
                and row['address'] not in found and change.get('actions')==['no-op']
                and change.get('before')==change.get('after') and not change.get('importing')
                and not change.get('replace_paths'),'CREATION_RESUME_PLAN_CHANGED')
        found.add(row['address'])
    require(found==addresses,'CREATION_RESUME_PLAN_CHANGED')
    for name,change in plan.get('output_changes',{}).items():
        if change.get('actions')==['no-op'] and change.get('before')==change.get('after'):continue
        require(name=='public_ip' and state is not None and node is not None
                and change.get('actions')==['update']
                and change.get('before')==state['outputs']['public_ip']['value']
                and change.get('after')==node.get('PublicIpAddress'),'CREATION_RESUME_PLAN_CHANGED')


def execute(lease,operations,wait_seconds=0):
    require(type(lease) is CreationLease,'CREATION_AUTHORITY_REQUIRED')
    c=lease.configuration
    lease.authorize(wait_seconds)
    if lease.resume is not None:
        # No apply in this branch. Check exact state before changing ownership,
        # repeat under that authority, and verify idle host before Ansible roles.
        outputs=operations.inspect_resume(c,lease)
        with lease.hold():
            lease.assert_held();outputs=operations.inspect_resume(c,lease)
            operations.install(c,outputs,lease)
            with operations.remote_lease(c,outputs,lease) as remote:
                lease.handoff(remote);remote.assert_held()
                operations.verify_prepared(c,outputs,lease)
                lease.release_to_host(remote)
        return {'code':'RECOVERY_HOST_PREPARED','instance_id':lease.record['host']['instance'],
                'backup_complete':False,'restore_proven':False,'destroy_authorized':False}
    # Absence is observed, never inferred from an input/flag or API failure.
    with lease.hold(wait_seconds):
        operations.assert_absent(c);lease.assert_held()
        if lease.reconciliation_receipt is not None:lease.assert_reconciled_state(lease.reconciliation_receipt)
        plan=operations.plan(c,lease);validate_plan(plan,c,getattr(operations,'retained',None))
        operations.assert_absent(c);lease.assert_held()
        if lease.reconciliation_receipt is not None:lease.assert_reconciled_state(lease.reconciliation_receipt)
        outputs=operations.apply(c,lease)
        identity=operations.verify_created(c,outputs);lease.bind(identity)
        lease.assert_held();operations.install(c,outputs,lease)
        with operations.remote_lease(c,outputs,lease) as remote:
            lease.handoff(remote);remote.assert_held()
            operations.verify_prepared(c,outputs,lease)
            lease.assert_held();remote.assert_held()
            lease.release_to_host(remote)
    return {'code':'RECOVERY_HOST_PREPARED','instance_id':identity['instance'],
            'backup_complete':False,'restore_proven':False,'destroy_authorized':False}
