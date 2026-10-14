"""GitHub ephemeral OIDC credentials, memory-only. No configure-credentials files."""
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit,parse_qsl,urlencode,urlunsplit
from .ssm import Target,need
from .sdk import dependencies,TemporaryCredentials,clients
from ..capture.storage import CaptureFailure


def oidc_token():
    try:
        url=urlsplit(os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'])
        need(url.scheme=='https' and url.hostname.endswith('.actions.githubusercontent.com')
             and url.port in (None,443) and not url.username and not url.password and not url.fragment,
             'GITHUB_OIDC_ENDPOINT_REJECTED')
        values=[(k,v) for k,v in parse_qsl(url.query) if k!='audience']+[('audience','sts.amazonaws.com')]
        address=urlunsplit((url.scheme,url.netloc,url.path,urlencode(values),''))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request=urllib.request.Request(address,headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
        with opener.open(request,timeout=15) as response:raw=response.read(32769)
        need(len(raw)<=32768,'GITHUB_OIDC_RESPONSE_REJECTED')
        from ..json_input import parse_json
        from ..limits import Limits
        token=parse_json(raw,Limits(json_bytes=32768))['value'];need(isinstance(token,str) and 16<len(token)<=16384,'GITHUB_OIDC_RESPONSE_REJECTED')
        return token
    except CaptureFailure:raise
    except Exception:raise CaptureFailure('GITHUB_OIDC_FAILED') from None


def record(response):
    value=response['Credentials']
    result={'access_key':value['AccessKeyId'],'secret_key':value['SecretAccessKey'],'token':value['SessionToken'],
            'expiration':value['Expiration'].isoformat()}
    TemporaryCredentials(result['access_key'],result['secret_key'],result['token'])
    return result


def assume_oidc(role,target,session_name):
    need(type(target) is Target and role==f'arn:aws:iam::{target.account}:role/{target.role_name}'
         and re.fullmatch(r'[A-Za-z0-9+=,.@_-]{2,64}',session_name),'OIDC_ROLE_SCOPE_REJECTED')
    dependencies()
    from botocore import UNSIGNED
    from botocore.config import Config
    import boto3
    # OIDC exchange is unsigned. Explicit dummy values suppress credential
    # discovery even if the process environment has unrelated AWS credentials.
    sts=boto3.Session(aws_access_key_id='unused',aws_secret_access_key='unused',region_name=target.region).client('sts',
        endpoint_url=f'https://sts.{target.region}.amazonaws.com',verify=True,
        config=Config(signature_version=UNSIGNED,proxies={},connect_timeout=10,read_timeout=30,retries={'total_max_attempts':1}))
    try:
        return record(sts.assume_role_with_web_identity(RoleArn=role,RoleSessionName=session_name,
            WebIdentityToken=oidc_token(),DurationSeconds=7200 if target.role_name.endswith(('-deploy-guard','-reprovision')) else 3600))
    except BaseException:raise CaptureFailure('OIDC_ROLE_EXCHANGE_FAILED') from None
