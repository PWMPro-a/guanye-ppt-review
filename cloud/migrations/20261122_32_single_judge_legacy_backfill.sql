-- Authenticated judge-only entry for legacy-library classification and pre-score jobs.
begin;

create function public.single_judge_backfill(
 w uuid,p_kind text,p_offset integer,p_limit integer
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_LEGACY_BACKFILL_M32
 access_info=public.single_judge_access(w);
 if access_info->>'mode' is distinct from 'single-judge' or access_info->>'role' is distinct from 'judge' then
  raise exception '仅指定专业评分师可以补档老库' using errcode='42501';
 end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then
  raise exception '队列类型无效' using errcode='22023'; end if;
 if p_offset is null or p_offset<0 or p_limit is null or p_limit not between 1 and 20 then
  raise exception '补档分页参数无效' using errcode='22023'; end if;
 return public.ai_review_backfill(w,auth.uid(),p_kind,p_offset,p_limit);
end $$;
revoke all on function public.single_judge_backfill(uuid,text,integer,integer)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_backfill(uuid,text,integer,integer) to authenticated;
commit;
