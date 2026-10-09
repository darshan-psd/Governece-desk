export type Item = {id:string;kind:string;revision:number;updated:string;[key:string]:any};
export const statuses=['Not started','In progress','Awaiting information','Under review','Approved','Completed'];
export const transitions:Record<string,string[]>={
 'Not started':['In progress'],'In progress':['Awaiting information','Under review'],
 'Awaiting information':['In progress'],'Under review':['In progress','Approved'],
 'Approved':['In progress','Completed'],'Completed':['In progress']
};
export function transitionError(item:Item,next:string,hasEvidence:boolean){
 if(!transitions[item.status]?.includes(next))return 'This status change is not allowed. Refresh and try again.';
 if(['Approved','Completed'].includes(next)&&!hasEvidence)return 'Attach a supporting document before approval or completion.';
 if(next==='Completed'&&item.filing&&!item.acknowledgement)return 'Record the submission acknowledgement before completing this filing.';
 return null;
}
export function validate(kind:string,input:any){
 const clean:Record<string,any>={};
 const fields:Record<string,string[]>={company:['name','jurisdiction','registration','yearEnd','contact'],task:['title','company','due','owner','category','priority','notes','source','acknowledgement'],meeting:['title','company','due','owner','notes'],close:['title','company','due','owner','notes']};
 if(!fields[kind])throw new Error('Unsupported record type.');
 for(const key of fields[kind])clean[key]=String(input[key]??'').trim().slice(0,key==='notes'?5000:500);
 if(!(kind==='company'?clean.name:clean.title))throw new Error('Enter a name or title.');
 if(kind!=='company'){
  if(!clean.company)throw new Error('Choose a company.');
  if(!/^\d{4}-\d{2}-\d{2}$/.test(clean.due)||new Date(clean.due+'T00:00:00Z').toISOString().slice(0,10)!==clean.due)throw new Error('Enter a valid due date.');
  clean.status='Not started';clean.evidence=[];clean.filing=!!input.filing;
 }
 return clean;
}
export function sampleRecords(){
 const date=(offset:number)=>{const d=new Date();d.setDate(d.getDate()+offset);return d.toISOString().slice(0,10)};
 const companies=[['c1','Northstar Technologies','India','DEMO-001','31 March','Priya Sharma'],['c2','Meridian Holdings','United Kingdom','DEMO-002','31 December','Daniel Reed'],['c3','Atlas Advisory','Singapore','DEMO-003','31 December','Mei Tan']].map(([id,name,jurisdiction,registration,yearEnd,contact])=>({id,kind:'company',name,jurisdiction,registration,yearEnd,contact,sample:true}));
 const tasks=[['t1','Annual accounts review','c1',-2,'Priya Sharma','Accounting','High','In progress'],['t2','Prepare quarterly board pack','c2',2,'Daniel Reed','Board','High','Under review'],['t3','Confirm director particulars','c3',4,'Mei Tan','Corporate records','Normal','Awaiting information'],['t4','Review registration renewal','c1',7,'Priya Sharma','Compliance','Normal','Not started'],['t5','Update ownership register','c2',10,'Daniel Reed','Corporate records','Normal','In progress'],['t6','Approve annual meeting agenda','c3',12,'Mei Tan','Board','Normal','Not started']].map(([id,title,company,offset,owner,category,priority,status])=>({id,kind:'task',title,company,due:date(Number(offset)),owner,category,priority,status,evidence:[],notes:'Illustrative workflow. Confirm applicability and dates before use.',source:'Sample obligation — not a statutory deadline',sample:true}));
 return [...companies,...tasks,{id:'m1',kind:'meeting',title:'Quarterly board meeting',company:'c2',due:date(8),owner:'Daniel Reed',status:'Not started',notes:'Agenda: financial performance; compliance update; actions from previous meeting.',evidence:[],sample:true},{id:'cl1',kind:'close',title:'September financial close',company:'c1',due:date(5),owner:'Priya Sharma',status:'In progress',notes:'Bank reconciliation, supporting schedules and management review.',evidence:[],sample:true}];
}
