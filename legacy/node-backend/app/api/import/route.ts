import {database,bucket,identity,originCheck,getRecord,auditStatement,failure} from '@/lib/server';
import {parseTrialBalance} from '@/lib/accounting';
export async function POST(request:Request){try{originCheck(request);const owner=identity(request);const form=await request.formData();const file=form.get('file');const company=String(form.get('company')||'');const period=String(form.get('period')||'');const currency=String(form.get('currency')||'');
 if(!(file instanceof File)||file.size>1024*1024||!file.name.toLowerCase().endsWith('.csv'))throw new Error('Upload a CSV file smaller than 1 MB.');
 if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(period)||!['INR','GBP','USD','SGD','EUR','AUD'].includes(currency))throw new Error('Select a valid period and currency.');
 if((await getRecord(owner,company))?.kind!=='company')throw new Error('Company not found.');
 const parsed=parseTrialBalance(await file.text());if(!parsed.balanced)throw new Error(`Trial balance is out of balance by ${(parsed.difference/100).toFixed(2)} ${currency}. Correct the file and import again.`);
 const id=crypto.randomUUID(),key=owner+'/'+id;await bucket().put(key,await file.arrayBuffer());
 try{await database().batch([database().prepare('INSERT INTO records (owner,id,kind,data,revision,updated) VALUES (?,?,?,?,1,?)').bind(owner,id,'trialbalance',JSON.stringify({company,title:file.name,period,currency,...parsed,key,status:'Imported — review required'}),new Date().toISOString()),auditStatement(owner,'Imported trial balance',file.name+' · '+period)]);}catch(e){await bucket().delete(key);throw e;}
 return Response.json({ok:true});}catch(e){return failure(e)}}
