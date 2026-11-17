// Exercise frontend cloud transport and aggregation without opening a browser.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const workspace='11111111-1111-4111-8111-111111111111';
 const own={user_id:'alice',name:'Alice',role:'reviewer'};
 const members=[own,{user_id:'bob',name:'Bob',role:'reviewer'}];
 const records=Array.from({length:1101},(_,i)=>({user_id:'bob',deck_id:'other-'+i,data:{scores:{visual:3,layout:4}}}));
 records.push({user_id:'alice',deck_id:'deck',data:{scores:{visual:4,layout:5},note:'a'}},{user_id:'bob',deck_id:'deck',data:{scores:{visual:2},note:'partial'}});
 const settings={revision:0,configRevision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉效果'},{id:'layout',name:'排版'}],reviews:{}};
 const dataset={members,reviews:records,decks:[{id:'deck',data:{id:'deck',title:'test',images:[{id:'img',path:workspace+'/deck/image.jpg'}]}}]};
 const uploads=[];
 const client={auth:{getSession:async()=>({data:{session:{user:{id:'alice'}}}})},rpc:async(name,args)=>{if(name==='add_review_deck'){dataset.decks.push({id:args.deck.id,data:args.deck});return {data:null};}return {data:name==='get_review_state'?structuredClone(settings):{revision:args.incoming.revision+1,configRevision:0}};},storage:{from:()=>({upload:async(path,bytes)=>{uploads.push({path,size:bytes.length});return {data:{path}};},createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'https://signed.example/'+path}))})})},from(table){let filters=[],begin=0,end=Infinity;const q={select(){return q;},eq(k,v){filters.push([k,v]);return q;},order(){return q;},range(a,b){begin=a;end=b;return q;},maybeSingle:async()=>({data:own}),then(resolve,reject){const rows=dataset[table].filter(r=>filters.every(([k,v])=>k==='workspace_id'||r[k]===v)).slice(begin,end+1);return Promise.resolve({data:structuredClone(rows)}).then(resolve,reject);}};return q;}};
 const context={window:{CLOUD_CONFIG:{url:'https://example.supabase.co',anonKey:'test',workspaceId:workspace},supabase:{createClient:()=>client}},structuredClone,Map,Set,Uint8Array,URL,crypto:require('node:crypto').webcrypto,atob,location:{href:'https://site.example/review/'}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('public/cloud.js','utf8'),context);
 const cloud=context.window.Cloud;assert.equal(await cloud.ready(),true);
 const library=await cloud.request('/api/library');assert.equal(library.decks.length,1);assert.equal(cloud.team.reviews.length,1103);assert.ok(library.decks[0].images[0].url.startsWith('https://signed.example/'));
 assert.equal((await cloud.request('/api/state',{body:JSON.stringify(settings)})).revision,1);
 const imported=await cloud.request('/api/import',{body:JSON.stringify({name:'reviewer import',images:[{name:'test.png',data:'aGVsbG8='}]})});
 assert.equal(imported.added,1);assert.equal(imported.created.length,1);assert.equal(imported.decks.length,2);assert.equal(uploads.length,1);assert.ok(uploads[0].path.startsWith(workspace+'/'));
 Object.assign(context,{document:{addEventListener(){}},Cloud:cloud,decks:library.decks,state:settings,hasScore:n=>typeof n==='number'&&Number.isFinite(n)});
 vm.runInContext(fs.readFileSync('public/scoring.js','utf8'),context);context.Scoring=context.window.Scoring;
 vm.runInContext(fs.readFileSync('public/team.js','utf8'),context);
 let rows=context.teamRows();assert.equal(rows[0].mean,4.5);assert.equal(rows[0].records.length,1);
 records.at(-1).data.scores.layout=3;await cloud.refreshTeam();rows=context.teamRows();assert.equal(rows[0].mean,3.5);assert.equal(rows[0].records.length,2);
 records.at(-1).data={mode:'page',scores:{},pages:{img:{scores:{visual:2,layout:3}}}};await cloud.refreshTeam();rows=context.teamRows();assert.equal(rows[0].mean,3.5);assert.equal(rows[0].records.length,2);
 delete records.at(-1).data.pages.img.scores.layout;await cloud.refreshTeam();assert.equal(context.teamRows()[0].records.length,1);
 console.log('PASS: cloud transport, >1000 team records, signed private images, isolated save, ordinary member image import, complete-only team averages');
})().catch(e=>{console.error(e);process.exit(1);});
