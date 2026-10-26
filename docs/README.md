# LocalCloud documentation

Start with the **[complete handbook](HANDBOOK.md)**. It explains the project in
plain language and walks through setup, application integration, local CI,
EC2, Kubernetes, the core engine, development and troubleshooting. Chapters 26–33
define future contributor work: all 51 service build cards, core versus optional
features, independent component contracts, acceptance tests and release criteria.

## Choose a guide

| Need                                                | Guide                                                |
| --------------------------------------------------- | ---------------------------------------------------- |
| A complete introduction and working scenarios       | [Handbook](HANDBOOK.md)                              |
| Build a future service, add-on or reusable backend | [Contributor chapters](HANDBOOK.md#26-contributor-guide-to-the-future-project) |
| Short first-run steps                               | [Getting started](USAGE.md)                          |
| Exact CLI command families                          | [CLI reference](CLI.md)                              |
| Browser connection and resource controls            | [Dashboard](DASHBOARD.md)                            |
| Add local CI to an app                              | [Local CI](CI_CD.md)                                 |
| Full runnable Node.js application                   | [Node integration tutorial](NODEJS_LOCAL_CI.md)      |
| Current EC2 implementation and limits               | [EC2 release checkpoint](EC2_RELEASE.md)             |
| EC2 networking, storage, SSH and metadata           | [Infrastructure](EC2_INFRASTRUCTURE.md)              |
| Actual networking observations and differences      | [Networking REA report](EC2_NETWORKING.md)           |
| Actual Docker storage observations and differences | [Storage REA report](REA_EC2_STORAGE.md)             |
| EBS defaults, history and batch consistency          | [Storage follow-up](REA_EC2_STORAGE_FOLLOWUP.md)       |
| EC2 mock, Docker and Kubernetes executor evidence   | [Executor REA report](REA_EC2_EXECUTORS.md)           |
| Run EC2 guests and persistent data in Kubernetes    | [EC2 Kubernetes guide](EC2_KUBERNETES.md)             |
| EC2 role credential reference and live evidence | [Role REA report](REA_EC2_INSTANCE_ROLES.md) |
| EC2 role credentials and automatic SDK authentication | [Instance roles](EC2_INSTANCE_ROLES.md) |
| Metadata available during Kubernetes EC2 user data | [Bootstrap metadata audit](REA_EC2_KUBERNETES_BOOTSTRAP.md) |
| Kubernetes EC2 provisioning recovery and repair     | [Recovery audit](REA_EC2_KUBERNETES_COMPLETION.md)    |
| Template/version workflow                           | [Launch templates](EC2_TEMPLATES.md)                 |
| Operator, persistent instances and Kubernetes Jobs  | [Kubernetes](KUBERNETES.md)                          |
| Direct HTTP integration                             | [HTTP reference](HTTP_API.md)                        |
| Current supported subsets and planned work          | [Feature ledger](FEATURES.md)                        |
| All 51 screenshot services and component boundaries | [Service roadmap](SERVICE_IMPLEMENTATION_ROADMAP.md) |
| Core-engine source investigation                    | [Engine investigation](ENGINE_RE.md)                 |
| Evidence boundaries and historical observations     | [Evidence ledger](EVIDENCE.md)                       |

## Reading implementation status correctly

The handbook describes the current integration path. Some investigation reports
are historical checkpoints and retain earlier operation/test counts and limits.
Use the current release guide and source for current EC2 behavior; follow their
linked artifacts for the exact evidence behind a claim.

“Planned” is not runnable configuration. CI currently has no native CD/stages
parser; the Kubernetes operator is not EKS; networking records are not live AWS
firewall/routing behavior. Independent components in the roadmap still require
integration with their service providers.
