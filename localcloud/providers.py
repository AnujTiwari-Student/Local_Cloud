"""Explicit operation adapters and lazy provider loading.

Architecture reference: LocalStack services/plugins.py, aws/skeleton.py and
aws/handlers/service.py at 8b9a79f05846835cf4dff63ab7eefdde9df83783.
This module adapts existing LocalCloud services; it does not bundle LocalStack.
"""
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable
from .engine import Error

SQS_OPERATIONS = frozenset({'CreateQueue', 'ListQueues', 'GetQueueUrl', 'GetQueueAttributes',
                           'SetQueueAttributes', 'DeleteQueue', 'PurgeQueue', 'SendMessage',
                           'ReceiveMessage', 'DeleteMessage', 'ChangeMessageVisibility'})
S3_METHODS = {'ListBuckets': 'GET', 'CreateBucket': 'PUT', 'DeleteBucket': 'DELETE',
              'ListObjects': 'GET', 'PutObject': 'PUT', 'GetObject': 'GET', 'DeleteObject': 'DELETE'}


class OperationProvider:
    """An explicit service-operation dispatch table, independent of HTTP encoding."""
    def __init__(self, service, operations):
        self.service = service
        self.operations = dict(operations)

    def invoke(self, context):
        handler = self.operations.get(context.operation)
        if handler is None:
            raise Error('UnsupportedOperation', f'{self.service}.{context.operation}', 501)
        return handler(context)


def sqs_provider(engine):
    return OperationProvider('sqs', {name: lambda context: engine.sqs(
        context.operation, context.service_request, context.principal) for name in SQS_OPERATIONS})


def ec2_provider(engine):
    from .ec2 import OPERATIONS
    return OperationProvider('ec2', {name: lambda context: engine.ec2.call(
        context.operation, context.service_request, context.principal) for name in OPERATIONS})


def s3_provider(engine):
    def dispatch(context):
        request = context.service_request
        return engine.s3(S3_METHODS[context.operation], request.get('Bucket', ''),
                         request.get('Key', ''), request.get('Body', b''), context.principal)
    return OperationProvider('s3', {name: dispatch for name in S3_METHODS})

def identity_provider(engine, service):
    from .identity import IAM_OPERATIONS, STS_OPERATIONS
    operations = IAM_OPERATIONS if service == 'iam' else STS_OPERATIONS
    return OperationProvider(service, {name: lambda context: engine.tools.call(
        service, context.operation, context.principal, '*', lambda: engine.identity.call(
            service, context.operation, context.service_request, context.principal)) for name in operations})


@dataclass
class ProviderEntry:
    factory: Callable
    provider: OperationProvider | None = None
    state: str = 'available'
    error: Exception | None = None
    lock: RLock = field(default_factory=RLock)


class ProviderRegistry:
    """Load each adapter once under its own lock; do not retry failed loads silently."""
    def __init__(self):
        self._entries = {}
        self._lock = RLock()

    def register(self, service, factory):
        with self._lock:
            if service in self._entries:
                raise ValueError(f'Provider already registered: {service}')
            self._entries[service] = ProviderEntry(factory)

    def require(self, service):
        with self._lock:
            entry = self._entries.get(service)
        if entry is None:
            raise Error('UnsupportedService', service, 501)
        with entry.lock:
            if entry.error is not None:
                raise entry.error
            if entry.provider is None:
                entry.state = 'starting'
                try:
                    provider = entry.factory()
                    if not isinstance(provider, OperationProvider) or provider.service != service:
                        raise TypeError('Provider factory returned the wrong service adapter')
                    entry.provider = provider
                    entry.state = 'running'
                except Exception as error:
                    entry.error, entry.state = error, 'error'
                    raise
            return entry.provider

    def describe(self):
        # Metadata inspection must not trigger provider loading.
        with self._lock:
            return {name: {'state': item.state,
                           'operations': sorted(item.provider.operations) if item.provider else []}
                    for name, item in sorted(self._entries.items())}
