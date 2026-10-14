"""Administrative broker scoped to the shared host lock and registered owner.

Trusted broker is sole ordinary IAM issuer. This does not prove absence of hostile
root, emergency-role use or concurrent IAM administration; those are explicit
administrative exclusions. Do not instantiate outside Coordinator.window().
"""
from contextlib import contextmanager
from .fencing import PolicyAuthority
from .fence_policy import policy_document, require
from .policy_ownership import assert_policy_owner_clear, guard_statement


class RegisteredAuthority(PolicyAuthority):
    def __init__(self, store, api):
        self.store, self.api, self.held = store, api, None
    def verify(self, scope):
        record = assert_policy_owner_clear(self.store)
        require(record.get('scope') == scope.__dict__, 'POLICY_OWNER_SCOPE_MISMATCH')
        identity = self.api.identity(scope)
        require(identity['account'] == scope.account and identity['role_arn'] == scope.administrative_role,
                'FENCE_ADMIN_IDENTITY_MISMATCH')
        document = policy_document(self.api.read_policy(scope))
        require([s for s in document['Statement'] if s.get('Sid') == 'ExclusiveAdministrativeDataPolicyOwner']
                == [guard_statement(scope)], 'POLICY_OWNER_GUARD_MISSING')
    @contextmanager
    def hold(self, scope, operation):
        require(self.held is None, 'POLICY_AUTHORITY_BUSY')
        self.verify(scope)
        self.held = (scope, operation)
        try: yield
        finally: self.held = None
    def assert_held(self, scope, operation):
        require(self.held == (scope, operation), 'POLICY_AUTHORITY_LOST')
        self.verify(scope)
