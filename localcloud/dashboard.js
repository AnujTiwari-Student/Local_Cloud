'use strict';
const el = id => document.getElementById(id);
let connected = false, busy = false, state = null, view = 'overview';
let activeToken = '', activePrincipal = 'test';
let selectedInstance = null, selectedBucket = null, selectedQueue = null, queueUrl = null;
let traceEvents = [], connecting = false;
const titles = {overview:'Resource Browser', ec2:'EC2 instances', s3:'S3 buckets', sqs:'SQS queues', state:'Snapshots & state', policies:'IAM policies', inspector:'Request Inspector', chaos:'Fault injection', executions:'Kubernetes jobs'};
el('instance-endpoint').value = location.origin;
function message(text, error = false) { el('status').textContent = text; el('status').classList.toggle('error', error); }
function controls() {
  el('workspace').disabled = !connected || busy;
  el('connect').disabled = busy;
  el('disconnect').disabled = !connected || busy;
  el('health').textContent = connected ? (busy ? 'Working…' : 'Connected') : 'Disconnected';
  el('health').classList.toggle('offline', !connected);
  el('connection-dot').classList.toggle('online', connected);
}
function disconnect(clearToken = true) {
  connected = false; state = null; connecting = false; activeToken = '';
  if (clearToken) el('management-token').value = '';
  selectedInstance = selectedBucket = selectedQueue = queueUrl = null;
  el('sidebar-instance').textContent = 'No instance connected';
  el('sidebar-endpoint').textContent = 'Start your server to begin';
  el('endpoint').textContent = 'No instance connected';
  for (const id of ['instances','buckets','queues','object-list','message-list','ec2-detail-fields','ec2-json','queue-attributes','snapshot-list','trace-list','fault-list','execution-list','execution-logs','audit','instance-details']) el(id).replaceChildren();
  for (const id of ['ec2-detail','bucket-detail','queue-detail']) el(id).hidden = true;
  for (const id of ['ec2-count','s3-count','sqs-count']) el(id).textContent = '0';
  controls();
}
async function api(path, data, method) {
  if (!connected && !connecting) throw Error('Connect to a running instance first.');
  const headers = {'X-Local-Principal':activePrincipal};
  if (activeToken) headers.Authorization = 'Bearer ' + activeToken;
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch('/_local/' + path, {method:method || (data !== undefined ? 'POST' : 'GET'), headers, signal:controller.signal, ...(data !== undefined ? {body:JSON.stringify(data)} : {})});
    let result;
    try { result = await response.json(); } catch (_) { throw Error('The server returned an invalid response. Check the endpoint and restart the server if needed.'); }
    if (!response.ok) {
      if (response.status === 401) { disconnect(false); throw Error('Connection requires a valid management token. Enter the running server’s token and connect again.'); }
      throw Error((result.__type ? result.__type + ': ' : '') + (result.message || 'Request failed'));
    }
    return result;
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) {
      disconnect(false);
      throw Error('Cannot reach the LocalCloud instance. Check that the server or port-forward is running, then reconnect. A timed-out action may still have completed; refresh before retrying.');
    }
    throw error;
  } finally { clearTimeout(timer); }
}
async function run(task, success = 'Ready') {
  if (busy) return;
  busy = true; controls(); message('Working…');
  try { await task(); message(typeof success === 'function' ? success() : success); } catch (error) { message(error.message, true); }
  finally { busy = false; controls(); }
}
function text(tag, value, className) { const node = document.createElement(tag); node.textContent = value; if (className) node.className = className; return node; }
function button(label, handler, className = 'secondary') { const node = text('button', label, className); node.type = 'button'; node.onclick = () => run(handler); return node; }
function details(id, values) { const root = el(id); root.replaceChildren(); for (const [key,value] of Object.entries(values)) root.append(text('dt',key), text('dd',value ?? '—')); }
function dataTable(id, headers, rows) {
  const root = el(id); root.replaceChildren();
  if (!rows.length) { root.append(text('div','No resources to show.','empty')); return; }
  const table = document.createElement('table'), head = table.createTHead().insertRow();
  for (const label of headers) head.append(text('th',label));
  const body = table.createTBody();
  for (const values of rows) { const row = body.insertRow(); for (const value of values) { const cell = row.insertCell(); cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—'))); } }
  root.append(table);
}
function actions(...items) { const root = document.createElement('div'); root.className = 'actions'; root.append(...items); return root; }
function saveFile(filename, data, type = 'application/octet-stream') {
  const url = URL.createObjectURL(new Blob([data], {type})), a = document.createElement('a');
  a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}
function ec2(action, payload = {}) { return api('ec2', {Action:action,...payload}); }
function sqs(action, payload = {}) { return api('sqs', {Action:action,...payload}); }
function s3(action, payload = {}) { return api('s3', {Action:action,...payload}); }
function navigate(target) {
  view = target;
  for (const node of document.querySelectorAll('.view')) node.hidden = node.id !== 'view-' + view;
  for (const node of document.querySelectorAll('[data-view]')) node.classList.toggle('active', node.dataset.view === view);
  el('page-title').textContent = titles[view]; el('breadcrumb').textContent = view.toUpperCase();
}
for (const node of document.querySelectorAll('[data-view],[data-go]')) node.onclick = () => { navigate(node.dataset.view || node.dataset.go); if (connected) run(refresh); };
function renderOverview() {
  el('ec2-count').textContent = state.instances.length;
  el('s3-count').textContent = state.buckets.length; el('sqs-count').textContent = state.queues.length;
  el('sidebar-instance').textContent = state.instanceName;
  el('sidebar-endpoint').textContent = location.origin;
  el('endpoint').textContent = state.instanceName + ' · ' + location.origin;
  details('instance-details', {'Name':state.instanceName,'Connection endpoint':location.origin,'Advertised service endpoint':state.endpoint,'Services':state.services.join(', '),'EC2 execution':state.ec2Backend,'IAM mode':state.iamMode,'Region':'us-east-1','Account':'000000000000'});
  el('iam-mode').textContent = state.iamMode;
  el('audit').textContent = state.audit.length ? JSON.stringify(state.audit,null,2) : 'No violations';
  el('ec2-mode').textContent = state.ec2Backend === 'docker' ? 'Docker execution enabled. Choose an allowlisted image; publish ports to reach your app on the Docker host.' : 'Mock mode: instances are state records and do not execute applications. Enable Docker execution on the server to run apps.';
  el('ec2-images').replaceChildren(...state.ec2Images.map(image => { const option = document.createElement('option'); option.value = image.ImageId; option.label = image.DockerImage; return option; }));
  el('ec2-publish').disabled = state.ec2Backend !== 'docker';
  const docker = state.ec2Backend === 'docker';
  el('state-notice').textContent = docker ? 'Snapshot save/restore and state import are disabled in Docker EC2 mode. Workload filesystems cannot be restored from emulator snapshots.' : 'Save emulator resources and policies. Restore replaces current state.';
  for (const node of el('snapshot-form').elements) node.disabled = docker;
  el('import-state').disabled = docker;
}
function renderInstances() {
  const filter = el('ec2-search').value.toLowerCase();
  dataTable('instances',['Instance / name','State','Image / type','Runtime','Actions'],state.instances.filter(i => JSON.stringify(i).toLowerCase().includes(filter)).map(i => {
    const identity = document.createElement('div'); identity.append(button(i.Name || i.InstanceId,async()=>showInstance(i),'text-link')); identity.append(text('small',i.InstanceId));
    const runtime = document.createElement('div'); runtime.append(text('span',i.Backend)); if (i.ContainerId) runtime.append(text('small',i.ContainerId.slice(0,12)));
    const mutate = (action, prompt) => async () => { if (prompt && !confirm(prompt)) return; await ec2(action,{InstanceIds:[i.InstanceId]}); await refresh(); };
    const start = button('Start',mutate('StartInstances')), stop = button('Stop',mutate('StopInstances'));
    const terminate = button('Terminate',mutate('TerminateInstances','Terminate '+(i.Name || i.InstanceId)+'? Its container will be removed.'),'danger');
    start.disabled = i.State.Name !== 'stopped'; stop.disabled = i.State.Name !== 'running'; terminate.disabled = i.State.Name === 'terminated';
    return [identity,i.State.Name,i.ImageId+' / '+i.InstanceType,runtime,actions(start,stop,terminate)];
  }));
  if (selectedInstance) { const item = state.instances.find(i=>i.InstanceId === selectedInstance); if (item) showInstance(item); else el('ec2-detail').hidden = true; }
}
function showInstance(item) {
  selectedInstance = item.InstanceId; el('ec2-detail').hidden = false;
  details('ec2-detail-fields', {'Name':item.Name || 'Unnamed','Instance ID':item.InstanceId,'State':item.State.Name,'Image ID':item.ImageId,'Instance type':item.InstanceType,'Backend':item.Backend,'Launch time':item.LaunchTime,'Container ID':item.ContainerId,'Container name':item.ContainerName,'Port mappings':(item.PortMappings || []).map(p=>'127.0.0.1:'+p.HostPort+' → '+p.ContainerPort+'/tcp').join(', ') || 'None','Configured endpoints':(item.Endpoints || []).join(', ') || 'None'});
  el('ec2-detail-type').value = item.InstanceType;
  el('ec2-modify-button').disabled = item.State.Name !== 'stopped';
  el('ec2-endpoint-links').replaceChildren();
  if(item.State.Name === 'running') for(const endpoint of item.Endpoints || []) {
    const url = new URL(endpoint);
    if(url.protocol === 'http:' && url.hostname === '127.0.0.1') {
      const link = text('a','Open application: '+url.host); link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';el('ec2-endpoint-links').append(link);
    }
  }
  el('ec2-new-name').value = item.Name || ''; el('ec2-json').textContent = JSON.stringify(item,null,2);
}
function renderBuckets() {
  dataTable('buckets',['Bucket name','Objects','Actions'],state.buckets.map(b=>[button(b.name,()=>selectBucket(b.name),'text-link'),b.objects,button('View objects',()=>selectBucket(b.name))]));
  if (selectedBucket && !state.buckets.some(b=>b.name === selectedBucket)) { selectedBucket = null; el('bucket-detail').hidden = true; }
}
async function selectBucket(name) {
  const result = await s3('ListObjects',{Bucket:name}); selectedBucket = name; el('bucket-detail').hidden = false; el('bucket-title').textContent = name + ' / objects';
  dataTable('object-list',['Object key','Size','Actions'],result.objects.map(object=>[object.key,object.size+' bytes',actions(button('Download',async()=>{
    const result = await s3('GetObject',{Bucket:name,Key:object.key}); const binary = atob(result.ContentBase64); saveFile(object.key.split('/').pop() || 'object',Uint8Array.from(binary,c=>c.charCodeAt(0)));
  }),button('Delete',async()=>{if(!confirm('Delete object '+object.key+'?'))return;await s3('DeleteObject',{Bucket:name,Key:object.key});await refresh();},'danger'))]));
}
function renderQueues() {
  dataTable('queues',['Queue name','Stored messages','Actions'],state.queues.map(q=>[button(q.name,()=>selectQueue(q.name),'text-link'),q.messages,button('View queue',()=>selectQueue(q.name))]));
  if (selectedQueue && !state.queues.some(q=>q.name === selectedQueue)) { selectedQueue = queueUrl = null; el('queue-detail').hidden = true; el('message-list').replaceChildren(); }
}
async function selectQueue(name, preserveMessages = false) {
  const found = await sqs('GetQueueUrl',{QueueName:name});
  const result = await sqs('GetQueueAttributes',{QueueUrl:found.QueueUrl});
  if (selectedQueue !== name || !preserveMessages) el('message-list').replaceChildren();
  selectedQueue = name; queueUrl = found.QueueUrl; el('queue-detail').hidden = false; el('queue-title').textContent = name;
  details('queue-attributes',{'Queue URL':queueUrl,...result.Attributes});
  el('queue-detail-visibility').value = result.Attributes.VisibilityTimeout;
}
async function renderTools() {
  if (view === 'inspector') { traceEvents = await api('traces'); renderTraces(); }
  if (view === 'chaos') {
    const rules = await api('faults'); el('fault-list').replaceChildren();
    for (const rule of rules) { const row = text('p',rule.name+' · '+rule.service+':'+rule.operation+' · '+rule.latency_ms+' ms · '+(rule.error?.code || 'Latency only')+' '); row.append(button('Remove',async()=>{await api('faults/delete',{name:rule.name});await renderTools();})); el('fault-list').append(row); }
  }
  if (view === 'state') {
    el('snapshot-list').replaceChildren();
    for (const snapshot of state.snapshots) { const row = text('p',snapshot.name+' · '+new Date(snapshot.created*1000).toLocaleString()+' '); const restore = button('Restore',async()=>{if(!confirm('Replace current resources and policies with '+snapshot.name+'?'))return;await api('snapshots/restore',{name:snapshot.name});selectedBucket=selectedQueue=selectedInstance=null;await refresh();}); restore.disabled = state.ec2Backend === 'docker'; row.append(restore); el('snapshot-list').append(row); }
  }
  if (view === 'executions') await refreshExecutions();
}
function renderTraces() {
  const root = el('trace-list'); root.replaceChildren(); const filter = el('trace-search').value.toLowerCase();
  for (const event of traceEvents.filter(e=>JSON.stringify(e).toLowerCase().includes(filter))) { const detail = document.createElement('details'); detail.append(text('summary',event.status+' · '+event.service+':'+event.operation+' · '+event.principal+' · '+event.duration_ms+' ms'),text('pre',JSON.stringify(event,null,2))); root.append(detail); }
  if (!root.childNodes.length) root.append(text('p','No matching calls.'));
}
async function refreshExecutions() {
  el('submit-execution').disabled = !state.executor;
  el('executor-status').textContent = state.executor ? 'Allowed images: '+state.workloadImages.join(', ') : 'Executor disabled. Start the server with Kubernetes executor configuration to run jobs.';
  el('execution-list').replaceChildren(); if (!state.executor) return;
  for (const job of await api('executions')) { const row = text('p',job.name+' · '+job.phase+' '); row.append(button('Logs',async()=>{const result = await api('executions/'+encodeURIComponent(job.name)+'/logs');el('execution-logs').textContent=result.pending?'Pod pending':result.logs;}),button('Delete job',async()=>{if(!confirm('Delete job '+job.name+'?'))return;await api('executions/'+encodeURIComponent(job.name),undefined,'DELETE');await refreshExecutions();},'danger')); el('execution-list').append(row); }
}
async function refresh() {
  state = await api('resources'); renderOverview(); renderBuckets(); renderQueues();
  if (view === 'ec2') { const result = await ec2('DescribeInstances'); state.instances = result.Reservations.flatMap(r=>r.Instances); renderInstances(); }
  if (view === 's3' && selectedBucket) await selectBucket(selectedBucket);
  if (view === 'sqs' && selectedQueue) await selectQueue(selectedQueue,true);
  await renderTools();
}
el('connection-form').onsubmit = event => { event.preventDefault(); run(async()=>{
  const endpoint = new URL(el('instance-endpoint').value);
  if (!['http:','https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || !['','/'].includes(endpoint.pathname)) throw Error('Enter an HTTP(S) origin without credentials, path or query.');
  if (endpoint.origin !== location.origin) { if(confirm('Open the dashboard at '+endpoint.origin+'? You will enter that instance’s token there.')) location.assign(endpoint.origin+'/_local'); return; }
  disconnect(false); activeToken = el('management-token').value.trim(); activePrincipal = el('connection-principal').value.trim();
  connecting = true;
  try { const health = await api('health'); if (health.status !== 'running') throw Error('The endpoint is not a running LocalCloud instance.'); await refresh(); connected = true; }
  catch(error) { disconnect(false); throw error; }
  finally { connecting = false; }
},()=>connected?'Connected. Choose a service to manage its resources.':'Open the selected endpoint and connect there.'); };
el('disconnect').onclick = () => { disconnect(); message('Disconnected. The server and its workloads continue running.'); };
el('refresh').onclick = () => run(refresh,'Resources refreshed.');
function form(id, handler, success) { el(id).onsubmit = event => {event.preventDefault();run(handler,success);}; }
form('ec2-form',async()=>{
  const payload = {ImageId:el('ec2-image').value,InstanceType:el('ec2-type').value,MinCount:1,MaxCount:1};
  if(el('ec2-name').value) payload.Name=el('ec2-name').value;
  const ports = el('ec2-publish').disabled ? '' : el('ec2-publish').value.trim();
  if(ports) payload.PortMappings=ports.split(',').map(value=>{if(!/^\s*\d+:\d+\s*$/.test(value))throw Error('Use comma-separated HOST_PORT:CONTAINER_PORT mappings.');const [HostPort,ContainerPort]=value.trim().split(':').map(Number);return {HostPort,ContainerPort};});
  const result = await ec2('RunInstances',payload);selectedInstance=result.Instances[0].InstanceId;await refresh();
},'Instance launched. Open its details to see configured endpoints.');
form('ec2-modify',async()=>{await ec2('ModifyInstanceAttribute',{InstanceIds:[selectedInstance],InstanceType:el('ec2-detail-type').value});await refresh();},'Instance type updated.');
form('ec2-rename',async()=>{await ec2('NameInstance',{InstanceIds:[selectedInstance],Name:el('ec2-new-name').value});await refresh();},'Instance name saved.');
el('ec2-search').oninput = () => state && renderInstances();
form('bucket-form',async()=>{const name=el('bucket-name').value;await s3('CreateBucket',{Bucket:name});await refresh();await selectBucket(name);},'Bucket created.');
el('bucket-delete').onclick = () => run(async()=>{if(!confirm('Delete bucket '+selectedBucket+'? It must be empty.'))return;await s3('DeleteBucket',{Bucket:selectedBucket});selectedBucket=null;el('bucket-detail').hidden=true;await refresh();},'Bucket deleted.');
form('object-form',async()=>{
  const file=el('object-file').files[0];if(!file)throw Error('Choose a file.');if(file.size>5*1024*1024)throw Error('Maximum dashboard upload is 5 MiB.');
  const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let offset=0;offset<bytes.length;offset+=32768)binary+=String.fromCharCode(...bytes.subarray(offset,offset+32768));
  await s3('PutObject',{Bucket:selectedBucket,Key:el('object-key').value,ContentBase64:btoa(binary)});el('object-form').reset();await refresh();
},'Object uploaded.');
el('object-file').onchange=()=>{if(!el('object-key').value&&el('object-file').files[0])el('object-key').value=el('object-file').files[0].name;};
form('queue-form',async()=>{const name=el('queue-name').value;await sqs('CreateQueue',{QueueName:name,Attributes:{VisibilityTimeout:String(el('queue-visibility').value)}});await refresh();await selectQueue(name);},'Queue created.');
el('queue-delete').onclick=()=>run(async()=>{if(!confirm('Delete queue '+selectedQueue+' and its messages?'))return;await sqs('DeleteQueue',{QueueUrl:queueUrl});selectedQueue=queueUrl=null;el('queue-detail').hidden=true;await refresh();},'Queue deleted.');
el('queue-purge').onclick=()=>run(async()=>{if(!confirm('Remove every message from '+selectedQueue+'?'))return;await sqs('PurgeQueue',{QueueUrl:queueUrl});el('message-list').replaceChildren();await refresh();},'Queue purged.');
form('queue-settings',async()=>{await sqs('SetQueueAttributes',{QueueUrl:queueUrl,Attributes:{VisibilityTimeout:String(el('queue-detail-visibility').value)}});await refresh();},'Queue settings saved.');
form('message-form',async()=>{await sqs('SendMessage',{QueueUrl:queueUrl,MessageBody:el('message-body').value,DelaySeconds:Number(el('message-delay').value)});el('message-body').value='';await refresh();},'Message sent.');
el('receive-messages').onclick=()=>run(async()=>{
  const result=await sqs('ReceiveMessage',{QueueUrl:queueUrl,MaxNumberOfMessages:10});const root=el('message-list');root.replaceChildren();const source=queueUrl;
  for(const item of result.Messages||[]){const row=document.createElement('div');row.className='message';row.append(text('strong',item.MessageId),text('pre',item.Body),button('Acknowledge / delete',async()=>{await sqs('DeleteMessage',{QueueUrl:source,ReceiptHandle:item.ReceiptHandle});row.remove();await refresh();},'danger'));root.append(row);}
  if(!root.childNodes.length)root.append(text('p','No messages are currently visible.'));await selectQueue(selectedQueue,true);
},'Receive completed. Acknowledge messages before their receipt handles expire.');
form('snapshot-form',async()=>{await api('snapshots',{name:el('snapshot-name').value});await refresh();},'Snapshot saved.');
el('export-state').onclick=()=>run(async()=>saveFile('localcloud-state.json',JSON.stringify(await api('state'),null,2),'application/json'),'State downloaded.');
el('import-state').onchange=()=>run(async()=>{const file=el('import-state').files[0];if(!file)return;if(file.size>8*1024*1024)throw Error('Bundle exceeds 8 MiB.');if(!confirm('Replace current resources and policies with this state bundle?'))return;await api('state/import',JSON.parse(await file.text()));selectedBucket=selectedQueue=selectedInstance=null;await refresh();el('import-state').value='';},'State imported.');
form('policy-form',async()=>{await api('policies',{principal:el('principal').value,policy:JSON.parse(el('policy').value)});await refresh();},'Policy saved.');
for(const [id,source] of [['generate-policy','successful'],['generate-denials','violations']])el(id).onclick=()=>run(async()=>{el('policy').value=JSON.stringify(await api('policy-stream?principal='+encodeURIComponent(el('principal').value)+'&source='+source),null,2);},'Proposal generated. Review before saving.');
el('trace-search').oninput=renderTraces;
el('clear-traces').onclick=()=>run(async()=>{await api('traces/clear',{});await renderTools();},'Traces cleared.');
form('fault-form',async()=>{const code=el('fault-error').value;const rule={name:el('fault-name').value,service:el('fault-service').value,operation:el('fault-operation').value,probability:Number(el('fault-probability').value),latency_ms:Number(el('fault-latency').value)};if(code)rule.error={code,status:code==='ThrottlingException'?429:503};await api('faults',rule);await renderTools();},'Fault rule saved.');
form('execution-form',async()=>{await api('executions',{image:el('execution-image').value,command:JSON.parse(el('execution-command').value)});await refreshExecutions();},'Job submitted.');
setInterval(()=>{if(connected&&!busy&&!document.activeElement.matches('input,textarea,select'))run(refresh,'Resources refreshed.');},15000);
controls();
