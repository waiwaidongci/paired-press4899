import {studies,runs,erasures} from '../api/research.js';
export const store={studies:[],runs:[],erasures:[],studyId:0,runId:0,rows:[]};
export async function load(){
  [store.studies,store.runs,store.erasures]=await Promise.all([studies.list(),runs.list(),erasures.list()]);
  if(!store.studyId)store.studyId=store.studies[0]?.id||0;
}
export async function loadRows(){store.rows=store.studyId?await studies.rows(store.studyId):[];}
