# Coverage against the supplied paid-tier guide

This matrix is the implementation scope ledger. "Subset" means executable local behavior with documented limits; it never means AWS or LocalStack parity. Product packaging, quotas, support levels and pricing are reference information, not emulator functionality.

| Guide area                      | Current status      | Implemented behavior or remaining work                                                                                                                                      |
| ------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Plans and packaging          | Reference only      | No commercial entitlement integration. Current pricing was checked separately; see EVIDENCE.md.                                                                             |
| 2. AWS service coverage         | Subset              | S3 and SQS only, through one HTTP endpoint. Other providers and SDK compatibility remain open.                                                                              |
| 3.1 Local persistence           | Implemented locally | SQLite commits survive process restart.                                                                                                                                     |
| 3.2 Cloud Pods                  | Subset              | Named local snapshots and portable JSON state. No hosted registry, remote version history or sharing service.                                                               |
| 3.3 Export/import               | Implemented locally | Versioned envelope, checksum, complete schema validation, atomic replacement. Up to 6 MiB canonical state.                                                                  |
| 3.4 Launchpad                   | Subset              | `--load-state` validates and loads a bundle before serving. No graphical launcher.                                                                                          |
| 4. IAM enforcement              | Subset              | Off/soft/enforce, Action/Resource identity policies, deny precedence and evaluation records. No AWS identity authentication or complete IAM policy language.                |
| 4. IAM policy stream            | Subset              | Inspect the most recent 1,000 calls and generate action/resource proposals for successful calls or violations, per principal. No automatic installation.                    |
| 5. Chaos engineering            | Subset              | Persistent named rules for service/operation, probability, latency and HTTP error. Management bypasses faults. No network disruption or AWS FIS API.                        |
| 6. Cloud Sandbox | Subset | Private cluster preview instances with per-instance state, endpoint and operator-driven TTL cleanup. No public hosting, ingress, subscription quotas or managed cloud provisioner. |
| 7. Resource Browser             | Subset              | Read queue/bucket counts, search, health, snapshots, policies, fault controls and traces. Individual resource editors remain open.                                          |
| 7. App Inspector                | Subset              | Searchable request metadata, latency, status and IAM decisions for S3/SQS calls reaching the engine. No full payload capture, service-to-service spans or dependency graph. |
| 7. Teams, analytics, replicator | Not implemented     | No organization management, real-AWS access or copying live resources.                                                                                                      |
| 8. Extensions                   | Not implemented     | Needs a provider/hook interface and versioned extension lifecycle.                                                                                                          |
| 9. Workflow | Subset | AWS-style CLI for supported services/tools, private connection profiles, Kubernetes Secret login, startup seeding, durable state, container image and Kubernetes workload execution. No Lambda hot reload/debugging, multi-account/region, TLS termination or transparent injection. |
| 10. Kubernetes pack | Implemented subset | Namespaced CRD/operator, private Service, optional retained PVC, bounded Job executor, status/logs/cancel, and TTL previews. Live cluster validation pending; no Lambda/ECS/EC2 API emulation. See [usage](KUBERNETES.md). |
| 11. CI/CD | Implemented subset | Isolated live HTTP CI runner, fixture loading, per-run management token, diagnostics and failure cleanup; GitHub Actions Python/container/Kind jobs. No hosted billing/credit service. See [usage](CI_CD.md). |
| 12. Compliance/security packs   | Not implemented     | No enterprise identity, audit export/retention guarantees or compliance claims.                                                                                             |
| 13. AI integrations             | Not implemented     | Management REST endpoints exist; no MCP transport/schema.                                                                                                                   |
| 14. Support tiers               | Outside code scope  | No support or SLA service is represented.                                                                                                                                   |
| 15–16. Study/evidence           | Documented          | Public sources, observed screenshot/runtime boundaries, runnable tests and limits in EVIDENCE.md.                                                                           |

## Extension order

1. Extract a provider interface and add the AWS operations required by an actual application, with API contract tests.
2. Add account/region namespaces and authenticated management access before remote or shared use.
3. Add a bounded workload runner for Lambda/RDS-style workloads, with lifecycle and resource-limit tests.
4. Extend the implemented private cluster previews with remote snapshot storage, authenticated tenant isolation and optional ingress.
5. Add collaboration, AWS workload API providers, live-resource replication and MCP after the local provider and instance lifecycle contracts are stable.

Each additional service should specify operations, request/response shapes, errors, persistence rules and cross-service interactions. A list of 110 service names alone does not define those contracts.
