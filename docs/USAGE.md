# Getting started with LocalCloud

Use the `localcloud` command for the implemented services and developer tools. No curl or AWS CLI installation is required.

## Enable the command

From the project checkout:

```bash
export PATH="$PWD/bin:$PATH"
localcloud --help
```

For a persistent launcher without pip:

```bash
python3 scripts/install-cli.py
export PATH="$HOME/.local/bin:$PATH"
```

## Connect to your existing Kubernetes instance

Keep the Service port-forward running in another terminal:

```bash
kubectl -n localcloud port-forward service/dev 4566:4566 --address=127.0.0.1
```

Then:

```bash
localcloud configure --endpoint-url http://127.0.0.1:4566
localcloud auth login --kubernetes-secret localcloud-access --namespace localcloud
localcloud status
localcloud resources
```

Login reads the Secret through kubectl, verifies access and saves the token privately. It never displays the token. Your current backend does not need an image rebuild or restart to use the host CLI.

Alternatively, start a local backend using `localcloud start --iam soft`. Plain local backends without management authentication do not require login.

## Use S3 and SQS

```bash
localcloud s3 mb s3://assets
printf 'hello from LocalCloud\n' > hello.txt
localcloud s3 cp hello.txt s3://assets/hello.txt
localcloud s3 ls s3://assets
localcloud s3 cp s3://assets/hello.txt downloaded.txt

localcloud sqs create-queue --queue-name jobs
localcloud sqs send-message --queue-name jobs --message-body 'hello'
localcloud sqs receive-message --queue-name jobs
```

Receive hides the delivered message for its visibility timeout. Acknowledge it using the returned receipt:

```bash
localcloud sqs delete-message --queue-name jobs --receipt-handle 'PASTE_RECEIPT_HANDLE'
```

## Use developer tools

```bash
localcloud snapshots save baseline
localcloud snapshots list
localcloud state export baseline.json
localcloud traces list

localcloud jobs run --image localcloud:dev --command python3 -c 'print("Hello from Kubernetes")'
localcloud jobs list

localcloud ci
```

Jobs require the Kubernetes-backed executor; CI starts a separate temporary local backend. Restore/import replace resource state and policies, so use those commands deliberately.

## Complete references

- [CLI commands](CLI.md): every S3/SQS operation, profiles and token login, IAM, snapshots/state, traces, faults, jobs and lifecycle commands.
- [Kubernetes](KUBERNETES.md): initial cluster/operator setup, storage, workload limits and previews.
- [CI/CD](CI_CD.md): fixtures, runner settings, diagnostic artifacts and GitHub workflows.
- [HTTP API](HTTP_API.md): lower-level curl examples for direct integrations.
- [Feature coverage](FEATURES.md): implemented subsets and remaining gaps.

Use `localcloud sqs --help`, `localcloud s3 --help`, or `localcloud jobs run --help` for command-specific options. The client implements only the backend's supported service subset; it does not forward unknown operations to real AWS.
