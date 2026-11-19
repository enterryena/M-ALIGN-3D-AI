const fs=require('fs'),vm=require('vm');globalThis.window=globalThis;
globalThis.localStorage={_:{},getItem(k){return this._[k]||null},setItem(k,v){this._[k]=v}};
vm.runInThisContext(fs.readFileSync('process.argv[3]','utf8'));
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
const S=globalThis.MASKIT_YARD_SIM, mem=new S.CaseMemory('k');
(async()=>{ for (const seed of [1,2,3]){
  const y=new S.Yard({seed,memory:mem,vary:true,speed:1}).start(); const t0=Date.now();
  while(y.t<1000){ y.step(.1); y._wake(); await new Promise(r=>setImmediate(r)); }
  const s=y.state(); y.stop();
  console.log('seed',seed,'wall',((Date.now()-t0)/1000).toFixed(1)+'s',JSON.stringify({in:s.stats.inbound,out:s.stats.outbound,contacts:s.stats.contacts,horns:s.stats.horns,det:s.stats.detours,clr:s.stats.clears,af:s.stats.align_fail,occ:s.occupied}),
   'walk%',(100*s.metrics.worker_on_walkway/Math.max(1,s.metrics.worker_steps)).toFixed(1),'wgap',s.metrics.worker_min_gap_lawful.toFixed(2),'cgap',s.metrics.car_min_gap,'cases',mem.cases.length, JSON.stringify(s.metrics.wg_at));
  if(seed===1) console.log(s.log.slice(-8).map(l=>l.t+' '+l.who+' '+l.text).join('\n'));
}})();
