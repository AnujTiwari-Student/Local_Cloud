export function normalizeDocument(text) {
  if (typeof text !== 'string' || !text.trim()) throw new Error('Document text must not be empty.');
  return text.trim().toUpperCase();
}

export class DocumentProcess {
  constructor(cloud, {
    bucket = process.env.DOCUMENTS_BUCKET ?? 'app-documents',
    queueName = process.env.JOBS_QUEUE ?? 'app-jobs',
  } = {}) {
    this.cloud = cloud;
    this.bucket = bucket;
    this.queueName = queueName;
  }

  async initialize() {
    this.queueUrl = (await this.cloud.sqs('GetQueueUrl', { QueueName: this.queueName })).QueueUrl;
    return this;
  }

  async submit(id, text) {
    if (!/^[a-zA-Z0-9_-]{1,80}$/.test(id)) throw new Error('Use a short document ID containing letters, numbers, underscores or hyphens.');
    normalizeDocument(text); // Validate before storing or queuing anything.
    const key = `documents/${id}.txt`;
    await this.cloud.putObject(this.bucket, key, Buffer.from(text));
    const message = await this.cloud.sqs('SendMessage', {
      QueueUrl: this.queueUrl, MessageBody: JSON.stringify({ id, key }),
    });
    return { id, key, messageId: message.MessageId };
  }

  async processOne() {
    const response = await this.cloud.sqs('ReceiveMessage', {
      QueueUrl: this.queueUrl, MaxNumberOfMessages: 1, VisibilityTimeout: 60,
    });
    const message = response.Messages?.[0];
    if (!message) return null;
    const job = JSON.parse(message.Body);
    const input = await this.cloud.getObject(this.bucket, job.key);
    const result = normalizeDocument(input.toString('utf8'));
    await this.cloud.putObject(this.bucket, `results/${job.id}.txt`, Buffer.from(result));
    // Acknowledge only after the result has been stored successfully.
    await this.cloud.sqs('DeleteMessage', { QueueUrl: this.queueUrl, ReceiptHandle: message.ReceiptHandle });
    return { id: job.id, result };
  }
}
