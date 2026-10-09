import {context,readState,mutate,exportData} from '@/lib/phase-service';
import {originCheck} from '@/lib/server';
export function errorResponse(error:unknown){const message=error instanceof Error?error.message:'The request could not be completed.';const match=/^(403|404|409):/.exec(message);return Response.json({error:message.replace(/^(403|404|409):/,'')},{status:match?Number(match[1]):400})}
export async function GET(request:Request){try{if(new URL(request.url).searchParams.has('export'))return await exportData(request);return Response.json(await readState(request),{headers:{'Cache-Control':'no-store'}})}catch(e){return errorResponse(e)}}
export async function POST(request:Request){try{originCheck(request);return Response.json(await mutate(request,await request.json()))}catch(e){return errorResponse(e)}}
