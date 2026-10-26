"""Source-guided gateway structure, retaining LocalCloud's current API contracts.

Reference: LocalStack aws/app.py and aws/api/core.py, commit
8b9a79f05846835cf4dff63ab7eefdde9df83783. This is a structural migration, not
LocalStack's Rolo/Botocore runtime or an exact behavioral compatibility claim.
"""
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4
from .engine import Error
from .providers import ProviderRegistry, ec2_provider, s3_provider, sqs_provider, identity_provider

UPSTREAM_COMMIT = '8b9a79f05846835cf4dff63ab7eefdde9df83783'


@dataclass
class RequestContext:
    service: str
    operation: str
    service_request: dict
    principal: str = 'test'
    protocol: str = 'local-json'
    account_id: str = '000000000000'
    region: str = 'us-east-1'
    request_id: str = field(default_factory=lambda: str(uuid4()))
    service_response: Any = None
    service_exception: Exception | None = None
    provider: Any = None
    stages: list = field(default_factory=list)


class Gateway:
    def __init__(self, engine):
        self.engine = engine
        self.providers = ProviderRegistry()
        for name, factory in [('ec2', ec2_provider), ('s3', s3_provider), ('sqs', sqs_provider)]:
            self.providers.register(name, lambda factory=factory: factory(engine))
        for service in ('iam', 'sts'):
            self.providers.register(service, lambda service=service: identity_provider(engine, service))
        self.request_handlers = [self.validate_context, self.load_provider, self.dispatch_operation]
        self.response_handlers = []
        self.exception_handlers = []
        self.finalizers = []

    @staticmethod
    def validate_context(context):
        if not isinstance(context.service, str) or not context.service or not isinstance(context.operation, str) or not context.operation or not isinstance(context.service_request, dict):
            raise Error('InvalidRequest', 'Expected a service, operation and request object')
        if context.region != 'us-east-1' or context.account_id != '000000000000':
            raise Error('UnsupportedOperation', 'Account/region isolation has not been implemented', 501)

    def load_provider(self, context):
        context.provider = self.providers.require(context.service)

    @staticmethod
    def dispatch_operation(context):
        context.service_response = context.provider.invoke(context)

    def handle(self, context):
        previous = getattr(self.engine.request_context, 'gateway_context', None)
        self.engine.request_context.gateway_context = context
        try:
            try:
                for handler in tuple(self.request_handlers):
                    context.stages.append(handler.__name__)
                    handler(context)
                for handler in tuple(self.response_handlers):
                    handler(context)
                return context.service_response
            except Exception as error:
                context.service_exception = error
                for handler in tuple(self.exception_handlers):
                    handler(context, error)
                raise
            finally:
                for finalizer in tuple(self.finalizers):
                    finalizer(context)
        finally:
            self.engine.request_context.gateway_context = previous

    def invoke(self, service, operation, request, principal='test', protocol='local-json', request_id=None):
        context = RequestContext(service, operation, request, principal=principal, protocol=protocol)
        if request_id:
            context.request_id = request_id
        return self.handle(context)

    def describe(self):
        return {'architecture': 'gateway-provider-registry',
                'reference': {'repository': 'https://github.com/localstack/localstack',
                              'commit': UPSTREAM_COMMIT, 'scope': 'public source architecture',
                              'exactParity': False, 'upstreamRuntimeExecuted': False},
                'services': self.providers.describe(),
                'requestHandlers': [handler.__name__ for handler in self.request_handlers],
                'responseHandlers': [handler.__name__ for handler in self.response_handlers],
                'exceptionHandlers': [handler.__name__ for handler in self.exception_handlers],
                'finalizers': [handler.__name__ for handler in self.finalizers],
                'region': 'us-east-1', 'accountId': '000000000000'}
