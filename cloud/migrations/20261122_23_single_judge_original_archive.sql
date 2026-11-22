-- Publish only after the trusted local converter has uploaded all private objects.
begin;

create table guanye_internal.single_judge_originals (
 deck_id text primary key references public.decks(id) on delete restrict,
 workspace_id uuid not null references public.workspaces(id) on delete restrict,
 object_path text not null unique,
 original_name text not null check (length(original_name) between 1 and 255),
 sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
 size_bytes bigint not null check (size_bytes between 1 and 26214400),
 deck_version text not null,
 created_by uuid not null references auth.users(id) on delete restrict,
 created_at timestamptz not null default now()
);
alter table guanye_internal.single_judge_originals enable row level security;
revoke all on guanye_internal.single_judge_originals from public,anon,authenticated,service_role;

create function public.single_judge_publish_import(w uuid, actor uuid, p_deck jsonb, p_original jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype; previous guanye_internal.single_judge_originals%rowtype;
 target text; original_file text; digest text; extension text; address text; size bigint; version text;
 item jsonb; expected_position integer=0; seen text[]='{}'; image_id text; image_path text; thumb_path text;
 queued jsonb; canonical_deck jsonb;
begin
 select * into ws from public.workspaces where id=w for update;
 if not found or ws.single_judge_admin_id is null or actor is null or actor is distinct from ws.dedicated_judge_id then
  raise exception '仅评分师设备可发布原始素材' using errcode='42501';
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
  if previous.workspace_id is distinct from w or previous.sha256 is distinct from digest or previous.size_bytes is distinct from size
     or previous.original_name is distinct from original_file
     or not exists(select 1 from public.decks where workspace_id=w and id=target and data=canonical_deck) then
   raise exception '导入编号已绑定其他素材' using errcode='22023';
  end if;
  return jsonb_build_object('ok',true,'deckId',target,'deckVersion',previous.deck_version,'idempotent',true);
 end if;
 perform public.ai_import_review_deck(w,actor,canonical_deck,digest);
 version=public.ai_review_deck_version(w,target);
 insert into guanye_internal.single_judge_originals(deck_id,workspace_id,object_path,original_name,sha256,size_bytes,deck_version,created_by)
 values(target,w,address,original_file,digest,size,version,actor);
 queued=public.ai_review_prepare(w,actor,jsonb_build_object('deckId',target,'scope','deck'));
 return jsonb_build_object('ok',true,'deckId',target,'deckVersion',version,'idempotent',false,'preEvaluation',queued);
end $$;
revoke all on function public.single_judge_publish_import(uuid,uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.single_judge_publish_import(uuid,uuid,jsonb,jsonb) to service_role;

create function public.single_judge_original(w uuid, target_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare manifest jsonb;
begin
 if not public.is_member(w) or not exists(select 1 from public.workspaces where id=w and single_judge_admin_id is not null) then
  raise exception '没有权限查看原始素材' using errcode='42501';
 end if;
 if not exists(select 1 from public.decks where workspace_id=w and id=target_id and source_kind='uploaded') then
  raise exception '上传作品不存在' using errcode='22023';
 end if;
 select jsonb_build_object('available',true,'path',object_path,'name',original_name,'sizeBytes',size_bytes,
  'sha256',sha256,'deckVersion',deck_version) into manifest
 from guanye_internal.single_judge_originals where workspace_id=w and deck_id=target_id;
 return coalesce(manifest,jsonb_build_object('available',false));
end $$;
revoke all on function public.single_judge_original(uuid,text) from public,anon,authenticated;
grant execute on function public.single_judge_original(uuid,text) to authenticated;

commit;
