-- Judge-only pagination over the existing review queue.
begin;

create function public.single_judge_review_queue(
 w uuid,p_kind text,p_after text,p_limit integer
) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_REVIEW_QUEUE_M33
 access_info=public.single_judge_access(w);
 if access_info->>'mode' is distinct from 'single-judge' or access_info->>'role' is distinct from 'judge' then
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
revoke all on function public.single_judge_review_queue(uuid,text,text,integer)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_review_queue(uuid,text,text,integer) to authenticated;
commit;
