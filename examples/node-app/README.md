# Node process with LocalCloud project CI

This is a complete standalone example. It requires Node 22+, npm, Python 3.11+ and the `localcloud` command. There are no npm dependencies.

From this directory:

```bash
npm run test:unit
localcloud ci
```

`localcloud ci` discovers the **localcloud.toml beside package.json**. It starts an isolated backend, creates the declared S3 bucket/SQS queue, loads policies, and runs `npm test` with its endpoint and token in the environment. No manually started backend or cluster is needed.

The process stores a document in S3, sends a job to SQS, processes it, writes a result, then acknowledges the message. Tests cover that workflow plus IAM denial, snapshots, state import/export, failure injection, traces and policy proposals. The integration suite intentionally restores resource state and requires the isolated runner.

For a manual backend with the bucket/queue already provisioned:

```bash
export LOCALCLOUD_URL=http://127.0.0.1:4568
npm start -- submit example-1 'hello world'
npm start -- work
```

See the [complete start-to-finish guide](../../docs/NODEJS_LOCAL_CI.md) for installation, file layout, every config field, adapting an existing app, manual service commands, result inspection and optional Node workload Jobs in Kubernetes.
