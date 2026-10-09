import assert from 'node:assert/strict';
const base='http://127.0.0.1:8787';const owner='test-'+Date.now();
async function call(path,body,who=owner){const headers={'oai-authenticated-user-id':who,'oai-authenticated-user-email':'test@example.invalid'};if(body&&!(body instanceof FormData))headers['Content-Type']='application/json';const r=await fetch(base+path,{method:body?'POST':'GET',headers,body:body?(body instanceof FormData?body:JSON.stringify(body)):undefined});const d=await r.json();return {status:r.status,data:d}}
const company=(await call('/api/workspace',{action:'create',kind:'company',data:{name:'Integration test'}})).data.id;assert.ok(company);
const task=(await call('/api/workspace',{action:'create',kind:'task',data:{title:'Evidence workflow test',company,due:'2026-12-01'}})).data.id;assert.ok(task);
const fresh=async()=>{const r=await call('/api/workspace');return r.data.records.find(x=>x.id===task)};
let t=await fresh();assert.equal((await call('/api/workspace',{action:'transition',id:task,revision:t.revision,status:'Completed'})).status,400);
for(const status of ['In progress','Under review']){t=await fresh();assert.equal((await call('/api/workspace',{action:'transition',id:task,revision:t.revision,status})).status,200)}
t=await fresh();assert.equal((await call('/api/workspace',{action:'transition',id:task,revision:t.revision,status:'Approved'})).status,400);
const form=new FormData();form.set('company',company);form.set('file',new Blob(['Test evidence'],{type:'text/plain'}),'evidence.txt');const doc=(await call('/api/files',form)).data.id;assert.ok(doc);
assert.equal((await call('/api/workspace',{action:'edit',id:task,revision:t.revision,data:{...t,evidence:[doc]}})).status,200);
assert.equal((await call('/api/workspace',{action:'edit',id:task,revision:t.revision,data:t})).status,409);
for(const status of ['Approved','Completed']){t=await fresh();assert.equal((await call('/api/workspace',{action:'transition',id:task,revision:t.revision,status})).status,200)}
t=await fresh();assert.equal(t.status,'Completed');assert.ok(t.approval);
assert.equal((await call('/api/workspace',{action:'edit',id:task,revision:t.revision,data:{...t,title:'Should be blocked'}})).status,400);
assert.equal((await call('/api/workspace',undefined,owner+'-other')).data.records.length,0);
const read=await fetch(base+'/api/files?id='+doc,{headers:{'oai-authenticated-user-id':owner,'oai-authenticated-user-email':'test@example.invalid'}});assert.equal(await read.text(),'Test evidence');
const denied=await fetch(base+'/api/files?id='+doc,{headers:{'oai-authenticated-user-id':owner+'-other','oai-authenticated-user-email':'test@example.invalid'}});assert.equal(denied.status,404);
const csv=new FormData();csv.set('company',company);csv.set('period','2026-09');csv.set('currency','INR');csv.set('file',new Blob(['account,debit,credit\nCash,100,0\nCapital,0,100']),'tb.csv');assert.equal((await call('/api/import',csv)).status,200);
assert.ok((await call('/api/workspace')).data.audit.some(x=>x.action==='Status: Completed'));
console.log('Integration passed: saved CRUD, transitions, evidence, upload/download, stale update, approved lock, account isolation, accounting import and audit.');
