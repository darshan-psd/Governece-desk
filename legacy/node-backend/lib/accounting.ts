export function parseTrialBalance(text:string){
 const rows:string[][]=[];let row:string[]=[],cell='',quoted=false;
 for(let i=0;i<text.length;i++){const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){cell+='"';i++}else quoted=!quoted}else if(c===','&&!quoted){row.push(cell);cell=''}else if(c==='\n'&&!quoted){row.push(cell.replace(/\r$/,''));if(row.some(v=>v.trim()))rows.push(row);row=[];cell=''}else cell+=c}
 if(quoted)throw new Error('CSV contains an unfinished quoted field.');row.push(cell.replace(/\r$/,''));if(row.some(v=>v.trim()))rows.push(row);
 const headers=(rows.shift()||[]).map(v=>v.trim().toLowerCase().replace(/^\uFEFF/,''));const ai=headers.indexOf('account'),di=headers.indexOf('debit'),ci=headers.indexOf('credit');
 if([ai,di,ci].some(i=>i<0))throw new Error('CSV must contain account, debit and credit columns.');if(!rows.length||rows.length>2000)throw new Error('Import between 1 and 2,000 account rows.');
 const amount=(v:string,line:number)=>{v=(v||'0').trim();if(!/^\d+(\.\d{1,2})?$/.test(v))throw new Error(`Invalid amount on row ${line}. Use positive amounts with up to two decimals.`);const [whole,decimal='']=v.split('.');const n=Number(whole)*100+Number(decimal.padEnd(2,'0'));if(!Number.isSafeInteger(n)||n>1e13)throw new Error('Amount exceeds supported range.');return n};
 const entries=rows.map((r,i)=>{const account=(r[ai]||'').trim();if(!account)throw new Error(`Account is missing on row ${i+2}.`);return {account,debit:amount(r[di],i+2),credit:amount(r[ci],i+2)}});
 const debit=entries.reduce((sum,r)=>sum+r.debit,0),credit=entries.reduce((sum,r)=>sum+r.credit,0);
 if(!Number.isSafeInteger(debit)||!Number.isSafeInteger(credit))throw new Error('Total exceeds supported range.');
 return {entries,debit,credit,difference:debit-credit,balanced:debit===credit};
}
