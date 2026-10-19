import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizeDocument, DocumentProcess } from '../src/app.mjs';
import { LocalCloud } from '../src/localcloud.mjs';

test('document normalization handles spaces and unicode', () => {
  assert.equal(normalizeDocument('  hello world  '), 'HELLO WORLD');
  assert.equal(normalizeDocument('café'), 'CAFÉ');
  assert.throws(() => normalizeDocument('  '), /empty/);
});

test('a failed result upload never acknowledges the queue message', async () => {
  const operations = [];
  const fakeCloud = {
    sqs: async operation => {
      operations.push(operation);
      if (operation === 'GetQueueUrl') return { QueueUrl: 'queue' };
      if (operation === 'ReceiveMessage') return { Messages: [{ Body: JSON.stringify({ id: 'one', key: 'input' }), ReceiptHandle: 'receipt' }] };
      return {};
    },
    getObject: async () => Buffer.from('hello'),
    putObject: async () => { throw new Error('storage unavailable'); },
  };
  const app = await new DocumentProcess(fakeCloud).initialize();
  await assert.rejects(app.processOne(), /storage unavailable/);
  assert.ok(!operations.includes('DeleteMessage'));
});

test('empty success bodies are accepted and credentials go only to management routes', async () => {
  const requests = [];
  const client = new LocalCloud({ endpoint: 'http://127.0.0.1:4566', token: 'test-secret', fetchImpl: async (url, options) => {
    requests.push({ url, options });
    return new Response('', { status: 200, headers: { 'content-type': 'application/json' } });
  } });
  await client.putObject('bucket', 'a ?#', Buffer.from('body'));
  assert.ok(!requests[0].options.headers.Authorization);
  assert.match(requests[0].url, /a%20%3F%23$/);
  await client.management('GET', 'resources');
  assert.equal(requests[1].options.headers.Authorization, 'Bearer test-secret');
});
