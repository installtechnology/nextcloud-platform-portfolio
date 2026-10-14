"""Manual context checks and an unconditional operational barrier, without I/O."""
import re

WORKFLOW_NAME = 'Backup platform (manual)'
BLOCKERS = ('CONNECTIVITY_UNDECIDED', 'OPERATIONAL_CAPTURE_ADAPTER_MISSING',
            'AWS_TRANSPORT_ADAPTER_MISSING', 'LINUX_POSIX_VALIDATION_PENDING',
            'REAL_CONTAINER_FAILURE_TRIALS_PENDING', 'RECOVERY_KEY_CUSTODY_PENDING',
            'IAM_ACTIVATION_AND_OIDC_VALIDATION_PENDING', 'DEPLOY_INTERRUPTED_CAPTURE_GUARD_MISSING',
            'SEPARATE_ACTIVATION_REQUIRED')
PUBLIC_RESULT_CODES = frozenset({'OPERATIONAL_BACKUP_DISABLED', 'SYNTHETIC_PREFLIGHT_CONFORMANT',
                                'MANUAL_EVENT_REQUIRED', 'CONFIRMATION_REQUIRED', 'BRANCH_CONFIGURATION_INVALID',
                                'BRANCH_MISMATCH', 'WORKFLOW_MISMATCH', 'ENVIRONMENT_INVALID',
                                'CONCURRENCY_CONTEXT_MISSING', 'INVALID_ARGUMENTS', 'INTERNAL_GATE_ERROR'})


class GateFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise GateFailure(code)


def literal(value, pattern):
    return isinstance(value, str) and len(value) <= 128 and re.fullmatch(pattern, value) is not None


def dispatch_context(context):
    """Validate only selected GitHub metadata; never print or return input values."""
    require(context.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'MANUAL_EVENT_REQUIRED')
    require(context.get('BACKUP_CONFIRMATION') == 'BACKUP', 'CONFIRMATION_REQUIRED')
    branch = context.get('DEPLOY_BRANCH')
    require(literal(branch, r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}')
            and re.search(r'(\.\.|//|(^|/)\.|\.lock($|/)|[./]$)', branch) is None,
            'BRANCH_CONFIGURATION_INVALID')
    require(context.get('GITHUB_REF') == 'refs/heads/' + branch, 'BRANCH_MISMATCH')
    require(context.get('GITHUB_WORKFLOW') == WORKFLOW_NAME, 'WORKFLOW_MISMATCH')
    require(literal(context.get('BACKUP_ENVIRONMENT'), r'[a-z][a-z0-9-]{1,63}'), 'ENVIRONMENT_INVALID')
    require(literal(context.get('AWS_ACCOUNT_ID'), r'[0-9]{12}')
            and literal(context.get('PROJECT_NAME'), r'[a-z][a-z0-9-]{1,63}'), 'CONCURRENCY_CONTEXT_MISSING')


def blocked_result(code='OPERATIONAL_BACKUP_DISABLED'):
    if not isinstance(code, str) or code not in PUBLIC_RESULT_CODES:
        code = 'INTERNAL_GATE_ERROR'
    return {'code': code, 'operational_allowed': False, 'backup_complete': False,
            'functional_restore': 'not_verified', 'blockers': list(BLOCKERS)}
