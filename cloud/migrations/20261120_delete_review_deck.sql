create or replace function public.delete_review_deck(w uuid, target_id text, expected_revision bigint) returns bigint
language plpgsql security definer set search_path='' as $$
declare current_revision bigint;
begin
 if not exists (select 1 from public.members where workspace_id=w and user_id=auth.uid() and role='admin') then raise exception '仅管理员可以删除作品'; end if;
 perform 1 from public.workspaces where id=w for update;
 select revision into current_revision from public.members where workspace_id=w and user_id=auth.uid() and role='admin' for update;
 if not found then raise exception '仅管理员可以删除作品'; end if;
 if current_revision is distinct from expected_revision then raise exception '评审记录已更新，请刷新后重试'; end if;
 perform 1 from public.decks where workspace_id=w and id=target_id for update;
 if not found then raise exception '作品不存在或已被删除'; end if;
 update public.members m set revision=m.revision+1 where m.workspace_id=w and exists (select 1 from public.reviews r where r.workspace_id=w and r.deck_id=target_id and r.user_id=m.user_id);
 delete from public.reviews where workspace_id=w and deck_id=target_id;
 delete from public.decks where workspace_id=w and id=target_id;
 return (select revision from public.members where workspace_id=w and user_id=auth.uid());
end $$;
revoke all on function public.delete_review_deck(uuid,text,bigint) from public,anon;
grant execute on function public.delete_review_deck(uuid,text,bigint) to authenticated;
