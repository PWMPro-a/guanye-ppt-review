'use strict';
function decorateCloud(){
  $('.workspace-label').textContent=Cloud.member.name+'的评审工作台';
  $('.breadcrumb').firstChild.textContent='团队空间 ';
  const label=$('.local-label');if(label)label.innerHTML='<span class="status-dot"></span>团队云端作品库';
  const avatar=$('.avatar');if(avatar){avatar.textContent=Cloud.member.name.slice(0,1);avatar.title=Cloud.member.name;}
  if(!$('.sidebar [data-cloud-action]'))$('.sidebar-divider').insertAdjacentHTML('afterend','<button class="nav-item" data-cloud-action="team">'+icon('list')+'<span>团队评分汇总</span></button><button class="nav-item" data-cloud-action="logout">'+icon('arrow')+'<span>退出登录</span></button>');
  if(!$('.topbar-actions [data-cloud-action]'))$('.topbar-actions').insertAdjacentHTML('afterbegin','<button class="button small" data-cloud-action="team">团队汇总</button>');
  $$('.save-status').forEach(el=>{el.innerHTML=el.innerHTML.replace('已保存到本地','已保存到云端');});
  if(Cloud.member.role!=='admin')$$('[data-action="settings"],[data-action="import"],[data-action="append"]').forEach(el=>el.hidden=true);
}
async function showTeam(){
  if(!await flushSave())return;
  showModal(modalHeader('团队评分汇总','各人独立评分，汇总仅统计已完成全部评分项的记录。')+'<div id="team-content" class="team-content">正在读取团队评分…</div><div class="modal-footer"><button class="button" data-cloud-action="team-csv">导出团队明细</button><button class="button primary" data-action="close-modal">完成</button></div>','team-modal');
  try{await Cloud.refreshTeam();renderTeam();}catch(err){$('#team-content').textContent=err.message;}
}
function teamRows(){
  const data=Cloud.team;
  return decks.map(d=>{const records=data.reviews.filter(r=>r.deck_id===d.id&&state.dimensions.every(dim=>hasScore(r.data.scores[dim.id])));return {d,records,mean:records.length?records.reduce((sum,r)=>sum+state.dimensions.reduce((s,dim)=>s+r.data.scores[dim.id],0)/state.dimensions.length,0)/records.length:null};}).sort((a,b)=>(b.mean??-1)-(a.mean??-1));
}
function renderTeam(){
  const {members}=Cloud.team;
  $('#team-content').innerHTML='<p class="team-description">'+members.length+' 位评审 · '+decks.length+' 套作品 · '+state.maxScore+' 分制</p><div class="team-table-scroll"><table class="team-table"><thead><tr><th>作品</th><th>团队均分</th><th>已评人数</th>'+members.map(m=>'<th>'+esc(m.name)+'</th>').join('')+'</tr></thead><tbody>'+teamRows().map(({d,records,mean})=>'<tr><th>'+esc(d.title)+'</th><td class="team-mean">'+fmt(mean)+'</td><td>'+records.length+' / '+members.length+'</td>'+members.map(m=>{const record=Cloud.team.reviews.find(r=>r.deck_id===d.id&&r.user_id===m.user_id);const values=state.dimensions.map(dim=>record?.data.scores[dim.id]);const complete=values.every(hasScore);return '<td><b>'+ (complete?fmt(values.reduce((s,n)=>s+n,0)/values.length):record?'评分中':'—')+'</b>'+(record?'<small>'+state.dimensions.map(dim=>esc(dim.name)+' '+fmt(record.data.scores[dim.id])).join(' · ')+'</small>'+(record.data.note?'<small class="team-note">'+esc(record.data.note)+'</small>':''):'')+'</td>';}).join('')+'</tr>').join('')+'</tbody></table></div>';
}
function exportTeam(){
 const cell=x=>'"'+String(x??'').replace(/^[=+\-@\t\r]/,"'$&").replace(/"/g,'""')+'"';
 const rows=[['作品','评审',...state.dimensions.map(d=>d.name),'个人均分','状态','笔记']];
 for(const d of decks)for(const m of Cloud.team.members){const record=Cloud.team.reviews.find(r=>r.deck_id===d.id&&r.user_id===m.user_id);const values=state.dimensions.map(dim=>record?.data.scores[dim.id]);const complete=values.every(hasScore);rows.push([d.title,m.name,...values,complete?fmt(values.reduce((s,n)=>s+n,0)/values.length):'',complete?'已评分':record?'评分中':'待评分',record?.data.note||'']);}
 download('观页-团队评分-'+dateStamp()+'.csv','\uFEFF'+rows.map(row=>row.map(cell).join(',')).join('\r\n'),'text/csv;charset=utf-8');
}
document.addEventListener('click',e=>{const button=e.target.closest('[data-cloud-action]');if(!button)return;if(button.dataset.cloudAction==='team')showTeam();if(button.dataset.cloudAction==='logout'){if(pending||saving){toast('请等待评分保存后退出',true);return;}Cloud.logout();}if(button.dataset.cloudAction==='team-csv')exportTeam();});
