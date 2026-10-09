import {portalState,portalMutate,portalExport} from '@/lib/portal-service';
import {originCheck} from '@/lib/server';
import {errorResponse} from '../v2/route';
export async function GET(request:Request){try{return new URL(request.url).searchParams.has('export')?await portalExport(request):Response.json(await portalState(request),{headers:{'Cache-Control':'no-store'}})}catch(e){return errorResponse(e)}}
export async function POST(request:Request){try{originCheck(request);return Response.json(await portalMutate(request,await request.json()))}catch(e){return errorResponse(e)}}
