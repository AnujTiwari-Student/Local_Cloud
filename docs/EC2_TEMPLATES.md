# EC2 launch templates

Launch templates save repeatable EC2 settings in LocalCloud's persistent state. A template has numbered versions and one default version. Starting an instance copies the selected version; later template edits do not change existing instances.

The implementation is independent code informed by an isolated, normally licensed LocalStack 2026.10.0 reference replay. It supports eight selected operations, rather than every launch-template parameter.

## Start and use

Run your configured LocalCloud server first and authenticate its CLI as described in [the EC2 guide](EC2_INFRASTRUCTURE.md). The following examples use the mock `ami-local`. For an actual Docker workload, substitute an AMI configured with the server's `--ec2-image AMI=DOCKER_IMAGE` option.

Create `template.json` in your application directory:

```json
{
  "ImageId": "ami-local",
  "InstanceType": "t3.micro",
  "MetadataOptions": {"HttpTokens": "required"},
  "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "application", "Value": "demo"}]}]
}
```

Create the template, inspect it, and start an instance:

```bash
localcloud ec2 create-launch-template --launch-template-name demo-web \
  --launch-template-data file://template.json --version-description initial
localcloud ec2 describe-launch-templates --launch-template-names demo-web
localcloud ec2 run-instances \
  --launch-template '{"LaunchTemplateName":"demo-web","Version":"$Default"}'
```

Create an inherited version and make it the default:

```bash
localcloud ec2 create-launch-template-version --launch-template-name demo-web \
  --source-version '1' --version-description larger \
  --launch-template-data '{"InstanceType":"t3.small"}'
localcloud ec2 describe-launch-template-versions --launch-template-name demo-web
localcloud ec2 modify-launch-template --launch-template-name demo-web --default-version '2'
localcloud ec2 run-instances \
  --launch-template '{"LaunchTemplateName":"demo-web","Version":"$Latest"}' \
  --instance-type t3.micro
```

Explicit run settings override template defaults. In the last example, the new instance uses the latest template's image and other settings, with `t3.micro` supplied explicitly. Structured fields such as `MetadataOptions`, `TagSpecifications`, and `BlockDeviceMappings` are replaced as a whole when overridden. Omit `--source-version` to create an independent version; set it to a version number, `$Default`, or `$Latest` to inherit settings.

Delete an old version or the whole template:

```bash
localcloud ec2 delete-launch-template-versions --launch-template-name demo-web --versions 1
localcloud ec2 delete-launch-template --launch-template-name demo-web
```

Deleting a template leaves instances launched from it running. A default version cannot be deleted; set a different default first. Deletion reports per-version success/failure. Version numbers are not reused. `$Latest` selects the highest surviving version.

## Supported settings

- Image ID, instance type, key name, base64 user data.
- Security-group IDs and metadata options already supported by the local EC2 runner.
- Instance tag specifications.
- Filesystem EBS mappings already supported by the local runner.
- One new primary interface with `DeviceIndex: 0`, subnet ID, security-group IDs, private IP and `DeleteOnTermination: true`. Existing interfaces, secondary interfaces and public-IP allocation through template fields are rejected.

`GetLaunchTemplateData` returns a bounded reusable subset of a local instance's settings. It does not export a live container filesystem, application files, IAM credentials or runtime ownership metadata. The current export omits existing block-device attachments; describe those separately and specify a snapshot or new volume explicitly.

IAM instance profiles, instance requirements, capacity reservations, placement groups, monitoring, Spot options and other unimplemented settings fail explicitly. A saved template does not add those features to the runner. Limits are 1000 templates and 1000 surviving versions per template; storage is included in normal LocalCloud state persistence and portable-state validation.

## Reference evidence and differences

Reference: `localstack/localstack@sha256:aa5632e041ca54ff61a0a25e17b1c898b09a1eb7f611bfc05f73ef53b7266872`, normally activated Student entitlement, mock EC2 executor. REA process Evidence: `ev_0ba830f1187c3c6fda835b78923b45750cddf86c7b9f0efda361194963b73dbc`. Exact recorded requests and responses are in [the reference report](research/rea/ec2-templates-reference.json); the SDK replay is [the probe script](../scripts/ec2-templates-scenario.py).

The observed reference supports create, describe, version creation, default selection, template-based launch and instance-to-template data export. Missing IDs in `DescribeLaunchTemplates` return an empty list. LocalCloud follows these basic workflows, with explicit differences:

| Case | Observed reference | LocalCloud |
|---|---|---|
| Same create request and `ClientToken` | Duplicate-name error | Returns the original result; changed parameters fail |
| `SourceVersion` inheritance | Probe's version 2 omitted inherited image and user data | Merges source settings with explicit replacement fields |
| Delete versions | HTTP 501, action unimplemented | Supported, with protected default and per-version errors |
| Template tags | Reference copied template-resource tags into version data | Template-resource tags stay separate from instance tags |
| `GetLaunchTemplateData` | Broad generated output, including placeholder fields | Only supported reusable instance settings |

These are deliberate useful behaviors, not claims of byte-for-byte reference equivalence. Networking and Docker execution retain the limitations in [the EC2 networking guide](EC2_NETWORKING.md). The reference container and probe-created resources were removed after capture; no user server or saved tokens were changed.
