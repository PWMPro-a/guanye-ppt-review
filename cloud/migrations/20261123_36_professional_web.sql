-- M36 is inert until an operator activates a backed-up workspace for professional web.
begin;

alter table public.workspaces add column professional_web boolean not null default false;
alter table public.workspaces add constraint professional_web_distinct_roles check (
 not professional_web or (single_judge_admin_id is not null and dedicated_judge_id is not null
 and single_judge_admin_id<>dedicated_judge_id));

alter table public.decks add column professional_archived_at timestamptz,
 add column professional_archived_by uuid references auth.users(id) on delete set null;
alter table public.ai_review_decisions add column professional_web boolean not null default false;
create index professional_web_decisions_latest on public.ai_review_decisions(workspace_id,deck_id,created_at desc,id desc)
 where professional_web;

create table guanye_internal.professional_web_cutovers (
 workspace_id uuid primary key references public.workspaces(id),
 previous_judge uuid,
 previous_admin uuid,
 previous_workers integer,
 active boolean not null,
 updated_at timestamptz not null default now()
);
alter table guanye_internal.professional_web_cutovers enable row level security;
revoke all on guanye_internal.professional_web_cutovers from public,anon,authenticated,service_role;

create function guanye_internal.professional_web(w uuid) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.workspaces ws where ws.id=w and ws.professional_web)
$$;
create function guanye_internal.professional_scorer(w uuid,actor uuid) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.workspaces ws join public.members m on m.workspace_id=ws.id
  where ws.id=w and m.user_id=actor and
   (actor=ws.dedicated_judge_id or ws.professional_web and actor=ws.single_judge_admin_id))
$$;
revoke all on function guanye_internal.professional_web(uuid),guanye_internal.professional_scorer(uuid,uuid)
 from public,anon,authenticated,service_role;

create function public.professional_web_activate(w uuid,judge_id uuid,admin_id uuid) returns void
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype; workers integer;
begin
 select * into ws from public.workspaces where id=w for update;
 if not found or judge_id is null or admin_id is null or judge_id=admin_id
  or not exists(select 1 from public.members m where m.workspace_id=w and m.user_id=judge_id and m.role='reviewer')
  or not exists(select 1 from public.members m where m.workspace_id=w and m.user_id=admin_id and m.role='admin') then
  raise exception '请选择两个不同且已授权的管理员、评分师账号' using errcode='22023';
 end if;
 if ws.professional_web then
  if (ws.dedicated_judge_id,ws.single_judge_admin_id) is distinct from (judge_id,admin_id) then
   raise exception '已上线的工作区不能从此入口改绑账号' using errcode='42501';
  end if;
  return;
 end if;
 select max_workers into workers from public.ai_generation_settings where workspace_id=w;
 insert into guanye_internal.professional_web_cutovers values(w,ws.dedicated_judge_id,ws.single_judge_admin_id,workers,true,now())
 on conflict(workspace_id) do update set previous_judge=excluded.previous_judge,previous_admin=excluded.previous_admin,
  previous_workers=excluded.previous_workers,active=true,updated_at=now();
 update public.workspaces set dedicated_judge_id=judge_id,single_judge_admin_id=admin_id,professional_web=true where id=w;
 insert into public.ai_generation_settings(workspace_id,max_workers) values(w,2)
 on conflict(workspace_id) do update set max_workers=2;
end $$;

create function public.professional_web_rollback(w uuid) returns void
language plpgsql security definer set search_path='' as $$
declare checkpoint guanye_internal.professional_web_cutovers%rowtype;
begin
 perform 1 from public.workspaces where id=w and professional_web for update;
 if not found then return; end if;
 select * into checkpoint from guanye_internal.professional_web_cutovers where workspace_id=w and active for update;
 if not found then raise exception '缺少上线前的回退记录'; end if;
 update public.workspaces set professional_web=false,dedicated_judge_id=checkpoint.previous_judge,
  single_judge_admin_id=checkpoint.previous_admin where id=w;
 if checkpoint.previous_workers is null then
  delete from public.ai_generation_settings where workspace_id=w;
 else
  update public.ai_generation_settings set max_workers=checkpoint.previous_workers where workspace_id=w;
 end if;
 update guanye_internal.professional_web_cutovers set active=false,updated_at=now() where workspace_id=w;
end $$;
revoke all on function public.professional_web_activate(uuid,uuid,uuid),public.professional_web_rollback(uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.professional_web_activate(uuid,uuid,uuid),public.professional_web_rollback(uuid) to service_role;

create function public.professional_worker_generation_job(w uuid,job_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if not guanye_internal.professional_web(w) or public.single_judge_access(w)->>'role' is distinct from 'judge' then
  raise exception '仅当前工作区后台评分师可读取生图任务' using errcode='42501';
 end if;
 return public.ai_generation_job_json(w,job_id);
end $$;
revoke all on function public.professional_worker_generation_job(uuid,uuid) from public,anon,authenticated,service_role;
grant execute on function public.professional_worker_generation_job(uuid,uuid) to authenticated;

create function guanye_internal.professional_require_admin(w uuid) returns void
language plpgsql stable security definer set search_path='' as $$
begin
 if not guanye_internal.professional_web(w) or public.single_judge_access(w)->>'role' is distinct from 'admin' then
  raise exception '仅当前工作区管理员可以操作' using errcode='42501';
 end if;
end $$;

create function public.professional_admin_archive(w uuid,p_deck text,p_archive boolean) returns void
language plpgsql security definer set search_path='' as $$
begin
 perform 1 from public.workspaces where id=w for share;
 perform guanye_internal.professional_require_admin(w);
 if p_archive is null then raise exception '缺少归档操作' using errcode='22023'; end if;
 update public.decks set professional_archived_at=case when p_archive then coalesce(professional_archived_at,clock_timestamp()) end,
  professional_archived_by=case when p_archive then coalesce(professional_archived_by,auth.uid()) end
 where workspace_id=w and id=p_deck;
 if not found then raise exception '作品不存在' using errcode='22023'; end if;
end $$;

create function public.professional_admin_directory(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 perform guanye_internal.professional_require_admin(w);
 return jsonb_build_object(
  'members',coalesce((select jsonb_agg(jsonb_build_object('id',m.user_id,'name',m.name,'email',u.email,
   'role',case when m.user_id=ws.single_judge_admin_id then 'admin' when m.user_id=ws.dedicated_judge_id then 'judge' else 'submitter' end) order by m.name,m.user_id)
    from public.members m join auth.users u on u.id=m.user_id join public.workspaces ws on ws.id=m.workspace_id
    where m.workspace_id=w),'[]'::jsonb),
  'archived',coalesce((select jsonb_agg(jsonb_build_object('id',d.id,'title',coalesce(d.data->>'title',d.data->>'name',d.id),
   'archivedAt',d.professional_archived_at) order by d.professional_archived_at desc)
    from public.decks d where d.workspace_id=w and d.professional_archived_at is not null),'[]'::jsonb));
end $$;

create function public.professional_admin_add_member(w uuid,p_email text,p_name text) returns void
language plpgsql security definer set search_path='' as $$
declare target uuid;
begin
 perform guanye_internal.professional_require_admin(w);
 select u.id into target from auth.users u where lower(u.email)=lower(btrim(p_email));
 if not found then raise exception '该邮箱还没有观页账号，请先完成账号注册' using errcode='22023'; end if;
 perform public.professional_admin_member(w,target,p_name,true);
end $$;

create function public.professional_admin_export(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 perform guanye_internal.professional_require_admin(w);
 return jsonb_build_object('version',1,'workspaceId',w,'exportedAt',now(),
  'settings',(select settings from public.workspaces where id=w),
  'members',coalesce((select jsonb_agg(to_jsonb(m) order by m.name,m.user_id) from public.members m where m.workspace_id=w),'[]'::jsonb),
  'decks',coalesce((select jsonb_agg(to_jsonb(d) order by d.id) from public.decks d where d.workspace_id=w),'[]'::jsonb),
  'teamReviews',coalesce((select jsonb_agg(to_jsonb(r) order by r.deck_id,r.user_id) from public.reviews r where r.workspace_id=w),'[]'::jsonb),
  'scoreHistory',coalesce((select jsonb_agg(jsonb_build_object('actorId',s.human_user_id,'deckId',s.deck_id,'scope',s.scope,'professionalWeb',d.professional_web,
   'unitId',s.unit_id,'confirmedAt',d.created_at,'scores',d.formal_scores,'total',d.formal_total,'maxScore',d.max_score)
    order by d.created_at,d.id) from public.ai_review_decisions d join public.ai_review_submissions s on s.id=d.submission_id
   where d.workspace_id=w),'[]'::jsonb));
end $$;

create function public.professional_admin_settings(w uuid,p_revision bigint,p_max numeric,p_dimensions jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare previous jsonb; revised jsonb; item jsonb; identifiers text[]='{}'; identifier text;
begin
 select settings into previous from public.workspaces where id=w for update;
 perform guanye_internal.professional_require_admin(w);
 if p_revision is distinct from coalesce((previous->>'revision')::bigint,0) then
  raise exception '评分标准已变化，请刷新后再保存' using errcode='40001';
 end if;
 if p_max is null or p_max not between 1 and 100 or jsonb_typeof(p_dimensions) is distinct from 'array'
  or jsonb_array_length(p_dimensions) not between 1 and 12 then
  raise exception '评分设置无效' using errcode='22023';
 end if;
 for item in select value from jsonb_array_elements(p_dimensions) loop
  identifier=item->>'id';
  if jsonb_typeof(item) is distinct from 'object' or item-array['id','name','description']<>'{}'::jsonb
    or coalesce(identifier,'') !~ '^[A-Za-z0-9_-]{1,80}$' or identifier=any(identifiers)
    or length(btrim(coalesce(item->>'name',''))) not between 1 and 30
    or length(coalesce(item->>'description',''))>200 then
   raise exception '评分项名称或格式无效' using errcode='22023';
  end if;
  identifiers=array_append(identifiers,identifier);
 end loop;
 revised=previous||jsonb_build_object('maxScore',p_max,'dimensions',p_dimensions,'revision',p_revision+1);
 update public.workspaces set settings=revised where id=w;
 return revised;
end $$;

create function public.professional_admin_member(w uuid,p_user uuid,p_name text,p_enable boolean) returns void
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype;
begin
 select * into ws from public.workspaces where id=w for update;
 perform guanye_internal.professional_require_admin(w);
 if p_user is null or p_enable is null or p_user in (ws.single_judge_admin_id,ws.dedicated_judge_id) then
  raise exception '不能通过此入口更改管理员或评分师' using errcode='22023';
 end if;
 if p_enable then
  if length(btrim(coalesce(p_name,''))) not between 1 and 40 then raise exception '成员姓名无效'; end if;
  insert into public.members(workspace_id,user_id,name,role) values(w,p_user,btrim(p_name),'reviewer')
  on conflict(workspace_id,user_id) do update set name=excluded.name;
 else
  delete from public.members where workspace_id=w and user_id=p_user;
 end if;
end $$;

revoke all on function guanye_internal.professional_require_admin(uuid),public.professional_admin_export(uuid),
 public.professional_admin_settings(uuid,bigint,numeric,jsonb),public.professional_admin_member(uuid,uuid,text,boolean)
 from public,anon,authenticated,service_role;
grant execute on function public.professional_admin_export(uuid),public.professional_admin_settings(uuid,bigint,numeric,jsonb),
 public.professional_admin_member(uuid,uuid,text,boolean) to authenticated;

-- Updated privilege and version gates for the M36 opt-in.

revoke all on function public.professional_admin_archive(uuid,text,boolean),public.professional_admin_directory(uuid),
 public.professional_admin_add_member(uuid,text,text) from public,anon,authenticated,service_role;
grant execute on function public.professional_admin_archive(uuid,text,boolean),public.professional_admin_directory(uuid),
 public.professional_admin_add_member(uuid,text,text) to authenticated;

create or replace function guanye_internal.single_judge_guard_submission() returns trigger
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype;
begin
 select * into ws from public.workspaces where id=new.workspace_id for share;
 if ws.single_judge_admin_id is not null and
    (not guanye_internal.professional_scorer(ws.id,new.human_user_id)
     or nullif(current_setting('guanye.single_judge_bundle',true),'') is distinct from new.workspace_id::text) then
  raise exception '仅指定评分师可通过专业版提交评分' using errcode='42501';
 end if;
 return new;
end $$;

create or replace function public.ai_review_submit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare member public.members%rowtype; deck_row public.decks%rowtype; workspace_settings jsonb; scope_value text; unit_json jsonb; latest_human_version text; latest_entry jsonb; latest_actor uuid; revision_token text;
 entry jsonb; score_record record; unit_key text; unit_position integer; deck_hash text; rubric_hash text; human_hash text;
 existing_review jsonb; submission_uuid uuid; inserted_uuid uuid; submission_ids jsonb='[]'::jsonb;
 matched_baseline integer=0; matched_assisted integer=0; max_value numeric; note_value text; reason_other_value text;
begin
 perform set_config('guanye.ai_review_internal','on',true);
 if actor is null then raise exception '请先登录'; end if;
 select * into member from public.members where workspace_id=w and user_id=actor;
 if member.user_id is null then raise exception '请先加入团队'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or length(coalesce(payload->>'deckId','')) not between 1 and 100 then raise exception '提交内容无效'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=payload->>'deckId';
 if deck_row.id is null then raise exception '作品不存在'; end if;
 select settings into workspace_settings from public.workspaces where id=w;
 max_value=coalesce((workspace_settings->>'maxScore')::numeric,5);
 deck_hash=public.ai_review_deck_version(w,deck_row.id);rubric_hash=public.ai_review_rubric_version(w);
 if nullif(payload->>'deckVersion','') is not null and payload->>'deckVersion'<>deck_hash then raise exception '作品版本已变化，请刷新后重新提交'; end if;
 if nullif(payload->>'rubricVersion','') is not null and payload->>'rubricVersion'<>rubric_hash then raise exception '评分标准已变化，请刷新后重新提交'; end if;
 scope_value=coalesce(payload->>'scope','deck');
 if scope_value not in ('deck','page') then raise exception '评分范围无效'; end if;
 if jsonb_typeof(payload->'units') is distinct from 'array' or jsonb_array_length(payload->'units') not between 1 and 600 then raise exception '请选择至少一个评分单元'; end if;
 for unit_json in select value from jsonb_array_elements(payload->'units') loop
  if jsonb_typeof(unit_json) is distinct from 'object' then raise exception '提交内容无效'; end if;
  unit_key=case when scope_value='page' then coalesce(unit_json->>'unitId','') else '' end;
  unit_position=case when scope_value='page' then coalesce((unit_json->>'position')::integer,0) else 0 end;
  if scope_value='page' and not exists(select 1 from jsonb_array_elements(case when jsonb_typeof(deck_row.data->'images')='array' then deck_row.data->'images' else '[]'::jsonb end) image where image->>'id'=unit_key) then raise exception '评分页面不存在'; end if;
  if jsonb_typeof(unit_json->'scores') is distinct from 'object' then raise exception '分数格式无效'; end if;
  for score_record in select key,value from jsonb_each(unit_json->'scores') loop
   if jsonb_typeof(score_record.value)<>'number' or (score_record.value::text)::numeric<0 or (score_record.value::text)::numeric>max_value then raise exception '分数超出范围'; end if;
  end loop;
  note_value=coalesce(unit_json->>'note','');if length(note_value)>10000 then raise exception '备注过长'; end if;
  if unit_json ? 'favorite' and jsonb_typeof(unit_json->'favorite')<>'boolean' then raise exception '收藏状态无效'; end if;
  if unit_json ? 'classificationSuggestion' and jsonb_typeof(unit_json->'classificationSuggestion') not in ('string','object') then raise exception '分类建议无效'; end if;
  if unit_json ? 'classificationCorrection' and jsonb_typeof(unit_json->'classificationCorrection') not in ('string','object') then raise exception '分类纠偏无效'; end if;
  if unit_json ? 'reasons' then
   if jsonb_typeof(unit_json->'reasons')<>'array' or jsonb_array_length(unit_json->'reasons')>20 then raise exception '原因选择无效'; end if;
   for reason_other_value in select value #>> '{}' from jsonb_array_elements(unit_json->'reasons') loop
    if length(trim(reason_other_value)) not between 1 and 160 then raise exception '原因选择无效'; end if;
   end loop;
  end if;
  reason_other_value=coalesce(unit_json->>'reasonOther','');
  if length(reason_other_value)>500 then raise exception '其他原因过长'; end if;
  if (unit_json->'reasons') ? 'other' and length(trim(reason_other_value))=0 then raise exception '选择“其他”时请填写原因'; end if;  entry=unit_json-'unitId'-'position';
  human_hash=public.ai_review_json_sha(jsonb_build_object('deckVersion',deck_hash,'rubricVersion',rubric_hash,'scope',scope_value,'unitId',unit_key,'entry',entry));
  -- SINGLE_JUDGE_SCORE_REVISIONS_M26
  if nullif(current_setting('guanye.single_judge_revision',true),'') is not null
     and exists(select 1 from public.workspaces ws where ws.id=w and ws.single_judge_admin_id is not null) then
   revision_token=nullif(current_setting('guanye.single_judge_revision',true),'');
   select s.human_version,s.entry,s.human_user_id into latest_human_version,latest_entry,latest_actor
   from public.ai_review_submissions s
   left join public.ai_review_decisions decision on guanye_internal.professional_web(w) and decision.submission_id=s.id
    and decision.workspace_id=w and decision.deck_id=s.deck_id and decision.professional_web
    and decision.status in ('confirmed','corrected') and decision.judge_user_id=s.human_user_id
   where s.workspace_id=w and s.deck_id=deck_row.id and s.source_kind=deck_row.source_kind
    and s.deck_version=deck_hash and s.rubric_version=rubric_hash
    and s.scope=scope_value and s.unit_id=unit_key
    and case when guanye_internal.professional_web(w) then decision.id is not null else s.human_user_id=actor end
   order by decision.created_at desc nulls last,s.created_at desc,s.id desc limit 1;
   if latest_human_version is not null and latest_entry is not distinct from entry and latest_actor=actor then
    human_hash=latest_human_version;
   elsif revision_token is not null and (latest_human_version is not null or guanye_internal.professional_web(w)) then
    human_hash=human_hash||':r:'||revision_token;
   end if;
  end if;
  if deck_row.source_kind='uploaded' and not guanye_internal.professional_web(w) then
   select data into existing_review from public.reviews where workspace_id=w and user_id=actor and deck_id=deck_row.id;
   if existing_review is null then existing_review=jsonb_build_object('scores','{}'::jsonb,'note','','favorite',false,'pages','{}'::jsonb); end if;
   if scope_value='page' then
    existing_review=existing_review||jsonb_build_object('mode','page');
    if jsonb_typeof(existing_review->'pages')<>'object' then existing_review=jsonb_set(existing_review,'{pages}','{}'::jsonb,true); end if;
    existing_review=jsonb_set(existing_review,array['pages',unit_key],coalesce(existing_review->'pages'->unit_key,'{}'::jsonb)||entry,true);
   else
    existing_review=existing_review||entry||jsonb_build_object('mode','deck');
   end if;
   insert into public.reviews(workspace_id,user_id,deck_id,data) values(w,actor,deck_row.id,existing_review)
   on conflict(workspace_id,user_id,deck_id) do update set data=excluded.data;
  end if;
  inserted_uuid=null;
  insert into public.ai_review_submissions(workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,
   deck_version,rubric_version,human_version,max_score,entry)
  values(w,deck_row.id,deck_row.source_kind,scope_value,unit_key,unit_position,actor,deck_hash,rubric_hash,human_hash,max_value,entry)
  on conflict(workspace_id,deck_id,scope,unit_id,human_user_id,human_version) do nothing returning id into inserted_uuid;
  if inserted_uuid is null then
   select id into submission_uuid from public.ai_review_submissions where workspace_id=w and deck_id=deck_row.id
    and scope=scope_value and unit_id=unit_key and human_user_id=actor and human_version=human_hash;
  else
   submission_uuid=inserted_uuid;submission_ids=submission_ids||to_jsonb(submission_uuid);
   update public.members set revision=revision+1 where workspace_id=w and user_id=actor;
  end if;
  insert into public.ai_review_matches(submission_id,record_id,variant)
  select submission_uuid,r.id,r.variant from public.ai_review_records r
  where r.workspace_id=w and r.deck_id=deck_row.id and r.scope=scope_value and r.unit_id=unit_key
   and r.deck_version=deck_hash and r.rubric_version=rubric_hash
   and public.ai_review_scores_complete(w,deck_row.id,entry,max_value)
  on conflict(submission_id,record_id) do nothing;
  select count(*) filter(where variant='baseline'),count(*) filter(where variant='assisted') into matched_baseline,matched_assisted
  from public.ai_review_matches where submission_id=submission_uuid;
 end loop;
 perform set_config('guanye.ai_review_internal','off',true);
 return jsonb_build_object('ok',true,'deckId',deck_row.id,'sourceKind',deck_row.source_kind,'scope',scope_value,
  'deckVersion',deck_hash,'rubricVersion',rubric_hash,'submissionIds',submission_ids,
  'predictionAvailable',(matched_baseline+matched_assisted)>0,'matchedBaseline',matched_baseline,'matchedAssisted',matched_assisted,
  'aiVisible',(matched_baseline+matched_assisted)>0,'humanRevision',(select revision from public.members where workspace_id=w and user_id=actor),
  'message',case when (matched_baseline+matched_assisted)>0 then '人工评分已提交，已复用同版本同粒度预评' else '人工评分已提交，等待同版本同粒度预评' end);
end $$;

create or replace function public.single_judge_submit_bundle(w uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid=auth.uid(); ws public.workspaces%rowtype; d public.decks%rowtype; unit jsonb;
 deck_unit jsonb; page_units jsonb='[]'::jsonb; seen text[]='{}'; unit_key text; page_position integer;
 maximum numeric; binding jsonb; deck_result jsonb; page_result jsonb; deck_hash text; rubric_hash text;
 submitted jsonb; submission public.ai_review_submissions%rowtype; dimensions jsonb; sanitized jsonb; decisions jsonb='[]'::jsonb; revision_token text;
begin
 select * into ws from public.workspaces where id=w for share;
 if actor is null or ws.single_judge_admin_id is null or not guanye_internal.professional_scorer(w,actor) or not public.is_member(w) then
  raise exception '仅指定评分师可提交评分' using errcode='42501';
 end if;
 if jsonb_typeof(payload) is distinct from 'object' or jsonb_typeof(payload->'units') is distinct from 'array' then
  raise exception '评分提交格式无效' using errcode='22023';
 end if;
 if jsonb_array_length(payload->'units') not between 1 and 601 then raise exception '评分单元数量无效' using errcode='22023'; end if;
 select * into d from public.decks where workspace_id=w and id=payload->>'deckId' and (not ws.professional_web or professional_archived_at is null) for update;
 if not found then raise exception '作品不存在' using errcode='22023'; end if;
 if ws.professional_web and current_setting('transaction_isolation') not in ('read committed','read uncommitted') then
  raise exception '请在 READ COMMITTED 事务中提交正式评分' using errcode='25000'; end if;
 deck_hash=public.ai_review_deck_version(w,d.id); rubric_hash=public.ai_review_rubric_version(w);
 if payload->>'deckVersion' is distinct from deck_hash or payload->>'rubricVersion' is distinct from rubric_hash then
  raise exception '作品或评分标准版本已变化，请刷新后重新提交' using errcode='22023';
 end if;
 maximum=coalesce((ws.settings->>'maxScore')::numeric,5);
 dimensions=case when d.source_kind='generated' then '[{"id":"visual"},{"id":"layout"},{"id":"match"}]'::jsonb else ws.settings->'dimensions' end;
 for unit in select value from jsonb_array_elements(payload->'units') loop
  if not public.ai_review_scores_complete(w,d.id,unit,maximum) then raise exception '请填完所评分单元的全部评分项' using errcode='22023'; end if;
  if exists(select 1 from jsonb_object_keys(unit->'scores') k
      where not exists(select 1 from jsonb_array_elements(dimensions) dim where dim->>'id'=k)) then
   raise exception '评分项不属于当前评分标准' using errcode='22023';
  end if;
  select jsonb_object_agg(key,value) into sanitized from jsonb_each(unit)
   where key in ('scores','note','favorite','classificationSuggestion','classificationCorrection','reasons','reasonOther');
  if unit->>'scope'='deck' and deck_unit is null and coalesce(unit->>'unitId','')='' then
   deck_unit=sanitized;
  elsif unit->>'scope'='page' then
   unit_key=nullif(unit->>'unitId','');
   select case when (image.value->>'position') ~ '^[1-9][0-9]{0,5}$' then (image.value->>'position')::integer else image.ordinality::integer end
   into page_position from jsonb_array_elements(d.data->'images') with ordinality as image(value,ordinality) where image.value->>'id'=unit_key;
   if not found or unit_key=any(seen) then raise exception '评分页面不存在或重复' using errcode='22023'; end if;
   seen=array_append(seen,unit_key);
   page_units=page_units || jsonb_build_array(sanitized || jsonb_build_object('unitId',unit_key,'position',page_position));
  else raise exception '每次必须提交一份整套评分' using errcode='22023';
  end if;
 end loop;
 if deck_unit is null and (not ws.professional_web or jsonb_array_length(page_units)=0) then
  raise exception '请提交整套评分或至少一个页面评分' using errcode='22023'; end if;
 binding=jsonb_build_object('deckId',d.id,'deckVersion',deck_hash,'rubricVersion',rubric_hash);
 revision_token=replace(gen_random_uuid()::text,'-','');
 perform set_config('guanye.single_judge_bundle',w::text,true);
 perform set_config('guanye.single_judge_revision',revision_token,true);
 if deck_unit is not null then
  deck_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','deck','units',jsonb_build_array(deck_unit)));
 end if;
 if jsonb_array_length(page_units)>0 then
  page_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','page','units',page_units));
 end if;
 perform set_config('guanye.single_judge_revision','',true);
 perform set_config('guanye.single_judge_bundle','',true);
 submitted=coalesce(deck_result->'submissionIds','[]'::jsonb)||coalesce(page_result->'submissionIds','[]'::jsonb);
 for submission in select s.* from public.ai_review_submissions s
   where s.workspace_id=w and s.id in (select value::uuid from jsonb_array_elements_text(submitted))
   order by s.position,s.id loop
  decisions=decisions||jsonb_build_array(public.ai_review_confirm(w,actor,jsonb_build_object(
   'submissionId',submission.id, 'formalScores',submission.entry->'scores',
   'classification',coalesce(nullif(submission.entry->'classificationCorrection','""'::jsonb),
      nullif(submission.entry->'classificationSuggestion','""'::jsonb)),
   'reasons',coalesce(submission.entry->'reasons','[]'::jsonb),'reasonOther',submission.entry->>'reasonOther')));
 end loop;
 return binding||jsonb_build_object('ok',true,'submissionIds',submitted,
  'deck',deck_result,'pages',page_result,'decisions',decisions,'message','人工评分已保存');
end $$;

create or replace function guanye_internal.single_judge_order_generated_score() returns trigger
language plpgsql security definer set search_path='' as $$
declare active boolean; previous_at timestamptz;
begin
 if guanye_internal.professional_web(new.workspace_id) then
  if tg_table_name='ai_review_submissions' then new.created_at=clock_timestamp(); end if;
  return new;
 end if;
 select ws.single_judge_admin_id is not null into active
 from public.workspaces ws where ws.id=new.workspace_id for share;
 if not coalesce(active,false) then return new; end if;
 perform 1 from public.decks d
 where d.workspace_id=new.workspace_id and d.id=new.deck_id and d.source_kind='generated' for update;
 if not found then return new; end if;
 -- A fixed old snapshot cannot use a row-lock-only protocol to observe a concurrent revision.
 if current_setting('transaction_isolation') not in ('read committed','read uncommitted') then
  raise exception '请在 READ COMMITTED 事务中提交生成作品评分' using errcode='25000';
 end if;
 if tg_table_name='ai_review_submissions' then
  select max(s.created_at) into previous_at from public.ai_review_submissions s
  where s.workspace_id=new.workspace_id and s.deck_id=new.deck_id and s.human_user_id=new.human_user_id;
 else
  select max(d.created_at) into previous_at from public.ai_review_decisions d
  where d.workspace_id=new.workspace_id and d.deck_id=new.deck_id and d.submission_id=new.submission_id;
 end if;
 new.created_at=greatest(clock_timestamp(),previous_at+interval '1 microsecond');
 return new;
end $$;

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
 if not guanye_internal.professional_scorer(w,actor) then raise exception '仅专职评委可以确认正式评分'; end if;
 if jsonb_typeof(payload) is distinct from 'object' then raise exception '确认内容无效'; end if;
 select * into submission_row from public.ai_review_submissions where workspace_id=w and id=nullif(payload->>'submissionId','')::uuid;
 if submission_row.id is null then raise exception '人工提交不存在'; end if;
 if guanye_internal.professional_web(w) and submission_row.human_user_id is distinct from actor then
  raise exception '不能代签他人的正式分' using errcode='42501'; end if;
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
  classification,reasons,reason_other,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,expires_at,summary,professional_web,created_at)
 values(w,deck_row.id,submission_row.id,record_row.id,actor,decision_key_value,status_value,classification_value,reasons_value,
  left(coalesce(payload->>'reasonOther',''),500),formal_scores_value,formal_total_value,submission_row.deck_version,submission_row.rubric_version,
  submission_row.max_score,score_ratio_value,now()+interval '7 days',left(coalesce(payload->>'summary',''),2000),
  guanye_internal.professional_web(w),case when guanye_internal.professional_web(w) then greatest(clock_timestamp(),
   (select max(x.created_at)+interval '1 microsecond' from public.ai_review_decisions x
    where x.workspace_id=w and x.deck_id=deck_row.id and x.professional_web)) else now() end)
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

create or replace function public.single_judge_review_workspace(
 w uuid,
 p_kind text,
 p_deck_id text default null
) returns jsonb
language plpgsql
stable
security definer
set search_path=''
as $$
declare
 actor uuid=auth.uid();
 ws public.workspaces%rowtype;
 member_row public.members%rowtype;
 d public.decks%rowtype;
 selected_deck_id text;
 deck_json jsonb;
 review_json jsonb;
 submissions_json jsonb='[]'::jsonb;
 records_json jsonb='[]'::jsonb;
 decisions_json jsonb='[]'::jsonb;
 settings_json jsonb;
 workspace_settings jsonb='{}'::jsonb;
 queue_json jsonb;
 queues_json jsonb;
 queue_cursors_json jsonb;
 upload_queue jsonb;
 generated_queue jsonb;

 submission_ids uuid[]='{}'::uuid[];
 deck_version_value text='';
 rubric_version_value text='';
 pre_status text='not_queued';
 warnings jsonb='[]'::jsonb;
 job_status text;
 job_attempts integer;
 job_available_at timestamptz;
 warning_key text;
begin
 -- SINGLE_JUDGE_REVIEW_WORKSPACE_M27
 if actor is null then
  raise exception '请先登录' using errcode='42501';
 end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then
  raise exception '队列类型无效' using errcode='22023';
 end if;

 select * into ws from public.workspaces where id=w;
 select * into member_row from public.members where workspace_id=w and user_id=actor;
 if ws.id is null or ws.single_judge_admin_id is null
    or not guanye_internal.professional_scorer(w,actor) or member_row.user_id is null then
  raise exception '仅该工作区指定评分师可读取专业版评审' using errcode='42501';
 end if;

 if p_deck_id is not null and length(p_deck_id)>100 then
  raise exception '作品标识无效' using errcode='22023';
 end if;

 upload_queue=public.ai_review_queue_decks(w,'uploaded',null,8);
 generated_queue=public.ai_review_queue_decks(w,'generated',null,8);
 queues_json=jsonb_build_object(
  'uploaded',coalesce(upload_queue->'items','[]'::jsonb),
  'generated',coalesce(generated_queue->'items','[]'::jsonb)
 );
 queue_cursors_json=jsonb_build_object(
  'uploaded',upload_queue->'nextCursor',
  'generated',generated_queue->'nextCursor'
 );
 queue_json=public.ai_review_queue_counts(w);

 selected_deck_id=nullif(btrim(coalesce(p_deck_id,'')),'');
 if selected_deck_id is null then
  selected_deck_id=queues_json->p_kind->0->>'id';
 end if;

 if selected_deck_id is not null then
  select * into d from public.decks where workspace_id=w and id=selected_deck_id and (not ws.professional_web or professional_archived_at is null);
  if d.id is null then
   raise exception '作品不存在' using errcode='22023';
  end if;
  if d.source_kind is distinct from p_kind then
   raise exception '作品与队列类型不匹配' using errcode='22023';
  end if;
  deck_json=to_jsonb(d);
  deck_version_value=coalesce(public.ai_review_deck_version(w,d.id),'');
 else
  deck_json='null'::jsonb;
 end if;

 rubric_version_value=coalesce(public.ai_review_rubric_version(w),'');
 workspace_settings=coalesce(ws.settings,'{}'::jsonb);

 select jsonb_build_object(
  'visionWeight',s.vision_weight,
  'layoutWeight',s.layout_weight,
  'matchWeight',s.match_weight,
  'humanThreshold',s.human_threshold,
  'modelHint',s.model_hint,
  'reasonCatalog',s.reason_catalog
 ) into settings_json
 from public.ai_review_settings s where s.workspace_id=w;
 if settings_json is null then
  raise exception 'AI 评审配置尚未初始化' using errcode='22023';
 end if;

 if d.id is not null then
  select to_jsonb(r) into review_json from public.reviews r
   where r.workspace_id=w and r.user_id=actor and r.deck_id=d.id
   limit 1;

  select coalesce(array_agg(s.id order by s.created_at desc,s.id desc),'{}'::uuid[])
  into submission_ids
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=d.id and (case when not ws.professional_web then s.human_user_id=actor else exists(
    select 1 from public.ai_review_decisions confirmed where confirmed.workspace_id=w and confirmed.deck_id=d.id
     and confirmed.submission_id=s.id and confirmed.judge_user_id=s.human_user_id and confirmed.professional_web
     and confirmed.status in ('confirmed','corrected')) end)
   and s.deck_version=deck_version_value and s.rubric_version=rubric_version_value;

  select coalesce(jsonb_agg(jsonb_build_object(
   'id',s.id,
   'deckId',s.deck_id,
   'scope',s.scope,
   'unitId',coalesce(s.unit_id,''),
   'position',s.position,
   'deckVersion',s.deck_version,
   'rubricVersion',s.rubric_version,
   'humanVersion',s.human_version,
   'actorId',s.human_user_id,
   'entry',s.entry,
   'scoresComplete',public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score),
   'createdAt',s.created_at
  ) order by s.created_at desc,s.id desc),'[]'::jsonb)
  into submissions_json
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=d.id and (case when not ws.professional_web then s.human_user_id=actor else exists(
    select 1 from public.ai_review_decisions confirmed where confirmed.workspace_id=w and confirmed.deck_id=d.id
     and confirmed.submission_id=s.id and confirmed.judge_user_id=s.human_user_id and confirmed.professional_web
     and confirmed.status in ('confirmed','corrected')) end)
   and s.deck_version=deck_version_value and s.rubric_version=rubric_version_value;

  -- ai_review_workspace_batch is service-role-only SECURITY INVOKER. Project the same
  -- frozen-match contract directly so this authenticated-only definer never needs to
  -- widen its execution role. Only matches already frozen to current-version submissions
  -- are visible; late records without a match are excluded.
  select coalesce(jsonb_agg(jsonb_build_object(
   'recordId',rr.id,
   'submissionId',rr.submission_id,
   'jobId',rr.job_id,
   'deckId',rr.deck_id,
   'scope',rr.scope,
   'unitId',coalesce(rr.unit_id,''),
   'variant',rr.variant,
   'deckVersion',rr.deck_version,
   'rubricVersion',rr.rubric_version,
   'modelHint',rr.model_hint,
   'result',coalesce(rr.ai_result,'{}'::jsonb)
  ) order by rr.created_at desc,rr.id),'[]'::jsonb)
  into records_json
  from (
   select distinct on (record.id)
    record.id,record.job_id,record.deck_id,record.scope,record.unit_id,record.variant,
    record.deck_version,record.rubric_version,record.model_hint,record.ai_result,record.created_at,
    matched.submission_id
   from public.ai_review_submissions sub
   join public.ai_review_matches matched on matched.submission_id=sub.id
   join public.ai_review_records record on record.id=matched.record_id and record.variant=matched.variant
   join public.ai_review_jobs job on job.id=record.job_id
   where sub.workspace_id=w and sub.deck_id=d.id and (case when not ws.professional_web then sub.human_user_id=actor else exists(
    select 1 from public.ai_review_decisions confirmed where confirmed.workspace_id=w and confirmed.deck_id=d.id
     and confirmed.submission_id=sub.id and confirmed.judge_user_id=sub.human_user_id and confirmed.professional_web
     and confirmed.status in ('confirmed','corrected')) end)
    and sub.deck_version=deck_version_value and sub.rubric_version=rubric_version_value
    and public.ai_review_scores_complete(w,sub.deck_id,sub.entry,sub.max_score)
    and record.workspace_id=w and job.workspace_id=w
    and record.deck_id=sub.deck_id and job.deck_id=record.deck_id
    and record.source_kind=sub.source_kind and job.source_kind=record.source_kind
    and job.variant=record.variant
    and record.scope=sub.scope and record.unit_id=sub.unit_id
    and job.scope=record.scope and job.unit_id=record.unit_id
    and record.deck_version=sub.deck_version and record.rubric_version=sub.rubric_version
    and job.deck_version=record.deck_version and job.rubric_version=record.rubric_version
    and abs(record.max_score-sub.max_score)<=1e-9
   order by record.id,record.created_at desc
  ) rr;

  select coalesce(jsonb_agg(jsonb_build_object(
   'decisionId',decision.id,
   'submissionId',decision.submission_id,
   'aiRecordId',decision.ai_record_id,
   'status',decision.status,
   'classification',decision.classification,
   'reasons',coalesce(decision.reasons,'[]'::jsonb),
   'reasonOther',coalesce(decision.reason_other,''),
   'formalScores',decision.formal_scores,
   'formalTotal',decision.formal_total,
   'scoreRatio',decision.score_ratio,
   'expiresAt',decision.expires_at,
   'confirmedAt',decision.created_at,
   'actorId',decision.judge_user_id,
   'libraryVisible',coalesce(entry.library_visible,false),
   'entryId',entry.id,
   'summary',coalesce(decision.summary,'')
  ) order by decision.created_at desc,decision.id),'[]'::jsonb)
  into decisions_json
  from public.ai_review_submissions sub
  join public.ai_review_decisions decision
   on decision.submission_id=sub.id and decision.workspace_id=w and decision.deck_id=sub.deck_id
   and (not ws.professional_web or decision.professional_web and decision.judge_user_id=sub.human_user_id)
  left join lateral (
   select generated.id,generated.library_visible
   from public.ai_generated_entries generated
   where generated.workspace_id=w and generated.deck_id=decision.deck_id and generated.decision_id=decision.id
   order by generated.created_at desc,generated.id desc
   limit 1
  ) entry on true
  where sub.workspace_id=w and sub.deck_id=d.id and (case when not ws.professional_web then sub.human_user_id=actor else exists(
    select 1 from public.ai_review_decisions confirmed where confirmed.workspace_id=w and confirmed.deck_id=d.id
     and confirmed.submission_id=sub.id and confirmed.judge_user_id=sub.human_user_id and confirmed.professional_web
     and confirmed.status in ('confirmed','corrected')) end)
   and sub.deck_version=deck_version_value and sub.rubric_version=rubric_version_value;

  select j.status,j.attempts,j.available_at
  into job_status,job_attempts,job_available_at
  from public.ai_review_jobs j
  where j.workspace_id=w and j.deck_id=d.id and j.source_kind=p_kind
   and j.scope='deck' and j.deck_version=deck_version_value and j.rubric_version=rubric_version_value
   and (p_kind<>'generated' or j.variant='assisted')
  order by (p_kind='uploaded' and j.variant='baseline') desc,j.updated_at desc,j.created_at desc
  limit 1;
  if found then
   if job_status in ('queued','running','completed') then
    pre_status=job_status;
   elsif job_status='failed' and job_attempts<3 and job_available_at is distinct from 'infinity'::timestamptz then
    pre_status='retry_scheduled';
   else
    pre_status='failed';
   end if;
  end if;

  if d.source_kind='generated' then
   if jsonb_typeof(d.data->'generation') is distinct from 'object' then
    warnings=jsonb_build_array('generated 作品缺少 decks.data.generation，已按常规 images 合同继续');
   else
    foreach warning_key in array array['outline','style','pages','references'] loop
     if not (d.data->'generation' ? warning_key) then
      warnings=warnings||jsonb_build_array('decks.data.generation 缺少 '||warning_key);
     end if;
    end loop;
   end if;
  end if;
 end if;

 return jsonb_build_object(
  'kind',p_kind,
  'deck',coalesce(deck_json,'null'::jsonb),
  'review',coalesce(review_json,'null'::jsonb),
  'submissions',submissions_json,
  'ai',records_json,
  'decisions',decisions_json,
  'settings',settings_json,
  'workspaceSettings',workspace_settings,
  'role',member_row.role,
  'isJudge',true,
  'singleJudge',true,
  'professionalWeb',ws.professional_web,
  'queue',queue_json,
  'queues',queues_json,
  'queueCursors',queue_cursors_json,
  'preEvaluationStatus',pre_status,
  'deckVersion',deck_version_value,
  'rubricVersion',rubric_version_value,
  'contractWarnings',warnings,
  'blind',jsonb_build_object(
   'aiVisible',jsonb_array_length(records_json)>0,
   'rule','仅与已提交人工记录同版本、同评分标准、同粒度的预评解锁'
  )
 );
end $$;

create or replace function public.single_judge_review_queue(
 w uuid,p_kind text,p_after text,p_limit integer
) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_REVIEW_QUEUE_M33
 access_info=public.single_judge_access(w);
 if access_info->>'mode' is distinct from 'single-judge' or (access_info->>'role' is distinct from 'judge' and
    not (guanye_internal.professional_web(w) and access_info->>'role'='admin')) then
  raise exception '仅指定专业评分师可以读取评审队列' using errcode='42501';
 end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then
  raise exception '队列类型无效' using errcode='22023'; end if;
 if p_after is not null and p_after='' then
  raise exception '分页游标无效' using errcode='22023'; end if;
 if p_limit is null or p_limit not between 1 and 32 then
  raise exception '分页大小无效' using errcode='22023'; end if;
 return public.ai_review_queue_decks(w,p_kind,p_after,p_limit);
end $$;

create or replace function public.single_judge_backfill(
 w uuid,p_kind text,p_offset integer,p_limit integer
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_LEGACY_BACKFILL_M32
 access_info=public.single_judge_access(w);
 if access_info->>'mode' is distinct from 'single-judge' or (access_info->>'role' is distinct from 'judge' and
    not (guanye_internal.professional_web(w) and access_info->>'role'='admin')) then
  raise exception '仅指定专业评分师可以补档老库' using errcode='42501';
 end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then
  raise exception '队列类型无效' using errcode='22023'; end if;
 if p_offset is null or p_offset<0 or p_limit is null or p_limit not between 1 and 20 then
  raise exception '补档分页参数无效' using errcode='22023'; end if;
 return public.ai_review_backfill(w,(select dedicated_judge_id from public.workspaces where id=w),p_kind,p_offset,p_limit);
end $$;

create or replace function public.ai_review_uploaded_formal_scores(w uuid)
returns table(deck_id text,scope text,unit_id text,score numeric)
language sql stable security definer set search_path='' as $$
 with workspace_scale as (
   select coalesce((ws.settings->>'maxScore')::numeric,5) as max_score
   from public.workspaces ws where ws.id=w
 ), current_pages as (
   select distinct d.id as deck_id,image->>'id' as unit_id
   from public.decks d
   cross join lateral jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) image
   where d.workspace_id=w
     and nullif(image->>'id','') is not null
     and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
     and strpos(coalesce(image->>'path',''),'..')=0
     and strpos(coalesce(image->>'path',''),chr(92))=0
 ), ranked as (
   -- LIBRARY_FORMAL_SQL_V14
   select distinct on (x.deck_id,s.scope,s.unit_id)
     x.deck_id,s.scope,s.unit_id,x.formal_total as score
   from public.workspaces ws
   join public.decks d on d.workspace_id=ws.id
   join public.ai_review_submissions s on s.workspace_id=ws.id and s.deck_id=d.id
   join public.ai_review_decisions x on x.workspace_id=ws.id and x.deck_id=d.id and x.submission_id=s.id
   cross join workspace_scale scale
   where ws.id=w
     and ws.dedicated_judge_id is not null
     and d.source_kind='uploaded'
     and case when ws.professional_web then x.professional_web and d.professional_archived_at is null else x.judge_user_id=ws.dedicated_judge_id end
     and x.status in ('confirmed','corrected')
     and x.formal_total is not null
     and x.deck_version=public.ai_review_deck_version(w,d.id)
     and x.rubric_version=public.ai_review_rubric_version(w)
     and x.max_score=scale.max_score
     and public.ai_review_scores_complete(w,d.id,jsonb_build_object('scores',x.formal_scores),x.max_score)
     and (s.scope='deck' or exists(
       select 1 from current_pages p where p.deck_id=d.id and p.unit_id=s.unit_id
     ))
   order by x.deck_id,s.scope,s.unit_id,x.created_at desc,x.id desc
 )
 select ranked.deck_id,ranked.scope,ranked.unit_id,ranked.score from ranked
$$;

create or replace function public.ai_review_effective_meta(w uuid, target_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare
 deck_data jsonb;
 deck_kind text;
 deck_hash text;
 rubric_hash text;
 existing_class text;
 existing_style text;
 formal_class text;
 formal_style text;
 human_class text;
 human_style text;
 ai_class text;
 ai_style text;
 has_submission boolean=false;
 has_legacy_review_evidence boolean=false;
 class_source text='none';
 style_source text='none';
begin
 -- LIBRARY_CLASSIFICATION_BACKFILL_V18
 select data,source_kind into deck_data,deck_kind from public.decks where workspace_id=w and id=target_id;
 if deck_data is null then
  return jsonb_build_object('effectiveClassification',null,'effectiveStyle',null,
   'classificationSource','none','styleSource','none');
 end if;

 existing_class=coalesce(
  nullif(btrim(case
   when jsonb_typeof(deck_data->'classification')='string' then deck_data->>'classification'
   when jsonb_typeof(deck_data->'classification')='object' then coalesce(
    nullif(btrim(deck_data#>>'{classification,primary}'),''),
    nullif(btrim(deck_data#>>'{classification,label}'),''),
    nullif(btrim(deck_data#>>'{classification,name}'),''),
    nullif(btrim(deck_data#>>'{classification,category}'),''))
  end),''),
  nullif(btrim(deck_data->>'category'),''),
  nullif(btrim(deck_data->>'primaryClassification'),''));
 existing_style=nullif(btrim(deck_data->>'style'),'');
 deck_hash=public.ai_review_deck_version(w,target_id);
 rubric_hash=public.ai_review_rubric_version(w);

 with valid_formal as (
  select d.created_at,d.id,
   nullif(btrim(case
    when jsonb_typeof(d.classification)='string' then d.classification #>> '{}'
    when jsonb_typeof(d.classification)='object' then coalesce(
     nullif(btrim(d.classification->>'primary'),''),
     nullif(btrim(d.classification->>'label'),''),
     nullif(btrim(d.classification->>'name'),''),
     nullif(btrim(d.classification->>'category'),''))
   end),'') as class_value,
   nullif(btrim(case when jsonb_typeof(d.classification)='object' then d.classification->>'style' end),'') as style_value
  from public.ai_review_decisions d
  join public.ai_review_submissions s on s.id=d.submission_id
  join public.workspaces ws on ws.id=d.workspace_id
  where d.workspace_id=w and d.deck_id=target_id
   and case when ws.professional_web then d.professional_web else d.judge_user_id=ws.dedicated_judge_id end
   and d.status in ('confirmed','corrected')
   and s.workspace_id=w and s.deck_id=d.deck_id and s.scope='deck' and s.unit_id=''
   and d.deck_version=deck_hash and d.rubric_version=rubric_hash
   and s.deck_version=d.deck_version and s.rubric_version=d.rubric_version
   and d.max_score=coalesce((ws.settings->>'maxScore')::numeric,5)
   and public.ai_review_scores_complete(w,d.deck_id,jsonb_build_object('scores',d.formal_scores),d.max_score)
 )
 select
  (array_agg(class_value order by created_at desc,id desc) filter(where class_value is not null))[1],
  (array_agg(style_value order by created_at desc,id desc) filter(where style_value is not null))[1]
 into formal_class,formal_style
 from valid_formal;

 with valid_human as (
  select s.created_at,s.id,
   nullif(btrim(case
    when jsonb_typeof(s.entry->'classificationCorrection')='string' then s.entry->'classificationCorrection' #>> '{}'
    when jsonb_typeof(s.entry->'classificationCorrection')='object' then coalesce(
     nullif(btrim(s.entry#>>'{classificationCorrection,primary}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,label}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,name}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,category}'),''))
   end),'') as class_value,
   nullif(btrim(case when jsonb_typeof(s.entry->'classificationCorrection')='object'
    then s.entry#>>'{classificationCorrection,style}' end),'') as style_value
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=target_id and s.scope='deck' and s.unit_id=''
   and s.deck_version=deck_hash and s.rubric_version=rubric_hash
   and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
 )
 select exists(select 1 from valid_human),
  (array_agg(class_value order by created_at desc,id desc) filter(where class_value is not null))[1],
  (array_agg(style_value order by created_at desc,id desc) filter(where style_value is not null))[1]
 into has_submission,human_class,human_style
 from valid_human;

 -- v12 intentionally requires a valid whole-deck human submission before AI
 -- metadata is visible. For untouched historical uploads only, an existing
 -- valid review is the human evidence that requests the deck forecast. The
 -- fallback accepts both the original whole-deck reviews.scores shape and the
 -- existing page-level shape. Any modern submission keeps the normal blind
 -- gate, and generated/new works never enter this legacy branch.
 if deck_kind='uploaded' and not has_submission
    and not exists(select 1 from public.ai_review_submissions s where s.workspace_id=w and s.deck_id=target_id) then
  with current_rubric as (
   select coalesce((ws.settings->>'maxScore')::numeric,5) as max_score,
    case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end as dimensions
   from public.workspaces ws
   where ws.id=w
  )
  select
   exists(
    select 1
    from public.reviews r
    cross join current_rubric rb
    where r.workspace_id=w and r.deck_id=target_id
     and public.ai_review_scores_complete(
      w,target_id,jsonb_build_object('scores',r.data->'scores'),rb.max_score
     )
   )
   or exists(
    select 1
    from public.reviews r
    cross join lateral jsonb_each(
     case when jsonb_typeof(r.data->'pages')='object' then r.data->'pages' else '{}'::jsonb end
    ) as page(unit_id,entry)
    cross join current_rubric rb
    where r.workspace_id=w and r.deck_id=target_id
     and exists(
      select 1
      from jsonb_array_elements(
       case when jsonb_typeof(deck_data->'images')='array' then deck_data->'images' else '[]'::jsonb end
      ) image
      where image->>'id'=page.unit_id
       and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
     )
     and jsonb_typeof(page.entry->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(score.value)='number' then (score.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(page.entry->'scores')='object' then page.entry->'scores' else '{}'::jsonb end) score
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(page.entry->'scores')='object' then page.entry->'scores' else '{}'::jsonb end) score
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=score.key
      )
     )
   )
  into has_legacy_review_evidence;
 end if;

 if has_submission or has_legacy_review_evidence then
  with valid_ai as (
   select r.created_at,r.id,case when r.variant='assisted' then 0 else 1 end as variant_order,
    nullif(btrim(case
     when jsonb_typeof(r.ai_result->'classification')='string' then r.ai_result->'classification' #>> '{}'
     when jsonb_typeof(r.ai_result->'classification')='object' then coalesce(
      nullif(btrim(r.ai_result#>>'{classification,primary}'),''),
      nullif(btrim(r.ai_result#>>'{classification,label}'),''),
      nullif(btrim(r.ai_result#>>'{classification,name}'),''),
      nullif(btrim(r.ai_result#>>'{classification,category}'),''))
    end),'') as class_value,
    nullif(btrim(case when jsonb_typeof(r.ai_result->'classification')='object'
     then r.ai_result#>>'{classification,style}' end),'') as style_value
   from public.ai_review_records r
   where r.workspace_id=w and r.deck_id=target_id and r.scope='deck' and r.unit_id=''
    and r.deck_version=deck_hash and r.rubric_version=rubric_hash
  )
  select
   (array_agg(class_value order by variant_order,created_at desc,id desc) filter(where class_value is not null))[1],
   (array_agg(style_value order by variant_order,created_at desc,id desc) filter(where style_value is not null))[1]
  into ai_class,ai_style
  from valid_ai;
 end if;

 if formal_class is not null then human_class=formal_class;class_source='human';
 elsif human_class is not null then class_source='human';
 elsif existing_class is not null then human_class=existing_class;class_source='existing';
 elsif ai_class is not null then human_class=ai_class;class_source='ai';
 end if;
 if formal_style is not null then human_style=formal_style;style_source='human';
 elsif human_style is not null then style_source='human';
 elsif existing_style is not null then human_style=existing_style;style_source='existing';
 elsif ai_style is not null then human_style=ai_style;style_source='ai';
 end if;
 return jsonb_build_object('effectiveClassification',human_class,'effectiveStyle',human_style,
  'classificationSource',class_source,'styleSource',style_source);
end $$;

create or replace function guanye_internal.single_judge_creation_actor(w uuid) returns uuid
language plpgsql stable security definer set search_path='' as $$
declare actor uuid := auth.uid();
begin
 if actor is null or not exists (
  select 1 from public.workspaces ws join public.members m on m.workspace_id=ws.id
  where ws.id=w and ws.single_judge_admin_id is not null
    and (ws.dedicated_judge_id=actor or ws.professional_web and ws.single_judge_admin_id=actor) and m.user_id=actor
 ) then raise exception '仅当前工作区专职评分师可操作' using errcode='42501'; end if;
 return actor;
end $$;

create or replace function guanye_internal.single_judge_generation_style_reference_ok(
 p_workspace uuid,
 p_job public.ai_generation_jobs,
 p_path text
) returns boolean
language plpgsql
stable
security definer
set search_path=''
as $$
declare
 plan_uuid uuid;
 plan_row public.ai_planning_plans%rowtype;
 current_plan jsonb;
 version_confirmed_by uuid;
 path_parts text[];
begin
 -- SINGLE_JUDGE_GENERATION_REFERENCES_M29
 if p_workspace is null or p_job.id is null or p_path is null
    or p_job.workspace_id is distinct from p_workspace then
  return false;
 end if;
 if p_job.plan_id !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' then
  return false;
 end if;

 -- Style inputs are reserved for the current professional judge using a plan
 -- that the judge personally created and confirmed. Legacy admin or version
 -- grants do not widen this read authority.
 if not guanye_internal.professional_scorer(p_workspace,p_job.user_id) then return false; end if;

 plan_uuid=p_job.plan_id::uuid;
 select * into plan_row
 from public.ai_planning_plans p
 where p.id=plan_uuid and p.workspace_id=p_workspace;
 if not found
    or plan_row.status<>'confirmed'
    or plan_row.current_version_id is null
    or plan_row.created_by is distinct from p_job.user_id then
  return false;
 end if;
 select v.confirmed_by into version_confirmed_by
 from public.ai_planning_versions v
 where v.id=plan_row.current_version_id
   and v.plan_id=plan_uuid
   and v.workspace_id=p_workspace;
 if not found or version_confirmed_by is distinct from p_job.user_id then return false; end if;

 -- The generation job must still snapshot the current immutable plan version.
 begin
  current_plan=public.ai_planning_read_confirmed_plan(plan_uuid);
 exception when others then
  return false;
 end;
 if current_plan is null
    or current_plan->>'planId' is distinct from plan_uuid::text
    or current_plan->>'workspaceId' is distinct from p_workspace::text
    or current_plan->>'versionId' is distinct from plan_row.current_version_id::text
    or current_plan->>'confirmedBy' is distinct from p_job.user_id::text
    or p_job.plan_snapshot is distinct from current_plan then
  return false;
 end if;

 -- The path must be explicitly present in the current confirmed plan's style
 -- inputs, not inferred from a mutable job snapshot or path prefix alone.
 if not exists(
  select 1
  from jsonb_array_elements(
   case when jsonb_typeof(current_plan->'generationStyleImages')='array'
    then current_plan->'generationStyleImages' else '[]'::jsonb end
  ) value
  where value #>> '{}'=p_path
 ) then
  return false;
 end if;

 path_parts=string_to_array(p_path,'/');
 if array_length(path_parts,1) is distinct from 5
    or path_parts[1] is distinct from p_workspace::text
    or path_parts[2] is distinct from 'ai-planning'
    or path_parts[3] is distinct from plan_uuid::text
    or path_parts[4] not in ('reference','style-rendered')
    or path_parts[5] is null
    or path_parts[5]=''
    or position('..' in p_path)>0
    or position(chr(92) in p_path)>0
    or position(chr(13) in p_path)>0
    or position(chr(10) in p_path)>0 then
  return false;
 end if;
 if not exists(
  select 1 from storage.objects o
  where o.bucket_id='ppt-library' and o.name=p_path
 ) then
  return false;
 end if;
 return true;
end $$;

create or replace function public.single_judge_admit(w uuid,p_submission uuid,p_decision uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid=auth.uid(); ws public.workspaces%rowtype; deck_row public.decks%rowtype;
 submission_row public.ai_review_submissions%rowtype; decision_row public.ai_review_decisions%rowtype;
 entry_row public.ai_generated_entries%rowtype; threshold_value numeric;
begin
 select * into ws from public.workspaces where id=w for share;
 if actor is null or ws.single_judge_admin_id is null or not guanye_internal.professional_scorer(w,actor)
    or not public.is_member(w) then
  raise exception '仅指定评分师可收入素材库' using errcode='42501';
 end if;
 if current_setting('transaction_isolation') not in ('read committed','read uncommitted') then
  raise exception '请在 READ COMMITTED 事务中确认入库' using errcode='25000';
 end if;
 select * into submission_row from public.ai_review_submissions s
 where s.workspace_id=w and s.id=p_submission and (s.human_user_id=actor or ws.professional_web and actor=ws.single_judge_admin_id);
 if not found or submission_row.source_kind<>'generated' then
  raise exception '未找到可入库的生成作品评分' using errcode='22023';
 end if;
 -- Same workspace -> deck lock order and row as M26, before checking decisions or visibility.
 select * into deck_row from public.decks d
 where d.workspace_id=w and d.id=submission_row.deck_id and d.source_kind='generated' and (not ws.professional_web or d.professional_archived_at is null) for update;
 if not found then raise exception '未找到可入库的生成作品' using errcode='22023'; end if;
 select * into decision_row from public.ai_review_decisions d
 where d.workspace_id=w and d.deck_id=deck_row.id and d.id=p_decision and d.submission_id=p_submission
   and d.judge_user_id=submission_row.human_user_id and (not ws.professional_web or d.professional_web) and d.deck_version=submission_row.deck_version and d.rubric_version=submission_row.rubric_version;
 if not found then raise exception '未找到对应的正式评分' using errcode='22023'; end if;

 -- Return the actual existing admission on repeated confirmation, even if it is now stale.
 -- On different IDs prefer the newest visible historical entry (the library also prefers newest).
 select * into entry_row from public.ai_generated_entries e
 where e.workspace_id=w and e.deck_id=deck_row.id and e.scope=submission_row.scope
   and e.unit_id=submission_row.unit_id and e.library_visible
 order by (e.decision_id=p_decision) desc,e.created_at desc,e.id desc limit 1;
 if entry_row.decision_id is distinct from p_decision then
  -- Rank by formal *submission* first. A later legacy confirm on an older submission
  -- cannot outrank a revision. Unresolvable historical timestamp ties fail closed.
  if exists (
   select 1 from public.ai_review_submissions s
   join public.ai_review_decisions d on d.submission_id=s.id and d.workspace_id=s.workspace_id
    and d.deck_id=s.deck_id and (d.judge_user_id=actor or ws.professional_web) and d.deck_version=s.deck_version and d.rubric_version=s.rubric_version
   where s.workspace_id=w and s.deck_id=deck_row.id and (s.human_user_id=actor or ws.professional_web) and s.source_kind='generated'
    and s.scope=submission_row.scope and s.unit_id=submission_row.unit_id
    and s.deck_version=submission_row.deck_version and s.rubric_version=submission_row.rubric_version
    and d.id<>p_decision and case when ws.professional_web then d.professional_web and d.created_at>=decision_row.created_at
    else s.created_at>submission_row.created_at or
     (s.created_at=submission_row.created_at and (s.id<>p_submission or d.created_at>=decision_row.created_at)) end
  ) then raise exception '只能使用该评分单元的最新正式评分入库' using errcode='22023'; end if;
  if decision_row.formal_scores is distinct from submission_row.entry->'scores' then
   raise exception '正式分与评委提交的人分不一致' using errcode='22023';
  end if;
  -- Stabilize the rubric weights and threshold through the existing admission write.
  select s.human_threshold into threshold_value from public.ai_review_settings s where s.workspace_id=w for share;
  if not found then raise exception '未找到正式评分门槛' using errcode='22023'; end if;
  if entry_row.id is null then
   return guanye_internal.ai_review_admit(w,actor,jsonb_build_object('submissionId',p_submission,'decisionId',p_decision));
  end if;
  -- Another accepted decision for this unit is already visible. No new write is needed.
  if decision_row.expires_at<=now()
    or decision_row.deck_version is distinct from public.ai_review_deck_version(w,deck_row.id)
    or decision_row.rubric_version is distinct from public.ai_review_rubric_version(w)
    or decision_row.max_score<1 or decision_row.formal_total is null
    or round(decision_row.formal_total/decision_row.max_score,6)<threshold_value then
   raise exception '当前正式评分已失效或未达到入库门槛' using errcode='22023';
  end if;
 end if;
 return jsonb_build_object('ok',true,'idempotent',true,'admitted',true,'entryId',entry_row.id,
  'decisionId',entry_row.decision_id,'deckId',deck_row.id,'scope',entry_row.scope,'unitId',entry_row.unit_id,
  'scoreRatio',entry_row.score_ratio,'libraryLabel','AI 自我学习生成');
end $$;

create or replace function guanye_internal.ai_review_admit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare submission_row public.ai_review_submissions%rowtype; decision_row public.ai_review_decisions%rowtype; entry_row public.ai_generated_entries%rowtype;
 deck_row public.decks%rowtype; settings_row public.ai_review_settings%rowtype; ratio_value numeric;
begin
 if not guanye_internal.professional_scorer(w,actor) then raise exception '仅专职评委可以收入素材库'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or nullif(payload->>'submissionId','') is null then raise exception '缺少人工提交'; end if;
 select * into submission_row from public.ai_review_submissions where workspace_id=w and id=(payload->>'submissionId')::uuid;
 if submission_row.id is null then raise exception '人工提交不存在'; end if;
 if submission_row.source_kind<>'generated' then raise exception '仅 AI 生成作品需要入库确认'; end if;
 if nullif(payload->>'decisionId','') is not null then
  select * into decision_row from public.ai_review_decisions where workspace_id=w and submission_id=submission_row.id and id=(payload->>'decisionId')::uuid;
 else
  select * into decision_row from public.ai_review_decisions where workspace_id=w and submission_id=submission_row.id order by created_at desc limit 1;
 end if;
 if decision_row.id is null then raise exception '未找到正式评分'; end if;
 if decision_row.expires_at<=now() then raise exception '正式评分已过期，请重新确认后再入库'; end if;
 if decision_row.status not in ('confirmed','corrected') then raise exception '正式评分状态无效'; end if;
 if decision_row.deck_version<>public.ai_review_deck_version(w,submission_row.deck_id) then raise exception '作品版本已变化，正式评分不能入库'; end if;
 if decision_row.rubric_version<>public.ai_review_rubric_version(w) then raise exception '评分标准已变化，正式评分不能入库'; end if;
 if decision_row.max_score is null or decision_row.max_score<1 or decision_row.formal_total is null then raise exception '正式评分缺少满分或总分'; end if;
 ratio_value=round(decision_row.formal_total/decision_row.max_score,6);
 select * into settings_row from public.ai_review_settings where workspace_id=w;
 if ratio_value<settings_row.human_threshold then
  raise exception '正式加权分未达到满分门槛：% < %',ratio_value,settings_row.human_threshold;
 end if;
 select * into entry_row from public.ai_generated_entries where decision_id=decision_row.id for update;
 if entry_row.id is null then
  select * into deck_row from public.decks where workspace_id=w and id=submission_row.deck_id;
  insert into public.ai_generated_entries(workspace_id,deck_id,decision_id,scope,unit_id,source,reference_refs,formal_scores,formal_total,weights,
   human_threshold,human_coverage,threshold_met,ai_self_learning,library_visible,library_label,score_ratio)
  values(w,submission_row.deck_id,decision_row.id,submission_row.scope,submission_row.unit_id,
   coalesce(deck_row.data->'generation','{}'::jsonb),coalesce(deck_row.data->'generation'->'references',deck_row.data->'references','[]'::jsonb),
   decision_row.formal_scores,decision_row.formal_total,jsonb_build_object('visual',settings_row.vision_weight,'layout',settings_row.layout_weight,'match',settings_row.match_weight),
   settings_row.human_threshold,0,true,true,false,'AI 自我学习生成',ratio_value)
  returning * into entry_row;
 end if;
 if entry_row.library_visible then
  return jsonb_build_object('ok',true,'idempotent',true,'admitted',true,'entryId',entry_row.id,'decisionId',decision_row.id,
   'deckId',submission_row.deck_id,'scope',submission_row.scope,'unitId',submission_row.unit_id,'scoreRatio',ratio_value,
   'libraryLabel','AI 自我学习生成');
 end if;
 update public.ai_generated_entries set library_visible=true,threshold_met=true,score_ratio=ratio_value,admitted_at=now(),admitted_by=actor,library_label='AI 自我学习生成'
 where id=entry_row.id returning * into entry_row;
 return jsonb_build_object('ok',true,'idempotent',false,'admitted',true,'entryId',entry_row.id,'decisionId',decision_row.id,
  'deckId',submission_row.deck_id,'scope',submission_row.scope,'unitId',submission_row.unit_id,'scoreRatio',ratio_value,
  'threshold',settings_row.human_threshold,'libraryLabel','AI 自我学习生成','deckDataMutation',false);
end $$;

create or replace function public.get_review_library(w uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if not public.is_member(w) then raise exception '请先加入团队'; end if;
 return (
  with whole as (
   select distinct on (e.deck_id) e.* from public.ai_generated_entries e
   where e.workspace_id=w and (not guanye_internal.professional_web(w) or exists(
    select 1 from public.decks d where d.workspace_id=w and d.id=e.deck_id and d.professional_archived_at is null)) and e.scope='deck' and e.library_visible
   order by e.deck_id,e.created_at desc
  ), page_entries as (
   select distinct on (e.deck_id,e.unit_id) e.* from public.ai_generated_entries e
   where e.workspace_id=w and (not guanye_internal.professional_web(w) or exists(
    select 1 from public.decks d where d.workspace_id=w and d.id=e.deck_id and d.professional_archived_at is null)) and e.scope='page' and e.library_visible
   order by e.deck_id,e.unit_id,e.created_at desc
  ), library_decks as (
   select d.id, d.data || jsonb_build_object('sourceKind','uploaded','libraryScope','deck') || public.ai_review_effective_meta(w,d.id) as deck
   from public.decks d where d.workspace_id=w and d.source_kind='uploaded' and (not guanye_internal.professional_web(w) or d.professional_archived_at is null)
   union all
   select d.id, d.data || jsonb_build_object('sourceKind','generated','libraryScope','deck',
    'generatedEntryId',whole.id,'formalScore',whole.formal_total,'aiLabel','AI 自我学习生成','libraryLabel','AI 自我学习生成') || public.ai_review_effective_meta(w,d.id) as deck
   from public.decks d join whole on whole.deck_id=d.id
   where d.workspace_id=w and d.source_kind='generated'
   union all
   select d.id, jsonb_build_object(
    'id',d.id,'name',coalesce(d.data->>'name',d.id),'title',coalesce(d.data->>'title',d.data->>'name',d.id),
    'style',coalesce(d.data->>'style',''),'origin',coalesce(d.data->>'origin','generated'),
    'sourceKind','generated','libraryScope','pages','aiLabel','AI 自我学习生成','libraryLabel','AI 自我学习生成',
    'effectiveClassification',public.ai_review_effective_meta(w,d.id)->'effectiveClassification','effectiveStyle',public.ai_review_effective_meta(w,d.id)->'effectiveStyle',
    'classificationSource',public.ai_review_effective_meta(w,d.id)->'classificationSource','styleSource',public.ai_review_effective_meta(w,d.id)->'styleSource',
    'images',coalesce((
     select jsonb_agg(image order by ordinality)
     from jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end)
      with ordinality as x(image,ordinality)
     where exists(select 1 from page_entries p where p.deck_id=d.id and p.unit_id=image->>'id')
    ),'[]'::jsonb),
    'approvedPageIds',coalesce((select jsonb_agg(p.unit_id order by p.unit_id) from page_entries p where p.deck_id=d.id),'[]'::jsonb),
    'generatedPageScores',coalesce((select jsonb_object_agg(p.unit_id,jsonb_build_object('formalScores',p.formal_scores,'formalTotal',p.formal_total,'entryId',p.id,'aiLabel','AI 自我学习生成')) from page_entries p where p.deck_id=d.id),'{}'::jsonb)
   ) as deck
   from public.decks d
   where d.workspace_id=w and d.source_kind='generated'
    and not exists(select 1 from whole x where x.deck_id=d.id)
    and exists(select 1 from page_entries p where p.deck_id=d.id)
  ), entries as (
   select e.id,e.deck_id,e.scope,e.unit_id,e.formal_scores,e.formal_total,e.created_at
   from whole e union all select e.id,e.deck_id,e.scope,e.unit_id,e.formal_scores,e.formal_total,e.created_at from page_entries e
  )
  select jsonb_build_object(
   'schemaVersion',1,'workspaceId',w,
   'decks',coalesce((select jsonb_agg(jsonb_build_object('id',id,'data',deck) order by id) from library_decks),'[]'::jsonb),
   'entries',coalesce((select jsonb_agg(jsonb_build_object('id',id,'deckId',deck_id,'scope',scope,'unitId',unit_id,
    'formalScores',formal_scores,'formalTotal',formal_total) order by created_at desc) from entries),'[]'::jsonb)
  )
 );
end $$;

create or replace function public.ai_review_queue_decks(
 w uuid,
 p_kind text,
 p_after text,
 p_limit integer
) returns jsonb
language plpgsql
stable
security definer
set search_path=''
as $$
begin
 if w is null then raise exception '工作区不能为空'; end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if p_limit is null or p_limit<1 or p_limit>50 then raise exception '分页大小无效'; end if;
 -- Cursors are opaque deck ids: reject only the empty string and compare all other text verbatim.
 if p_after='' then raise exception '分页游标无效'; end if;

 return (
  with candidates as (
   select
    d.id,
    coalesce(nullif(d.data->>'title',''),nullif(d.data->>'name',''),d.id) as title,
    coalesce(nullif(d.data->>'name',''),d.id) as deck_name,
    d.source_kind
   from public.decks d
   where d.workspace_id=w
     and d.source_kind=p_kind
     and (not guanye_internal.professional_web(w) or d.professional_archived_at is null)
     and (p_after is null or d.id>p_after)
   order by d.id
   limit p_limit+1
  ),
  page as (
   select * from candidates order by id limit p_limit
  )
  select jsonb_build_object(
   'items',coalesce((
    select jsonb_agg(jsonb_build_object(
     'id',p.id,
     'title',p.title,
     'name',p.deck_name,
     'sourceKind',p.source_kind
    ) order by p.id)
    from page p
   ),'[]'::jsonb),
   'nextCursor',case
    when (select count(*) from candidates)>p_limit then (select p.id from page p order by p.id desc limit 1)
    else null
   end
  )
 );
end $$;

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
    and (not guanye_internal.professional_web(w) or d.professional_archived_at is null)
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

create or replace function public.ai_review_queue_counts(w uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path=''
as $$
declare result jsonb;
begin
 if w is null then raise exception '工作区不能为空'; end if;

 select jsonb_build_object(
  'uploaded',jsonb_build_object(
   'queued',count(*) filter (where source_kind='uploaded' and status='queued'),
   'running',count(*) filter (where source_kind='uploaded' and status='running'),
   'completed',count(*) filter (where source_kind='uploaded' and status='completed'),
   'failed',count(*) filter (where source_kind='uploaded' and status='failed')
  ),
  'generated',jsonb_build_object(
   'queued',count(*) filter (where source_kind='generated' and status='queued'),
   'running',count(*) filter (where source_kind='generated' and status='running'),
   'completed',count(*) filter (where source_kind='generated' and status='completed'),
   'failed',count(*) filter (where source_kind='generated' and status='failed')
  )
 ) into result
 from public.ai_review_jobs j
 where j.workspace_id=w and (not guanye_internal.professional_web(w) or exists(
  select 1 from public.decks d where d.workspace_id=w and d.id=j.deck_id and d.professional_archived_at is null));
 return result;
end $$;

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
 where d.workspace_id=w and d.source_kind=p_kind and d.ai_allowed
  and (not guanye_internal.professional_web(w) or d.professional_archived_at is null);

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
  and (not guanye_internal.professional_web(w) or d.professional_archived_at is null)
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

-- The browser and internet gateway both require the database cutover flag.
create or replace function public.single_judge_access(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('mode','single-judge','role',
  case when ws.dedicated_judge_id=auth.uid() then 'judge'
       when ws.single_judge_admin_id=auth.uid() then 'admin'
       else 'submitter' end) || case when ws.professional_web then '{"professionalWeb":true}'::jsonb else '{}'::jsonb end
 from public.workspaces ws
 join public.members m on m.workspace_id=ws.id and m.user_id=auth.uid()
 where ws.id=w and ws.single_judge_admin_id is not null
 limit 1
$$;

create function public.professional_score_summary(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if not guanye_internal.professional_web(w) or not public.is_member(w) then
  raise exception '当前账号无权读取正式评分' using errcode='42501';
 end if;
 return (
  with identifiers as (
   select public.ai_review_rubric_version(w) as rubric_version,
    coalesce((ws.settings->>'maxScore')::numeric,5) as maximum
   from public.workspaces ws where ws.id=w
  ), recent as (
   select distinct on (d.id,s.scope,s.unit_id) d.id,s.scope,s.unit_id,x.formal_total as score
   from public.ai_review_decisions x
   join public.ai_review_submissions s on s.id=x.submission_id and s.workspace_id=w and s.deck_id=x.deck_id
   join public.decks d on d.id=x.deck_id and d.workspace_id=w
   cross join identifiers current
   where x.workspace_id=w and x.professional_web and d.professional_archived_at is null
    and x.judge_user_id=s.human_user_id and x.status in ('confirmed','corrected') and x.formal_total is not null
    and x.rubric_version=current.rubric_version and s.rubric_version=x.rubric_version and x.max_score=current.maximum
    and x.deck_version=public.ai_review_deck_version(w,d.id) and s.deck_version=x.deck_version
    and (s.scope='deck' or exists(select 1 from jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) image where image->>'id'=s.unit_id))
    and (d.source_kind='uploaded'
      or public.ai_review_deck_library_visible(w,d.id)
      or s.scope='page' and public.ai_review_page_library_visible(w,d.id,s.unit_id))
   order by d.id,s.scope,s.unit_id,x.created_at desc,x.id desc
  ), decks as (
   select id,max(score) filter(where scope='deck') score,
    jsonb_object_agg(unit_id,score) filter(where scope='page') pages
   from recent group by id
  ) select coalesce(jsonb_object_agg(id,jsonb_strip_nulls(jsonb_build_object('formalScore',score,'formalPageScores',pages))),'{}'::jsonb) from decks
 );
end $$;
revoke all on function public.professional_score_summary(uuid) from public,anon,authenticated,service_role;
grant execute on function public.professional_score_summary(uuid) to authenticated;

commit;
