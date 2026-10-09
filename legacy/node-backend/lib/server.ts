import { env } from 'cloudflare:workers';
export function database(){if(!env.DB)throw new Error('Workspace storage is temporarily unavailable.');return env.DB;}
export function bucket(){if(!env.BUCKET)throw new Error('Document storage is temporarily unavailable.');return env.BUCKET;}
export function identity(request:Request){
 const id=request.headers.get('oai-authenticated-user-id');
 if(id)return id;
 if(process.env.NODE_ENV==='development')return 'local-preview';
 throw new Error('Please sign in to access your workspace.');
}
export function originCheck(request:Request){const origin=request.headers.get('origin');if(origin&&origin!==new URL(request.url).origin)throw new Error('Request origin is not permitted.');}
export function auditStatement(owner:string,action:string,subject:string,conditional=false){return database().prepare(`INSERT INTO audit (id,owner,action,subject,at) SELECT ?,?,?,?,? ${conditional?'WHERE changes() = 1':''}`).bind(crypto.randomUUID(),owner,action,subject,new Date().toISOString());}
export async function getRecord(owner:string,id:string){const row=await database().prepare('SELECT * FROM records WHERE owner = ? AND id = ?').bind(owner,id).first<any>();return row?{...JSON.parse(row.data),id:row.id,kind:row.kind,revision:row.revision,updated:row.updated}:null;}
export function failure(error:unknown){const message=error instanceof Error?error.message:'Unable to save your changes.';console.error(message);return Response.json({error:message},{status:message.includes('sign in')?401:400});}
