-- AI reviews: independent pre-evaluation, blind human submission, exact-version matching.
begin;

create table if not exists public.ai_review_settings (
 workspace_id uuid primary key references public.workspaces(id) on delete cascade,
 vision_weight numeric(6,2) not null default 40 check(vision_weight>=0 and vision_weight<=100),
 layout_weight numeric(6,2) not null default 30 check(layout_weight>=0 and layout_weight<=100),
 match_weight numeric(6,2) not null default 30 check(match_weight>=0 and match_weight<=100),
 human_threshold numeric(5,4) not null default 0.85 check(human_threshold>=0 and human_threshold<=1),
 model_hint text not null default 'gpt-6.2-sol',
 reason_catalog jsonb not null default '[{"code":"visual_impact","label":"视觉冲击"},{"code":"hierarchy","label":"层级清晰"},{"code":"consistency","label":"风格一致"},{"code":"readability","label":"易读性"},{"code":"content_fit","label":"内容匹配"},{"code":"other","label":"其他"}]'::jsonb,
 updated_by uuid references auth.users(id) on delete set null,
 updated_at timestamptz not null default now(),
 check(abs((vision_weight+layout_weight+match_weight)-100)<0.001),
 check(jsonb_typeof(reason_catalog)='array')
);
insert into public.ai_review_settings(workspace_id)
select id from public.workspaces on conflict(workspace_id) do nothing;

create or replace function public.ai_review_ensure_settings() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 insert into public.ai_review_settings(workspace_id) values(new.id) on conflict(workspace_id) do nothing;
 return new;
end $$;
drop trigger if exists ai_review_settings_after_workspace on public.workspaces;
create trigger ai_review_settings_after_workspace after insert on public.workspaces
for each row execute function public.ai_review_ensure_settings();

create table if not exists public.ai_review_jobs (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 source_kind text not null check(source_kind in ('uploaded','generated')),
 scope text not null check(scope in ('deck','page')),
 unit_id text not null default '',
 position integer not null default 0,
 variant text not null check(variant in ('baseline','assisted')),
 bucket integer not null check(bucket between 0 and 4),
 split text not null check(split in ('calibration','holdout')),
 deck_version text not null,
 rubric_version text not null,
 max_score numeric not null check(max_score>=1 and max_score<=100),
 status text not null default 'queued' check(status in ('queued','running','completed','failed')),
 attempts integer not null default 0 check(attempts>=0),
 judge_user_id uuid references auth.users(id) on delete set null,
 error text,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
);
create index if not exists ai_review_jobs_queue on public.ai_review_jobs(workspace_id,source_kind,status,created_at);
create table if not exists public.ai_review_records (
 id uuid primary key default gen_random_uuid(),
 job_id uuid not null unique references public.ai_review_jobs(id) on delete cascade,
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 source_kind text not null check(source_kind in ('uploaded','generated')),
 scope text not null check(scope in ('deck','page')),
 unit_id text not null default '',
 variant text not null check(variant in ('baseline','assisted')),
 bucket integer not null check(bucket between 0 and 4),
 split text not null check(split in ('calibration','holdout')),
 deck_version text not null,
 rubric_version text not null,
 max_score numeric not null check(max_score>=1 and max_score<=100),
 model_hint text not null,
 ai_result jsonb not null check(jsonb_typeof(ai_result)='object'),
 judge_user_id uuid references auth.users(id) on delete set null,
 created_at timestamptz not null default now(),
 unique(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
);
create index if not exists ai_review_records_binding on public.ai_review_records(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version);

create table if not exists public.ai_review_submissions (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 source_kind text not null check(source_kind in ('uploaded','generated')),
 scope text not null check(scope in ('deck','page')),
 unit_id text not null default '',
 position integer not null default 0,
 human_user_id uuid not null references auth.users(id) on delete cascade,
 deck_version text not null,
 rubric_version text not null,
 human_version text not null,
 max_score numeric not null check(max_score>=1 and max_score<=100),
 entry jsonb not null check(jsonb_typeof(entry)='object'),
 created_at timestamptz not null default now(),
 unique(workspace_id,deck_id,scope,unit_id,human_user_id,human_version)
);
create index if not exists ai_review_submissions_lookup on public.ai_review_submissions(workspace_id,deck_id,human_user_id,created_at desc);

create table if not exists public.ai_review_matches (
 id uuid primary key default gen_random_uuid(),
 submission_id uuid not null references public.ai_review_submissions(id) on delete cascade,
 record_id uuid not null references public.ai_review_records(id) on delete cascade,
 variant text not null check(variant in ('baseline','assisted')),
 created_at timestamptz not null default now(),
 unique(submission_id,record_id)
);
create index if not exists ai_review_matches_submission on public.ai_review_matches(submission_id,variant);

create table if not exists public.ai_review_decisions (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 submission_id uuid not null references public.ai_review_submissions(id) on delete cascade,
 ai_record_id uuid references public.ai_review_records(id) on delete set null,
 judge_user_id uuid not null references auth.users(id) on delete restrict,
 decision_key text not null,
 status text not null check(status in ('confirmed','corrected')),
 classification jsonb,
 reasons jsonb not null default '[]'::jsonb check(jsonb_typeof(reasons)='array'),
 reason_other text not null default '',
 formal_scores jsonb not null check(jsonb_typeof(formal_scores)='object'),
 formal_total numeric,
 deck_version text not null,
 rubric_version text not null,
 max_score numeric not null check(max_score>=1 and max_score<=100),
 score_ratio numeric,
 expires_at timestamptz not null default now()+interval '7 days',
 summary text not null default '',
 created_at timestamptz not null default now(),
 unique(workspace_id,decision_key)
);
create index if not exists ai_review_decisions_lookup on public.ai_review_decisions(workspace_id,deck_id,submission_id,created_at desc);
alter table public.ai_review_decisions add column if not exists deck_version text;
alter table public.ai_review_decisions add column if not exists rubric_version text;
alter table public.ai_review_decisions add column if not exists max_score numeric;
alter table public.ai_review_decisions add column if not exists score_ratio numeric;
alter table public.ai_review_decisions add column if not exists expires_at timestamptz not null default now()+interval '7 days';

create table if not exists public.ai_generated_entries (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 decision_id uuid not null unique references public.ai_review_decisions(id) on delete cascade,
 scope text not null default 'deck' check(scope in ('deck','page')),
 unit_id text not null default '',
 source jsonb not null default '{}'::jsonb,
 reference_refs jsonb not null default '[]'::jsonb,
 formal_scores jsonb not null check(jsonb_typeof(formal_scores)='object'),
 formal_total numeric,
 weights jsonb not null check(jsonb_typeof(weights)='object'),
 human_threshold numeric not null,
 human_coverage numeric not null default 0,
 threshold_met boolean not null default false,
 ai_self_learning boolean not null default true,
 library_visible boolean not null default false,
 library_label text not null default 'AI 自我学习生成',
 score_ratio numeric,
 admitted_at timestamptz,
 admitted_by uuid references auth.users(id) on delete set null,
 created_at timestamptz not null default now()
);
alter table public.ai_generated_entries add column if not exists scope text not null default 'deck';
alter table public.ai_generated_entries add column if not exists unit_id text not null default '';
alter table public.ai_generated_entries add column if not exists library_visible boolean not null default false;
alter table public.ai_generated_entries alter column library_visible set default false;
alter table public.ai_generated_entries add column if not exists library_label text not null default 'AI 自我学习生成';
alter table public.ai_generated_entries add column if not exists score_ratio numeric;
alter table public.ai_generated_entries add column if not exists admitted_at timestamptz;
alter table public.ai_generated_entries add column if not exists admitted_by uuid references auth.users(id) on delete set null;
update public.ai_generated_entries set library_visible=false where admitted_at is null;
create index if not exists ai_generated_entries_lookup on public.ai_generated_entries(workspace_id,created_at desc);
create index if not exists ai_generated_entries_library on public.ai_generated_entries(workspace_id,deck_id,scope,unit_id,library_visible,created_at desc);

alter table public.ai_review_settings enable row level security;
alter table public.ai_review_jobs enable row level security;
alter table public.ai_review_records enable row level security;
alter table public.ai_review_submissions enable row level security;
alter table public.ai_review_matches enable row level security;
alter table public.ai_review_decisions enable row level security;
alter table public.ai_generated_entries enable row level security;
revoke all on public.ai_review_settings,public.ai_review_jobs,public.ai_review_records,public.ai_review_submissions,
 public.ai_review_matches,public.ai_review_decisions,public.ai_generated_entries from public,anon,authenticated;
grant all on public.ai_review_settings,public.ai_review_jobs,public.ai_review_records,public.ai_review_submissions,
 public.ai_review_matches,public.ai_review_decisions,public.ai_generated_entries to service_role;
create or replace function public.ai_review_json_sha(value jsonb) returns text
language sql immutable set search_path='' as $$ select encode(extensions.digest(convert_to(value::text,'UTF8'),'sha256'),'hex') $$;

create or replace function public.ai_review_rubric(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object(
  'maxScore',coalesce((ws.settings->>'maxScore')::numeric,5),
  'dimensions',coalesce(ws.settings->'dimensions','[]'::jsonb),
  'weights',jsonb_build_object('visual',s.vision_weight,'layout',s.layout_weight,'match',s.match_weight)
 ) from public.workspaces ws join public.ai_review_settings s on s.workspace_id=ws.id where ws.id=w
$$;
create or replace function public.ai_review_rubric_version(w uuid) returns text
language sql stable security definer set search_path='' as $$ select public.ai_review_json_sha(public.ai_review_rubric(w)) $$;
create or replace function public.ai_review_deck_version(w uuid, target_id text) returns text
language sql stable security definer set search_path='' as $$
 select public.ai_review_json_sha(jsonb_build_object('id',d.id,'sourceKind',d.source_kind,'data',d.data))
 from public.decks d where d.workspace_id=w and d.id=target_id
$$;

create or replace function public.ai_review_update_settings(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare current_row public.ai_review_settings%rowtype; vision_value numeric; layout_value numeric; match_value numeric; threshold_value numeric; catalog_value jsonb;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以调整 AI 配置'; end if;
 if jsonb_typeof(payload) is distinct from 'object' then raise exception 'AI 配置无效'; end if;
 select * into current_row from public.ai_review_settings where workspace_id=w for update;
 if current_row.workspace_id is null then raise exception 'AI 配置不存在'; end if;
 vision_value=coalesce((payload->>'visionWeight')::numeric,current_row.vision_weight);
 layout_value=coalesce((payload->>'layoutWeight')::numeric,current_row.layout_weight);
 match_value=coalesce((payload->>'matchWeight')::numeric,current_row.match_weight);
 threshold_value=coalesce((payload->>'humanThreshold')::numeric,current_row.human_threshold);
 catalog_value=coalesce(payload->'reasonCatalog',current_row.reason_catalog);
 if vision_value<0 or layout_value<0 or match_value<0 or abs((vision_value+layout_value+match_value)-100)>=0.001 then raise exception '生成自评权重必须非负且合计 100'; end if;
 if threshold_value<0 or threshold_value>1 then raise exception '人工门槛必须在 0 到 1 之间'; end if;
 if jsonb_typeof(catalog_value)<>'array' or jsonb_array_length(catalog_value) not between 1 and 30 then raise exception '原因目录无效'; end if;
 update public.ai_review_settings set vision_weight=vision_value,layout_weight=layout_value,match_weight=match_value,
  human_threshold=threshold_value,reason_catalog=catalog_value,
  model_hint=left(coalesce(nullif(payload->>'modelHint',''),model_hint),200),updated_by=actor,updated_at=now()
 where workspace_id=w returning * into current_row;
 return jsonb_build_object('visionWeight',current_row.vision_weight,'layoutWeight',current_row.layout_weight,'matchWeight',current_row.match_weight,
  'humanThreshold',current_row.human_threshold,'modelHint',current_row.model_hint,'reasonCatalog',current_row.reason_catalog);
end $$;
create or replace function public.ai_review_prepare(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare deck_row public.decks%rowtype; rubric_value jsonb; rubric_hash text; deck_hash text;
 scope_value text; unit_json jsonb; unit_key text; position_value integer; bucket_value integer; split_value text;
 variant_value text; variants text[]; job_ids jsonb='[]'::jsonb; max_value numeric;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以准备 AI 预评'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or length(coalesce(payload->>'deckId','')) not between 1 and 100 then raise exception '预评内容无效'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=payload->>'deckId';
 if deck_row.id is null then raise exception '作品不存在'; end if;
 rubric_value=public.ai_review_rubric(w);rubric_hash=public.ai_review_json_sha(rubric_value);
 deck_hash=public.ai_review_deck_version(w,deck_row.id);max_value=(rubric_value->>'maxScore')::numeric;
 scope_value=coalesce(payload->>'scope','deck');
 if scope_value not in ('deck','page') then raise exception '预评粒度无效'; end if;
 bucket_value=mod(get_byte(decode(md5(deck_row.id),'hex'),0),5)::integer;
 split_value=case when bucket_value=0 then 'holdout' else 'calibration' end;
 variants=case when bucket_value=0 then array['baseline','assisted'] else array['assisted'] end;
 if scope_value='deck' then
  foreach variant_value in array variants loop
   insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
   values(w,deck_row.id,deck_row.source_kind,'deck','',0,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='deck'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash;
 else
  if jsonb_typeof(payload->'unitIds') is distinct from 'array' or jsonb_array_length(payload->'unitIds') not between 1 and 600 then raise exception '请选择需要预评的页面'; end if;
  for unit_json in select value from jsonb_array_elements(payload->'unitIds') loop
   unit_key=case when jsonb_typeof(unit_json)='string' then trim(unit_json #>> '{}') else coalesce(unit_json->>'unitId','') end;
   select coalesce((image->>'position')::integer,ordinality::integer) into position_value
   from jsonb_array_elements(case when jsonb_typeof(deck_row.data->'images')='array' then deck_row.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
   where image->>'id'=unit_key limit 1;
   if position_value is null then raise exception '预评页面不存在'; end if;
   foreach variant_value in array variants loop
    insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
    values(w,deck_row.id,deck_row.source_kind,'page',unit_key,position_value,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
    on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
   end loop;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.position,j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='page'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash
   and j.unit_id in (select case when jsonb_typeof(value)='string' then trim(value #>> '{}') else coalesce(value->>'unitId','') end from jsonb_array_elements(payload->'unitIds'));
 end if;
 return jsonb_build_object('ok',true,'deckId',deck_row.id,'sourceKind',deck_row.source_kind,'scope',scope_value,
  'deckVersion',deck_hash,'rubricVersion',rubric_hash,'jobs',job_ids,'aiVisible',false);
end $$;
create or replace function public.ai_review_claim_jobs(w uuid, actor uuid, p_kind text, p_limit integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare jobs_json jsonb;
begin
 if p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以执行预评'; end if;
 update public.ai_review_jobs set status='queued',error=coalesce(error,'执行超时，已重新排队'),updated_at=now()
 where workspace_id=w and status='running' and attempts<3 and updated_at<now()-interval '15 minutes';
 with picked as (
  select j.id from public.ai_review_jobs j where j.workspace_id=w and j.source_kind=p_kind and j.status in ('queued','failed') and j.attempts<3
  order by j.created_at for update skip locked limit greatest(1,least(coalesce(p_limit,4),20))
 ), changed as (
  update public.ai_review_jobs j set status='running',judge_user_id=actor,attempts=j.attempts+1,updated_at=now()
  from picked p where j.id=p.id returning j.*
 )
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'deckId',deck_id,'sourceKind',source_kind,'scope',scope,
  'unitId',unit_id,'position',position,'variant',variant,'bucket',bucket,'split',split,'deckVersion',deck_version,
  'rubricVersion',rubric_version,'maxScore',max_score,'attempts',attempts) order by created_at),'[]'::jsonb)
 into jobs_json from changed;
 return jobs_json;
end $$;

create or replace function public.ai_review_scores_complete(w uuid, target_id text, entry jsonb, maximum numeric) returns boolean
language plpgsql stable security definer set search_path='' as $$
declare expected jsonb; dimension jsonb; value_text text; score_value numeric;
begin
 if jsonb_typeof(entry) is distinct from 'object' or jsonb_typeof(entry->'scores') is distinct from 'object' then return false; end if;
 select case when d.source_kind='generated' then '[{"id":"visual"},{"id":"layout"},{"id":"match"}]'::jsonb
  else coalesce(ws.settings->'dimensions','[]'::jsonb) end
 into expected
 from public.decks d join public.workspaces ws on ws.id=d.workspace_id
 where d.workspace_id=w and d.id=target_id;
 if expected is null or jsonb_array_length(expected)=0 then return false; end if;
 if maximum is null then select coalesce((settings->>'maxScore')::numeric,5) into maximum from public.workspaces where id=w; end if;
 for dimension in select value from jsonb_array_elements(expected) loop
  value_text=dimension->>'id';
  if value_text is null or jsonb_typeof(entry->'scores'->value_text) is distinct from 'number' then return false; end if;
  score_value=(entry->'scores'->>value_text)::numeric;
  if score_value<0 or score_value>maximum then return false; end if;
 end loop;
 return true;
end $$;

create or replace function public.ai_review_finish_job(w uuid, actor uuid, target_job uuid, p_status text, p_result jsonb, p_error text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare job_row public.ai_review_jobs%rowtype; record_uuid uuid; linked integer=0;
begin
 if p_status not in ('completed','failed') then raise exception '任务状态无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以完成预评'; end if;
 select * into job_row from public.ai_review_jobs where workspace_id=w and id=target_job for update;
 if job_row.id is null then raise exception '任务不存在'; end if;
 if p_status='failed' then
  update public.ai_review_jobs set status='failed',error=left(coalesce(p_error,'预评失败'),1000),updated_at=now() where id=target_job;
  return jsonb_build_object('ok',true,'status','failed','jobId',target_job,'aiVisible',false);
 end if;
 if jsonb_typeof(p_result) is distinct from 'object' then raise exception 'AI 结果无效'; end if;
 select id into record_uuid from public.ai_review_records where workspace_id=w and deck_id=job_row.deck_id
  and scope=job_row.scope and unit_id=job_row.unit_id and deck_version=job_row.deck_version
  and rubric_version=job_row.rubric_version and variant=job_row.variant;
 if record_uuid is null then
  insert into public.ai_review_records(job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,
   deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id)
  values(job_row.id,w,job_row.deck_id,job_row.source_kind,job_row.scope,job_row.unit_id,job_row.variant,job_row.bucket,job_row.split,
   job_row.deck_version,job_row.rubric_version,job_row.max_score,coalesce(p_result->>'modelHint','gpt-6.2-sol'),p_result,actor)
  returning id into record_uuid;
 end if;
 insert into public.ai_review_matches(submission_id,record_id,variant)
 select s.id,record_uuid,job_row.variant from public.ai_review_submissions s
 where s.workspace_id=w and s.deck_id=job_row.deck_id and s.scope=job_row.scope and s.unit_id=job_row.unit_id
  and s.deck_version=job_row.deck_version and s.rubric_version=job_row.rubric_version
   and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
 on conflict(submission_id,record_id) do nothing;
 get diagnostics linked=row_count;
 update public.ai_review_jobs set status='completed',error=null,updated_at=now() where id=target_job;
 return jsonb_build_object('ok',true,'status','completed','jobId',target_job,'recordId',record_uuid,'matchedSubmissions',linked,'aiVisible',false);
end $$;
create or replace function public.ai_review_submit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare member public.members%rowtype; deck_row public.decks%rowtype; workspace_settings jsonb; scope_value text; unit_json jsonb;
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
  if deck_row.source_kind='uploaded' then
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
create or replace function public.ai_review_confirm(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare submission_row public.ai_review_submissions%rowtype; record_row public.ai_review_records%rowtype; deck_row public.decks%rowtype;
 decision_row public.ai_review_decisions%rowtype; decision_key_value text; classification_value jsonb; reasons_value jsonb;
 formal_scores_value jsonb; formal_total_value numeric; score_record record; settings_row public.ai_review_settings%rowtype;
 source_value jsonb; reference_value jsonb; threshold_met_value boolean=false; status_value text='confirmed'; weighted_value numeric;
 score_ratio_value numeric=0;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以确认正式评分'; end if;
 if jsonb_typeof(payload) is distinct from 'object' then raise exception '确认内容无效'; end if;
 select * into submission_row from public.ai_review_submissions where workspace_id=w and id=nullif(payload->>'submissionId','')::uuid;
 if submission_row.id is null then raise exception '人工提交不存在'; end if;
 if submission_row.deck_version<>public.ai_review_deck_version(w,submission_row.deck_id) then raise exception '作品版本已变化，请重新提交人工评分'; end if;
 if submission_row.rubric_version<>public.ai_review_rubric_version(w) then raise exception '评分标准已变化，请重新提交人工评分'; end if;
 if nullif(payload->>'aiRecordId','') is not null then
  select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
   where m.submission_id=submission_row.id and r.id=(payload->>'aiRecordId')::uuid;
 else
  select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
   where m.submission_id=submission_row.id order by case when m.variant='assisted' then 0 else 1 end,r.created_at desc limit 1;
 end if;
 select * into deck_row from public.decks where workspace_id=w and id=submission_row.deck_id;
 select * into settings_row from public.ai_review_settings where workspace_id=w;
 decision_key_value=nullif(payload->>'idempotencyKey','');
 if decision_key_value is null then
  decision_key_value='v1:'||public.ai_review_json_sha(jsonb_build_object('submissionId',submission_row.id,'recordId',record_row.id,
   'humanVersion',submission_row.human_version,'formalScores',payload->'formalScores','classification',payload->'classification'));
 end if;
 if length(decision_key_value)>128 then raise exception '幂等键过长'; end if;
 select * into decision_row from public.ai_review_decisions where workspace_id=w and decision_key=decision_key_value;
 if decision_row.id is not null then
  return jsonb_build_object('ok',true,'idempotent',true,'decisionId',decision_row.id,'formalTotal',decision_row.formal_total,
   'scoreRatio',decision_row.score_ratio,'expiresAt',decision_row.expires_at,'admitted',false,'aiVisible',true);
 end if;
 classification_value=case when payload ? 'classification' and jsonb_typeof(payload->'classification')<>'null' then payload->'classification' else null end;
 if classification_value is not null and jsonb_typeof(classification_value) not in ('string','object') then raise exception '正式分类无效'; end if;
 reasons_value=case when payload ? 'reasons' and jsonb_typeof(payload->'reasons')<>'null' then payload->'reasons' else '[]'::jsonb end;
 formal_scores_value=payload->'formalScores';
 if jsonb_typeof(reasons_value)<>'array' or jsonb_typeof(formal_scores_value) is distinct from 'object' or formal_scores_value='{}'::jsonb then raise exception '正式评分必须由评委显式填写正式分，原因必须是数组'; end if;
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
 if payload->>'status'='corrected'
  or (classification_value is not null and record_row.ai_result is not null and classification_value is distinct from record_row.ai_result->'classification')
  or (payload ? 'reasons' and record_row.ai_result is not null and reasons_value is distinct from coalesce(record_row.ai_result->'reasons','[]'::jsonb))
 then status_value='corrected'; end if;
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
  'formalScores',formal_scores_value,'formalTotal',formal_total_value,'scoreRatio',score_ratio_value,
  'thresholdMet',threshold_met_value,'expiresAt',decision_row.expires_at,'admitted',false,'aiVisible',true,
  'message','正式评分已确认；需评委单独点击“收入素材库”');
end $$;

create or replace function public.ai_review_admit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare submission_row public.ai_review_submissions%rowtype; decision_row public.ai_review_decisions%rowtype; entry_row public.ai_generated_entries%rowtype;
 deck_row public.decks%rowtype; settings_row public.ai_review_settings%rowtype; ratio_value numeric;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以收入素材库'; end if;
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

create or replace function public.ai_review_training_bundle(w uuid, actor uuid, p_limit integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare bundle jsonb;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以读取训练样本'; end if;
 with calibration as (
  select s.id,s.deck_id,j.scope,j.unit_id,j.variant,s.entry,s.created_at
  from public.ai_review_submissions s join public.ai_review_matches m on m.submission_id=s.id
  join public.ai_review_records r on r.id=m.record_id join public.ai_review_jobs j on j.id=r.job_id
  where s.workspace_id=w and j.split='calibration' order by s.created_at desc limit greatest(1,least(coalesce(p_limit,6),12))
 ), corrections as (
  select d.id,d.deck_id,d.classification,d.reasons,d.reason_other,d.formal_scores,d.formal_total,d.summary,d.created_at
  from public.ai_review_decisions d join public.ai_review_submissions s on s.id=d.submission_id
  join public.ai_review_matches m on m.submission_id=s.id join public.ai_review_records r on r.id=m.record_id
  join public.ai_review_jobs j on j.id=r.job_id
  where d.workspace_id=w and j.split='calibration' order by d.created_at desc limit greatest(1,least(coalesce(p_limit,8),10))
 )
 select jsonb_build_object('selectedSamples',(select count(*) from calibration),'selectedCorrections',(select count(*) from corrections),
  'samples',coalesce((select jsonb_agg(jsonb_build_object('deckId',deck_id,'scope',scope,'unitId',unit_id,
   'scores',entry->'scores','classificationSuggestion',entry->'classificationSuggestion','reasons',entry->'reasons',
   'reasonOther',entry->'reasonOther') order by created_at desc) from calibration),'[]'::jsonb),
  'corrections',coalesce((select jsonb_agg(jsonb_build_object('deckId',deck_id,'classification',classification,'reasons',reasons,
   'reasonOther',reason_other,'formalScores',formal_scores,'formalTotal',formal_total,'summary',summary) order by created_at desc) from corrections),'[]'::jsonb)
 ) into bundle;
 return bundle;
end $$;

revoke all on function public.ai_review_scores_complete(uuid,text,jsonb,numeric),public.ai_review_json_sha(jsonb),public.ai_review_rubric(uuid),public.ai_review_rubric_version(uuid),
 public.ai_review_deck_version(uuid,text),public.ai_review_update_settings(uuid,uuid,jsonb),
 public.ai_review_prepare(uuid,uuid,jsonb),public.ai_review_claim_jobs(uuid,uuid,text,integer),
 public.ai_review_finish_job(uuid,uuid,uuid,text,jsonb,text),public.ai_review_submit(uuid,uuid,jsonb),
 public.ai_review_confirm(uuid,uuid,jsonb),public.ai_review_admit(uuid,uuid,jsonb),public.ai_review_training_bundle(uuid,uuid,integer)
 from public,anon,authenticated;
grant execute on function public.ai_review_scores_complete(uuid,text,jsonb,numeric),public.ai_review_json_sha(jsonb),public.ai_review_rubric(uuid),public.ai_review_rubric_version(uuid),
 public.ai_review_deck_version(uuid,text),public.ai_review_update_settings(uuid,uuid,jsonb),
 public.ai_review_prepare(uuid,uuid,jsonb),public.ai_review_claim_jobs(uuid,uuid,text,integer),
 public.ai_review_finish_job(uuid,uuid,uuid,text,jsonb,text),public.ai_review_submit(uuid,uuid,jsonb),
 public.ai_review_confirm(uuid,uuid,jsonb),public.ai_review_admit(uuid,uuid,jsonb),public.ai_review_training_bundle(uuid,uuid,integer)
 to service_role;

create or replace function public.ai_review_deck_library_visible(w uuid, target_id text) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(
  select 1 from public.ai_generated_entries e
  join public.decks d on d.workspace_id=e.workspace_id and d.id=e.deck_id
  where e.workspace_id=w and e.deck_id=target_id and e.scope='deck' and e.library_visible and d.source_kind='generated'
 )
$$;
create or replace function public.ai_review_page_library_visible(w uuid, target_id text, page_id text) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(
  select 1 from public.ai_generated_entries e
  join public.decks d on d.workspace_id=e.workspace_id and d.id=e.deck_id
  where e.workspace_id=w and e.deck_id=target_id and e.scope='page' and e.unit_id=page_id
   and e.library_visible and d.source_kind='generated'
 )
$$;
create or replace function public.ai_review_review_allowed(w uuid, target_id text, review_data jsonb) returns boolean
language sql stable security definer set search_path='' as $$
 select coalesce((
  select case
   when d.source_kind='uploaded' then true
   when public.ai_review_deck_library_visible(w,target_id) then true
   when coalesce(review_data->>'mode','')='page' then not exists(
    select 1 from jsonb_each(case when jsonb_typeof(review_data->'pages')='object' then review_data->'pages' else '{}'::jsonb end) page
    where exists(select 1 from jsonb_each(case when jsonb_typeof(page.value->'scores')='object' then page.value->'scores' else '{}'::jsonb end) score where jsonb_typeof(score.value)='number')
     and not public.ai_review_page_library_visible(w,target_id,page.key)
   )
   else false
  end from public.decks d where d.workspace_id=w and d.id=target_id
 ),false)
$$;

create or replace function public.ai_review_legacy_guard() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 if current_setting('guanye.ai_review_internal',true)='on' then return new; end if;
 if not public.ai_review_review_allowed(new.workspace_id,new.deck_id,new.data) then
  raise exception '生成作品尚未正式入库，不能通过旧评分写入';
 end if;
 return new;
end $$;
drop trigger if exists ai_review_legacy_guard on public.reviews;
create trigger ai_review_legacy_guard before insert or update on public.reviews
for each row execute function public.ai_review_legacy_guard();

drop policy if exists decks_read on public.decks;
create policy decks_read on public.decks for select to authenticated using(
 public.is_member(workspace_id) and (
  source_kind='uploaded' or public.ai_review_deck_library_visible(workspace_id,id)
 )
);
drop policy if exists reviews_read on public.reviews;
create policy reviews_read on public.reviews for select to authenticated using(
 public.is_member(workspace_id) and public.ai_review_review_allowed(workspace_id,deck_id,data)
);
create or replace function public.ai_review_effective_meta(w uuid, target_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare deck_data jsonb; existing_class text; existing_style text; human_class text; human_style text; ai_class text; ai_style text;
 correction jsonb; classification_payload jsonb; has_submission boolean; class_source text='none'; style_source text='none';
begin
 select data into deck_data from public.decks where workspace_id=w and id=target_id;
 if deck_data is null then return jsonb_build_object('effectiveClassification',null,'effectiveStyle',null,'classificationSource','none','styleSource','none'); end if;
 existing_class=coalesce(nullif(deck_data->>'classification',''),nullif(deck_data->>'category',''),nullif(deck_data->>'primaryClassification',''));
 existing_style=nullif(deck_data->>'style','');
 select exists(select 1 from public.ai_review_submissions s where s.workspace_id=w and s.deck_id=target_id and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)) into has_submission;
 select entry into correction from public.ai_review_submissions s where s.workspace_id=w and s.deck_id=target_id and s.entry ? 'classificationCorrection' order by s.created_at desc limit 1;
 if correction ? 'classificationCorrection' then
  correction=correction->'classificationCorrection';
  if jsonb_typeof(correction)='string' then human_class=nullif(trim(correction #>> '{}'),'');
  elsif jsonb_typeof(correction)='object' then
   human_class=coalesce(nullif(correction->>'primary',''),nullif(correction->>'label',''),nullif(correction->>'name',''),nullif(correction->>'category',''));
   human_style=nullif(correction->>'style','');
  end if;
 end if;
 if has_submission then
  select ai_result into classification_payload from public.ai_review_records r
  where r.workspace_id=w and r.deck_id=target_id and r.scope='deck'
   and r.deck_version=public.ai_review_deck_version(w,target_id) and r.rubric_version=public.ai_review_rubric_version(w)
  order by case when r.variant='assisted' then 0 else 1 end,r.created_at desc limit 1;
  if classification_payload ? 'classification' then
   classification_payload=classification_payload->'classification';
   if jsonb_typeof(classification_payload)='string' then ai_class=nullif(trim(classification_payload #>> '{}'),'');
   elsif jsonb_typeof(classification_payload)='object' then
    ai_class=coalesce(nullif(classification_payload->>'primary',''),nullif(classification_payload->>'label',''),nullif(classification_payload->>'name',''),nullif(classification_payload->>'category',''));
    ai_style=nullif(classification_payload->>'style','');
   end if;
  end if;
 end if;
 if human_class is not null then class_source='human';
 elsif existing_class is not null then human_class=existing_class;class_source='existing';
 elsif ai_class is not null then human_class=ai_class;class_source='ai';
 end if;
 if human_style is not null then style_source='human';
 elsif existing_style is not null then human_style=existing_style;style_source='existing';
 elsif ai_style is not null then human_style=ai_style;style_source='ai';
 end if;
 return jsonb_build_object('effectiveClassification',human_class,'effectiveStyle',human_style,
  'classificationSource',class_source,'styleSource',style_source);
end $$;

create or replace function public.get_review_library(w uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if not public.is_member(w) then raise exception '请先加入团队'; end if;
 return (
  with whole as (
   select distinct on (e.deck_id) e.* from public.ai_generated_entries e
   where e.workspace_id=w and e.scope='deck' and e.library_visible
   order by e.deck_id,e.created_at desc
  ), page_entries as (
   select distinct on (e.deck_id,e.unit_id) e.* from public.ai_generated_entries e
   where e.workspace_id=w and e.scope='page' and e.library_visible
   order by e.deck_id,e.unit_id,e.created_at desc
  ), library_decks as (
   select d.id, d.data || jsonb_build_object('sourceKind','uploaded','libraryScope','deck') || public.ai_review_effective_meta(w,d.id) as deck
   from public.decks d where d.workspace_id=w and d.source_kind='uploaded'
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
create or replace function public.review_score_summary(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 with dims as (
  select dim->>'id' as id from public.workspaces ws cross join lateral jsonb_array_elements(ws.settings->'dimensions') dim
  where ws.id=w and public.is_member(w)
 ), personal as (
  select r.deck_id,r.user_id,avg(case when jsonb_typeof(v.score)='number' then (v.score::text)::numeric end) as mean
  from public.reviews r join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id
  cross join dims
  cross join lateral jsonb_array_elements(case when r.data->>'mode'='page' then d.data->'images' else '[{}]'::jsonb end) im
  cross join lateral (select case when r.data->>'mode'='page' then r.data#>array['pages',im->>'id','scores',dims.id] else r.data#>array['scores',dims.id] end as score) v
  where r.workspace_id=w and public.ai_review_review_allowed(w,r.deck_id,r.data)
  group by r.deck_id,r.user_id having count(*)=count(*) filter(where jsonb_typeof(v.score)='number')
 ), totals as (select deck_id,count(*) as n,avg(mean) as mean from personal group by deck_id)
 select coalesce(jsonb_object_agg(deck_id,jsonb_build_object('count',n,'mean',mean)),'{}'::jsonb) from totals
$$;
revoke all on function public.review_score_summary(uuid) from public,anon;
grant execute on function public.review_score_summary(uuid) to authenticated;

revoke all on function public.ai_review_effective_meta(uuid,text),public.ai_review_deck_library_visible(uuid,text),public.ai_review_page_library_visible(uuid,text,text),
 public.ai_review_review_allowed(uuid,text,jsonb),public.get_review_library(uuid) from public,anon;
grant execute on function public.ai_review_effective_meta(uuid,text),public.ai_review_deck_library_visible(uuid,text),public.ai_review_page_library_visible(uuid,text,text),
 public.ai_review_review_allowed(uuid,text,jsonb),public.get_review_library(uuid) to authenticated,service_role;

commit;
