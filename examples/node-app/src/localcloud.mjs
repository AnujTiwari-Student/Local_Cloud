// Small adapter for this emulator's implemented APIs, not a production AWS SDK.
export class CloudError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'CloudError';
    this.status = status;
    this.code = code;
  }
}

export class LocalCloud {
  constructor({
    endpoint = process.env.LOCALCLOUD_URL ?? process.env.AWS_ENDPOINT_URL,
    token = process.env.LOCALCLOUD_MANAGEMENT_TOKEN ?? '',
    principal = process.env.LOCALCLOUD_PRINCIPAL ?? 'test',
    fetchImpl = globalThis.fetch,
  } = {}) {
    if (!endpoint) throw new Error('Missing LOCALCLOUD_URL. Run this app through localcloud ci, or set its backend URL.');
    const url = new URL(endpoint);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) {
      throw new Error('LOCALCLOUD_URL must be an HTTP(S) origin.');
    }
    this.endpoint = url.origin;
    this.token = token;
    this.principal = principal;
    this.fetch = fetchImpl;
  }

  async request(method, path, { json, body, target } = {}) {
    if (!path.startsWith('/') || path.startsWith('//')) throw new Error('Expected a local API path.');
    const headers = { 'X-Local-Principal': this.principal };
    if (path.startsWith('/_local/') && this.token) headers.Authorization = `Bearer ${this.token}`;
    if (json !== undefined) {
      body = JSON.stringify(json);
      headers['Content-Type'] = target ? 'application/x-amz-json-1.0' : 'application/json';
    }
    if (target) headers['X-Amz-Target'] = `AmazonSQS.${target}`;
    const response = await this.fetch(this.endpoint + path, {
      method, headers, body, redirect: 'error', signal: AbortSignal.timeout(10_000),
    });
    const bytes = Buffer.from(await response.arrayBuffer());
    const isJSON = response.headers.get('content-type')?.includes('json');
    let value = bytes;
    if (bytes.length && isJSON) value = JSON.parse(bytes.toString('utf8'));
    if (!response.ok) {
      const code = isJSON ? value.__type : bytes.toString().match(/<Code>([^<]+)<\/Code>/)?.[1];
      throw new CloudError(response.status, code ?? 'HTTPError', isJSON ? (value.message ?? 'Request failed') : `Cloud request failed: ${code ?? response.status}`);
    }
    return value;
  }

  objectPath(bucket, key = '') {
    return '/' + encodeURIComponent(bucket) + (key ? '/' + key.split('/').map(encodeURIComponent).join('/') : '');
  }
  putObject(bucket, key, body) { return this.request('PUT', this.objectPath(bucket, key), { body }); }
  getObject(bucket, key) { return this.request('GET', this.objectPath(bucket, key)); }
  deleteObject(bucket, key) { return this.request('DELETE', this.objectPath(bucket, key)); }
  sqs(operation, parameters) { return this.request('POST', '/', { target: operation, json: parameters }); }
  management(method, path, json) { return this.request(method, '/_local/' + path, { json }); }
}
