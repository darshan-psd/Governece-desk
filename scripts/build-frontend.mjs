// Builds the React app (source: frontend/) into .sites-runtime/frontend, the generated build the FastAPI
// backend serves. frontend/ is source code; .sites-runtime/frontend/ is build output. Never edit the latter. Node is only used here as the frontend build tool.
import {readFileSync,writeFileSync,mkdirSync,copyFileSync,realpathSync,existsSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const require=createRequire(import.meta.url),out=resolve('.sites-runtime/frontend'),watch=process.argv.includes('--watch');
mkdirSync(out,{recursive:true});
const lucide=dirname(realpathSync(require.resolve('lucide-react/package.json')));
const names=/import \{([^}]+)\} from 'lucide-react'/.exec(readFileSync('frontend/app/workspace-next.tsx','utf8'))[1].split(',');
const aliases={History:'rotate-ccw-clock',AlertCircle:'circle-alert',CheckCircle2:'circle-check',Filter:'funnel',MoreHorizontal:'ellipsis',Loader2:'loader-circle'};
writeFileSync(resolve(out,'icons.ts'),names.map(n=>{n=n.trim();const f=aliases[n]||n.replace(/([a-z])([A-Z0-9])/g,'$1-$2').toLowerCase();return `export {default as ${n}} from ${JSON.stringify(resolve(lucide,'dist/esm/icons',f+'.mjs'))};`}).join('\n'));
// esbuild 0.28.0 is installed by pnpm as a platform-specific package.
const platform=`${process.platform}-${process.arch}`;
const esbuild=resolve(`node_modules/.pnpm/@esbuild+${platform}@0.28.0/node_modules/@esbuild/${platform}/bin/esbuild`);
if(!existsSync(esbuild))throw new Error('esbuild binary not found at '+esbuild+'. Run pnpm install first.');
copyFileSync('frontend/app/globals.css',resolve(out,'style.css'));
writeFileSync(resolve(out,'index.html'),'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Governance Desk | Your workspace</title><link rel="stylesheet" href="/style.css"></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>');
execFileSync(esbuild,['frontend/local/entry.tsx','--bundle','--format=esm','--minify','--jsx=automatic','--define:process.env.NODE_ENV="production"','--alias:lucide-react='+resolve(out,'icons.ts'),'--outfile='+resolve(out,'app.js'),...(watch?['--watch']:[])],{stdio:'inherit'});
console.log('React frontend built in .sites-runtime/frontend. Start the API with: cd backend && .venv/bin/python -m app');
