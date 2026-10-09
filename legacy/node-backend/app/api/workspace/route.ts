import {database,identity,originCheck,auditStatement,getRecord,failure} from '@/lib/server';
import {sampleRecords,validate,transitionError} from '@/lib/domain';
export async function GET(request:Request){try{const owner=identity(request);const db=database();const [rows,events]=await Promise.all([db.prepare('SELECT * FROM records WHERE owner = ? ORDER BY updated DESC').bind(owner).all<any>(),db.prepare('SELECT * FROM audit WHERE owner = ? ORDER BY at DESC LIMIT 100').bind(owner).all()]);return Response.json({records:rows.results.map(r=>({...JSON.parse(r.data),id:r.id,kind:r.kind,revision:r.revision,updated:r.updated})),audit:events.results,user:request.headers.get('oai-authenticated-user-email')||'Local preview'},{headers:{'Cache-Control':'no-store'}});}catch(e){return failure(e)}}
export async function POST(request:Request){try{
 originCheck(request);const owner=identity(request);const db=database();const body:any=await request.json();
 if(body.action==='seed'){
 const existing=await db.prepare('SELECT id FROM records WHERE owner = ? LIMIT 1').bind(owner).first();if(existing)return Response.json({ok:true});
 await db.batch([...sampleRecords().map(r=>db.prepare('INSERT OR IGNORE INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,1,?)').bind(owner,r.id,r.kind,JSON.stringify(r),new Date().toISOString())),auditStatement(owner,'Loaded illustrative workspace','Sample companies and workflows')]);return Response.json({ok:true});
 }
 if(body.action==='create'){
 const data=validate(body.kind,body.data);
 if(body.kind!=='company'){const company=await getRecord(owner,data.company);if(!company||company.kind!=='company')throw new Error('Company not found.');}
 const id=crypto.randomUUID();await db.batch([db.prepare('INSERT INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,1,?)').bind(owner,id,body.kind,JSON.stringify(data),new Date().toISOString()),auditStatement(owner,'Created '+body.kind,data.title||data.name)]);return Response.json({ok:true,id});
 }
 const item=await getRecord(owner,body.id);if(!item)throw new Error('Record not found.');if(item.revision!==body.revision)return Response.json({error:'This record changed. Close and reopen it to load the latest version.'},{status:409});
 let next={...item};let action='Updated';
 if(body.action==='transition'){
 let validEvidence=false;for(const id of item.evidence||[]){const doc=await getRecord(owner,id);if(doc?.kind==='document'&&doc.company===item.company)validEvidence=true;}
 const error=transitionError(item,body.status,validEvidence);if(error)throw new Error(error);
 next.status=body.status;action='Status: '+body.status;
 if(body.status==='Approved')next.approval={at:new Date().toISOString(),by:request.headers.get('oai-authenticated-user-email')||owner,evidence:[...item.evidence],revision:item.revision};
 if(body.status==='In progress')next.approval=null;
 }else if(body.action==='edit'){
 if(item.kind==='company'){next={...item,...validate('company',body.data)}}else{
 if(['Approved','Completed'].includes(item.status))throw new Error('Reopen this record before changing it.');
 const validated=validate(item.kind,{...item,...body.data});const company=await getRecord(owner,validated.company);if(company?.kind!=='company')throw new Error('Company not found.');
 const evidence=Array.isArray(body.data.evidence)?body.data.evidence:item.evidence||[];
 for(const id of evidence){const doc=await getRecord(owner,id);if(doc?.kind!=='document'||doc.company!==validated.company)throw new Error('Supporting documents must belong to this company.');}
 next={...item,...validated,status:item.status,evidence};
 }
 }else throw new Error('Unknown action.');
 const result=await db.batch([db.prepare('UPDATE records SET data = ?, revision = revision + 1, updated = ? WHERE owner = ? AND id = ? AND revision = ?').bind(JSON.stringify(next),new Date().toISOString(),owner,item.id,item.revision),auditStatement(owner,action,item.title||item.name,true)]);
 if(!result[0].meta.changes)return Response.json({error:'Another update was saved first. Refresh this record.'},{status:409});
 return Response.json({ok:true});
 }catch(e){return failure(e)}}
