"""Explicit injected regional SDK client. Read only; no credential discovery."""
from scripts.backup.operational.s3 import DataInventory
from scripts.backup.sealing.common import require,version


class BackupReader(DataInventory):
    actions=DataInventory.actions | frozenset({'get_object'})

    def list_versions(self,bucket,prefix='',cursor=None):
        require(prefix=='backups/' or prefix.startswith('backups/'),'RESTORE_PREFIX_REJECTED')
        return super().list_versions(bucket,prefix,cursor)

    def get(self,bucket,key,vid):
        self.scope(bucket,key)
        require(key.startswith('backups/') and len(key.split('/'))==3
                and key.rsplit('/',1)[1] in {'completion.json','manifest.json','object_inventory.json',
                    'database.dump','configuration.tar','local_files.tar','recovery_parameters.json'},
                'RESTORE_KEY_REJECTED')
        return self.call('get_object',Bucket=bucket,Key=key,VersionId=version(vid))


class SourceObjects(DataInventory):
    actions=DataInventory.actions | frozenset({'get_object'})

    def get(self,bucket,key,vid):
        self.scope(bucket,key)
        return self.call('get_object',Bucket=bucket,Key=key,VersionId=version(vid))


class DestinationObjects(SourceObjects):
    actions=SourceObjects.actions | frozenset({'copy_object','get_bucket_versioning','get_bucket_encryption',
        'get_public_access_block','get_bucket_ownership_controls','get_bucket_policy'})

    def __init__(self,client,bucket,account,region,source_bucket,forbidden_buckets):
        require(bucket not in {source_bucket,*forbidden_buckets},'RESTORE_ORIGIN_TARGET_REJECTED')
        super().__init__(client,bucket,account,region)
        self.source_bucket=source_bucket

    def assert_protections(self):
        import json
        versioning=self.call('get_bucket_versioning',Bucket=self.bucket)
        encryption=self.call('get_bucket_encryption',Bucket=self.bucket)
        public=self.call('get_public_access_block',Bucket=self.bucket)
        ownership=self.call('get_bucket_ownership_controls',Bucket=self.bucket)
        require(versioning.get('Status')=='Enabled','RESTORE_TARGET_VERSIONING_REQUIRED')
        rules=encryption.get('ServerSideEncryptionConfiguration',{}).get('Rules',[])
        require(len(rules)==1 and rules[0].get('ApplyServerSideEncryptionByDefault',{}).get('SSEAlgorithm')=='AES256',
                'RESTORE_TARGET_SSE_S3_REQUIRED')
        require(public.get('PublicAccessBlockConfiguration')==dict(BlockPublicAcls=True,IgnorePublicAcls=True,
                BlockPublicPolicy=True,RestrictPublicBuckets=True),'RESTORE_TARGET_PUBLIC_ACCESS_REJECTED')
        require(ownership.get('OwnershipControls',{}).get('Rules')==[{'ObjectOwnership':'BucketOwnerPreferred'}],
                'RESTORE_TARGET_ACL_COMPATIBILITY_REQUIRED')
        from scripts.backup.json_input import parse_json
        from scripts.backup.limits import Limits
        raw=self.call('get_bucket_policy',Bucket=self.bucket).get('Policy')
        require(isinstance(raw,str),'RESTORE_TARGET_TLS_POLICY_REQUIRED')
        policy=parse_json(raw.encode(),Limits(json_bytes=32768))
        arn='arn:aws:s3:::'+self.bucket
        statements=policy.get('Statement',[]) if isinstance(policy,dict) else []
        if isinstance(statements,dict):statements=[statements]
        require(isinstance(statements,list) and all(isinstance(s,dict) for s in statements),
                'RESTORE_TARGET_TLS_POLICY_REQUIRED')
        require(any(s.get('Effect')=='Deny' and s.get('Principal')=='*'
                    and s.get('Action') in ('s3:*',['s3:*'])
                    and set(s.get('Resource',[]) if isinstance(s.get('Resource'),list) else [s.get('Resource')])=={arn,arn+'/*'}
                    and s.get('Condition')=={'Bool':{'aws:SecureTransport':'false'}} for s in statements),
                'RESTORE_TARGET_TLS_POLICY_REQUIRED')

    def copy_version(self,source_bucket,key,vid):
        require(source_bucket==self.source_bucket,'RESTORE_COPY_SOURCE_REJECTED')
        self.scope(self.bucket,key)
        # Keep private ACL required by Nextcloud. Do not copy source tags: that
        # would require additional GetObjectTagging permissions unnecessarily.
        return self.call('copy_object',Bucket=self.bucket,Key=key,
            CopySource={'Bucket':source_bucket,'Key':key,'VersionId':version(vid)},
            ExpectedSourceBucketOwner=self.account,ServerSideEncryption='AES256',ACL='private',
            MetadataDirective='COPY',TaggingDirective='REPLACE',Tagging='')
