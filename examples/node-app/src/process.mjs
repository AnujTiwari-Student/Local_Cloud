import { LocalCloud } from './localcloud.mjs';
import { DocumentProcess } from './app.mjs';

try {
  const app = await new DocumentProcess(new LocalCloud()).initialize();
  const [operation, id, ...words] = process.argv.slice(2);
  let result;
  if (operation === 'submit' && id && words.length) result = await app.submit(id, words.join(' '));
  else if (operation === 'work') result = await app.processOne();
  else throw new Error('Usage: npm start -- submit DOCUMENT_ID "your text" | npm start -- work');
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error(`${error.name}: ${error.message}`);
  process.exitCode = 1;
}
