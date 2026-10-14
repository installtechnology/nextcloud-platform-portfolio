"""Pinned regional AWS, saved Terraform plan and existing Ansible preparation.

No production mutations on import. No Terraform lock bypass, retries, deletions,
host-key TOFU, plaintext backups or automatic IAM rebinding.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from scripts.backup.capture.storage import CaptureFailure
from scripts.backup.sealing.common import require
from scripts.backup.operational.remote_lease import AuthenticatedHostChannel,RemoteDeployLease
from scripts.backup.operational.ssm import Target
from scripts.backup.operational.ssh_channel import SSHChannel
from scripts.restore.creation_lease import digest
from scripts.restore.reprovision import state_key,retained_state,host_configuration,guard_configuration,access_configuration


class InitialSSHChannel(AuthenticatedHostChannel):
    """Authenticated admin key + independently verified EC2 console host key."""
    def __init__(self,argv):self.argv=argv;self.process=None;self.buffer=bytearray();self.mode='lease'
    request=SSHChannel.request
    @contextmanager
    def open(self):
        self.process=subprocess.Popen(self.argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
        try:yield self
        finally:
            self.process.stdin.close()
            try:self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=10)
            self.process.stdout.close();self.process=None;self.buffer.clear()


class ReprovisionOperations:
    def __init__(self,sdk,ci,c,owner,commit):
        self.sdk,self.ci,self.c,self.owner,self.commit=sdk,ci,c,owner,commit
        self.directory=ci.paths();self.plan_path=self.directory/'restore-creation.tfplan';self.bound=None
        self.retained=None;self.state_observation=None;self.preflight_observed=False
    def assert_absent(self,c):
        require(self.sdk['sts'].get_caller_identity()['Account']==c['account'],'CREATION_ACCOUNT_MISMATCH')
        response=self.sdk['ec2'].describe_instances(Filters=[{'Name':'tag:Project','Values':[c['project']]},
            {'Name':'tag:Environment','Values':[c['destination_environment']]}])
        require('NextToken' not in response,'CREATION_DISCOVERY_TRUNCATED')
        nodes=[n for r in response['Reservations'] for n in r['Instances']]
        require(all(n.get('State',{}).get('Name')=='terminated' for n in nodes),'CREATION_RUNTIME_ALREADY_EXISTS')
        s3=self.sdk['s3'];kw=dict(Bucket=c['state_bucket'],Key=state_key(c),ExpectedBucketOwner=c['account'])
        observed=None;retained=None
        try:value=s3.get_object(**kw)
        except Exception as e:
            if getattr(e,'response',{}).get('Error',{}).get('Code')!='NoSuchKey':
                raise CaptureFailure('CREATION_STATE_UNVERIFIED') from None
            # Deleted historical states are ambiguous, not a new environment.
            rows=s3.list_object_versions(Bucket=c['state_bucket'],Prefix=state_key(c),ExpectedBucketOwner=c['account'])
            require(rows.get('IsTruncated') is False and not rows.get('Versions') and not rows.get('DeleteMarkers'),
                    'CREATION_STATE_HISTORY_PRESENT')
        else:
            body=value['Body']
            try:
                from scripts.backup.json_input import parse_json
                from scripts.backup.limits import Limits
                raw=body.read(1048577)
                document=parse_json(raw,Limits(json_bytes=1048576))
                retained=retained_state(document,c)
                if retained is not None:
                    require(isinstance(value.get('VersionId'),str) and value['VersionId'] not in ('','null')
                            and value.get('ServerSideEncryption')=='AES256',
                            'CREATION_RETAINED_STATE_UNVERIFIED')
                observed=(value.get('VersionId'),__import__('hashlib').sha256(raw).hexdigest())
            finally:body.close()
        # A retained bucket is accepted only with its complete existing Terraform
        # ownership. This never imports an unrelated/partially initialized bucket.
        try:s3.head_bucket(Bucket=c['data_bucket'],ExpectedBucketOwner=c['account'])
        except Exception as e:
            response=getattr(e,'response',{})
            require(response.get('ResponseMetadata',{}).get('HTTPStatusCode')==404,
                    'CREATION_DESTINATION_BUCKET_UNVERIFIED')
            require(retained is None,'CREATION_RETAINED_BUCKET_MISSING')
        else:
            require(retained is not None,'CREATION_DESTINATION_BUCKET_EXISTS')
            from scripts.restore.s3_reader import DestinationObjects
            from scripts.backup.operational.fence_policy import policy_document,digest as policy_digest
            bucket=DestinationObjects(s3,c['data_bucket'],c['account'],c['region'],c['source_data_bucket'],
                                      {c['backups_bucket'],c['state_bucket']})
            bucket.assert_protections()
            actual=policy_document(s3.get_bucket_policy(Bucket=c['data_bucket'],ExpectedBucketOwner=c['account'])['Policy'])
            expected=policy_document(retained['aws_s3_bucket_policy.tls']['policy'])
            require(policy_digest(actual)==policy_digest(expected),'CREATION_RETAINED_POLICY_DRIFT')
            try:
                tags=s3.get_bucket_tagging(Bucket=c['data_bucket'],ExpectedBucketOwner=c['account'])
                versions=s3.get_bucket_versioning(Bucket=c['data_bucket'],ExpectedBucketOwner=c['account'])
            except Exception:raise CaptureFailure('CREATION_RETAINED_METADATA_UNVERIFIED') from None
            entries=tags.get('TagSet') if isinstance(tags,dict) else None
            require(isinstance(entries,list) and all(isinstance(t,dict) and set(t)=={'Key','Value'}
                    and isinstance(t['Key'],str) and isinstance(t['Value'],str) for t in entries)
                    and len({t['Key'] for t in entries})==len(entries)
                    and {t['Key']:t['Value'] for t in entries}==retained['aws_s3_bucket.data']['tags_all'],
                    'CREATION_RETAINED_TAGS_DRIFT')
            owned=retained['aws_s3_bucket_versioning.data']['versioning_configuration'][0]
            require(isinstance(versions,dict) and versions.get('Status')==owned['status']
                    and versions.get('MFADelete','Disabled')==(owned.get('mfa_delete') or 'Disabled'),
                    'CREATION_RETAINED_VERSIONING_DRIFT')
        if self.preflight_observed:
            require(observed==self.state_observation,'CREATION_STATE_CHANGED')
        else:
            self.state_observation=observed;self.retained=retained;self.preflight_observed=True
        from scripts.backup.operational.fence_policy import policy_document,digest as policy_digest
        raw=s3.get_bucket_policy(Bucket=c['source_data_bucket'],ExpectedBucketOwner=c['account'])['Policy']
        policy=policy_document(raw)
        require(policy_digest(policy)==c['source_policy_sha256'] and not any(str(s.get('Sid','')).startswith(
                    ('BackupObjectFence','BackupPolicyFence','BackupConfigurationFence')) for s in policy['Statement']),
                'CREATION_SOURCE_FENCING_UNVERIFIED')

    def plan(self,c,lease):
        lease.assert_held();ci=self.ci;root=ci.ROOT/'terraform/aws/restore'
        values={}
        values.update(aws_account_id=c['account'],aws_region=c['region'],project_name=c['project'],
            environment=c['destination_environment'],source_environment=c['source']['environment'],
            source_data_bucket_name=c['source_data_bucket'],data_bucket_name=c['data_bucket'],
            backups_bucket_name=c['backups_bucket'],state_bucket_name=c['state_bucket'],
            ec2_permissions_boundary_arn=f"arn:aws:iam::{c['account']}:policy/{c['project']}-{c['destination_environment']}-ec2-boundary",
            ssh_public_key=ci.required('SSH_PUBLIC_KEY'),ssh_allowed_cidrs=json.loads(ci.required('SSH_ALLOWED_CIDRS')),
            http_allowed_cidrs=json.loads(ci.required('HTTP_ALLOWED_CIDRS')),
            instance_type='t3.medium',root_volume_size_gib=30,
            tags={k:v for k,v in self.retained['aws_s3_bucket.data']['tags_all'].items()
                  if k not in ('Project','Environment','ManagedBy')} if self.retained else {},
            creation_grant=getattr(self,'original_creation_grant',lease.grant[1]))
        ci.private_file(self.directory/'restore-creation.tfvars.json',json.dumps(values))
        backend={'bucket':c['state_bucket'],'key':state_key(c),'region':c['region'],
                 'encrypt':True,'use_lockfile':True,'allowed_account_ids':[c['account']]}
        ci.private_file(self.directory/'restore-creation.tfbackend','\n'.join(k+' = '+json.dumps(v) for k,v in backend.items()))
        (root/'backend.tf').write_text((root/'backend.tf.example').read_text())
        self.tf=['terraform','-chdir=terraform/aws/restore']
        ci.run(self.tf+['init','-input=false','-lockfile=readonly','-backend-config='+str(self.directory/'restore-creation.tfbackend')])
        ci.run(self.tf+['validate']);lease.assert_held()
        ci.run(self.tf+['plan','-input=false','-lock-timeout=5m','-var-file='+str(self.directory/'restore-creation.tfvars.json'),'-out='+str(self.plan_path)])
        os.chmod(self.plan_path,0o600);self.plan_sha=__import__('hashlib').sha256(self.plan_path.read_bytes()).hexdigest()
        return json.loads(ci.run(self.tf+['show','-json',str(self.plan_path)],capture=True))

    def apply(self,c,lease):
        import hashlib
        lease.assert_held();require(hashlib.sha256(self.plan_path.read_bytes()).hexdigest()==self.plan_sha,'CREATION_PLAN_CHANGED')
        require(os.environ.get('CONFIRMATION')=='APPLY','CREATION_APPLY_NOT_AUTHORIZED')
        self.ci.run(self.tf+['apply','-input=false','-lock-timeout=5m',str(self.plan_path)])
        lease.assert_held();return json.loads(self.ci.run(self.tf+['output','-json'],capture=True))

    def inspect_resume(self,c,lease):
        """Pinned existing state only; never imports, applies or selects another host."""
        import hashlib
        from scripts.backup.json_input import parse_json
        from scripts.backup.limits import Limits
        from scripts.restore.reprovision import validate_resume_binding,PERSISTENT
        binding=validate_resume_binding(lease.resume,c)
        require(self.sdk['sts'].get_caller_identity()['Account']==c['account'],'CREATION_ACCOUNT_MISMATCH')
        value=self.sdk['s3'].get_object(Bucket=c['state_bucket'],Key=state_key(c),ExpectedBucketOwner=c['account'])
        body=value['Body']
        try:
            raw=body.read(1048577)
            require(value.get('VersionId')==binding['state']['version_id']
                    and value.get('ServerSideEncryption')=='AES256'
                    and len(raw)<=1048576 and hashlib.sha256(raw).hexdigest()==binding['state']['sha256'],
                    'CREATION_RESUME_STATE_CHANGED')
            state=parse_json(raw,Limits(json_bytes=1048576))
        finally:body.close()
        managed={};addresses=set();plan_addresses=set()
        for row in state.get('resources',[]):
            if row.get('mode')=='data':continue
            address=str(row.get('type'))+'.'+str(row.get('name'))
            require(row.get('mode')=='managed' and not row.get('module') and address not in addresses
                    and isinstance(row.get('instances'),list) and row['instances']
                    and all(isinstance(i.get('attributes'),dict) and not i.get('deposed')
                            and i.get('status') is None for i in row['instances']),
                    'CREATION_RESUME_STATE_INVALID')
            addresses.add(address);managed[address]=row
            for item in row['instances']:
                key=item.get('index_key')
                full=address if key is None else address+'['+json.dumps(key,separators=(',',':'))+']'
                require(full not in plan_addresses,'CREATION_RESUME_STATE_INVALID')
                plan_addresses.add(full)
        allowed=PERSISTENT|{'aws_iam_role_policy.ssm_agent','aws_vpc.main','aws_subnet.public',
            'aws_internet_gateway.main','aws_route_table.public','aws_route.internet',
            'aws_route_table_association.public','aws_security_group.host','aws_vpc_security_group_ingress_rule.ssh',
            'aws_vpc_security_group_ingress_rule.http','aws_vpc_security_group_ingress_rule.grafana',
            'aws_vpc_security_group_egress_rule.outbound','aws_key_pair.operator','aws_iam_role.host',
            'aws_iam_role_policy.storage','aws_iam_instance_profile.host','aws_instance.host'}
        require(addresses==allowed,'CREATION_RESUME_STATE_INVALID')
        self.resume_plan_addresses=plan_addresses
        require(all(len(managed[a]['instances'])==1 for a in PERSISTENT|{'aws_instance.host'}),
                'CREATION_RESUME_STATE_INVALID')
        retained={**state,'resources':[managed[a] for a in sorted(PERSISTENT)],'outputs':{}}
        self.retained=retained_state(retained,c)
        attrs=managed['aws_instance.host']['instances'][0]['attributes']
        identity=binding['previous']['record']['host']
        require(attrs.get('id')==identity['instance'] and
                attrs.get('tags',{}).get('ReprovisionGrant')==binding['creation_grant_sha256'],
                'CREATION_RESUME_TARGET_REJECTED')
        outputs=state.get('outputs',{})
        require(isinstance(outputs,dict) and outputs.get('instance_id',{}).get('value')==identity['instance'],
                'CREATION_RESUME_TARGET_REJECTED')
        node=self.sdk['ec2'].describe_instances(InstanceIds=[identity['instance']])['Reservations'][0]['Instances'][0]
        import copy,ipaddress
        require(node.get('InstanceId')==identity['instance'],'CREATION_RESUME_TARGET_REJECTED')
        require(sorted(g['GroupId'] for g in node.get('SecurityGroups',[]))==sorted(attrs.get('vpc_security_group_ids',[]))
                and node.get('SubnetId')==attrs.get('subnet_id'),'CREATION_RESUME_TARGET_REJECTED')
        try:address=ipaddress.IPv4Address(node['PublicIpAddress'])
        except Exception:raise CaptureFailure('CREATION_RESUME_ENDPOINT_UNVERIFIED') from None
        require(address.is_global,'CREATION_RESUME_ENDPOINT_UNVERIFIED')
        self.resume_state=state;self.resume_node=node
        # An EBS-backed stop/start preserves the instance, not its public IP.
        # Resolve only from EC2 API; SSH still verifies the console host key.
        outputs=copy.deepcopy(outputs)
        require('public_ip' in outputs,'CREATION_RESUME_ENDPOINT_UNVERIFIED')
        outputs['public_ip']['value']=str(address)
        self.verify_created(c,outputs)
        tags={t['Key']:t['Value'] for t in node.get('Tags',[])}
        require(tags.get('ReprovisionGrant')==binding['creation_grant_sha256'],
                'CREATION_RESUME_TARGET_REJECTED')
        discovered=self.sdk['ec2'].describe_instances(Filters=[{'Name':'tag:Project','Values':[c['project']]},
            {'Name':'tag:Environment','Values':[c['destination_environment']]}])
        require(not discovered.get('NextToken') and
                [n.get('InstanceId') for r in discovered['Reservations'] for n in r['Instances']
                 if n.get('State',{}).get('Name')!='terminated']==[identity['instance']],
                'CREATION_RESUME_TARGET_AMBIGUOUS')
        return outputs

    def verify_created(self,c,outputs):
        instance=outputs['instance_id']['value'];require(outputs['destination_data_bucket']['value']==c['data_bucket'],
                                                       'CREATION_OUTPUT_MISMATCH')
        from scripts.backup.operational.sdk import validate_ec2
        target=Target(c['account'],c['region'],instance,c['project']+'-'+c['github_environment']+'-reprovision')
        import time
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            node=self.sdk['ec2'].describe_instances(InstanceIds=[instance])['Reservations'][0]['Instances'][0]
            require(node['State']['Name'] in ('pending','running'),'CREATION_HOST_NOT_RUNNING')
            if node['State']['Name']=='running':break
            time.sleep(5)
        else:raise CaptureFailure('CREATION_HOST_START_TIMEOUT')
        validate_ec2(self.sdk['ec2'],target,c['project'],c['destination_environment'])
        node=self.sdk['ec2'].describe_instances(InstanceIds=[instance])['Reservations'][0]['Instances'][0]
        require(node['PublicIpAddress']==outputs['public_ip']['value'],'CREATION_OUTPUT_MISMATCH')
        roots=[m['Ebs']['VolumeId'] for m in node['BlockDeviceMappings'] if m['DeviceName']==node['RootDeviceName']]
        volumes=self.sdk['ec2'].describe_volumes(VolumeIds=roots)['Volumes']
        require(len(volumes)==1 and volumes[0]['Encrypted'] is True and volumes[0]['VolumeType']=='gp3'
                and 8<=volumes[0]['Size']<=30,'CREATION_VOLUME_REJECTED')
        self.bound=target
        self.ci.private_file(self.directory/'runtime.json',json.dumps({'instance_id':instance,'public_ip':node['PublicIpAddress'],
            'security_group_id':node['SecurityGroups'][0]['GroupId'],'ssh_user':'ubuntu','data_bucket_name':c['data_bucket']}))
        return {'account':c['account'],'region':c['region'],'instance':instance}

    def install(self,c,outputs,lease):
        if getattr(lease,'resume',None) is not None:
            # Keep the original Terraform grant tag; the new grant authorizes
            # preparation only. Resource mutations/unproved drift block; no apply.
            self.original_creation_grant=lease.resume['creation_grant_sha256']
            # Reuse the existing narrowly tagged stale-CI-rule cleanup before
            # refresh. It never revokes the operator's permanent CIDRs.
            runtime=json.loads((self.directory/'runtime.json').read_text())
            self.ci.revoke_owned(runtime['security_group_id'],current_only=False)
            plan=self.plan(c,lease)
            if any(r.get('address')=='aws_vpc.main' for r in plan.get('resource_drift',[])):
                # DescribeVpcs is already allowed; do not introduce a new IAM
                # dependency just to refresh the provider's implicit ACL ID.
                observed=self.sdk['ec2'].describe_vpcs(VpcIds=[self.resume_node['VpcId']])
                vpcs=observed.get('Vpcs',[])
                prior=next(r for r in self.resume_state['resources'] if r.get('type')=='aws_vpc'
                           and r.get('name')=='main')['instances'][0]['attributes']
                require(not observed.get('NextToken') and len(vpcs)==1
                        and vpcs[0].get('VpcId')==self.resume_node['VpcId']==prior['id']
                        and vpcs[0].get('OwnerId')==c['account']
                        and vpcs[0].get('CidrBlock')==prior['cidr_block'],
                        'CREATION_RESUME_NETWORK_UNVERIFIED')
                self.resume_node['_observed_vpc_id']=vpcs[0]['VpcId']
            from scripts.restore.reprovision import validate_resume_plan
            validate_resume_plan(plan,self.resume_plan_addresses,self.resume_state,self.resume_node)
        # Validate before opening the current runner's temporary SSH rule;
        # otherwise that authorized transient rule can pollute plan refresh.
        self.ci.connect_created_host(lease,self.bound)
        host=host_configuration(c,self.bound.instance)
        from scripts.restore.deployment import preparation_variables
        v=preparation_variables(host,self.ci.ROOT,self.directory,
             Path(self.ci.required('RUNNER_TEMP'))/'nextcloud-operational/wheels',self.commit)
        v.update(restore_shared_guard_configuration=guard_configuration(c,self.bound.instance),
            restore_access_configuration=access_configuration(c),restore_channel_confirmation='PREPARE_RESTORE_CHANNEL',
            restore_channel_public_key=self.ci.required('RESTORE_SSH_PUBLIC_KEY'),
            creation_receipt={'version':1,'owner':self.owner,'commit':self.commit,'configuration_sha256':digest(c),
                              'host':lease.record['host']})
        self.ci.private_file(self.directory/'creation-prepare.json',json.dumps(v))
        os.environ['ANSIBLE_SSH_ARGS']='-F '+str(self.directory/'ssh_config')
        for playbook in ('restore-prepare.yml','restore-channel-prepare.yml','creation-handoff-prepare.yml'):
            lease.assert_held();self.ci.run(['ansible-playbook','-i',str(self.directory/'inventory.json'),
                '--private-key',str(self.directory/'ssh-key'),'playbooks/'+playbook,
                '--extra-vars','@'+str(self.directory/'creation-prepare.json')],cwd=self.ci.ROOT/'ansible')
        lease.assert_held()

    @contextmanager
    def remote_lease(self,c,outputs,lease):
        ip=outputs['public_ip']['value']
        # Python must resolve the root-owned reviewed repository, not the SSH
        # account's home directory or an untrusted PYTHONPATH.
        argv=['/usr/bin/ssh','-F',str(self.directory/'ssh_config'),ip,
              'exec sudo -n /usr/bin/env --chdir=/usr/local/lib/nextcloud-recovery/repository '
              '/usr/local/lib/nextcloud-recovery/venv/bin/python -m scripts.restore.initial_lease']
        with InitialSSHChannel(argv).open() as channel:
            remote=RemoteDeployLease(channel,self.bound,self.owner)
            with remote.hold():yield remote

    def verify_prepared(self,c,outputs,lease):
        lease.assert_held()
        import time
        deadline=time.monotonic()+120
        while True:
            nodes=self.sdk['ssm'].describe_instance_information(Filters=[{'Key':'InstanceIds','Values':[self.bound.instance]}])
            rows=nodes.get('InstanceInformationList')
            require(isinstance(rows,list) and len(rows)<=1 and not nodes.get('NextToken'),'CREATION_SSM_AMBIGUOUS')
            if rows:
                require(rows[0].get('InstanceId')==self.bound.instance,'CREATION_SSM_AMBIGUOUS')
                if rows[0].get('PingStatus')=='Online':break
            require(time.monotonic()<deadline,'CREATION_SSM_NOT_ONLINE')
            lease.assert_held();time.sleep(5)
        known=(self.directory/'known_hosts').read_text().strip().split()
        require(len(known)==3 and known[1]=='ssh-ed25519','CREATION_HOST_KEY_UNVERIFIED')
        self.sdk['ec2'].create_tags(Resources=[self.bound.instance],Tags=[
            {'Key':'RecoveryPreparedCommit','Value':self.commit},
            {'Key':'RecoveryCreationOwner','Value':self.owner},
            {'Key':'RecoveryHostKey','Value':' '.join(known[1:])}])
        node=self.sdk['ec2'].describe_instances(InstanceIds=[self.bound.instance])['Reservations'][0]['Instances'][0]
        tags={t['Key']:t['Value'] for t in node['Tags']}
        require(tags.get('RecoveryPreparedCommit')==self.commit and tags.get('RecoveryCreationOwner')==self.owner
                and tags.get('RecoveryHostKey')==' '.join(known[1:]),'CREATION_PREPARED_TAGS_UNCONFIRMED')
        # Public binding for the next separately authorized restore; no secrets.
        from scripts.backup.sealing.common import json_bytes
        result={'instance_id':self.bound.instance,'resource_environment':c['destination_environment'],
                'data_bucket':c['data_bucket'],'reviewed_commit':self.commit}
        with open(self.ci.required('GITHUB_STEP_SUMMARY'),'a',encoding='utf-8') as f:
            f.write('Recovery target prepared; administrative restore IAM/host-key binding must be confirmed before restore.\n')
        self.ci.private_file(self.directory/'created-target.json',json_bytes(result).decode())
