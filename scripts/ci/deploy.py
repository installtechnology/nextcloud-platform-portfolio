#!/usr/bin/env python3
"""Runner orchestration. No infrastructure action is executed by importing this module."""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PREFIX = "nextcloud-deploy"


def required(name, pattern=None):
    value = os.environ.get(name, "")
    if not value or (pattern and not re.fullmatch(pattern, value)):
        raise ValueError(f"Missing/invalid configuration: {name}")
    return value


def run(args, capture=False, cwd=None):
    result = subprocess.run(args, cwd=cwd or ROOT, text=True, capture_output=capture, check=False)
    if result.returncode:
        # Captured outputs may contain console logs or state: never include them in errors.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]} (exit {result.returncode})")
    return result.stdout if capture else None


def aws(*args):
    return json.loads(run(["aws", *args, "--output", "json", "--no-cli-pager"], capture=True))


def private_file(path, content):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def paths():
    directory = Path(required("RUNNER_TEMP")) / "platform-deploy"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def context():
    account = required("AWS_ACCOUNT_ID", r"[0-9]{12}")
    region = required("AWS_REGION", r"[a-z]{2}(?:-[a-z]+)+-[0-9]+")
    project = required("PROJECT_NAME", r"[a-z][a-z0-9-]{1,24}")
    environment = required("DEPLOY_ENVIRONMENT", r"[a-z][a-z0-9-]{1,14}")
    buckets = [required(name, r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]") for name in ("DATA_BUCKET_NAME", "BACKUPS_BUCKET_NAME", "STATE_BUCKET_NAME")]
    if len(set(buckets)) != 3:
        raise ValueError("Data, backups and state require distinct buckets")
    if required("GITHUB_REF") != f"refs/heads/{required('DEPLOY_BRANCH')}":
        raise ValueError("Unauthorized branch")
    if required("DEPLOY_MODE") not in {"plan", "apply"}:
        raise ValueError("Invalid mode")
    deployment_purpose()
    if required("DEPLOY_MODE") == "apply" and os.environ.get("CONFIRMATION") != "APPLY":
        raise ValueError("Apply requires explicit confirmation=APPLY")
    return account, region, project, environment, buckets


def deployment_purpose():
    value = os.environ.get('DEPLOY_PURPOSE', 'new_installation')
    if value not in {'new_installation', 'prepare_restore', 'create_restore_target'}:
        raise ValueError('DEPLOY_PURPOSE_INVALID')
    return value


def restore_preparation():
    """Validate destination binding before any apply; no inferred source/target."""
    from scripts.restore.deployment import validate_preparation
    account, region, project, _, buckets = context()
    from scripts.backup.capture.storage import CaptureFailure
    try:
        return validate_preparation(required('RESTORE_HOST_CONFIGURATION'),
            account, region, project, buckets, required('RECOVERY_INSTANCE_ID'), ROOT)
    except CaptureFailure as error:
        raise ValueError(error.code) from None


def prepare():
    account, region, project, environment, buckets = context()
    if deployment_purpose() == 'create_restore_target':
        from scripts.restore.reprovision_runner import validate_context
        validate_context();return
    if deployment_purpose() == 'prepare_restore':
        restore_preparation()
    # Names are computed from bootstrap's conventions, never arbitrary IAM ARNs.
    role = f"arn:aws:iam::{account}:role/{project}-{environment}-github"
    if required("AWS_ROLE_ARN") != role:
        raise ValueError("Role ARN does not match expected account/project/environment")
    # JSON variables are non-secret inputs. Sizing overrides are optional, so root defaults remain authoritative.
    common = dict(aws_account_id=account, aws_region=region, project_name=project, environment=environment)
    persistent = dict(common, data_bucket_name=buckets[0], backups_bucket_name=buckets[1])
    runtime = dict(common, data_bucket_name=buckets[0], ssh_public_key=required("SSH_PUBLIC_KEY"),
                   ssh_allowed_cidrs=json.loads(required("SSH_ALLOWED_CIDRS")),
                   http_allowed_cidrs=json.loads(required("HTTP_ALLOWED_CIDRS")),
                   ec2_permissions_boundary_arn=f"arn:aws:iam::{account}:policy/{project}-{environment}-ec2-boundary")
    for cidr in runtime["ssh_allowed_cidrs"]:
        net = ipaddress.IPv4Network(cidr, strict=True)
        if net.prefixlen < 24:
            raise ValueError("Administrative SSH CIDR must be /24 or narrower")
    if not runtime["ssh_allowed_cidrs"] or not runtime["http_allowed_cidrs"]:
        raise ValueError("Administrative and HTTP CIDRs must be explicit")
    for cidr in runtime["http_allowed_cidrs"]:
        if ipaddress.IPv4Network(cidr, strict=True).prefixlen == 0:
            raise ValueError("Administrative HTTP/Grafana CIDR cannot allow the entire Internet")
    if os.environ.get("INSTANCE_TYPE"):
        runtime["instance_type"] = required("INSTANCE_TYPE", r"t3\.(nano|micro|small|medium|large|xlarge|2xlarge)")
    if os.environ.get("ROOT_VOLUME_SIZE_GIB"):
        runtime["root_volume_size_gib"] = int(required("ROOT_VOLUME_SIZE_GIB", r"[0-9]+"))
        if not 8 <= runtime["root_volume_size_gib"] <= sizing_default("root_volume_size_gib"):
            raise ValueError("Sizing override exceeds authorized laboratory envelope")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]+)?", runtime["ssh_public_key"]):
        raise ValueError("CI requires an OpenSSH Ed25519 public key")
    directory = paths()
    for root, values in (("persistent", persistent), ("runtime", runtime)):
        private_file(directory / f"{root}.tfvars.json", json.dumps(values))
        backend = dict(bucket=buckets[2], region=region,
                       key=f"states/{account}/{project}/{environment}/{root}/terraform.tfstate",
                       encrypt=True, use_lockfile=True, allowed_account_ids=[account])
        private_file(directory / f"{root}.tfbackend", "\n".join(f"{key} = {json.dumps(value)}" for key, value in backend.items()) + "\n")
        # Only CI's ephemeral checkout is activated. Local roots remain local until explicit migration.
        (ROOT / f"terraform/aws/{root}/backend.tf").write_text((ROOT / f"terraform/aws/{root}/backend.tf.example").read_text())
    print("Inputs validated; apply gate checked. Backend will be remote only.")


def sizing_default(variable):
    text = (ROOT / "terraform/aws/runtime/variables.tf").read_text(encoding="utf-8")
    block = re.search(r'variable "' + re.escape(variable) + r'"\s*\{(.*?)\n\}', text, re.S).group(1)
    raw = re.search(r"default\s*=\s*(\S+)", block).group(1)
    return json.loads(raw)


def preflight():
    context()
    if deployment_purpose() == 'create_restore_target':
        from scripts.restore.reprovision_runner import validate_context
        validate_context()
    if deployment_purpose() == 'prepare_restore':
        restore_preparation()
    passwords = ('NEXTCLOUD_ADMIN_PASSWORD', 'POSTGRES_ADMIN_PASSWORD', 'POSTGRES_PASSWORD', 'REDIS_PASSWORD', 'GRAFANA_ADMIN_PASSWORD')
    for name in passwords if deployment_purpose() == 'new_installation' else ():
        value = required(name, r'[A-Za-z0-9_-]{32,256}')
        if value.startswith('REPLACE'):
            raise ValueError('Placeholder secret is not allowed: ' + name)
    if deployment_purpose() == 'new_installation' and required('POSTGRES_ADMIN_PASSWORD') == required('POSTGRES_PASSWORD'):
        raise ValueError('PostgreSQL administrative and application passwords must differ')
    for name in ('NEXTCLOUD_ADMIN_USER', 'GRAFANA_ADMIN_USER') if deployment_purpose() == 'new_installation' else ():
        required(name, r'[A-Za-z0-9][A-Za-z0-9_.@-]+')
    key = paths() / 'ssh-key'
    try:
        private_file(key, required('SSH_PRIVATE_KEY').replace('\r\n', '\n').rstrip() + '\n')
        actual = run(['ssh-keygen', '-y', '-P', '', '-f', str(key)], capture=True).split()[:2]
        if actual != required('SSH_PUBLIC_KEY').split()[:2]:
            raise ValueError('SSH private/public key mismatch')
    finally:
        key.unlink(missing_ok=True)
    print('Deploy secrets and SSH key pair validated without logging values.')


def terraform(lease=None):
    account, region, project, environment, buckets = context()
    if required('DEPLOY_MODE') == 'apply':
        require_deploy_lease(lease)  # BEFORE any Terraform apply or API operation.
    if aws("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Authenticated AWS account differs from expected account")
    location = aws("s3api", "get-bucket-location", "--bucket", buckets[2], "--expected-bucket-owner", account)["LocationConstraint"] or "us-east-1"
    if location == "EU":
        location = "eu-west-1"
    if location != region:
        raise ValueError("State bucket region differs from expected region")
    if os.environ.get("TF_WORKSPACE", "default") != "default":
        raise ValueError("Only the default workspace is supported; keys isolate each root")
    # get-caller-identity does not attest deployment region: backend + provider enforce the configured region.
    directory = paths()
    print(json.dumps(dict(account=account, region=region, environment=environment,
                         instance_type=os.environ.get("INSTANCE_TYPE") or sizing_default("instance_type"),
                         ebs_gib=int(os.environ.get("ROOT_VOLUME_SIZE_GIB") or sizing_default("root_volume_size_gib")),
                         data_bucket=buckets[0], backups_bucket=buckets[1], state_bucket=buckets[2]), indent=2))
    run(["terraform", "fmt", "-check", "-recursive"])
    plans = []
    for root in ("persistent", "runtime"):
        tf = ["terraform", f"-chdir=terraform/aws/{root}"]
        run(tf + ["init", "-input=false", "-lockfile=readonly", f"-backend-config={directory / (root+'.tfbackend')}"])
        run(tf + ["validate"])
        # Saved plan contains no passwords, but is still treated as sensitive: never artifact/upload.
        plan = directory / f"{root}.tfplan"
        run(tf + ["plan", "-input=false", "-lock-timeout=5m", f"-var-file={directory / (root+'.tfvars.json')}", f"-out={plan}"])
        os.chmod(plan, 0o600)
        if root == 'persistent':
            from scripts.backup.operational.policy_ownership import validate_persistent_plan
            validate_persistent_plan(json.loads(run(tf + ['show', '-json', str(plan)], capture=True)),
                                     bucket_scope=(buckets[0], buckets[1]))
        elif required('DEPLOY_MODE') == 'apply':
            document=json.loads(run(tf + ['show','-json',str(plan)],capture=True))
            from scripts.backup.operational.deploy_plan import validate_runtime_target
            validate_runtime_target(document,lease.target.instance)
        plans.append((tf,plan))
    # Validate BOTH plans before the first apply. A replacement of the guarded
    # EC2 could otherwise strand a valid lease on a different host.
    for tf,plan in plans:
        if required("DEPLOY_MODE") == "apply":
            context()  # Check the explicit gate again immediately before apply.
            require_deploy_lease(lease)
            run(tf + ["apply", "-input=false", "-lock-timeout=5m", str(plan)])
            require_deploy_lease(lease)
        plan.unlink()
    if required("DEPLOY_MODE") == "apply":
        outputs = json.loads(run(["terraform", "-chdir=terraform/aws/runtime", "output", "-json"], capture=True))
        allowed = ("instance_id", "public_ip", "security_group_id", "ssh_user", "data_bucket_name")
        values = {key: outputs[key]["value"] for key in allowed}
        if values['instance_id'] != lease.target.instance or values['data_bucket_name'] != buckets[0]:
            raise RuntimeError('DEPLOY_OUTPUT_TARGET_MISMATCH')
        ipaddress.IPv4Address(values["public_ip"])
        private_file(directory / "runtime.json", json.dumps(values))
        # These contain public identifiers, not credentials or whole Terraform state.
        with open(required("GITHUB_ENV"), "a", encoding="utf-8") as stream:
            stream.write(f"PLATFORM_HOST={values['public_ip']}\nPLATFORM_SSH_USER={values['ssh_user']}\nNEXTCLOUD_S3_BUCKET={values['data_bucket_name']}\nNEXTCLOUD_S3_REGION={region}\n")
        # The guarded controller now invokes Ansible in this same process/step;
        # GITHUB_ENV alone would only populate subsequent GitHub steps.
        os.environ.update(PLATFORM_HOST=values['public_ip'],PLATFORM_SSH_USER=values['ssh_user'],
                          NEXTCLOUD_S3_BUCKET=values['data_bucket_name'],NEXTCLOUD_S3_REGION=region)


def owned_rule(rule, group, repository, project, environment):
    tags = {tag["Key"]: tag["Value"] for tag in rule.get("Tags", [])}
    return (rule.get("GroupId") == group and not rule.get("IsEgress", True)
            and rule.get("IpProtocol") == "tcp" and rule.get("FromPort") == rule.get("ToPort") == 22
            and ipaddress.IPv4Network(rule.get("CidrIpv4", "0.0.0.0/0")).prefixlen == 32
            and tags.get("Workflow") == PREFIX and tags.get("Repository") == repository
            and tags.get("Project") == project and tags.get("Environment") == environment
            and rule.get("Description", "").startswith(f"{PREFIX}:"))


def revoke_owned(group, current_only):
    _, _, project, environment, _ = context()
    repository = required("GITHUB_REPOSITORY")
    run_id = required("GITHUB_RUN_ID", r"[0-9]+")
    attempt = required("GITHUB_RUN_ATTEMPT", r"[0-9]+")
    rules = aws("ec2", "describe-security-group-rules", "--filters", f"Name=group-id,Values={group}")["SecurityGroupRules"]
    for rule in rules:
        if not owned_rule(rule, group, repository, project, environment):
            continue
        tags = {tag["Key"]: tag["Value"] for tag in rule.get("Tags", [])}
        current = tags.get("Run") == f"{run_id}-{attempt}"
        stale = tags.get("CreatedAt", "").isdigit() and time.time() - int(tags["CreatedAt"]) > 3 * 3600
        # Serialized runs + 90 minute timeout; three hours is intentionally beyond any live lease.
        if (current_only and current) or (not current_only and stale):
            aws("ec2", "revoke-security-group-ingress", "--group-id", group, "--security-group-rule-ids", rule["SecurityGroupRuleId"])


def console_fingerprints(console):
    section = re.search(r"BEGIN SSH HOST KEY FINGERPRINTS(.*?)END SSH HOST KEY FINGERPRINTS", console, re.S)
    if not section:
        return set()
    return set(re.findall(r"SHA256:[A-Za-z0-9+/]+(?==|\s|$)", section.group(1)))


def verify_scan(scan, ip, fingerprints):
    verified = []
    for line in scan.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != ip or parts[1] != "ssh-ed25519":
            continue
        blob = base64.b64decode(parts[2], validate=True)
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        if fingerprint in fingerprints:
            verified.append(" ".join(parts[:3]))
    if not verified:
        raise ValueError("SSH key not authenticated by AWS EC2 console fingerprints")
    return "\n".join(verified) + "\n"


def connect(lease=None):
    require_deploy_lease(lease)
    return _connect_verified()


def connect_created_host(lease,target):
    from scripts.restore.creation_lease import CreationLease
    if type(lease) is not CreationLease:raise RuntimeError('CREATION_AUTHORITY_REQUIRED')
    lease.assert_held()
    if lease.record['host']!={'account':target.account,'region':target.region,'instance':target.instance}:
        raise RuntimeError('CREATION_HOST_BINDING_INVALID')
    if lease.resume is not None and 'host_key' in lease.resume:
        from scripts.restore.reprovision import validate_resume_host_key
        proof=validate_resume_host_key(lease.resume['host_key'],target.instance)
        return _connect_verified(host_key=proof['public_key'])
    return _connect_verified()


def _connect_verified(host_key=None):
    account, _, project, environment, _ = context()
    directory = paths()
    values = json.loads((directory / "runtime.json").read_text())
    group = values["security_group_id"]
    instance = values["instance_id"]
    # Bind output to live authenticated API resources and tags before authorizing ingress.
    instance_info = aws("ec2", "describe-instances", "--instance-ids", instance)["Reservations"][0]
    info = instance_info["Instances"][0]
    tags = {item["Key"]: item["Value"] for item in info["Tags"]}
    if instance_info["OwnerId"] != account or tags.get("Project") != project or tags.get("Environment") != environment or info["PublicIpAddress"] != values["public_ip"] or group not in {item["GroupId"] for item in info["SecurityGroups"]}:
        raise ValueError("Runtime output does not match live EC2 identity")
    private_file(directory / "ssh-key", required("SSH_PRIVATE_KEY").replace("\r\n", "\n").rstrip() + "\n")
    actual_public = run(["ssh-keygen", "-y", "-P", "", "-f", str(directory / "ssh-key")], capture=True).split()[:2]
    if actual_public != required("SSH_PUBLIC_KEY").split()[:2]:
        raise ValueError("SSH private/public key mismatch")
    revoke_owned(group, current_only=False)
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
        runner_ip = ipaddress.IPv4Address(response.read().decode().strip())
    if not runner_ip.is_global:
        raise ValueError("Runner IPv4 must be public")
    run_id = required("GITHUB_RUN_ID", r"[0-9]+")
    attempt = required("GITHUB_RUN_ATTEMPT", r"[0-9]+")
    description = f"{PREFIX}:{run_id}:{attempt}"
    tags = {"Project": project, "Environment": environment, "Workflow": PREFIX,
            "Repository": required("GITHUB_REPOSITORY"), "Run": f"{run_id}-{attempt}", "CreatedAt": str(int(time.time()))}
    permissions = [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                    "IpRanges": [{"CidrIp": f"{runner_ip}/32", "Description": description}]}]
    # Rule discovered during cleanup by identity tags too, covering a crash before returned ID is saved.
    existing = aws("ec2", "describe-security-group-rules", "--filters", f"Name=group-id,Values={group}")["SecurityGroupRules"]
    duplicate = any(not rule.get("IsEgress", True) and rule.get("IpProtocol") == "tcp"
                    and rule.get("FromPort") == rule.get("ToPort") == 22
                    and rule.get("CidrIpv4") == f"{runner_ip}/32" for rule in existing)
    if duplicate:
        print("Exact SSH /32 rule already exists; reusing without changing its ownership or revoking it.")
    else:
        result = aws("ec2", "authorize-security-group-ingress", "--group-id", group,
                     "--ip-permissions", json.dumps(permissions), "--tag-specifications",
                     json.dumps([{"ResourceType": "security-group-rule", "Tags": [{"Key": key, "Value": value} for key, value in tags.items()]}]))
        private_file(directory / "ssh-rule.json", json.dumps(result["SecurityGroupRules"]))
    run(["aws", "ec2", "wait", "instance-status-ok", "--instance-ids", instance, "--no-cli-pager"])
    ip = values["public_ip"]
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if host_key is not None:
            # Explicit administrative SSM evidence from this run's immutable grant.
            # The network scan only compares against it; it never supplies trust.
            blob=base64.b64decode(host_key.split()[1],validate=True)
            fingerprints={"SHA256:"+base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")}
        else:
            console = aws("ec2", "get-console-output", "--instance-id", instance, "--latest").get("Output") or ""
            # AWS CLI decodes Output. Support an encoded SDK/fixture response without logging either form.
            if "BEGIN SSH HOST KEY" not in console:
                try:
                    console = base64.b64decode(console, validate=True).decode()
                except (ValueError, UnicodeDecodeError):
                    pass
            fingerprints = console_fingerprints(console)
        scan = subprocess.run(["ssh-keyscan", "-T", "5", "-t", "ed25519", ip], capture_output=True, text=True)
        if fingerprints and scan.returncode == 0:
            # A mismatch is a hard failure, never a fallback to TOFU or disabled host checking.
            private_file(directory / "known_hosts", verify_scan(scan.stdout, ip, fingerprints))
            break
        time.sleep(10)
    else:
        raise RuntimeError("Verified host key unavailable; deploy aborted")
    private_file(directory / "inventory.json", json.dumps(runtime_inventory(values)))
    # SSH identity/known_hosts paths are controlled temporary paths, not input-derived options.
    private_file(directory / "ssh_config", f"Host {ip}\n  HostName {ip}\n  User {values['ssh_user']}\n  IdentityFile {directory / 'ssh-key'}\n  UserKnownHostsFile {directory / 'known_hosts'}\n  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  BatchMode yes\n  ConnectTimeout 10\n")
    with open(required("GITHUB_ENV"), "a", encoding="utf-8") as stream:
        stream.write(f"ANSIBLE_SSH_ARGS=-F {directory / 'ssh_config'}\n")
    authority="administrative SSM observation bound to this run's grant" if host_key is not None else "authenticated AWS console"
    print("Temporary /32 rule ready; Ed25519 host key verified against "+authority+".")


def require_deploy_lease(lease):
    from scripts.backup.operational.remote_lease import RemoteDeployLease
    if type(lease) is not RemoteDeployLease:
        raise RuntimeError('REMOTE_DEPLOY_GUARD_REQUIRED')
    account, region, project, environment, _ = context()
    if lease.target.account != account or lease.target.region != region:
        raise RuntimeError('REMOTE_DEPLOY_TARGET_MISMATCH')
    if lease.target.role_name != f'{project}-{environment}-deploy-guard':
        raise RuntimeError('REMOTE_DEPLOY_ROLE_MISMATCH')
    _,_,_,_,buckets=context()
    if lease.scope != dict(project=project,environment=environment,data_bucket=buckets[0],backups_bucket=buckets[1],state_bucket=buckets[2]):
        raise RuntimeError('REMOTE_DEPLOY_SCOPE_MISMATCH')
    if lease.owner != required('GITHUB_RUN_ID') + ':' + required('GITHUB_RUN_ATTEMPT'):
        raise RuntimeError('REMOTE_DEPLOY_OWNER_MISMATCH')
    lease.assert_held()


def guarded_deploy(lease):
    """Single continuous lease across both applies and Ansible/Compose.

    No config flag, first-install bypass or default lease factory. The authenticated
    SSM bridge and host service must exist before publishing operational activation.
    """
    if required('DEPLOY_MODE') != 'apply': raise RuntimeError('APPLY_CONTEXT_REQUIRED')
    from scripts.backup.operational.remote_lease import RemoteDeployLease
    if type(lease) is not RemoteDeployLease: raise RuntimeError('REMOTE_DEPLOY_GUARD_REQUIRED')
    preparation = restore_preparation() if deployment_purpose() == 'prepare_restore' else None
    if preparation is not None and preparation['instance'] != lease.target.instance:
        raise RuntimeError('RESTORE_PREPARATION_LEASE_MISMATCH')
    with lease.hold():
        require_deploy_lease(lease)
        preflight()
        directory = paths()
        if preparation is not None:
            from scripts.restore.deployment import preparation_variables
            variables = preparation_variables(preparation, ROOT, directory,
                Path(required('RUNNER_TEMP'))/'nextcloud-operational'/'wheels', required('GITHUB_SHA'))
            private_file(directory/'restore-prepare.json', json.dumps(variables))
        terraform(lease)
        connect(lease)
        require_deploy_lease(lease)
        directory = paths()
        os.environ['ANSIBLE_SSH_ARGS'] = '-F ' + str(directory/'ssh_config')
        try:
            args = ['ansible-playbook', '-i', str(directory/'inventory.json'), '--private-key',
                    str(directory/'ssh-key')]
            if preparation is None:
                args.append('playbooks/deploy.yml')
            else:
                values = json.loads((directory/'runtime.json').read_text())
                if values['instance_id'] != preparation['instance'] or values['data_bucket_name'] != preparation['data_bucket']:
                    raise RuntimeError('RESTORE_PREPARATION_RUNTIME_MISMATCH')
                args += ['playbooks/restore-prepare.yml', '--extra-vars', '@'+str(directory/'restore-prepare.json')]
            run(args, cwd=ROOT/'ansible')
            require_deploy_lease(lease)
        finally:
            cleanup()


def cleanup():
    directory = paths()
    failure = None
    try:
        if (directory / "runtime.json").exists():
            group = json.loads((directory / "runtime.json").read_text())["security_group_id"]
            revoke_owned(group, current_only=True)
    except Exception as exc:
        failure = exc
    finally:
        # Native file deletion, confined to this workflow's private directory, even if AWS revocation fails.
        for name in ("ssh-key", "known_hosts", "ssh_config", "inventory.json", "runtime.json", "ssh-rule.json", "persistent.tfplan", "runtime.tfplan", "persistent.tfvars.json", "runtime.tfvars.json", "persistent.tfbackend", "runtime.tfbackend"):
            (directory / name).unlink(missing_ok=True)
    if failure:
        raise RuntimeError("SSH rule cleanup failed; operator must revoke this run's tagged rule") from failure
    print("Cleanup finished; temporary files removed.")


def runtime_inventory(values):
    ip = str(ipaddress.IPv4Address(values["public_ip"]))
    return {"all": {"children": {"platform": {"hosts": {"application_host": {
        "ansible_host": ip, "ansible_user": values["ssh_user"], "ansible_python_interpreter": "/usr/bin/python3"
    }}}}}}


def service_urls(public_ip):
    ip = str(ipaddress.IPv4Address(public_ip))
    return {"Nextcloud": f"http://{ip}", "Grafana": f"http://{ip}:3000"}


def summary():
    if deployment_purpose() in ('prepare_restore','create_restore_target'):
        message = 'Restore target preparation completed. Application is not installed or started; restore is still required.\n'
        print(message)
        with open(required('GITHUB_STEP_SUMMARY'), 'a', encoding='utf-8') as stream:
            stream.write(message)
        return
    urls = service_urls(required("PLATFORM_HOST"))
    message = "\n\n".join(f"{service}: {url}" for service, url in urls.items()) + "\n\n"
    message += "Browser access is restricted to HTTP_ALLOWED_CIDRS. Host probes ran inside EC2; external access must be tested from an authorized source.\n"
    print(message)
    with open(required("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as stream:
        stream.write(message)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "preflight", "terraform", "connect", "cleanup", "summary"))
    args = parser.parse_args()
    try:
        globals()[args.operation]()
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        # Never dump subprocess output, environment or secret-containing objects.
        raise SystemExit(str(error))
