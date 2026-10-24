# Service implementation roadmap

For detailed contributor contracts, all 51 service build cards and a worked
implementation example, read [handbook chapters 26–33](HANDBOOK.md#26-contributor-guide-to-the-future-project).

This roadmap covers all **51 service tiles** visible in the supplied LocalStack
Resource Browser screenshot. It describes the proposed LocalCloud architecture,
not verified LocalStack implementation details, paid-plan entitlements or a claim
that these services already work. Current implementation status and evidence stay
in [FEATURES.md](FEATURES.md) and the individual service guides.

Each service below separates three kinds of work:

- **Core functionality:** the smallest useful, executable service contract. A
  dashboard tile, lifecycle record or fixed success response alone is insufficient.
- **Independent component:** a provider, runtime or shared subsystem that can be
  developed and tested separately, then integrated. This does not require a
  separate process or microservice, and it does not remove its dependencies.
- **Later functionality within the service:** extensions that belong to that
  service's provider and can follow its core release. These are not proposed as
  separate services.

“Later” assumes the application's immediate requirements do not need the feature.
For example, IAM credentials are a prerequisite for testing instance roles even
if the credential issuer is implemented as a separate STS provider.

## Shared core engine — required across services

These foundations belong to the platform and must not be treated as optional
advanced features or duplicated independently inside every provider:

| Foundation                 | Required behavior                                                                                                                                   |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Request routing            | Service/operation dispatch; supported AWS protocol decoding and encoding; stable request IDs.                                                       |
| Account and region context | Resources, names and permissions scoped to the selected account/region; explicit rejection where isolation is not supported.                        |
| Identity and authorization | Separate management authentication from emulated workload credentials; policy evaluation at supported service operations; explicit denial behavior. |
| State integrity            | Durable resource state, schema validation, atomic updates, dependency checks and safe restart/recovery.                                             |
| Runtime ownership          | Only manage resources created by the selected instance; image allowlists, bounded execution and cleanup after failed launches.                      |
| API contracts              | Explicit supported operations/parameters, validation, pagination where required, AWS-style errors and documented differences.                       |
| Observability              | Operation results, logs, request traces and failure diagnostics without exposing secrets.                                                           |
| Integration                | SDK/CLI access and resource-browser operations backed by the same provider behavior.                                                                |
| Verification               | Positive and negative contract tests, persistence tests and real execution tests for runtime-backed functionality.                                  |

Providers can be developed in isolation, but they need these foundations before
being described as integrated, reliable services.

## Application integration — 9 services

| Screenshot service       | Core functionality to implement                                                                                                                           | Independent component or shared dependency                                          | Later functionality within this service                                                                                             |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| API Gateway              | API/resource/route lifecycle; stages; invoke URLs; route an actual HTTP request to a supported HTTP or Lambda integration; request validation and errors. | HTTP routing/proxy runtime; Lambda provider; IAM or Cognito authorization adapters. | Usage plans, API keys, caching, custom-domain mappings, advanced transformations and WebSocket API coverage.                        |
| SQS                      | Queue lifecycle; send/receive/delete messages; visibility and receipt handles; attributes; durable queued messages.                                       | Shared queue storage and delivery scheduler.                                        | FIFO ordering/deduplication, long polling, batching, delay semantics and redrive/DLQ workflows beyond the initial contract.         |
| SNS                      | Topic/subscription lifecycle; publish; actual delivery to supported local SQS and HTTP subscribers; delivery failures.                                    | Delivery worker; SQS adapter; local notification sink.                              | FIFO topics, richer subscription filters, additional delivery protocols, retries and DLQ policy coverage.                           |
| Step Functions           | State-machine lifecycle; execute a bounded state-language subset; execution status/history; invoke supported task integrations.                           | Workflow execution runtime; Lambda and service-integration adapters.                | Parallel/Map states, callbacks and task tokens, Express behavior, version/alias coverage and broader retry/catch semantics.         |
| EventBridge              | Event buses, rules and targets; put events; match a documented event-pattern subset; deliver to supported targets.                                        | Event dispatcher, scheduler and target adapters.                                    | Archives/replay, additional matching operators, cross-account buses, schema-registry APIs and broader target input transformations. |
| Application Auto Scaling | Scalable-target registration; policies; scheduled actions; drive an implemented target's capacity using local metrics or schedules.                       | Shared scaling controller, scheduler and workload-provider adapters.                | Additional policy types, predictive behavior, metric math and broader service-target coverage.                                      |
| MWAA                     | Environment lifecycle tied to a supported local Airflow runtime; load a DAG; execute it; inspect status and logs.                                         | Airflow runtime/executor; S3 DAG/artifact adapter; container execution.             | Additional Airflow versions, plugins/dependency installation, worker autoscaling and broader networking options.                    |
| MQ                       | Broker lifecycle backed by one supported broker engine; credentials; usable connection endpoint; real publish/consume and persistence.                    | Broker runtime adapter, initially for one chosen engine.                            | Additional broker engines/protocols, configurations, multi-node deployments and failover behavior.                                  |
| EventBridge Pipes        | Pipe lifecycle; connect one supported source to one supported target; actually transfer records; status and failure reporting.                            | Source pollers/checkpoint storage, event dispatcher and target adapters.            | Additional sources/targets, enrichment, advanced filtering/transformation, batching and broader retry/DLQ behavior.                 |

## Compute — 5 services

| Screenshot service | Core functionality to implement                                                                                                                                                       | Independent component or shared dependency                                                        | Later functionality within this service                                                                                                                                               |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| EC2                | Instance/image lifecycle; launch configuration; names/tags; usable local execution and endpoints; start/stop/reboot/terminate; user data and logs; durable configuration and cleanup. | Compute executor; EBS storage; VPC networking; IAM/STS credentials; optional Kubernetes executor. | More API fields and error fidelity, additional instance-type descriptions, attribute coverage, richer image metadata, additional launch-template options and broader waiter behavior. |
| Lambda             | Function/code/configuration lifecycle; real invocation; response/errors; logs; supported runtime with bounded execution.                                                              | Function executor/runtime-image manager; log sink; IAM/STS adapter.                               | More runtimes, versions/aliases, layers, concurrency controls, hot reload/debugging and additional event-source mappings.                                                             |
| ECS                | Cluster/task-definition lifecycle; run/stop/describe tasks; execute allowlisted containers; status/logs and usable endpoints.                                                         | Container executor, ECR image adapter, network adapter and IAM/STS credential adapter.            | Service scheduling, deployments, capacity providers, autoscaling, richer placement and broader load-balancer integration.                                                             |
| ECR                | Repository lifecycle; registry authentication; real image push/pull; image manifests/tags; inspect/delete images.                                                                     | Local OCI registry and credential adapter; shared image storage.                                  | Lifecycle policies, immutable-tag options, replication, scanning results and additional registry-policy coverage.                                                                     |
| EKS                | Cluster lifecycle connected to an actual local Kubernetes cluster; API endpoint and credentials; kubeconfig access; deploy and reach a workload.                                      | Cluster provisioner adapter such as kind; Kubernetes API client; node/workload executor.          | Managed node groups, add-ons, access-entry coverage, upgrade simulation and additional cluster configuration APIs.                                                                    |

An EKS provider, a generic Kubernetes executor and the LocalCloud Kubernetes
operator are different components. An operator managing LocalCloud deployments
does not by itself implement EKS. EC2 execution now uses the separate
[EC2 Kubernetes adapter](EC2_KUBERNETES.md), with readiness, lifecycle and PVC mounts.

## Management and governance — 6 services

| Screenshot service           | Core functionality to implement                                                                                                                 | Independent component or shared dependency                                            | Later functionality within this service                                                                                     |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| CloudFormation               | Stack create/update/delete; resource dependency order; invoke supported providers; outputs; events and rollback for the supported resource set. | Template parser, dependency planner and resource-provider adapters.                   | Change sets, nested stacks, drift detection, stack sets, more intrinsic functions and additional resource types.            |
| CloudWatch Logs              | Log-group/stream lifecycle; ingest events; retrieve/filter events; ordering and pagination; runtime log ingestion.                              | Shared durable log storage and query adapter.                                         | Logs Insights coverage, subscription filters, metric filters, retention automation and additional export workflows.         |
| CloudWatch                   | Publish/read metrics; namespaces/dimensions; basic aggregates; alarms that evaluate data and update state.                                      | Time-series store, alarm evaluator and notification dispatcher.                       | Metric math, composite alarms, dashboards, anomaly-detection simulation and additional aggregation behavior.                |
| Simple Systems Manager (SSM) | Parameter Store CRUD; hierarchy/path queries; versions; secure-string integration with local KMS where supported.                               | Secret/value storage, KMS adapter and a separate command-execution adapter if needed. | Parameter policies, labels and tiers; Run Command, sessions and automation documents as separately scoped SSM capabilities. |
| CloudTrail                   | Trail lifecycle; record supported emulated API events with caller/resource context; lookup and deliver events to a supported local destination. | Shared request-event recorder; S3 and log delivery adapters.                          | Event selectors, additional data events, event-data-store/query coverage and digest/integrity-validation workflows.         |
| Account                      | Account identifiers/metadata; supported contact and region-setting APIs; integrate settings with real emulator account boundaries.              | Account-context registry and tenant-isolation layer.                                  | Additional contacts/settings, region opt-in simulation and organization-related integrations.                               |

## Business applications — 1 service

| Screenshot service | Core functionality to implement                                                                                   | Independent component or shared dependency               | Later functionality within this service                                                                            |
| ------------------ | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| SES                | Identity management; send APIs; validate messages; store readable messages and delivery results in a local inbox. | Local email capture/inbox runtime; notification adapter. | Templates, configuration sets, attachment/MIME breadth, event destinations and additional quota/bounce simulation. |

## Developer tools — 2 services

| Screenshot service | Core functionality to implement                                                                                                                      | Independent component or shared dependency                | Later functionality within this service                                                                   |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| AppConfig          | Applications, environments and configuration profiles; versioned configuration; deployment; real client retrieval using supported session/poll APIs. | Configuration store and deployment scheduler.             | Additional validators, deployment strategies, extensions and monitoring-driven rollback.                  |
| CodeCommit         | Repository lifecycle backed by local Git; branch/commit/file operations; usable clone/push/pull through a supported transport.                       | Git storage/transport runtime and authentication adapter. | Pull requests, approvals, comments, merge strategies, triggers and additional repository-policy behavior. |

## Front-end web and mobile — 2 services

| Screenshot service | Core functionality to implement                                                                                                                | Independent component or shared dependency                                       | Later functionality within this service                                                                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| AppSync            | GraphQL API/schema lifecycle; real query/mutation execution; supported data sources and resolvers; authentication integration.                 | GraphQL execution runtime; DynamoDB/Lambda/HTTP adapters; identity adapter.      | Subscriptions, caching, pipeline-resolver breadth, version/conflict handling and additional authorization modes.         |
| Amplify            | Application/branch lifecycle; execute an explicit local build; publish an artifact; serve it through a local endpoint; deployment status/logs. | Build runner, artifact store, static-hosting runtime and optional CI/CD adapter. | Pull-request previews, additional framework adapters, custom-domain mappings and broader rewrite/redirect configuration. |

## Migration and transfer — 1 service

| Screenshot service               | Core functionality to implement                                                                                                               | Independent component or shared dependency        | Later functionality within this service                                                                     |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Database Migration Service (DMS) | Endpoint and task lifecycle; connect to a supported source/target pair; perform an actual bounded full-load copy; report progress and errors. | Database connector/copy engine and task executor. | Change-data capture, resumability, additional engine pairs, transformations and schema-conversion coverage. |

## Security, identity and compliance — 5 services

| Screenshot service        | Core functionality to implement                                                                                                                                                 | Independent component or shared dependency                              | Later functionality within this service                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Cognito                   | User-pool/client/user lifecycle; signup/confirmation; password authentication; token issue/refresh; JWT validation and local signing-key discovery.                             | Identity/token issuer; key store; optional external-identity adapter.   | Hosted login UI, MFA, federation, custom authentication challenges, additional triggers and identity-pool coverage.             |
| Secrets Manager           | Secret CRUD; version storage; stages; retrieve values; prevent secret leakage; local KMS integration where promised.                                                            | Secret store, key-management adapter and rotation worker.               | Scheduled rotation, additional rotation integrations, replication and broader recovery-window behavior.                         |
| KMS                       | Key/alias lifecycle; usable local encrypt/decrypt and data-key operations; key state and access checks; persist key material privately.                                         | Cryptographic backend and protected key store.                          | Additional key types, signing/MAC APIs, rotation/import behavior, grants and more complex policy coverage.                      |
| IAM                       | Users/roles/policies and instance-profile lifecycle; supported trust/identity-policy evaluation; connect credentials to principals and service permissions.                     | Identity/policy evaluator; separate STS issuer; shared account context. | Additional condition keys, policy-language breadth, permission boundaries, policy simulation and richer federation integration. |
| Certificate Manager (ACM) | Certificate import/request lifecycle; store certificate/key material privately; describe/list/delete; integrate usable development certificates with supported local endpoints. | Local certificate authority and TLS endpoint adapter.                   | Renewal scheduling, validation-method breadth, additional key algorithms and richer certificate metadata.                       |

## Storage — 2 services

| Screenshot service | Core functionality to implement                                                                                                    | Independent component or shared dependency                                            | Later functionality within this service                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| S3                 | Bucket/object lifecycle; real binary upload/download; list/head/delete; metadata; durable contents and documented access checks.   | Object/blob store and streaming/multipart storage helpers.                            | Versioning, multipart breadth, lifecycle, notifications, advanced policies, replication and object-lock behavior. |
| Backup             | Vault/plan/job lifecycle; create an actual snapshot for supported resources; discover recovery points; restore and report results. | Shared backup coordinator, scheduler and resource-specific snapshot/restore adapters. | Additional resource coverage, retention/lifecycle rules, copy jobs and more complete selection/restore metadata.  |

## Machine learning — 2 services

| Screenshot service | Core functionality to implement                                                                                                         | Independent component or shared dependency                                     | Later functionality within this service                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| SageMaker          | Model/endpoint/job lifecycle; run one supported local inference or training container; real results/artifacts; status/logs.             | Model-serving/training executor, artifact store and allowlisted image runtime. | More frameworks, tuning jobs, pipelines, endpoint variants, additional training modes and autoscaling integration.        |
| Transcribe Service | Start/get/list/delete jobs; read supplied local audio; execute a chosen speech engine; write transcript artifacts; failures and status. | Speech-to-text runtime and artifact adapter.                                   | More languages/models, streaming transcription, diarization, vocabulary customization and richer timestamps/alternatives. |

## Cloud financial management — 1 service

| Screenshot service | Core functionality to implement                                                                                        | Independent component or shared dependency                 | Later functionality within this service                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Cost Explorer      | Supported query/filter/grouping APIs over explicitly labelled fixture data or a documented local usage-estimate model. | Usage-event collector and optional cost-estimation engine. | Forecasts, anomaly simulation, additional dimensions and report/export coverage. |

Cost Explorer results are local test data or estimates, not real AWS charges.

## Databases — 6 services

| Screenshot service | Core functionality to implement                                                                                                                                         | Independent component or shared dependency                      | Later functionality within this service                                                                                      |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| DynamoDB           | Table/key-schema lifecycle; item CRUD; conditional writes; supported query/scan expressions; pagination and durable typed values.                                       | Key/value storage and expression evaluator.                     | Transactions, secondary indexes, streams, TTL, backups and additional capacity/error simulation.                             |
| RDS                | Instance/database lifecycle backed by one real local SQL engine; credentials; reachable endpoint; actual SQL reads/writes; persistent data and cleanup.                 | SQL engine runtime, volume storage and database backup adapter. | Additional engines/versions, clusters, replicas, parameter groups, failover simulation and more snapshot/restore operations. |
| ElastiCache        | Cache/replication-group lifecycle tied to one real cache engine; usable endpoint; real commands and persistence semantics documented.                                   | Cache runtime adapter.                                          | Additional engines/topologies, shards, parameter groups, replicas and failover simulation.                                   |
| Timestream         | Database/table lifecycle; write timestamped records; supported query subset with actual results; retention metadata and persistence.                                    | Time-series query/storage engine.                               | Broader query functions, scheduled queries, magnetic-store behavior and additional retention/tiering semantics.              |
| DocumentDB         | Cluster/instance lifecycle backed by a chosen document-query engine; usable endpoint; real document operations and persistence within an explicit compatibility subset. | Document-database runtime adapter.                              | Additional wire/query compatibility, replica topology, failover and snapshot APIs.                                           |
| Neptune            | Cluster/instance lifecycle; usable local graph endpoint; real graph writes/queries through one selected query language; durable graph storage.                          | Graph database/query runtime adapter.                           | Additional graph languages, bulk loading, replicas, failover and broader query/API compatibility.                            |

## Analytics and remaining screenshot entries — 9 services

The screenshot placed networking and search entries in this column too; they are
included here to preserve complete coverage rather than reinterpret the catalog.

| Screenshot service | Core functionality to implement                                                                                                                 | Independent component or shared dependency                                    | Later functionality within this service                                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Athena             | Workgroup/query lifecycle; execute a documented SQL subset against supported local data; query state/results; output artifacts.                 | SQL query runtime, Glue catalog adapter and S3 artifact/data adapter.         | More SQL/file formats, prepared statements, federated sources, result reuse and capacity simulation.                                  |
| Kinesis            | Stream/shard lifecycle; put/get records; iterators; ordering within supported shards; durable records.                                          | Append-only stream store and consumer delivery runtime.                       | Resharding, enhanced fan-out, additional retention/throughput behavior and encryption integration.                                    |
| Data Firehose      | Delivery-stream lifecycle; accept records; buffer and actually deliver to a supported destination; report failures.                             | Buffering/delivery worker; S3 and other destination adapters.                 | More destinations, format conversion, transformations, dynamic partitioning and broader retry/error-output behavior.                  |
| Kafka / MSK        | Cluster lifecycle tied to an actual local Kafka runtime; usable broker endpoints; real topic produce/consume; status and persistence.           | Kafka runtime and broker-authentication adapter.                              | Broker configuration breadth, multi-broker behavior, additional authentication modes, upgrades and serverless control-plane coverage. |
| Glue               | Catalog/database/table/partition APIs; metadata used by query providers; execute one supported local job type with real output and status/logs. | Metadata catalog, job executor and optional crawler runtime.                  | More job types/versions, crawlers, classifiers, workflows and schema-registry coverage.                                               |
| Route 53           | Hosted-zone/record lifecycle; supported record changes; actual resolution through a configured local DNS endpoint.                              | Local DNS server and record store.                                            | Additional record types, routing policies, DNSSEC simulation, health checks and private-zone association coverage.                    |
| CloudFront         | Distribution lifecycle connected to a local proxy; route a request to an origin; implement a basic cache contract and invalidation.             | Reverse proxy/cache runtime, S3/HTTP origin adapters and ACM/TLS integration. | More cache behaviors, signed URLs/cookies, functions, logging and advanced origin/failover configuration.                             |
| OpenSearch Service | Domain lifecycle backed by a real search engine; usable endpoint; index/search operations; persistent data and access checks.                   | Search-engine runtime adapter.                                                | Additional engine versions, cluster topology, snapshots, advanced domain configuration and security-plugin breadth.                   |
| Redshift           | Cluster/database lifecycle tied to a documented local SQL analytics backend; real connections/queries; persistence and result/error behavior.   | SQL analytics runtime and S3 load/unload adapter.                             | Wider SQL/data-type compatibility, additional COPY/UNLOAD formats, workgroups, snapshots and scaling simulation.                      |

## Independent subsystems that are not separate screenshot tiles

These are architectural building blocks, not additional claims about the
screenshot. They may be separate packages/providers or external runtimes. The
service API and the execution backend remain distinct responsibilities.

| Independent subsystem               | Functionality owned here                                                                                                      | Main consumers                                               | Dependency timing                                                                                                                |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| STS credential service              | Issue/expire local session credentials; AssumeRole/GetCallerIdentity subset; session-to-principal mapping.                    | IAM, EC2 instance profiles, Lambda, ECS and workload SDKs.   | Required before role-based credential flows can be called complete.                                                              |
| EBS/block-storage subsystem         | Volume/snapshot backend; attachment; filesystem or raw-block implementation; encryption, quotas and recovery where supported. | EC2 and backup/image workflows.                              | Durable filesystem storage is an early requirement; raw devices/encryption/quotas can follow when applications do not need them. |
| VPC networking subsystem            | Actual network isolation, routing, secondary guest interfaces and address forwarding.                                         | EC2, ECS, RDS, MQ and cluster runtimes.                      | Simple explicit endpoints can come first; real packet semantics must precede network-behavior claims.                            |
| Network-policy evaluator/enforcer   | Translate supported security-group/network policies into actual runtime enforcement.                                          | VPC, EC2, ECS and workload runtimes.                         | Required for network-security testing; can follow basic application deployment.                                                  |
| Compute executor                    | Container process lifecycle, resource limits, ownership and optional VM execution.                                            | EC2, ECS, Lambda, SageMaker and jobs.                        | A bounded container executor is an early requirement; VM boot, Windows and more sizing options can follow.                       |
| Kubernetes executor                 | Execute workloads in an explicitly selected existing cluster; status, logs and cleanup.                                       | EC2/ECS/Lambda adapters and local jobs.                      | Optional backend after a working initial executor.                                                                               |
| Cluster provisioner                 | Create/delete local Kubernetes clusters and provide endpoints/credentials.                                                    | EKS and test environments.                                   | Core dependency for provisioning-style EKS behavior; different from an executor or operator.                                     |
| Scheduler/workflow runtime          | Durable timers, polling, task retries and execution state.                                                                    | Step Functions, EventBridge/Pipes, scaling, Backup and jobs. | Introduce when the first supported workflow requires it.                                                                         |
| Data runtimes and connectors        | SQL, document, graph, cache, search, broker and streaming engine adapters.                                                    | Database/analytics services, DMS and MQ.                     | Required before their relevant data operations are usable; metadata alone is not execution.                                      |
| Git, OCI and artifact storage       | Repository transports, image storage and versioned build/output artifacts.                                                    | CodeCommit, ECR, Amplify, Lambda and CI/CD.                  | Required for the corresponding push/pull/build/deploy workflows.                                                                 |
| DNS, proxy and TLS runtimes         | Resolve names; route HTTP traffic; terminate development TLS; store local certificates.                                       | Route 53, CloudFront, ACM, API Gateway and hosting.          | Build when an advertised endpoint needs those actual behaviors.                                                                  |
| Identity and cryptographic backends | Local token signing, protected secrets/keys and cryptographic operations.                                                     | Cognito, IAM/STS, KMS, Secrets Manager and ACM.              | Core for the security behavior each provider advertises.                                                                         |

For EC2 specifically, real VM boot/Windows belongs to an executor, raw EBS to the
storage subsystem, and live routing/NICs/EIP forwarding to networking. They can be
built independently. EC2 request validation, launch-option compatibility and
response/error fidelity remain EC2 provider work.

## LocalCloud capabilities outside the 51 service tiles

Cloud Pods, Stack Insights and Extensions appeared in the screenshot's sidebar.
CI/CD and the Kubernetes pack are included because they are part of this project's
requested roadmap, not because they were AWS service tiles in that screenshot.

| Capability                             | Core functionality                                                                                                 | Independent component                                                | Later functionality within the capability                                                  |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Resource Browser / instance connection | Connect to a running server; authenticate; inspect and modify implemented resources; show actionable errors.       | Shared management API client and resource editors.                   | More resource editors, search, bulk actions and dependency navigation.                     |
| Cloud Pods / state sharing             | Capture a valid supported state bundle; identify versions; restore atomically; document runtime exclusions.        | Snapshot registry/object storage and sharing/access-control service. | Diffs, richer version history, retention policies and additional resource coverage.        |
| Stack Insights / App Inspector         | Request traces, service/resource relationships and operation outcomes backed by actual events.                     | Event recorder and graph/analysis store.                             | Richer correlation, performance summaries, dependency graphs and trace queries.            |
| Extensions                             | Versioned provider/hook interface; install/enable/disable; bounded lifecycle and clear compatibility errors.       | Plugin registry/loader.                                              | Discovery UI, dependency resolution and additional extension hooks.                        |
| Kubernetes pack/operator               | Namespaced instance reconciliation; service endpoints; supported state volumes; workload status/logs and cleanup.  | Kubernetes operator, executor and cluster adapters.                  | More reconciliation options, executor coverage, upgrade handling and advanced placement.   |
| Local CI                               | Project-root configuration; isolated test environment; run application checks; collect results; clean up failures. | Pipeline/task runner and artifact store.                             | Named stages, dependency graphs, caching, matrix execution and broader report formats.     |
| Local CD                               | Deploy a selected artifact to a supported local target; wait for health; record the release; roll back failures.   | Deployment controller and Docker/Kubernetes target adapters.         | Promotion rules, more rollout strategies, environment controls and richer release history. |
| Chaos/fault injection                  | Apply explicit supported faults to selected operations; inspect and remove rules; preserve management access.      | Fault scheduler and optional network-fault adapter.                  | More fault types, scenarios and separately scoped FIS-compatible operations.               |

## Completion rules and ordering

1. Establish the shared engine contracts and account/region boundaries.
2. Implement a service's core provider and its required runtime dependencies.
3. Verify a useful application workflow through the SDK/CLI and management API.
4. Verify state survives restart and invalid requests do not leave partial resources.
5. Document exact unsupported operations and intentional reference differences.
6. Add later provider features and optional independent backends as needed.

A subsystem can be implemented separately while still being critical to another
service. “Independent” is an ownership boundary, not permission to claim its
consumer is complete without it. Every core/later scope above is a development
proposal; exact compatibility claims require service-specific reference evidence.
