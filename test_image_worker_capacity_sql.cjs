const assert=require('node:assert/strict');

module.exports=async function testDefaultImageCapacity(db){
  const w='c2500000-0000-4000-8000-000000000001';
  const actor='c2500000-0000-4000-8000-000000000002';
  await db.exec('reset role; begin');
  try{
    await db.exec("select set_config('request.jwt.claim.sub','',true)");
    await db.query(`insert into auth.users(id) values ($1)`,[actor]);
    await db.query(`insert into workspaces(id,name,owner_email,invite_hash) values ($1,'capacity','capacity@example.test','unused')`,[w]);
    await db.query(`insert into members(workspace_id,user_id,name,role) values ($1,$2,'capacity','admin')`,[w,actor]);
    const settings=(await db.query('select max_workers from ai_generation_settings where workspace_id=$1',[w])).rows[0];
    assert.equal(settings.max_workers,16,'new workspace receives the new default');
    const {rows:jobs}=await db.query(`insert into ai_generation_jobs(workspace_id,user_id,plan_id,deck_id,plan_snapshot)
      values ($1,$2,'capacity-plan-a','capacity-deck-a','{}'),($1,$2,'capacity-plan-b','capacity-deck-b','{}') returning id,config`,[w,actor]);
    assert.deepEqual(jobs.map(x=>x.config.workers),[16,16]);
    for(const {id} of jobs)await db.query(`insert into ai_generation_pages(job_id,workspace_id,position,storage_path)
      select $1,$2,p,($2::uuid)::text||'/capacity/'||p||'.png' from generate_series(1,10) p`,[id,w]);
    const claim=async (worker,limit)=>{
      const query=limit===undefined?'select ai_generation_claim_pages($1,$2,$3) result':'select ai_generation_claim_pages($1,$2,$3,$4) result';
      return (await db.query(query,limit===undefined?[w,actor,worker]:[w,actor,worker,limit])).rows[0].result;
    };
    const first=await claim('node-a');
    assert.equal(first.length,16,'default claim uses all 16 shared slots');
    assert.equal(new Set(first.map(row=>row.jobId)).size,2,'shared cap spans simultaneous suites');
    assert.equal(new Set(first.map(row=>row.pageId)).size,16);
    assert.deepEqual(await claim('node-b',32),[],'second worker cannot exceed shared cap');
    await db.query(`update ai_generation_pages set lease_expires_at=now()-interval '1 second' where id=$1`,[first[0].pageId]);
    assert.equal((await claim('node-b',32)).length,1,'only the released slot is available');
    await db.query(`select ai_generation_set_workers($1,$2,4)`,[w,actor]);
    assert.deepEqual(await claim('node-c',32),[],'explicit lower cap stays effective');
    console.log('PASS shared 16-page default across two suites and worker nodes');
  }finally{await db.exec('rollback');}
};
