import {erasures} from '../api/research.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {esc} from '../utils/format.js';

const STEP_STATE={pending:'等待',purged:'已移除，待核验',verified:'已核验',failed:'未通过'};
const ERASE_STATE={pending:'待执行',completed:'已完成',failed:'中断，待恢复'};

function stepsTable(steps){
  return table(['清理位置','状态','已处理','核验说明'],steps.map(s=>[
    s.label,
    `<span class="status ${s.status==='verified'?'completed':s.status==='failed'?'failed':'pending'}">${STEP_STATE[s.status]||s.status}</span>`,
    s.removed>0?s.removed:'—',
    s.note||'—',
  ]));
}

function runsTable(runs){
  return table(['运行','涉及批次','快照行数','结果'],runs.length?runs.map(r=>[
    '#'+r.run_id,
    esc(r.name)+' / '+r.study_id,
    r.snapshot_rows,
    r.result_present?'曾生成（已清除）':'无',
  ]):[['—','没有分析引用该纸样编号','—','—']]);
}

function detail(e){
  const percent=e.steps.filter(s=>s.status==='verified').length/e.steps.length*100;
  return `<div class="method-note"><strong>纸样编号 ${esc(e.specimen_number)}</strong>
    <p>状态：${ERASE_STATE[e.status]||e.status} · 影响分析 ${e.affected_runs} 个 · 登记观测 ${e.affected_observations} 条 · 曾获批导出 ${e.prior_exports} 次</p>
    ${e.error?`<p class="error">${esc(e.error)}</p>`:''}
    ${e.receipt_sha256?`<p class="checksum">清除凭据 SHA256 ${esc(e.receipt_sha256)}</p>`:''}</div>
  <div class="progress"><i style="width:${percent}%"></i></div>
  <p>${percent}% 的位置已逐处核验；未全部核验前不会标记完成。</p>
  <h2>清除范围</h2>${runsTable(e.runs)}
  <h2>逐处核验</h2>${stepsTable(e.steps)}
  <div class="toolbar">${e.status!=='completed'?`<button id="erase-resume">恢复清除</button>`:''}<button id="erase-refresh" class="secondary">刷新进度</button></div>
  ${e.receipt?`<details><summary>清除凭据（不含任何测量原值）</summary><pre>${esc(e.receipt)}</pre></details>`:''}`;
}

export async function render(id){
  const list=await erasures.list();
  const writable=localStorage.getItem('research-role')==='admin';
  let selected=null;
  if(id)selected=await erasures.get(id);
  else if(list.length)selected=await erasures.get(list[0].id);
  return `<div class="eyebrow">06 / WITHDRAWAL REGISTER</div><h1>测量授权撤回与清除</h1>
    <p>供应方撤回某个纸样编号的测量授权后，按编号不可逆清除：立即停止受影响分析的读取与获批导出，移除登记、冻结输入、结果与留档中的原值和可还原数值，保留清除凭据。其他分析照常可用。</p>
    ${writable?`<form id="erase-form" class="worksheet"><label>纸样编号<input name="specimen_number" required maxlength="100" placeholder="如 DEMO-1-0001"></label>
      <label>撤回依据 / 说明<textarea name="reason" maxlength="2000" placeholder="记录供应方撤回授权的日期与范围"></textarea></label>
      <p>编号按全半角、大小写与首尾空格归一化匹配；清除后同编号重录和旧任务重试都会被拒绝，且不可撤销。</p>
      <button type="submit">确认撤回并清除 →</button></form>`:'<p class="method-note">仅负责人可以发起清除；所有角色可查看清除范围与进度。</p>'}
    <h2>清除记录</h2>
    ${table(['编号','纸样编号','状态','影响分析','核验凭据','操作'],list.length?list.map(e=>[
      '#'+e.id,esc(e.specimen_number),ERASE_STATE[e.status]||e.status,e.affected_runs,
      e.receipt_sha256?e.receipt_sha256.slice(0,12):'—',
      {html:`<a href="#erasure/${e.id}">查看范围与进度 →</a>`},
    ]):[['—','尚无清除记录','—','—','—','—']])}
    ${selected?`<h2>#${selected.id} 清除进度</h2>${detail(selected)}`:''}`;
}

export function bind(refresh){
  bindForm('erase-form',async data=>{
    const e=await erasures.create({specimen_number:data.specimen_number,reason:data.reason||''});
    if(e.status!=='completed')document.getElementById('notice').textContent='清除尚未完成：'+(e.error||'可在此页恢复');
    await refresh();
    location.hash='erasure/'+e.id;
  });
  const refreshButton=document.getElementById('erase-refresh');
  if(refreshButton)refreshButton.onclick=()=>refresh();
  const resume=document.getElementById('erase-resume');
  if(resume)resume.onclick=async()=>{
    resume.disabled=true;
    try{
      const e=await erasures.execute(Number(location.hash.split('/')[1]));
      if(e.status!=='completed')document.getElementById('notice').textContent='清除尚未完成：'+(e.error||'请稍后查看进度');
    }catch(error){document.getElementById('notice').textContent=error.message;}
    await refresh();
  };
}
