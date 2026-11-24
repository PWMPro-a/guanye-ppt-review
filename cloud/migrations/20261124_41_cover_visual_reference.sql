begin;

-- Cover pilot only. Keep analysis evidence out of editable planning drafts.
alter table guanye_internal.cover_training_plan_context add column visual_reference jsonb;

create function public.single_judge_cover_references(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_creation_actor(w);
 if not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled) then
  raise exception '封面训练未启用' using errcode='42501'; end if;
 return (select coalesce(jsonb_agg(r.item || jsonb_build_object('rank',r.ord,
      'title',left(r.item->>'title',300),'style',left(r.item->>'style',300),
      'imagePaths',jsonb_build_array(r.item#>>'{imagePaths,0}')) order by r.ord),'[]'::jsonb)
   from jsonb_array_elements(guanye_internal.single_judge_planning_candidates(w,20)) with ordinality r(item,ord)
   join public.decks d on d.workspace_id=w and d.id=r.item->>'deckId'
   where (r.item->>'score')::numeric between 4 and 5
     and r.item#>>'{imagePaths,0}' = coalesce(d.data#>>'{images,0,path}',d.data#>>'{images,0,imagePath}',d.data#>>'{images,0,storagePath}')
     and (r.item#>>'{imagePaths,0}') ~* '\.(png|jpe?g|webp)$');
end $$;

create function public.single_judge_cover_save_visual_plan(w uuid,p_plan_id uuid,p_draft jsonb,p_style bigint,p_choices jsonb,p_visual jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 if jsonb_typeof(p_visual) is distinct from 'object' or octet_length(p_visual::text)>18000
    or p_visual->>'model' is distinct from 'gpt-6.2-sol'
    or coalesce(p_visual->>'sha256','') !~ '^[0-9a-f]{64}$'
    or jsonb_typeof(p_visual->'facts') is distinct from 'object'
    or p_draft->>'pageCount' is distinct from '1'
    or p_draft#>>'{pages,0,narrativeRole}' is distinct from 'cover'
    or jsonb_array_length(p_draft->'references') is distinct from 1
    or p_draft#>>'{references,0,deckId}' is distinct from p_visual->>'deckId'
    or p_draft#>>'{references,0,imagePaths,0}' is distinct from p_visual->>'path'
    or not exists(select 1 from jsonb_array_elements(public.single_judge_cover_references(w)) r(item)
         where r.item->>'deckId'=p_visual->>'deckId' and r.item#>>'{imagePaths,0}'=p_visual->>'path') then
  raise exception '授权素材与策划证据不一致，请重新策划' using errcode='22023'; end if;
 result=public.single_judge_cover_save_plan(w,p_plan_id,p_draft,p_style,p_choices);
 update guanye_internal.cover_training_plan_context set visual_reference=p_visual where workspace_id=w and plan_id=p_plan_id::text;
 return result;
end $$;

create function public.single_judge_cover_plan_visual(w uuid,p_plan_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor uuid:=guanye_internal.single_judge_creation_actor(w);
begin
 return (select r.visual_reference from guanye_internal.cover_training_plan_context r
   join public.ai_planning_plans p on p.id=p_plan_id and p.workspace_id=w and p.created_by=actor
   where r.workspace_id=w and r.plan_id=p_plan_id::text);
end $$;

alter function public.single_judge_cover_start(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text)
 rename to single_judge_cover_start_before_visual;
create function public.single_judge_cover_start(
 w uuid,p_plan_id text,p_deck_id text,p_plan jsonb,p_title text,p_style text,p_outline jsonb,p_references jsonb,p_pages jsonb,
 p_reference_deck text,p_reference_path text
) returns jsonb language plpgsql security definer set search_path='' as $$
declare ref jsonb;
begin
 ref=public.single_judge_cover_plan_visual(w,p_plan_id::uuid);
 if ref is null or ref->>'deckId' is distinct from p_reference_deck or ref->>'path' is distinct from p_reference_path
    or not exists(select 1 from jsonb_array_elements(public.single_judge_cover_references(w)) r(item)
         where r.item->>'deckId'=p_reference_deck and r.item#>>'{imagePaths,0}'=p_reference_path) then
  raise exception '生成必须沿用策划时锁定且仍获授权的原图' using errcode='22023'; end if;
 return public.single_judge_cover_start_before_visual(w,p_plan_id,p_deck_id,p_plan,p_title,p_style,p_outline,p_references,p_pages,
                                                     p_reference_deck,p_reference_path);
end $$;

-- The existing lease/consent gate is still the authority for the object path.
-- Revisions resolve to the root's immutable plan context, not their own new plan_id.
create function public.ai_generation_cover_reference_info(p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare object_path text; info jsonb;
begin
 object_path=public.ai_generation_page_reference_path(p_workspace,p_actor,p_page,p_lease_token);
 if object_path is null then return null; end if;
 select ctx.visual_reference into info
 from guanye_internal.cover_training_candidates c
 join public.ai_generation_pages p on p.id=c.root_page_id and p.workspace_id=p_workspace
 join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=p_workspace
 join guanye_internal.cover_training_plan_context ctx on ctx.workspace_id=p_workspace and ctx.plan_id=j.plan_id
 where c.page_id=p_page and c.workspace_id=p_workspace;
 if info is not null and (info->>'path' is distinct from object_path or info->>'deckId' is null) then return null; end if;
 return jsonb_build_object('path',object_path,'sha256',info->>'sha256');
end $$;

revoke all on function public.single_judge_cover_save_plan(uuid,uuid,jsonb,bigint,jsonb),
 public.single_judge_cover_start_before_visual(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text),
 public.single_judge_cover_start(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text),
 public.single_judge_cover_references(uuid),public.single_judge_cover_plan_visual(uuid,uuid),
 public.single_judge_cover_save_visual_plan(uuid,uuid,jsonb,bigint,jsonb,jsonb),
 public.ai_generation_cover_reference_info(uuid,uuid,uuid,uuid) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_start(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text),
 public.single_judge_cover_references(uuid),public.single_judge_cover_plan_visual(uuid,uuid),
 public.single_judge_cover_save_visual_plan(uuid,uuid,jsonb,bigint,jsonb,jsonb) to authenticated;
grant execute on function public.ai_generation_cover_reference_info(uuid,uuid,uuid,uuid) to service_role;
notify pgrst,'reload schema';
commit;
