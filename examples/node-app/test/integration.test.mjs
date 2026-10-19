// Run this file only through localcloud ci: snapshot/import tests replace ALL state.
import { before, test } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { LocalCloud } from '../src/localcloud.mjs';
import { DocumentProcess } from '../src/app.mjs';

let cloud, app;
const unique = prefix => `${prefix}-${randomUUID().slice(0, 8)}`;
before(async () => {
  assert.equal(process.env.LOCALCLOUD_CI, '1', 'Run localcloud ci from the example directory; this suite must use an isolated backend.');
  cloud = new LocalCloud();
  app = await new DocumentProcess(cloud).initialize();
});

test('S3 + SQS: submit, process, persist result and acknowledge', async () => {
  const id = unique('document');
  const submitted = await app.submit(id, '  hello from my app  ');
  assert.ok(submitted.messageId);
  assert.equal((await cloud.getObject(app.bucket, submitted.key)).toString(), '  hello from my app  ');
  assert.deepEqual(await app.processOne(), { id, result: 'HELLO FROM MY APP' });
  assert.equal((await cloud.getObject(app.bucket, `results/${id}.txt`)).toString(), 'HELLO FROM MY APP');
  assert.equal(await app.processOne(), null);
});

test('IAM: reader can read an object but cannot upload or send messages', async () => {
  const reader = new LocalCloud({ principal: 'reader' });
  await cloud.putObject(app.bucket, 'reader-example', Buffer.from('read me'));
  assert.equal((await reader.getObject(app.bucket, 'reader-example')).toString(), 'read me');
  await assert.rejects(reader.putObject(app.bucket, 'forbidden', Buffer.from('no')), error => error.status === 403);
  await assert.rejects(reader.sqs('SendMessage', { QueueUrl: app.queueUrl, MessageBody: 'no' }), error => error.status === 403);
});

test('snapshots: restore removes changes made after save', async () => {
  const name = unique('baseline');
  await cloud.management('POST', 'snapshots', { name });
  await cloud.putObject(app.bucket, 'after-snapshot', Buffer.from('temporary'));
  await cloud.management('POST', 'snapshots/restore', { name });
  await assert.rejects(cloud.getObject(app.bucket, 'after-snapshot'), error => error.status === 404);
});

test('portable state: export and import reproduce a known fixture', async () => {
  const baseline = await cloud.management('GET', 'state');
  assert.equal(baseline.format, 'localcloud-state');
  await cloud.putObject(app.bucket, 'after-export', Buffer.from('temporary'));
  await cloud.management('POST', 'state/import', baseline);
  await assert.rejects(cloud.getObject(app.bucket, 'after-export'), error => error.status === 404);
});

test('chaos: application sees S3 failure, management remains available and service recovers', async () => {
  const name = unique('outage');
  try {
    await cloud.management('POST', 'faults', { name, service: 's3', operation: 'PutObject', probability: 1, latency_ms: 25, error: { code: 'ServiceUnavailable', status: 503 } });
    await assert.rejects(app.submit(unique('failed'), 'hello'), error => error.status === 503);
    assert.ok((await cloud.management('GET', 'faults')).some(rule => rule.name === name));
  } finally {
    await cloud.management('POST', 'faults/delete', { name });
  }
  const id = unique('recovered');
  await app.submit(id, 'works again');
  assert.deepEqual(await app.processOne(), { id, result: 'WORKS AGAIN' });
});

test('inspector and policy stream show actual app operations and denied permissions', async () => {
  const marker = unique('trace');
  await cloud.putObject(app.bucket, marker, Buffer.from('not-retained-as-payload'));
  const traces = await cloud.management('GET', 'traces?principal=app');
  assert.ok(traces.some(event => event.operation === 'PutObject' && event.resource.endsWith('/' + marker) && event.permission.decision === 'allow'));
  assert.ok(!JSON.stringify(traces).includes('not-retained-as-payload'));
  const policy = await cloud.management('GET', 'policy-stream?principal=app&source=successful');
  assert.ok(policy.Statement.some(statement => statement.Action === 's3:PutObject' && statement.Resource.endsWith('/' + marker)));
  const denials = await cloud.management('GET', 'policy-stream?principal=reader&source=violations');
  assert.ok(denials.Statement.some(statement => statement.Action === 'sqs:SendMessage'));
});
