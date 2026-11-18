import {DOMAIN} from '../constants/domain.js';
import {store,load} from '../stores/research.js';
import {runs} from '../api/research.js';
import {studySelect} from '../components/common/study-select.js';
import {runList} from '../components/common/run-list.js';
import {bindForm} from '../hooks/forms.js';
import {esc} from '../utils/format.js';

const labels={resamples:'重采样次数 (200–20000)'};

export function render(){
  const writable=localStorage.getItem('research-role')!=='viewer';
  const withdrawn=(store.erasures||[]).filter(e=>e.status!=='completed');
  return `<div class="eyebrow">02 / METHOD WORKSHEET</div><h1>${DOMAIN.field_label}</h1><p class="lead">${DOMAIN.algorithm}</p><div class="method-note">${DOMAIN.note}</div>
  ${withdrawn.length?`<div class="method-note"><strong>清除进行中</strong><p>以下纸样编号的授权已撤回，相关分析暂停读取与导出：${withdrawn.map(e=>esc(e.specimen_number)).join('、')}。其他纸样的分析照常可用。</p><a href="#erasure">查看清除范围与进度 →</a></div>`:''}
  ${writable?`<form id="run-form" class="worksheet">${studySelect()}<label>本次研究名称<input name="name" required minlength="2" value="${esc(DOMAIN.names[0])} · 核算"></label><div class="inline-form">${Object.entries(DOMAIN.defaults).map(([key,value])=>`<label>${labels[key]||key}<input name="${key}" type="number" step="any" value="${value}" required></label>`).join('')}</div><p>提交时冻结全部观测与参数，生成 SHA256。后台 worker 独立执行；已撤回授权的纸样不会进入新快照。</p><button type="submit">冻结输入并排队计算 →</button></form>`:'<p class="method-note">观察员可阅读研究记录，分析师与负责人可以发起计算。</p>'}<h2>计算记录</h2>${runList()}`;
}

export function bind(refresh){
  bindForm('run-form',async data=>{
    const parameters=Object.fromEntries(Object.keys(DOMAIN.defaults).map(k=>[k,+data[k]]));
    const r=await runs.create({study_id:+data.study_id,name:data.name,parameters});
    store.runId=r.id;await load();location.hash='results/'+r.id;
  });
  const sel=document.getElementById('study-select');if(sel)sel.onchange=()=>store.studyId=+sel.value;
}
