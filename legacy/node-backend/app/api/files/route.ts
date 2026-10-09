import {database,bucket,identity,originCheck,getRecord,auditStatement,failure} from '@/lib/server';
export async function POST(request:Request){try{
 originCheck(request);const owner=identity(request);const form=await request.formData();const file=form.get('file');const company=String(form.get('company')||'');
 if(!(file instanceof File)||!file.size)throw new Error('Choose a file to upload.');if(file.size>10*1024*1024)throw new Error('The maximum file size is 10 MB.');
 if(!/\.(pdf|csv|txt|xlsx|docx)$/i.test(file.name))throw new Error('Use a PDF, CSV, TXT, XLSX or DOCX file.');
 if((await getRecord(owner,company))?.kind!=='company')throw new Error('Choose a valid company.');
 const id=crypto.randomUUID();const key=owner+'/'+id;const bytes=await file.arrayBuffer();const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(n=>n.toString(16).padStart(2,'0')).join('');
 await bucket().put(key,bytes,{httpMetadata:{contentType:'application/octet-stream'}});
 try{await database().batch([database().prepare('INSERT INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,1,?)').bind(owner,id,'document',JSON.stringify({name:file.name,company,size:file.size,key,hash,version:1}),new Date().toISOString()),auditStatement(owner,'Uploaded document',file.name)]);}catch(e){await bucket().delete(key);throw e;}
 return Response.json({ok:true,id});
 }catch(e){return failure(e)}}
export async function GET(request:Request){try{const owner=identity(request);const id=new URL(request.url).searchParams.get('id')||'';const doc=await getRecord(owner,id);if(doc?.kind!=='document')return new Response('Not found',{status:404});const object=await bucket().get(doc.key);if(!object)return new Response('File unavailable',{status:404});return new Response(object.body,{headers:{'Content-Type':'application/octet-stream','Content-Disposition':`attachment; filename*=UTF-8''${encodeURIComponent(doc.name)}`,'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store'}});}catch(e){return failure(e)}}
