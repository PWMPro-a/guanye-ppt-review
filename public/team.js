'use strict';
function decorateCloud(){
  $('.workspace-label').textContent=Cloud.member.name+'的评审工作台';
  $('.breadcrumb').firstChild.textContent='团队空间 ';
  const label=$('.local-label');if(label)label.innerHTML='<span class="status-dot"></span>团队云端作品库';
  const avatar=$('.avatar');if(avatar){avatar.textContent=Cloud.member.name.slice(0,1);avatar.title=Cloud.member.name;}
  if(!$('.sidebar [data-cloud-action]'))$('.sidebar-divider').insertAdjacentHTML('afterend','<button class="nav-item" data-cloud-action="team">'+icon('list')+'<span>团队评分汇总</span></button><button class="nav-item" data-cloud-action="logout">'+icon('arrow')+'<span>退出登录</span></button>');
  if(!$('.sidebar [data-cloud-action="password"]'))$('.sidebar-divider').insertAdjacentHTML('afterend','<button class="nav-item" data-cloud-action="password">'+icon('settings')+'<span>修改登录密码</span></button>');
  if(!$('.topbar-actions [data-cloud-action]'))$('.topbar-actions').insertAdjacentHTML('afterbegin','<button class="button small" data-cloud-action="team">团队汇总</button>');
  if(avatar){avatar.tabIndex=0;avatar.setAttribute('role','button');avatar.setAttribute('aria-label','账号设置');avatar.dataset.cloudAction='account';}
  $$('.save-status').forEach(el=>{el.innerHTML=el.innerHTML.replace('已保存到本地','已保存到云端');});
}
async function showTeam(){
  if(!await flushSave())return;
  showModal(modalHeader('团队评分汇总','各人独立评分，汇总仅统计已完成全部评分项的记录。')+'<div id="team-content" class="team-content">正在读取团队评分…</div><div class="modal-footer"><button class="button" data-cloud-action="team-csv">导出团队明细</button><button class="button primary" data-action="close-modal">完成</button></div>','team-modal');
  try{await Cloud.refreshTeam();renderTeam();}catch(err){$('#team-content').textContent=err.message;}
}
function teamRows(){
  const data=Cloud.team;
  return decks.map(d=>{const records=data.reviews.filter(r=>r.deck_id===d.id&&Scoring.stats(r.data,d,state.dimensions).complete);return {d,records,mean:records.length?records.reduce((sum,r)=>sum+Scoring.stats(r.data,d,state.dimensions).mean,0)/records.length:null};}).sort((a,b)=>(b.mean??-1)-(a.mean??-1));
}
function teamCell(d,member){
  const record=Cloud.team.reviews.find(r=>r.deck_id===d.id&&r.user_id===member.user_id);
  if(!record)return '<td>—</td>';
  const r=record.data,stats=Scoring.stats(r,d,state.dimensions),perPage=Scoring.mode(r)==='page';
  const details=perPage?'<details><summary>查看逐页评分</summary>'+d.images.map((im,i)=>{const page=Scoring.page(r,im);return '<small>第 '+(i+1)+' 页 · '+state.dimensions.map(dim=>esc(dim.name)+' '+fmt(page.scores[dim.id])).join(' · ')+(page.note?' · '+esc(page.note):'')+'</small>';}).join('')+'</details>':'';
  return '<td><b>'+(stats.complete?fmt(stats.mean):'评分中')+'</b><small>'+(perPage?'逐页评分 · '+stats.pagesDone+'/'+d.images.length+' 页':'整套评分')+'</small><small>'+state.dimensions.map(dim=>esc(dim.name)+' '+fmt(stats.scores[dim.id])).join(' · ')+'</small>'+(!perPage&&r.note?'<small class="team-note">'+esc(r.note)+'</small>':'')+details+'</td>';
}
function renderTeam(){
  const {members}=Cloud.team;
  $('#team-content').innerHTML='<p class="team-description">'+members.length+' 位评审 · '+decks.length+' 套作品 · '+state.maxScore+' 分制</p><div class="team-table-scroll"><table class="team-table"><thead><tr><th>作品</th><th>团队均分</th><th>已评人数</th>'+members.map(m=>'<th>'+esc(m.name)+'</th>').join('')+'</tr></thead><tbody>'+teamRows().map(({d,records,mean})=>'<tr><th>'+esc(d.title)+'</th><td class="team-mean">'+fmt(mean)+'</td><td>'+records.length+' / '+members.length+'</td>'+members.map(m=>teamCell(d,m)).join('')+'</tr>').join('')+'</tbody></table></div>';
}
function exportTeam(){
 const cell=x=>'"'+String(x??'').replace(/^[=+\-@\t\r]/,"'$&").replace(/"/g,'""')+'"';
 const rows=[['作品','评审','评分模式','页码',...state.dimensions.map(d=>d.name),'个人均分','状态','笔记']];
 for(const d of decks)for(const m of Cloud.team.members){
   const record=Cloud.team.reviews.find(r=>r.deck_id===d.id&&r.user_id===m.user_id),r=record?.data,stats=Scoring.stats(r,d,state.dimensions),perPage=Scoring.mode(r)==='page';
   rows.push([d.title,m.name,perPage?'逐页评分':'整套评分','汇总',...state.dimensions.map(dim=>stats.scores[dim.id]),stats.complete?fmt(stats.mean):'',stats.complete?'已评分':stats.values.length?'评分中':'待评分',perPage?'':r?.note||'']);
   if(perPage)d.images.forEach((im,i)=>{const page=Scoring.page(r,im),values=state.dimensions.map(dim=>page.scores[dim.id]),complete=values.every(hasScore);rows.push([d.title,m.name,'逐页评分',i+1,...values,complete?fmt(values.reduce((a,b)=>a+b,0)/values.length):'',complete?'已评分':values.some(hasScore)?'评分中':'待评分',page.note||'']);});
 }
 download('观页-团队评分-'+dateStamp()+'.csv','\uFEFF'+rows.map(row=>row.map(cell).join(',')).join('\r\n'),'text/csv;charset=utf-8');
}
function showCloudPassword(){showModal(modalHeader('修改登录密码','设置至少 8 位的新密码。')+'<form id="cloud-password-form"><label class="field-label" for="new-cloud-password">新密码</label><input id="new-cloud-password" class="text-input" type="password" minlength="8" required autocomplete="new-password"><div id="password-error" class="form-error" role="alert"></div><div class="modal-footer"><button type="button" class="button" data-action="close-modal">取消</button><button class="button primary" type="submit">保存密码</button></div></form>');}
function showCloudAccount(){showModal(modalHeader('账号设置',Cloud.member.name)+'<div class="export-options"><button class="export-option" data-cloud-action="password">修改登录密码</button><button class="export-option" data-cloud-action="logout">退出登录</button></div><div class="modal-footer"><button class="button" data-action="close-modal">完成</button></div>');}
document.addEventListener('click',e=>{if(e.target.closest('[data-cloud-action="account"]'))showCloudAccount();});
document.addEventListener('keydown',e=>{if(e.target.matches('[data-cloud-action="account"]')&&['Enter',' '].includes(e.key)){e.preventDefault();showCloudAccount();}});
document.addEventListener('submit',async e=>{if(e.target.id!=='cloud-password-form')return;e.preventDefault();const button=e.target.querySelector('[type="submit"]');button.disabled=true;try{await Cloud.changePassword($('#new-cloud-password').value);modal.close();toast('登录密码已更新');}catch(err){$('#password-error').textContent=err.message;}finally{button.disabled=false;}});
document.addEventListener('click',e=>{const button=e.target.closest('[data-cloud-action]');if(!button)return;if(button.dataset.cloudAction==='team')showTeam();if(button.dataset.cloudAction==='password')showCloudPassword();if(button.dataset.cloudAction==='logout'){if(pending||saving){toast('请等待评分保存后退出',true);return;}Cloud.logout();}if(button.dataset.cloudAction==='team-csv')exportTeam();});
