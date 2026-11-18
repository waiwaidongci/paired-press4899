import {store} from '../../stores/research.js';
import {table} from './table.js';
import {status} from './status.js';
import {esc} from '../../utils/format.js';
export function runList(){return table(['编号 / 运行','状态','源数据快照','复核'],store.runs.map(r=>{
  const erased=r.status==='erased';
  const link=erased?{html:`#${r.id} ${esc(r.name)}`}:{html:`<a href="#results/${r.id}">#${r.id} ${esc(r.name)}</a>`};
  const detail=erased?`数值已清除${r.erasure_id?' · <a href="#erasure/'+r.erasure_id+'">清除凭据</a>':''}`:r.snapshot.length+'条 · '+r.checksum.slice(0,12);
  return [link,{html:status(r.status)},detail,erased?'随运行清除':r.review?'已复核':'待负责人复核'];
}));}
