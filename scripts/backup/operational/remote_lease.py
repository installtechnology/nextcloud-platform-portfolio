"""Continuous authenticated host lease protocol, no factory/SSH fallback.

Host implementation must retain the shared flock and durable journal across
requests. Failure to attest ownership blocks mutation, never releases by TTL.
"""
from contextlib import contextmanager
import secrets
from .fence_policy import require
from .ssm import Target


class AuthenticatedHostChannel:
    def request(self, request):
        from ..capture.storage import CaptureFailure
        raise CaptureFailure('AUTHENTICATED_HOST_CHANNEL_UNAVAILABLE')


class RemoteDeployLease:
    def __init__(self, channel, target, owner, scope=None, credentials=None):
        import re
        require(isinstance(channel, AuthenticatedHostChannel) and type(target) is Target
                and isinstance(owner, str) and re.fullmatch(r'[0-9]{1,20}:[0-9]{1,4}', owner), 'REMOTE_LEASE_CONFIGURATION_INVALID')
        self.channel, self.target, self.owner = channel, target, owner
        self.nonce, self.held = secrets.token_hex(32), False
        self.scope, self.credentials = scope, credentials
    def exchange(self, command):
        from ..capture.storage import CaptureFailure
        request = {'version': 1, 'command': command, 'owner': self.owner, 'nonce': self.nonce,
                   'account': self.target.account, 'region': self.target.region, 'instance': self.target.instance}
        if self.scope is not None:
            request.update(version=2, scope=self.scope)
        expected = {**request, 'phase': 'released' if command == 'release' else 'held'}
        if command == 'acquire' and self.scope is not None:
            request['credentials'] = self.credentials
        try: response = self.channel.request(request)
        except BaseException: raise CaptureFailure('REMOTE_LEASE_OUTCOME_UNKNOWN') from None
        require(response == expected, 'REMOTE_LEASE_ATTESTATION_INVALID')
    def assert_held(self):
        require(self.held, 'REMOTE_LEASE_NOT_HELD')
        self.exchange('assert')
    @contextmanager
    def hold(self):
        require(not self.held, 'REMOTE_LEASE_ALREADY_HELD')
        self.exchange('acquire'); self.held = True
        try:
            yield self
            self.assert_held()
            self.exchange('release')
        finally:
            # Interrupted channel/operation never sends release during unwind.
            self.held = False
