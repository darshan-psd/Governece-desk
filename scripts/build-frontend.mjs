// Builds the React app (source: frontend/) into .sites-runtime/frontend, the generated build the FastAPI
// backend serves. frontend/ is source code; .sites-runtime/frontend/ is build output. Never edit the latter. Node is only used here as the frontend build tool.
import {readFileSync,writeFileSync,mkdirSync,copyFileSync,realpathSync,existsSync,readdirSync,rmSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),out=resolve('.sites-runtime/frontend'),watch=process.argv.includes('--watch');
mkdirSync(out,{recursive:true});
const lucide=dirname(realpathSync(require.resolve('lucide-react/package.json')));
// Every lucide icon imported anywhere in frontend/app (the alias below maps 'lucide-react' to just these).
const sources=readdirSync('frontend/app',{recursive:true}).filter(f=>/\.tsx?$/.test(f)).map(f=>readFileSync(resolve('frontend/app',f),'utf8'));
const names=[...new Set(sources.flatMap(src=>[...src.matchAll(/import \{([^}]+)\} from 'lucide-react'/g)].flatMap(m=>m[1].split(',').map(n=>n.trim()).filter(Boolean))))];
const aliases={History:'rotate-ccw-clock',AlertCircle:'circle-alert',CheckCircle2:'circle-check',Filter:'funnel',MoreHorizontal:'ellipsis',Loader2:'loader-circle'};
writeFileSync(resolve(out,'icons.ts'),names.map(n=>{n=n.trim();const f=aliases[n]||n.replace(/([a-z])([A-Z0-9])/g,'$1-$2').toLowerCase();return `export {default as ${n}} from ${JSON.stringify(resolve(lucide,'dist/esm/icons',f+'.mjs'))};`}).join('\n'));
// esbuild 0.28.0 is installed by pnpm as a platform-specific package.
const platform=`${process.platform}-${process.arch}`;
const esbuild=resolve(`node_modules/.pnpm/@esbuild+${platform}@0.28.0/node_modules/@esbuild/${platform}/bin/esbuild`);
if(!existsSync(esbuild))throw new Error('esbuild binary not found at '+esbuild+'. Run pnpm install first.');
copyFileSync('frontend/app/globals.css',resolve(out,'style.css'));
// PDF.js renders in a worker, served same-origin at /pdf.worker.js (see backend app/api/frontend.py).
copyFileSync(resolve(dirname(realpathSync(require.resolve('pdfjs-dist/package.json'))),'build/pdf.worker.min.mjs'),resolve(out,'pdf.worker.js'));
// Code-split chunks (the document review workspace and its PDF/spreadsheet libraries) load on demand.
rmSync(resolve(out,'chunks'),{recursive:true,force:true});
writeFileSync(resolve(out,'index.html'),'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Governance Desk | Your workspace</title><link rel="stylesheet" href="/style.css"></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
execFileSync(esbuild,['app=frontend/local/entry.tsx','--bundle','--format=esm','--splitting','--chunk-names=chunks/[name]-[hash]','--minify','--jsx=automatic','--define:process.env.NODE_ENV="production"','--alias:lucide-react='+resolve(out,'icons.ts'),'--outdir='+out,...(watch?['--watch']:[])],{stdio:'inherit'});
console.log('React frontend built in .sites-runtime/frontend. Start the API with: cd backend && .venv/bin/python -m app');
