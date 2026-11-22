-- SINGLE_JUDGE_SUBMISSION_M24
-- Install only. This migration expands submission rights without changing read access.
begin;

alter table public.decks add column if not exists ai_allowed boolean;
update public.decks set ai_allowed=true where ai_allowed is null;
alter table public.decks alter column ai_allowed set default true;
alter table public.decks alter column ai_allowed set not null;

-- The trusted M19 importer inserts the deck before the new API can update it.
-- A transaction-local setting lets the publish API carry the explicit choice
-- through that insert, so the existing M05 enqueue trigger sees the real value.
create or replace function guanye_internal.single_judge_apply_ai_allowed() returns trigger
language plpgsql security definer set search_path='' as $$
declare configured text;
begin
 configured=nullif(current_setting('guanye.single_judge_ai_allowed',true),'');
 if configured is not null then new.ai_allowed=configured::boolean; end if;
 if new.ai_allowed is null then new.ai_allowed=true; end if;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_apply_ai_allowed() from public,anon,authenticated,service_role;
drop trigger if exists single_judge_apply_ai_allowed_before_insert on public.decks;
create trigger single_judge_apply_ai_allowed_before_insert before insert on public.decks
for each row execute function guanye_internal.single_judge_apply_ai_allowed();

-- Every existing members row is authorized to submit. Only the two configured
-- accounts retain their existing judge/admin roles; everyone else is a submitter.
create or replace function public.single_judge_access(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('mode','single-judge','role',
  case when ws.dedicated_judge_id=auth.uid() then 'judge'
       when ws.single_judge_admin_id=auth.uid() then 'admin'
       else 'submitter' end)
 from public.workspaces ws
 join public.members m on m.workspace_id=ws.id and m.user_id=auth.uid()
 where ws.id=w and ws.single_judge_admin_id is not null
 limit 1
$$;
revoke all on function public.single_judge_access(uuid) from public,anon,authenticated;
grant execute on function public.single_judge_access(uuid) to authenticated;

-- Last-line queue boundary: even a direct service-role prepare/backfill call
-- cannot create a claimable AI job for a manual-only deck.
create or replace function guanye_internal.single_judge_manual_only_job_guard() returns trigger
language plpgsql security definer set search_path='' as $$
declare allowed_value boolean;
begin
 select d.ai_allowed into allowed_value
 from public.decks d
 where d.workspace_id=new.workspace_id and d.id=new.deck_id;
 if not found then raise exception '预评作品不存在' using errcode='22023'; end if;
 if not allowed_value then
  raise exception '该作品已选择仅人工评分，不能加入 AI 队列' using errcode='42501';
 end if;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_manual_only_job_guard() from public,anon,authenticated,service_role;
drop trigger if exists single_judge_manual_only_job_guard on public.ai_review_jobs;
create trigger single_judge_manual_only_job_guard before insert on public.ai_review_jobs
for each row execute function guanye_internal.single_judge_manual_only_job_guard();

create or replace function guanye_internal.single_judge_manual_only_decision_guard() returns trigger
language plpgsql security definer set search_path='' as $$
declare allowed_value boolean;
begin
 -- SINGLE_JUDGE_DECISION_GUARD_M24
 select d.ai_allowed into allowed_value
 from public.decks d
 where d.workspace_id=new.workspace_id and d.id=new.deck_id;
 if not found then raise exception '纠偏作品不存在' using errcode='22023'; end if;
 if not allowed_value and (new.ai_record_id is not null or new.status<>'confirmed') then
  raise exception '仅人工作品只能保存无 AI 关联的 confirmed 正式评分' using errcode='42501';
 end if;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_manual_only_decision_guard() from public,anon,authenticated,service_role;
drop trigger if exists single_judge_manual_only_decision_guard on public.ai_review_decisions;
create trigger single_judge_manual_only_decision_guard before insert on public.ai_review_decisions
for each row execute function guanye_internal.single_judge_manual_only_decision_guard();

create or replace function public.single_judge_publish_upload(
 w uuid,
 actor uuid,
 p_deck jsonb,
 p_original jsonb,
 p_ai_allowed boolean
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype; previous guanye_internal.single_judge_originals%rowtype;
 target text; original_file text; digest text; extension text; address text; size bigint; version text;
 item jsonb; expected_position integer=0; seen text[]='{}'; image_id text; image_path text; thumb_path text;
 queued jsonb=null; canonical_deck jsonb;
begin
 -- SINGLE_JUDGE_SUBMISSION_PUBLISH_M24
 select * into ws from public.workspaces where id=w for update;
 if not found or ws.single_judge_admin_id is null or ws.dedicated_judge_id is null then
  raise exception '单评委工作区未启用' using errcode='42501';
 end if;
 if actor is null or p_ai_allowed is null then
  raise exception '投递人和 AI 授权必须是明确值' using errcode='22023';
 end if;
 perform 1 from public.members m
  where m.workspace_id=w and m.user_id=actor
  for share;
 if not found then raise exception '投递人不是该工作区现有成员' using errcode='42501'; end if;
 if not exists(select 1 from public.members m where m.workspace_id=w and m.user_id=ws.dedicated_judge_id) then
  raise exception '指定评分师已不是该工作区现有成员' using errcode='22023';
 end if;
 if jsonb_typeof(p_deck) is distinct from 'object' or jsonb_typeof(p_original) is distinct from 'object' then
  raise exception '素材清单无效' using errcode='22023';
 end if;
 target=p_deck->>'id'; original_file=p_original->>'name'; digest=p_original->>'sha256';
 extension=lower(substring(original_file from '\.([a-zA-Z0-9]+)$'));
 if target is null or target !~ '^import-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
    or original_file is null or length(original_file) not between 1 and 255 or original_file ~ '[[:cntrl:]/]'
    or strpos(original_file,chr(92))>0 or extension is null or extension not in ('ppt','pptx','pdf')
    or digest is null or digest !~ '^[0-9a-f]{64}$' or coalesce(p_original->>'sizeBytes','') !~ '^[1-9][0-9]{0,7}$' then
  raise exception '原始文件标识、格式、哈希或大小无效' using errcode='22023';
 end if;
 size=(p_original->>'sizeBytes')::bigint;
 if size>26214400 then raise exception '原始文件超过 25 MiB' using errcode='22023'; end if;
 address=w::text||'/'||target||'/original/'||digest||'.'||extension;
 if address is distinct from p_original->>'path' then raise exception '原始文件路径无效' using errcode='22023'; end if;
 if not exists(select 1 from storage.objects where bucket_id='ppt-library' and name=address) then
  raise exception '原始文件尚未同步到私有存储' using errcode='22023';
 end if;
 if jsonb_typeof(p_deck->'images') is distinct from 'array' then raise exception '预览清单无效' using errcode='22023'; end if;
 if jsonb_array_length(p_deck->'images') not between 1 and 600 then raise exception '预览页数无效' using errcode='22023'; end if;
 for item in select value from jsonb_array_elements(p_deck->'images') loop
  expected_position=expected_position+1;
  image_id=item->>'id'; image_path=item->>'path'; thumb_path=item->>'thumbPath';
  if jsonb_typeof(item) is distinct from 'object' or image_id is null or image_id !~ '^[0-9a-zA-Z_-]{1,100}$'
     or image_id=any(seen) or (item->>'position') is distinct from expected_position::text
     or image_path is distinct from w::text||'/'||target||'/preview/'||expected_position::text||'.png'
     or thumb_path is distinct from image_path then
   raise exception '预览标识、页序或路径无效' using errcode='22023';
  end if;
  if not exists(select 1 from storage.objects where bucket_id='ppt-library' and name=image_path) then
   raise exception '预览第 % 页尚未同步到私有存储',expected_position using errcode='22023';
  end if;
  seen=array_append(seen,image_id);
 end loop;
 canonical_deck=jsonb_build_object('id',target,'title',left(coalesce(nullif(btrim(p_deck->>'title'),''),original_file),100),
  'name',left(coalesce(nullif(btrim(p_deck->>'title'),''),original_file),100),'origin','import','images',p_deck->'images');
 select * into previous from guanye_internal.single_judge_originals where deck_id=target;
 if found then
  if previous.created_by is distinct from actor then
   raise exception '仅原投递人可以重复提交同一素材' using errcode='42501';
  end if;
  if previous.workspace_id is distinct from w or previous.sha256 is distinct from digest or previous.size_bytes is distinct from size
     or previous.original_name is distinct from original_file
     or not exists(select 1 from public.decks where workspace_id=w and id=target and data=canonical_deck
        and ai_allowed=p_ai_allowed) then
   raise exception '导入编号已绑定其他素材或 AI 选择' using errcode='22023';
  end if;
  return jsonb_build_object('ok',true,'deckId',target,'deckVersion',previous.deck_version,'idempotent',true,
   'aiAllowed',p_ai_allowed,'submittedBy',actor,'preEvaluation',null);
 end if;
 perform set_config('guanye.single_judge_ai_allowed',case when p_ai_allowed then 'true' else 'false' end,true);
 perform public.ai_import_review_deck(w,actor,canonical_deck,digest);
 perform set_config('guanye.single_judge_ai_allowed','',true);
 version=public.ai_review_deck_version(w,target);
 insert into guanye_internal.single_judge_originals(deck_id,workspace_id,object_path,original_name,sha256,size_bytes,deck_version,created_by)
 values(target,w,address,original_file,digest,size,version,actor);
 if p_ai_allowed then
  queued=public.ai_review_prepare(w,ws.dedicated_judge_id,jsonb_build_object('deckId',target,'scope','deck'));
 else
  queued=jsonb_build_object('ok',true,'deckId',target,'scope','deck','jobs','[]'::jsonb,
   'skipped',true,'reason','manual-only');
 end if;
 return jsonb_build_object('ok',true,'deckId',target,'deckVersion',version,'idempotent',false,
  'aiAllowed',p_ai_allowed,'submittedBy',actor,'preEvaluation',queued);
end $$;
revoke all on function public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)
 to service_role;


-- M05 keeps its public contract, but a manual-only deck never reaches prepare.
create or replace function public.ai_review_enqueue_preview() returns trigger
language plpgsql security definer set search_path='' as $$
declare judge uuid;
begin
 if not new.ai_allowed then return new; end if;
 if TG_OP='UPDATE' and old.data is not distinct from new.data and old.source_kind=new.source_kind and old.ai_allowed is not distinct from new.ai_allowed then return new; end if;
 if jsonb_typeof(new.data->'images') is distinct from 'array' then return new; end if;
 if not exists(select 1 from jsonb_array_elements(new.data->'images') as p(image)
     where nullif(p.image->>'id','') is not null and left(p.image->>'path',length(new.workspace_id::text)+1)=new.workspace_id::text||'/') then
  return new;
 end if;
 select dedicated_judge_id into judge from public.workspaces where id=new.workspace_id;
 if judge is not null then
  perform public.ai_review_prepare(new.workspace_id,judge,jsonb_build_object('deckId',new.id,'scope','deck'));
 end if;
 return new;
end $$;
revoke all on function public.ai_review_enqueue_preview() from public,anon,authenticated;

-- A stale job cannot become claimable if a deck is ever marked manual-only later.
create or replace function public.ai_review_claim_jobs(w uuid, actor uuid, p_kind text, p_limit integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare jobs_json jsonb;
begin
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以执行预评'; end if;
 update public.ai_review_jobs set status='failed',error='预评执行超时',available_at='infinity',updated_at=now()
 where workspace_id=w and source_kind=p_kind and status='running' and attempts>=3 and updated_at<now()-interval '15 minutes';
 with picked as (
  select j.id from public.ai_review_jobs j
  join public.decks d on d.workspace_id=j.workspace_id and d.id=j.deck_id and d.ai_allowed
  where j.workspace_id=w and j.source_kind=p_kind and j.attempts<3
   and ((j.status in ('queued','failed') and j.available_at<=now())
     or (j.status='running' and j.updated_at<now()-interval '15 minutes'))
  order by j.created_at,j.id for update skip locked limit greatest(1,least(coalesce(p_limit,1),20))
 ), changed as (
  update public.ai_review_jobs j set status='running',judge_user_id=actor,attempts=j.attempts+1,error=null,updated_at=now()
  from picked p where j.id=p.id returning j.*
 )
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'deckId',deck_id,'sourceKind',source_kind,'scope',scope,
  'unitId',unit_id,'position',position,'variant',variant,'bucket',bucket,'split',split,'deckVersion',deck_version,
  'rubricVersion',rubric_version,'maxScore',max_score,'attempts',attempts) order by created_at,id),'[]'::jsonb)
 into jobs_json from changed;
 return jobs_json;
end $$;
revoke all on function public.ai_review_claim_jobs(uuid,uuid,text,integer) from public,anon,authenticated;
grant execute on function public.ai_review_claim_jobs(uuid,uuid,text,integer) to service_role;

-- Formal confirmation remains service-only. A manual-only deck still persists a
-- complete confirmed human decision; only AI record links and corrected examples
-- are forbidden by the guard below.
create or replace function public.ai_review_confirm(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare submission_row public.ai_review_submissions%rowtype; record_row public.ai_review_records%rowtype; deck_row public.decks%rowtype;
 decision_row public.ai_review_decisions%rowtype; decision_key_value text; classification_value jsonb; reasons_value jsonb;
 classification_label text; classification_style text; ai_classification_label text; ai_classification_style text;
 formal_scores_value jsonb; formal_total_value numeric; score_record record; settings_row public.ai_review_settings%rowtype;
 source_value jsonb; reference_value jsonb; threshold_met_value boolean=false; status_value text='confirmed'; weighted_value numeric;
 score_ratio_value numeric=0; ai_score numeric;
begin
 -- FORMAL_CORRECTION_V13
 -- SINGLE_JUDGE_MANUAL_FORMAL_DECISION_M24
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以确认正式评分'; end if;
 if jsonb_typeof(payload) is distinct from 'object' then raise exception '确认内容无效'; end if;
 select * into submission_row from public.ai_review_submissions where workspace_id=w and id=nullif(payload->>'submissionId','')::uuid;
 if submission_row.id is null then raise exception '人工提交不存在'; end if;
 if submission_row.deck_version<>public.ai_review_deck_version(w,submission_row.deck_id) then raise exception '作品版本已变化，请重新提交人工评分'; end if;
 if submission_row.rubric_version<>public.ai_review_rubric_version(w) then raise exception '评分标准已变化，请重新提交人工评分'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=submission_row.deck_id;
 if deck_row.id is null then raise exception '作品不存在'; end if;
 if not deck_row.ai_allowed then
  if payload->>'status'='corrected' then
   raise exception '仅人工作品不能生成 AI 纠偏示例' using errcode='42501';
  end if;
  if nullif(payload->>'aiRecordId','') is not null then
   raise exception '仅人工作品不能关联 AI 记录' using errcode='42501';
  end if;
 else
  if nullif(payload->>'aiRecordId','') is not null then
   select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
    where m.submission_id=submission_row.id and r.id=(payload->>'aiRecordId')::uuid;
  else
   select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
    where m.submission_id=submission_row.id order by case when m.variant='assisted' then 0 else 1 end,r.created_at desc limit 1;
  end if;
 end if;
 select * into settings_row from public.ai_review_settings where workspace_id=w;
 decision_key_value=nullif(payload->>'idempotencyKey','');
 if decision_key_value is null then
  decision_key_value='v1:'||public.ai_review_json_sha(jsonb_build_object('submissionId',submission_row.id,'recordId',record_row.id,
   'humanVersion',submission_row.human_version,'formalScores',payload->'formalScores','classification',payload->'classification'));
 end if;
 if length(decision_key_value)>128 then raise exception '幂等键过长'; end if;
 select * into decision_row from public.ai_review_decisions where workspace_id=w and decision_key=decision_key_value;
 if decision_row.id is not null then
  return jsonb_build_object('ok',true,'idempotent',true,'decisionId',decision_row.id,'status',decision_row.status,
   'aiRecordId',decision_row.ai_record_id,'formalTotal',decision_row.formal_total,'scoreRatio',decision_row.score_ratio,
   'expiresAt',decision_row.expires_at,'admitted',false,'aiVisible',deck_row.ai_allowed,'manualOnly',not deck_row.ai_allowed);
 end if;
 classification_value=case when payload ? 'classification' and jsonb_typeof(payload->'classification')<>'null' then payload->'classification' else null end;
 if classification_value is not null and jsonb_typeof(classification_value) not in ('string','object') then raise exception '正式分类无效'; end if;
 classification_label=case
  when jsonb_typeof(classification_value)='string' then nullif(btrim(classification_value #>> '{}'),'')
  when jsonb_typeof(classification_value)='object' then coalesce(
   nullif(btrim(classification_value->>'primary'),''),nullif(btrim(classification_value->>'label'),''),
   nullif(btrim(classification_value->>'name'),''),nullif(btrim(classification_value->>'category'),''))
  else null end;
 classification_style=case when jsonb_typeof(classification_value)='object' then nullif(btrim(classification_value->>'style'),'') else null end;
 ai_classification_label=case
  when jsonb_typeof(record_row.ai_result->'classification')='string' then nullif(btrim(record_row.ai_result->'classification' #>> '{}'),'')
  when jsonb_typeof(record_row.ai_result->'classification')='object' then coalesce(
   nullif(btrim(record_row.ai_result#>>'{classification,primary}'),''),nullif(btrim(record_row.ai_result#>>'{classification,label}'),''),
   nullif(btrim(record_row.ai_result#>>'{classification,name}'),''),nullif(btrim(record_row.ai_result#>>'{classification,category}'),''))
  else null end;
 ai_classification_style=case when jsonb_typeof(record_row.ai_result->'classification')='object'
  then nullif(btrim(record_row.ai_result#>>'{classification,style}'),'') else null end;
 reasons_value=case when payload ? 'reasons' and jsonb_typeof(payload->'reasons')<>'null' then payload->'reasons' else '[]'::jsonb end;
 formal_scores_value=payload->'formalScores';
 if jsonb_typeof(reasons_value)<>'array' or jsonb_typeof(formal_scores_value) is distinct from 'object' or formal_scores_value='{}'::jsonb then raise exception '正式评分必须由评委显式填写正式分，原因必须是数组'; end if;
 if submission_row.source_kind='generated' then
  formal_scores_value=jsonb_build_object('visual',formal_scores_value->'visual','layout',formal_scores_value->'layout','match',formal_scores_value->'match');
 else
  select coalesce(jsonb_object_agg(dimension->>'id',formal_scores_value->(dimension->>'id')),'{}'::jsonb)
  into formal_scores_value
  from public.workspaces ws
  cross join lateral jsonb_array_elements(case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end) dimension
  where ws.id=w and nullif(btrim(dimension->>'id'),'') is not null;
 end if;
 if not public.ai_review_scores_complete(w,submission_row.deck_id,jsonb_build_object('scores',formal_scores_value),submission_row.max_score) then
  raise exception '正式评分必须覆盖当前评分标准的全部维度';
 end if;
 for score_record in select key,value from jsonb_each(formal_scores_value) loop
  if jsonb_typeof(score_record.value)<>'number' or (score_record.value::text)::numeric<0 or (score_record.value::text)::numeric>submission_row.max_score then raise exception '正式分超出范围'; end if;
 end loop;
 if submission_row.source_kind='generated' then
  weighted_value=round((coalesce((formal_scores_value->>'visual')::numeric,0)*settings_row.vision_weight+
   coalesce((formal_scores_value->>'layout')::numeric,0)*settings_row.layout_weight+
   coalesce((formal_scores_value->>'match')::numeric,0)*settings_row.match_weight)/100,2);
  formal_total_value=weighted_value;
 else
  formal_total_value=round((select avg((value::text)::numeric) from jsonb_each(formal_scores_value)),2);
 end if;
 score_ratio_value=round(formal_total_value/nullif(submission_row.max_score,0),6);
 threshold_met_value=score_ratio_value>=settings_row.human_threshold;
 if payload->>'status'='corrected' then status_value='corrected'; end if;
 if record_row.ai_result is not null then
  for score_record in select key,value from jsonb_each(formal_scores_value) loop
   if jsonb_typeof(record_row.ai_result->'scores'->score_record.key)='number' then
    ai_score=(record_row.ai_result->'scores'->>score_record.key)::numeric;
    if (score_record.value::text)::numeric<>ai_score then status_value='corrected'; end if;
   end if;
  end loop;
  if classification_label is not null and (ai_classification_label is null or classification_label<>ai_classification_label) then status_value='corrected'; end if;
  if classification_style is not null and (ai_classification_style is null or classification_style<>ai_classification_style) then status_value='corrected'; end if;
 end if;
 insert into public.ai_review_decisions(workspace_id,deck_id,submission_id,ai_record_id,judge_user_id,decision_key,status,
  classification,reasons,reason_other,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,expires_at,summary)
 values(w,deck_row.id,submission_row.id,record_row.id,actor,decision_key_value,status_value,classification_value,reasons_value,
  left(coalesce(payload->>'reasonOther',''),500),formal_scores_value,formal_total_value,submission_row.deck_version,submission_row.rubric_version,
  submission_row.max_score,score_ratio_value,now()+interval '7 days',left(coalesce(payload->>'summary',''),2000))
 returning * into decision_row;
 if submission_row.source_kind='generated' then
  source_value=coalesce(deck_row.data->'generation','{}'::jsonb);
  reference_value=coalesce(deck_row.data->'generation'->'references',deck_row.data->'references','[]'::jsonb);
  insert into public.ai_generated_entries(workspace_id,deck_id,decision_id,scope,unit_id,source,reference_refs,formal_scores,formal_total,weights,
   human_threshold,human_coverage,threshold_met,ai_self_learning,library_visible,library_label,score_ratio)
  values(w,deck_row.id,decision_row.id,submission_row.scope,submission_row.unit_id,source_value,reference_value,formal_scores_value,formal_total_value,
   jsonb_build_object('visual',settings_row.vision_weight,'layout',settings_row.layout_weight,'match',settings_row.match_weight),
   settings_row.human_threshold,0,threshold_met_value,true,false,'AI 自我学习生成',score_ratio_value) on conflict(decision_id) do nothing;
 end if;
 return jsonb_build_object('ok',true,'idempotent',false,'decisionId',decision_row.id,'status',status_value,
  'aiRecordId',decision_row.ai_record_id,'formalScores',formal_scores_value,'formalTotal',formal_total_value,'scoreRatio',score_ratio_value,
  'thresholdMet',threshold_met_value,'expiresAt',decision_row.expires_at,'admitted',false,
  'aiVisible',deck_row.ai_allowed,'manualOnly',not deck_row.ai_allowed,
  'message',case when deck_row.ai_allowed then '正式评分已确认；需评委单独点击“收入素材库”' else '仅人工正式评分已保存' end);
end $$;
revoke all on function public.ai_review_confirm(uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.ai_review_confirm(uuid,uuid,jsonb) to service_role;

-- Backfill skips manual-only decks instead of failing the batch.
create or replace function public.ai_review_backfill(
 w uuid,
 actor uuid,
 p_kind text,
 p_offset integer,
 p_limit integer
) returns jsonb
language plpgsql
security definer
set search_path=''
as $$
declare
 selected record;
 total_decks bigint;
 selected_decks integer=0;
 prepared_units integer=0;
 next_offset bigint;
 has_whole_rating boolean=false;
 page_ids text[]='{}'::text[];
 classification_meta jsonb;
begin
 -- REVIEW_BACKFILL_CLASSIFICATION_V18
 -- SINGLE_JUDGE_AI_ONLY_BACKFILL_M24
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if p_offset is null or p_offset<0 then raise exception '分页偏移无效'; end if;
 if p_limit is null or p_limit not between 1 and 20 then raise exception '分页大小无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then
  raise exception '仅专职评委可以补档预评';
 end if;

 select count(*) into total_decks
 from public.decks d
 where d.workspace_id=w and d.source_kind=p_kind and d.ai_allowed;

 for selected in
  select d.id,d.data,
   exists(
    select 1
    from jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
    where nullif(btrim(image->>'id'),'') is not null
     and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
   ) as private_preview
  from public.decks d
  where d.workspace_id=w and d.source_kind=p_kind and d.ai_allowed
  order by d.id
  offset p_offset limit p_limit
 loop
  selected_decks=selected_decks+1;
  if not selected.private_preview then continue; end if;

  if p_kind='generated' then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','deck'));
   prepared_units=prepared_units+1;
   continue;
  end if;

  with rubric as (
   select
    coalesce((ws.settings->>'maxScore')::numeric,5) as max_score,
    case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end as dimensions
   from public.workspaces ws
   where ws.id=w
  ),
  current_images as (
   select image->>'id' as unit_id,ordinality as image_order
   from jsonb_array_elements(case when jsonb_typeof(selected.data->'images')='array' then selected.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
   where nullif(btrim(image->>'id'),'') is not null
    and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
  ),
  current_unique as (
   select distinct on (unit_id) unit_id,image_order
   from current_images
   order by unit_id,image_order
  ),
  review_rows as (
   select case when jsonb_typeof(r.data)='object' then r.data else '{}'::jsonb end as data
   from public.reviews r
   where r.workspace_id=w and r.deck_id=selected.id
  ),
  review_data as (
   select r.data,
    case when jsonb_typeof(r.data->'pages')='object' then r.data->'pages' else '{}'::jsonb end as pages
   from review_rows r
  ),
  valid_reviews as (
   select r.data,r.pages,
    (jsonb_typeof(r.data->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(s.value)='number' then (s.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(r.data->'scores')='object' then r.data->'scores' else '{}'::jsonb end) s
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(r.data->'scores')='object' then r.data->'scores' else '{}'::jsonb end) s
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=s.key
      )
     )) as whole_valid
   from review_data r
   cross join rubric rb
  ),
  page_candidates as (
   select cu.unit_id,cu.image_order
   from current_unique cu
   cross join rubric rb
   where exists(
    select 1
    from valid_reviews vr
    cross join lateral jsonb_each(vr.pages) p(unit_id,entry)
    where p.unit_id=cu.unit_id
     and jsonb_typeof(p.entry->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(s.value)='number' then (s.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(p.entry->'scores')='object' then p.entry->'scores' else '{}'::jsonb end) s
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(p.entry->'scores')='object' then p.entry->'scores' else '{}'::jsonb end) s
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=s.key
      )
     )
   )
  )
  select
   exists(select 1 from valid_reviews where whole_valid),
   coalesce((select array_agg(unit_id order by image_order) from page_candidates),'{}'::text[])
  into has_whole_rating,page_ids;

  classification_meta=public.ai_review_effective_meta(w,selected.id);
  if has_whole_rating or coalesce(array_length(page_ids,1),0)=0
     or nullif(btrim(coalesce(classification_meta->>'effectiveClassification','')),'') is null then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','deck'));
   prepared_units=prepared_units+1;
  end if;
  if coalesce(array_length(page_ids,1),0)>0 then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','page','unitIds',to_jsonb(page_ids)));
   prepared_units=prepared_units+coalesce(array_length(page_ids,1),0);
  end if;
 end loop;

 next_offset=case when p_offset::bigint+p_limit<total_decks then p_offset::bigint+p_limit else null end;
 return jsonb_build_object(
  'ok',true,
  'kind',p_kind,
  'offset',p_offset,
  'nextOffset',next_offset,
  'decks',selected_decks,
  'preparedUnits',prepared_units,
  'submittedUnits',0,
  'totalDecks',total_decks
 );
end $$;

revoke all on function public.ai_review_backfill(uuid,uuid,text,integer,integer) from public,anon,authenticated;
grant execute on function public.ai_review_backfill(uuid,uuid,text,integer,integer) to service_role;

-- Planning references are drawn only from decks that opted into AI use.
create or replace function public.ai_planning_reference_candidates(
 w uuid, max_items integer default 12
) returns jsonb
language sql stable security definer set search_path='' as $$
-- PLANNING_ORIGINAL_REFERENCES_V15
-- SINGLE_JUDGE_AI_ONLY_PLANNING_M24
with dimensions as (
 select ws.id as workspace_id,dim.value as dimension
 from public.workspaces ws
 cross join lateral jsonb_array_elements(coalesce(ws.settings->'dimensions','[]'::jsonb)) as dim(value)
 where ws.id=w
), score_rows as (
 select r.workspace_id,r.deck_id,r.user_id,
  case when coalesce(r.data->>'mode','deck')='page' then im->>'id' else '__deck__' end as unit_id,
  dm.dimension->>'id' as dimension_id,
  case when coalesce(r.data->>'mode','deck')='page' then
   case when jsonb_typeof(r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])='number'
    then (r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])::text::numeric else null end
  else case when jsonb_typeof(r.data#>array['scores',dm.dimension->>'id'])='number'
    then (r.data#>array['scores',dm.dimension->>'id'])::text::numeric else null end end as score
 from public.reviews r
 join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id and d.source_kind='uploaded' and d.ai_allowed
 join dimensions dm on dm.workspace_id=r.workspace_id
 cross join lateral jsonb_array_elements(
  case when coalesce(r.data->>'mode','deck')='page'
   then case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end
   else '[{}]'::jsonb end
 ) im
), review_totals as (
 select deck_id,user_id,count(*)::integer as expected,count(score)::integer as scored,avg(score) as mean
 from score_rows group by deck_id,user_id
), complete_reviews as (
 select deck_id,user_id,mean from review_totals where expected>0 and scored=expected
), team_totals as (
 select deck_id,count(*)::integer as complete_review_count,round(avg(mean),2) as score
 from complete_reviews group by deck_id
), formal_decks as (
 select deck_id,max(score) as score
 from public.ai_review_uploaded_formal_scores(w) where scope='deck' group by deck_id
), uploaded_decks as (
 select d.id as deck_id,d.data,
  guanye_internal.ai_review_source_group_bucket(w,d.id,'uploaded') as bucket
 from public.decks d
 where d.workspace_id=w and d.source_kind='uploaded' and d.ai_allowed
), eligible as (
 select u.deck_id,u.data,u.bucket,
  coalesce(t.complete_review_count,0) as complete_review_count,
  case when f.deck_id is not null then f.score else t.score end as score,
  case when f.deck_id is not null then 'formal' else 'team' end as score_source
 from uploaded_decks u
 left join team_totals t on t.deck_id=u.deck_id
 left join formal_decks f on f.deck_id=u.deck_id
 where u.bucket<>0 and (f.deck_id is not null or t.deck_id is not null)
), ranked as (
 select * from eligible
 order by score desc nulls last,complete_review_count desc,deck_id
 limit least(greatest(coalesce(max_items,12),1),20)
)
select coalesce(jsonb_agg(jsonb_build_object(
 'deckId',deck_id,
 'title',coalesce(nullif(data->>'title',''),nullif(data->>'name',''),deck_id),
 'style',coalesce(data->>'style',''),
 'score',score,
 'completeReviewCount',complete_review_count,
 'pageCount',jsonb_array_length(case when jsonb_typeof(data->'images')='array' then data->'images' else '[]'::jsonb end),
 'bucket',bucket,'split','calibration','sourceKind','uploaded','scoreSource',score_source,
 'imagePaths',coalesce((
  select jsonb_agg(path order by page_number)
  from (
   select im.value->>'path' as path,im.page_number
   from jsonb_array_elements(case when jsonb_typeof(data->'images')='array' then data->'images' else '[]'::jsonb end)
    with ordinality as im(value,page_number)
   where nullif(im.value->>'path','') is not null
    and left(coalesce(im.value->>'path',''),length(w::text)+1)=w::text||'/'
    and strpos(coalesce(im.value->>'path',''),'..')=0
    and strpos(coalesce(im.value->>'path',''),chr(92))=0
   order by im.page_number limit 2
  ) paths
 ),'[]'::jsonb)
) order by score desc nulls last,complete_review_count desc,deck_id),'[]'::jsonb)
from ranked;
$$;
revoke all on function public.ai_planning_reference_candidates(uuid,integer)
 from public,anon,authenticated;
grant execute on function public.ai_planning_reference_candidates(uuid,integer)
 to service_role;

-- Calibration samples and formal correction examples exclude manual-only decks.
create or replace function public.ai_review_calibration(
 w uuid,
 actor uuid,
 p_kind text,
 p_limit integer,
 p_deck_id text,
 p_scope text
) returns jsonb
language plpgsql security definer set search_path='' as $calibration$
declare
 target_meta jsonb;
 target_class text;
 target_style text;
 target_group_key text;
 max_value numeric;
 limit_value integer;
 result jsonb;
begin
 -- SOURCE_GROUP_ROUTING_V20
 -- SINGLE_JUDGE_AI_ONLY_CALIBRATION_M24
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '校准轨道无效'; end if;
 if p_scope is null or p_scope not in ('deck','page') then raise exception '校准粒度无效'; end if;
 if p_deck_id is null or length(p_deck_id) not between 1 and 100 then raise exception '目标作品无效'; end if;
 if actor is null or not exists(
  select 1 from public.workspaces ws where ws.id=w and ws.dedicated_judge_id=actor
 ) then raise exception '仅专职评委可以读取校准样本'; end if;
 if not exists(
  select 1 from public.decks d where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind and d.ai_allowed
 ) then raise exception '目标作品不存在、不属于该轨道或已选择仅人工'; end if;

 select public.ai_review_effective_meta(w,d.id),coalesce((ws.settings->>'maxScore')::numeric,5),
  guanye_internal.ai_review_source_group_key(w,d.id,p_kind)
 into target_meta,max_value,target_group_key
 from public.decks d join public.workspaces ws on ws.id=d.workspace_id
 where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind and d.ai_allowed;
 target_class=nullif(lower(btrim(coalesce(target_meta->>'effectiveClassification',''))),'');
 target_style=nullif(lower(btrim(coalesce(target_meta->>'effectiveStyle',''))),'');
 limit_value=greatest(1,least(coalesce(p_limit,6),12));

 with
 meta as (
  select d.id,d.source_kind,d.data,d.created_at,
   guanye_internal.ai_review_source_group_bucket(w,d.id,d.source_kind) as bucket,
   guanye_internal.ai_review_source_group_key(w,d.id,d.source_kind) as group_key,
   coalesce(
    nullif(m.meta->>'effectiveClassification',''),
    nullif(d.data->>'classification',''),nullif(d.data->>'category',''),nullif(d.data->>'primaryClassification',''),
    case when jsonb_typeof(d.data->'classification')='object' then
     coalesce(nullif(d.data#>>'{classification,label}',''),nullif(d.data#>>'{classification,primary}',''),
      nullif(d.data#>>'{classification,name}',''),nullif(d.data#>>'{classification,category}',''))
    end
   ) as classification_value,
   coalesce(
    nullif(m.meta->>'effectiveStyle',''),nullif(d.data->>'style',''),
    case when jsonb_typeof(d.data->'generation'->'style')='string' then d.data#>>'{generation,style}' end,
    case when jsonb_typeof(d.data->'generation'->'style')='object' then
     coalesce(nullif(d.data#>>'{generation,style,name}',''),nullif(d.data#>>'{generation,style,label}',''),
      nullif(d.data#>>'{generation,style,summary}',''))
    end
   ) as style_value,
   m.meta
  from public.decks d
  cross join lateral (select public.ai_review_effective_meta(w,d.id) as meta) m
  where d.workspace_id=w and d.ai_allowed
 ),
 meta_keys as (
  select m.*,nullif(lower(btrim(classification_value)),'') as classification_key,
   nullif(lower(btrim(style_value)),'') as style_key
  from meta m
 ),
 image_rows as (
  select d.id as deck_id,
   coalesce(nullif(image->>'id',''),nullif(image->>'pageId',''),'page-'||ordinality::text) as unit_id,
   coalesce(nullif(image->>'name',''),nullif(image->>'label',''),nullif(image->>'title',''),'page-'||ordinality::text) as unit_name,
   coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) as preview_path,
   ordinality::integer as position
  from meta_keys d
  cross join lateral jsonb_array_elements(
   case when jsonb_typeof(d.data->'images')='array' then d.data->'images'
    when jsonb_typeof(d.data->'generation'->'pages')='array' then d.data->'generation'->'pages'
    else '[]'::jsonb end
  ) with ordinality as image_row(image,ordinality)
  where coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) is not null
   and left(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),length(w::text)+1)=w::text||'/'
   and strpos(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),'..')=0
   and strpos(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),chr(92))=0
 ),
 preview as (
  select deck_id,'page'::text as scope,unit_id,unit_name,preview_path,position from image_rows
  union all
  select deck_id,'deck'::text,'',unit_name,preview_path,position
  from (
   select distinct on (deck_id) deck_id,unit_id,unit_name,preview_path,position
   from image_rows order by deck_id,position,unit_id
  ) first_image
 ),
 legacy as (
  select 'legacy_review'::text as sample_type,
   ('legacy:'||r.user_id::text||':'||d.id||':'||p.scope||':'||p.unit_id)::text as sample_id,
   d.id as deck_id,d.source_kind as track,p.scope,p.unit_id,p.unit_name,p.preview_path,
   r.user_id as human_user_id,coalesce(entry->'scores','{}'::jsonb) as scores,max_value as max_score,
   'workspaces.settings.maxScore'::text as score_scale_source,d.created_at as sort_at,null::timestamptz as created_at,
   d.classification_value,d.style_value,d.classification_key,d.style_key,
   d.meta->>'classificationSource' as classification_source,d.meta->>'styleSource' as style_source
  from public.reviews r
  join meta_keys d on d.id=r.deck_id
  join preview p on p.deck_id=d.id
  cross join lateral (select case when p.scope='page' then r.data->'pages'->p.unit_id else r.data end as entry) scored
  where p_kind='uploaded' and r.workspace_id=w and d.source_kind='uploaded' and d.bucket<>0
   and d.id<>p_deck_id and d.group_key is distinct from target_group_key
   and jsonb_typeof(scored.entry)='object'
   and public.ai_review_scores_complete(w,d.id,scored.entry,max_value)
 ),
 sample_sources as (
  select distinct on (s.id)
   s.id as submission_id,s.deck_id,s.source_kind as track,s.scope,s.unit_id,s.human_user_id,s.entry,s.max_score,
   s.created_at as sort_at,s.created_at,
   d.classification_value,d.style_value,d.classification_key,d.style_key,
   d.meta->>'classificationSource' as classification_source,d.meta->>'styleSource' as style_source
  from public.ai_review_submissions s
  join public.ai_review_matches m on m.submission_id=s.id
  join public.ai_review_records r on r.id=m.record_id and r.workspace_id=w
  join public.ai_review_jobs j on j.id=r.job_id and j.workspace_id=w
  join meta_keys d on d.id=s.deck_id
  where s.workspace_id=w and s.source_kind=p_kind and r.source_kind=p_kind and j.source_kind=p_kind and d.source_kind=p_kind
   and m.variant=r.variant and j.split='calibration' and r.split='calibration'
   and j.bucket=d.bucket and r.bucket=d.bucket and d.bucket<>0
   and s.deck_id<>p_deck_id and d.group_key is distinct from target_group_key
   and s.deck_version=public.ai_review_deck_version(w,s.deck_id) and s.rubric_version=public.ai_review_rubric_version(w)
   and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version and r.scope=s.scope and r.unit_id=s.unit_id
   and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
  order by s.id,case when r.variant='assisted' then 0 else 1 end,r.created_at desc,r.id
 ),
 sample_pool as (
  select * from legacy
  union all
  select 'human_submission'::text,('submission:'||s.submission_id::text)::text,s.deck_id,s.track,p.scope,p.unit_id,p.unit_name,p.preview_path,
   s.human_user_id,coalesce(s.entry->'scores','{}'::jsonb),s.max_score,'ai_review_submissions.maxScore'::text,
   s.sort_at,s.created_at,s.classification_value,s.style_value,s.classification_key,s.style_key,s.classification_source,s.style_source
  from sample_sources s
  join preview p on p.deck_id=s.deck_id
   and ((p.scope='deck' and s.scope='deck') or (p.scope='page' and s.scope='page' and p.unit_id=s.unit_id))
 ),
 sample_ranked as (
  select s.*,coalesce(s.scope=p_scope,false) as exact_scope,
   coalesce(target_class is not null and s.classification_key=target_class,false) as same_classification,
   coalesce(target_style is not null and s.style_key=target_style,false) as same_style,
   row_number() over (order by coalesce(s.scope=p_scope,false) desc,
    coalesce(target_class is not null and s.classification_key=target_class,false) desc,
    coalesce(target_style is not null and s.style_key=target_style,false) desc,s.sort_at desc nulls last,s.sample_id) as sample_order
  from (
   select distinct on (deck_id,scope,unit_id,human_user_id) *
   from sample_pool
   order by deck_id,scope,unit_id,human_user_id,case when sample_type='human_submission' then 0 else 1 end,
    sort_at desc nulls last,sample_id
  ) s
 ),
 selected_samples as (select * from sample_ranked where sample_order<=limit_value),
 correction_sources as (
  select distinct on (d.deck_id,s.scope,s.unit_id)
   d.id::text as correction_id,d.deck_id,p_kind as track,s.scope,s.unit_id,d.status,d.classification,d.reasons,d.reason_other,
   d.formal_scores,d.formal_total,d.max_score,d.score_ratio,d.summary,d.created_at,
   dm.classification_value,dm.style_value,dm.classification_key,dm.style_key,
   dm.meta->>'classificationSource' as classification_source,dm.meta->>'styleSource' as style_source
  from public.ai_review_decisions d
  join public.ai_review_submissions s on s.id=d.submission_id and s.workspace_id=w
  join public.ai_review_records r on r.id=d.ai_record_id and r.workspace_id=w
  join public.ai_review_jobs j on j.id=r.job_id and j.workspace_id=w
  join meta_keys dm on dm.id=d.deck_id
  where d.workspace_id=w and d.judge_user_id=actor and d.status='corrected'
   and d.deck_id<>p_deck_id and dm.group_key is distinct from target_group_key
   and dm.source_kind=p_kind and s.source_kind=p_kind and r.source_kind=p_kind and j.source_kind=p_kind
   and j.split='calibration' and r.split='calibration' and j.bucket=dm.bucket and r.bucket=dm.bucket and dm.bucket<>0
   and d.rubric_version=public.ai_review_rubric_version(w) and d.deck_version=public.ai_review_deck_version(w,d.deck_id)
   and s.rubric_version=d.rubric_version and s.deck_version=d.deck_version and s.scope=r.scope and s.unit_id=r.unit_id
   and s.deck_id=d.deck_id and r.deck_id=d.deck_id and j.deck_id=d.deck_id
   and s.max_score=d.max_score and r.max_score=d.max_score and d.max_score=max_value
   and exists(select 1 from public.ai_review_matches m where m.submission_id=s.id and m.record_id=r.id and m.variant=r.variant)
  order by d.deck_id,s.scope,s.unit_id,d.created_at desc,d.id desc
 ),
 correction_ranked as (
  select c.*,p.scope as actual_scope,p.unit_id as actual_unit_id,p.unit_name,p.preview_path,
   coalesce(p.scope=p_scope,false) as exact_scope,
   coalesce(target_class is not null and c.classification_key=target_class,false) as same_classification,
   coalesce(target_style is not null and c.style_key=target_style,false) as same_style,
   row_number() over (order by coalesce(p.scope=p_scope,false) desc,
    coalesce(target_class is not null and c.classification_key=target_class,false) desc,
    coalesce(target_style is not null and c.style_key=target_style,false) desc,c.created_at desc nulls last,c.correction_id) as correction_order
  from correction_sources c
  join preview p on p.deck_id=c.deck_id
   and ((p.scope='deck' and c.scope='deck') or (p.scope='page' and c.scope='page' and p.unit_id=c.unit_id))
 ),
 selected_corrections as (select * from correction_ranked where correction_order<=limit_value)
 select jsonb_build_object(
  'samples',coalesce((select jsonb_agg(jsonb_build_object(
   'sampleId',s.sample_id,'sampleType',s.sample_type,'track',s.track,'split','calibration','deckId',s.deck_id,
   'scope',s.scope,'scopeFallback',not s.exact_scope,'unitId',nullif(s.unit_id,''),'unitName',s.unit_name,
   'previewPath',s.preview_path,'scores',s.scores,'maxScore',s.max_score,'scoreScaleSource',s.score_scale_source,
   'classification',s.classification_value,'style',s.style_value,'classificationSource',s.classification_source,
   'styleSource',s.style_source,'sameClassification',s.same_classification,'sameStyle',s.same_style,'createdAt',s.created_at
  ) order by s.sample_order) from selected_samples s),'[]'::jsonb),
  'corrections',coalesce((select jsonb_agg(jsonb_build_object(
   'correctionId',c.correction_id,'sampleType','judge_formal_decision','track',c.track,'split','calibration','deckId',c.deck_id,
   'scope',c.actual_scope,'scopeFallback',not c.exact_scope,'unitId',nullif(c.actual_unit_id,''),'unitName',c.unit_name,
   'previewPath',c.preview_path,'status',c.status,'formalScores',c.formal_scores,'maxScore',c.max_score,
   'formalTotal',c.formal_total,'scoreRatio',c.score_ratio,'classification',c.classification,'style',c.style_value,
   'reasons',c.reasons,'reasonOther',c.reason_other,'summary',c.summary,'sameClassification',c.same_classification,
   'sameStyle',c.same_style,'createdAt',c.created_at
  ) order by c.correction_order) from selected_corrections c),'[]'::jsonb),
  'selectedSamples',(select count(*) from selected_samples),
  'selectedCorrections',(select count(*) from selected_corrections),
  'kind',p_kind,'requestedScope',p_scope,
  'scopeFallback',coalesce((select bool_or(not exact_scope) from selected_samples),false),
  'correctionScopeFallback',coalesce((select bool_or(not exact_scope) from selected_corrections),false)
 ) into result;
 return result;
end
$calibration$;
revoke all on function public.ai_review_calibration(uuid,uuid,text,integer,text,text)
 from public,anon,authenticated;
grant execute on function public.ai_review_calibration(uuid,uuid,text,integer,text,text)
 to service_role;

-- Model-sample exports carry only AI-authorized decks and their human reviews.
create or replace function public.get_ai_review_sample_snapshot(w uuid) returns jsonb
language plpgsql stable security invoker set search_path='' as $$
declare result jsonb;
begin
 -- SOURCE_GROUP_ROUTING_V20
 -- SINGLE_JUDGE_AI_ONLY_SNAPSHOT_M24
 if not exists (select 1 from public.workspaces where id=w) then raise exception '工作区不存在'; end if;
 with
 workspace as (
  select ws.id,ws.settings,ws.dedicated_judge_id
  from public.workspaces ws where ws.id=w
 ),
 deck_rows as (
  select d.id,d.source_kind,d.data,
   guanye_internal.ai_review_source_group_bucket(w,d.id,d.source_kind) as bucket
  from public.decks d where d.workspace_id=w and d.ai_allowed
 ),
 review_base as (
  select r.deck_id,r.user_id,
   case when r.data->>'mode'='page' then 'page' else 'deck' end as mode,
   r.data
  from public.reviews r where r.workspace_id=w
 ),
 review_units as (
  select rb.deck_id,rb.user_id,'deck'::text as scope,0::bigint as position,
   null::jsonb as image,rb.data as original_entry,
   case when jsonb_typeof(rb.data->'scores')='object' then rb.data->'scores' else '{}'::jsonb end as scores
  from review_base rb
  union all
  select rb.deck_id,rb.user_id,'page'::text as scope,im.ordinality::bigint as position,
   im.value as image,
   case when jsonb_typeof(rb.data->'pages'->(im.value->>'id'))='object' then rb.data->'pages'->(im.value->>'id') else '{}'::jsonb end as original_entry,
   case when jsonb_typeof((rb.data->'pages'->(im.value->>'id'))->'scores')='object'
    then (rb.data->'pages'->(im.value->>'id'))->'scores' else '{}'::jsonb end as scores
  from review_base rb
  join deck_rows d on d.id=rb.deck_id
  cross join lateral jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end)
   with ordinality as im(value,ordinality)
 ),
 unit_rows as (
  select u.*,
   exists(select 1 from jsonb_each(u.scores) s where jsonb_typeof(s.value)='number') as has_scores,
   coalesce((
    select bool_and((u.scores ? (dim->>'id')) and jsonb_typeof(u.scores->(dim->>'id'))='number')
    from jsonb_array_elements((select settings->'dimensions' from workspace)) dim
   ),false) as complete
  from review_units u
 ),
 scope_agg as (
  select u.deck_id,u.user_id,u.scope,
   jsonb_agg(jsonb_build_object(
    'scope',u.scope,'unitType',u.scope,
    'unitId',case when u.scope='page' then u.image->>'id' else null end,
    'position',u.position,
    'imageId',case when u.scope='page' then u.image->>'id' else null end,
    'imageName',case when u.scope='page' then u.image->>'name' else null end,
    'image',u.image,'originalEntry',u.original_entry,'scores',u.scores,
    'hasScores',u.has_scores,'complete',u.complete
   ) order by u.position) as units,
   jsonb_agg(jsonb_build_object(
    'scope',u.scope,'unitType',u.scope,
    'unitId',case when u.scope='page' then u.image->>'id' else null end,
    'position',u.position,
    'imageId',case when u.scope='page' then u.image->>'id' else null end,
    'imageName',case when u.scope='page' then u.image->>'name' else null end,
    'image',u.image,'originalEntry',u.original_entry,'scores',u.scores,
    'hasScores',u.has_scores,'complete',u.complete
   ) order by u.position) filter (where u.has_scores) as scored_units,
   bool_or(u.has_scores) as has_scores,
   coalesce(bool_and(u.complete),false) as complete
  from unit_rows u
  group by u.deck_id,u.user_id,u.scope
 ),
 review_rows as (
  select rb.deck_id,rb.user_id,rb.mode,rb.data,
   coalesce(p.units,'[]'::jsonb) as units,
   coalesce(p.has_scores,false) as has_scores,
   coalesce(p.complete,false) as complete,
   coalesce(p.has_scores,false) or coalesce(o.has_scores,false) as any_has_scores,
   coalesce(p.complete,false) or coalesce(o.complete,false) as any_complete,
   case when rb.mode='deck' then 'page' else 'deck' end as other_scope,
   coalesce(o.scored_units,'[]'::jsonb) as other_scope_units,
   coalesce(o.has_scores,false) as other_scope_has_scores,
   coalesce(o.complete,false) as other_scope_complete,
   case when not coalesce(p.has_scores,false) then 'unrated'
    when coalesce(p.complete,false) then 'complete' else 'partial' end as rating_status,
   case when not (coalesce(p.has_scores,false) or coalesce(o.has_scores,false)) then 'unrated'
    when coalesce(p.complete,false) or coalesce(o.complete,false) then 'complete'
    else 'partial' end as overall_rating_status
  from review_base rb
  left join scope_agg p on p.deck_id=rb.deck_id and p.user_id=rb.user_id and p.scope=rb.mode
  left join scope_agg o on o.deck_id=rb.deck_id and o.user_id=rb.user_id and o.scope<>rb.mode
 ),
 deck_agg as (
  select d.id,d.source_kind,d.data,d.bucket,
   jsonb_agg(jsonb_build_object(
    'userId',r.user_id,'mode',r.mode,'otherScope',r.other_scope,
    'ratingStatus',r.rating_status,'overallRatingStatus',r.overall_rating_status,
    'maxScore',(select settings->'maxScore' from workspace),
    'dimensions',(select settings->'dimensions' from workspace),
    'original',r.data,'hasScores',r.has_scores,'anyHasScores',r.any_has_scores,
    'complete',r.complete,'anyComplete',r.any_complete,'units',r.units,
    'otherScopeUnits',r.other_scope_units,'otherScopeHasScores',r.other_scope_has_scores,
    'otherScopeComplete',r.other_scope_complete
   ) order by r.user_id) filter (where r.user_id is not null) as reviews,
   count(*) filter (where r.any_has_scores)::int as rated_review_count,
   count(*) filter (where r.user_id is not null and not r.any_has_scores)::int as unrated_review_count,
   count(*) filter (where r.any_complete)::int as complete_review_count
  from deck_rows d
  left join review_rows r on r.deck_id=d.id
  group by d.id,d.source_kind,d.data,d.bucket
 ),
 deck_samples as (
  select *,
   case when rated_review_count=0 then 'unrated'
    when complete_review_count=rated_review_count then 'complete' else 'partial' end as rating_status
  from deck_agg
 )
 select jsonb_build_object(
  'schemaVersion',1,
  'workspaceId',(select id from workspace),
  'splitAlgorithm',case when guanye_internal.ai_review_has_source_groups(w)
   then 'source-sha256-or-md5-first-byte-mod-5-v1'
   else 'md5-first-byte-mod-5-v1' end,
  'holdoutBucket',0,
  'settings',(select settings from workspace),
  'dedicatedJudgeId',(select dedicated_judge_id from workspace),
  'deckCount',count(*),
  'ratedDeckCount',count(*) filter (where rated_review_count>0),
  'unratedDeckCount',count(*) filter (where rated_review_count=0),
  'calibrationDeckIds',coalesce(jsonb_agg(id order by id) filter (where bucket<>0),'[]'::jsonb),
  'holdoutDeckIds',coalesce(jsonb_agg(id order by id) filter (where bucket=0),'[]'::jsonb),
  'decks',coalesce(jsonb_agg(jsonb_build_object(
   'deckId',id,
   'split',case when bucket=0 then 'holdout' else 'calibration' end,
   'bucket',bucket,'sourceKind',source_kind,'originalDeck',data,
   'ratingStatus',rating_status,'ratedReviewCount',rated_review_count,
   'unratedReviewCount',unrated_review_count,'completeReviewCount',complete_review_count,
   'reviews',coalesce(reviews,'[]'::jsonb)
  ) order by id),'[]'::jsonb)
 ) into result
 from deck_samples;
 return result;
end $$;
revoke all on function public.get_ai_review_sample_snapshot(uuid)
 from public,anon,authenticated;
grant execute on function public.get_ai_review_sample_snapshot(uuid) to service_role;

do $single_judge_submission_self_check$
declare
 publish_oid oid := to_regprocedure('public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)');
 legacy_oid oid := to_regprocedure('public.single_judge_publish_import(uuid,uuid,jsonb,jsonb)');
 access_oid oid := to_regprocedure('public.single_judge_access(uuid)');
 confirm_oid oid := to_regprocedure('public.ai_review_confirm(uuid,uuid,jsonb)');
 feed_oid oid := to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)');
 snap_oid oid := to_regprocedure('public.get_ai_review_sample_snapshot(uuid)');
 plan_oid oid := to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)');
 backfill_oid oid := to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)');
begin
 -- SINGLE_JUDGE_SUBMISSION_SELF_CHECK_M24
 if not exists(
  select 1 from pg_attribute
  where attrelid='public.decks'::regclass and attname='ai_allowed' and attnotnull and not attisdropped
 ) then raise exception 'M24 decks.ai_allowed missing or nullable'; end if;
 if not exists(
  select 1 from pg_attrdef d
  where d.adrelid='public.decks'::regclass and d.adnum=(select attnum from pg_attribute where attrelid='public.decks'::regclass and attname='ai_allowed')
    and pg_get_expr(d.adbin,d.adrelid)='true'
 ) then raise exception 'M24 decks.ai_allowed default is not true'; end if;
 if publish_oid is null or legacy_oid is null or access_oid is null or confirm_oid is null
    or feed_oid is null or snap_oid is null or plan_oid is null or backfill_oid is null then
  raise exception 'M24 required RPC missing';
 end if;
 if not exists(
  select 1 from pg_proc p where p.oid=publish_oid and p.prosecdef
   and position('SINGLE_JUDGE_SUBMISSION_PUBLISH_M24' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as c(value)
              where c.value in ('search_path=','search_path=""'))
 ) then raise exception 'M24 publish RPC security posture invalid'; end if;
 if not has_function_privilege('service_role',publish_oid,'EXECUTE')
    or has_function_privilege('authenticated',publish_oid,'EXECUTE')
    or has_function_privilege('anon',publish_oid,'EXECUTE') then
  raise exception 'M24 publish RPC ACL invalid'; end if;
 if not has_function_privilege('service_role',legacy_oid,'EXECUTE')
    or has_function_privilege('authenticated',legacy_oid,'EXECUTE')
    or has_function_privilege('anon',legacy_oid,'EXECUTE') then
  raise exception 'M23 legacy publish ACL changed'; end if;
 if not has_function_privilege('authenticated',access_oid,'EXECUTE')
    or has_function_privilege('anon',access_oid,'EXECUTE') then
  raise exception 'M24 access RPC ACL invalid'; end if;
 if not exists(
  select 1 from pg_proc p where p.oid=confirm_oid
   and position('FORMAL_CORRECTION_V13' in p.prosrc)>0
   and position('SINGLE_JUDGE_MANUAL_FORMAL_DECISION_M24' in p.prosrc)>0
 ) then raise exception 'M24 confirmed manual decision path missing'; end if;
 if not exists(
  select 1 from pg_proc p where p.oid=feed_oid
   and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0
   and position('SINGLE_JUDGE_AI_ONLY_CALIBRATION_M24' in p.prosrc)>0
 ) then raise exception 'M24 calibration filter missing'; end if;
 if not exists(
  select 1 from pg_proc p where p.oid=snap_oid
   and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0
   and position('SINGLE_JUDGE_AI_ONLY_SNAPSHOT_M24' in p.prosrc)>0
 ) then raise exception 'M24 snapshot filter missing'; end if;
 if not exists(
  select 1 from pg_proc p where p.oid=plan_oid
   and position('PLANNING_ORIGINAL_REFERENCES_V15' in p.prosrc)>0
   and position('SINGLE_JUDGE_AI_ONLY_PLANNING_M24' in p.prosrc)>0
 ) then raise exception 'M24 planning filter missing'; end if;
 if not exists(
  select 1 from pg_proc p where p.oid=backfill_oid
   and position('REVIEW_BACKFILL_CLASSIFICATION_V18' in p.prosrc)>0
   and position('SINGLE_JUDGE_AI_ONLY_BACKFILL_M24' in p.prosrc)>0
 ) then raise exception 'M24 backfill filter missing'; end if;
 if not exists(
  select 1 from pg_trigger t where t.tgname='single_judge_apply_ai_allowed_before_insert'
   and t.tgrelid='public.decks'::regclass and not t.tgisinternal
 ) then raise exception 'M24 ai_allowed insert trigger missing'; end if;
 if not exists(
  select 1 from pg_trigger t where t.tgname='single_judge_manual_only_job_guard'
   and t.tgrelid='public.ai_review_jobs'::regclass and not t.tgisinternal
 ) then raise exception 'M24 AI job guard missing'; end if;
 if not exists(
  select 1 from pg_trigger t where t.tgname='single_judge_manual_only_decision_guard'
   and t.tgrelid='public.ai_review_decisions'::regclass and not t.tgisinternal
 ) then raise exception 'M24 correction guard missing'; end if;
end
$single_judge_submission_self_check$;

commit;
