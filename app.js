/* Toast pre-bake analyzer — 100% client-side.
   The analysis engine below is a direct port of the Python (analyze.py) so the
   numbers match. Everything runs in the browser: nothing is uploaded anywhere. */

const DOW_NAMES = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const DOW_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
const DT = 0.25; // simulation step, minutes (15s)

/* ---------- small math utils ---------- */
function mulberry32(seed){ let a=seed>>>0; return function(){
  a|=0; a=a+0x6D2B79F5|0; let t=Math.imul(a^a>>>15,1|a);
  t=t+Math.imul(t^t>>>7,61|t)^t; return ((t^t>>>14)>>>0)/4294967296; }; }

function poissonRand(lam, rng){ if(lam<=0) return 0;
  const L=Math.exp(-lam); let k=0,p=1; do{k++; p*=rng();}while(p>L); return k-1; }

function poissonSf(k, mu){ // P(N >= k)
  if(k<=0) return 1;
  let cdf=Math.exp(-mu), term=cdf;
  for(let i=1;i<k;i++){ term*=mu/i; cdf+=term; }
  return Math.max(0,1-cdf);
}
function expectedMin(mu,q){ let s=0; for(let j=1;j<=q;j++) s+=poissonSf(j,mu); return s; }

function smooth(x,w){ if(w<=1) return x; const n=x.length,out=new Array(n).fill(0),h=Math.floor(w/2);
  for(let i=0;i<n;i++){ let s=0; for(let d=-h;d<=h;d++){ const j=i+d; if(j>=0&&j<n) s+=x[j]; } out[i]=s/w; }
  return out; }

function parseDate(s){ if(s instanceof Date) return s; let d=new Date(s);
  if(isNaN(d) && typeof s==="string") d=new Date(s.replace(" ","T"));
  return isNaN(d)?null:d; }

/* ---------- config ---------- */
function openMinutes(c){ return (c.close_hour-c.open_hour)*60; }
function bakeNominal(c){ return (c.bake_time_min+c.bake_time_max)/2; }
function margin(c){ return c.price-c.cog; }
function clock(c,m){ const t=Math.round(c.open_hour*60+m); const floorMod=(a,n)=>((a%n)+n)%n;
  const hh=floorMod(Math.floor(t/60),24), mm=floorMod(t,60);
  return String(hh).padStart(2,"0")+":"+String(mm).padStart(2,"0"); }

function validate(c){ const e=[];
  if(c.price<=0) e.push("price must be > 0");
  if(c.cog<0) e.push("cost must be >= 0");
  if(c.cog>=c.price) e.push("cost must be less than price");
  if(c.order_service_min<0) e.push("order/serve time must be >= 0");
  if(!(c.bake_time_min>0 && c.bake_time_max>=c.bake_time_min)) e.push("need 0 < bake min <= bake max");
  if(c.oven_slots<1) e.push("oven slots must be >= 1");
  if(c.fresh_window<=0) e.push("freshness window must be > 0");
  if(c.patience_min<=0) e.push("wait-before-walkout must be > 0");
  if(!(c.open_hour>=0 && c.open_hour<c.close_hour && c.close_hour<=24)) e.push("need open hour < close hour");
  if(!(c.success_threshold>0 && c.success_threshold<1)) e.push("success threshold must be 0-1");
  return e;
}

/* ---------- 1. demand estimation ---------- */
function estimateDemand(rows, c){
  const open=openMinutes(c);
  const perDow={}; DOW_ORDER.forEach(d=>perDow[d]={sum:new Array(open).fill(0),dates:new Set()});
  for(const r of rows){
    const d=r.date; const m=(d.getHours()-c.open_hour)*60+d.getMinutes();
    if(m<0||m>=open) continue;
    const dow=DOW_NAMES[d.getDay()];
    perDow[dow].sum[m]+=r.qty;
    perDow[dow].dates.add(d.getFullYear()+"-"+d.getMonth()+"-"+d.getDate());
  }
  const profiles={};
  for(const dow of DOW_ORDER){ const nd=Math.max(1,perDow[dow].dates.size);
    profiles[dow]=smooth(perDow[dow].sum.map(v=>v/nd),5); }
  return profiles;
}

/* how many calendar days of each weekday are in the data, regardless of
   time-of-day — used to scale a simulated day onto the annual total. Kept
   separate from estimateDemand's per-dow day count (which is restricted to
   business hours, since that one only normalizes the demand curve). */
function dowCounts(rows){
  const sets={}; DOW_ORDER.forEach(d=>sets[d]=new Set());
  for(const r of rows){ const d=r.date, dow=DOW_NAMES[d.getDay()];
    sets[dow].add(d.getFullYear()+"-"+d.getMonth()+"-"+d.getDate()); }
  return Object.fromEntries(DOW_ORDER.map(d=>[d,sets[d].size]));
}

/* ---------- 2. pre-bake schedule ---------- */
function buildSchedule(lam, c, cap){
  const w=c.fresh_window, bake=bakeNominal(c), open=openMinutes(c);
  const ovenOut=c.oven_slots*(w/bake); const rows=[]; let ws=0;
  while(ws<open){
    const lo=Math.floor(ws), hi=Math.min(open,Math.floor(ws+w));
    let mu=0; for(let i=lo;i<hi;i++) mu+=lam[i];
    const spare=Math.max(0,ovenOut-mu);
    let q=0; while(poissonSf(q+1,mu)>=c.success_threshold) q++;
    if(cap) q=Math.min(q,Math.floor(spare));
    if(q>0){ const exp=expectedMin(mu,q);
      // Don't ignite all q toasts at once — that grabs q ovens simultaneously
      // and can starve walk-in customers for the whole bake time, even though
      // "spare" was only ever an average over the window. Spread the q starts
      // evenly across the window (spacing = w/q) so concurrent oven draw from
      // pre-baking stays within the spare-capacity cap at any instant, not
      // just on average. See scheduleToStarts, which expands each row this way.
      const spacing=q>1?w/q:0, startLast=ws-bake+spacing*(q-1), readyLast=ws+spacing*(q-1);
      rows.push({ready_min:ws,start_min:ws-bake,ready_clock:clock(c,ws),start_clock:clock(c,ws-bake),
        ready_min_last:readyLast,start_min_last:startLast,
        ready_clock_last:clock(c,readyLast),start_clock_last:clock(c,startLast),
        qty:q,arrivals_expected:Math.round(mu*100)/100,p_first_sells:Math.round(poissonSf(1,mu)*1000)/1000,
        p_marginal_sells:Math.round(poissonSf(q,mu)*1000)/1000,
        exp_sold_fresh:Math.round(exp*100)/100,exp_waste:Math.round((q-exp)*100)/100}); }
    ws+=w;
  }
  return rows;
}
function scheduleToStarts(schedule,c){ const starts={}, n=Math.round(openMinutes(c)/DT), w=c.fresh_window;
  for(const r of schedule){ const spacing=r.qty>1?w/r.qty:0;
    for(let i=0;i<r.qty;i++){ let s=r.start_min+spacing*i; if(s<0) s=0;
      const step=Math.round(s/DT);
      if(step>=0&&step<n) starts[step]=(starts[step]||0)+1; } }
  return starts; }

/* ---------- 3. Monte-Carlo day simulation ---------- */
function drawArrivals(lam,c,rng){ const open=openMinutes(c), n=Math.round(open/DT), a=new Array(n);
  for(let s=0;s<n;s++){ const m=Math.min(Math.floor(s*DT),open-1); a[s]=poissonRand(lam[m]*DT,rng); } return a; }

function simulateDay(c,starts,rng,arrivals){
  const n=arrivals.length, slots=c.oven_slots, nominal=bakeNominal(c);
  const free=new Float64Array(slots), pre=new Array(slots).fill(false); let inv=[];
  let served=0,prebakedSold=0,balked=0,waste=0;
  for(let step=0;step<n;step++){ const now=step*DT;
    for(let i=0;i<slots;i++) if(free[i]<=now && pre[i]){ inv.push(free[i]+c.fresh_window); pre[i]=false; }
    if(inv.length){ const keep=[]; for(const e of inv){ if(e<=now) waste++; else keep.push(e); } inv=keep; }
    const want=starts[step]||0;
    if(want){ let done=0; for(let i=0;i<slots&&done<want;i++) if(free[i]<=now){
      free[i]=now+c.bake_time_min+(c.bake_time_max-c.bake_time_min)*rng(); pre[i]=true; done++; } }
    let arr=arrivals[step];
    while(arr-->0){
      if(inv.length){ let mi=0; for(let j=1;j<inv.length;j++) if(inv[j]<inv[mi]) mi=j; inv.splice(mi,1);
        served++; prebakedSold++; continue; }
      let f=-1; for(let i=0;i<slots;i++) if(free[i]<=now){ f=i; break; }
      if(f>=0){ const waitNow=nominal+c.order_service_min;
        if(waitNow>c.patience_min) balked++;
        else { free[f]=now+c.bake_time_min+(c.bake_time_max-c.bake_time_min)*rng(); served++; } }
      else {
        // No free oven right now. Two ways this customer could still get fed:
        // (a) queue behind a WALK-IN order for a dedicated fresh bake once that
        //     slot frees, or (b) claim an already-in-progress SCHEDULED PRE-BAKE
        //     once it finishes (no extra bake needed - it's already cooking).
        // Pick whichever is sooner; never silently cancel a pre-bake in favor
        // of (a) — that would throw away real oven output for nothing.
        let miWalk=-1,miPre=-1;
        for(let i=0;i<slots;i++){ if(pre[i]){ if(miPre<0||free[i]<free[miPre]) miPre=i; }
          else { if(miWalk<0||free[i]<free[miWalk]) miWalk=i; } }
        const waitWalk=miWalk>=0?(free[miWalk]-now)+nominal+c.order_service_min:Infinity;
        const waitPre=miPre>=0?(free[miPre]-now):Infinity;
        const wait=Math.min(waitWalk,waitPre);
        if(wait>c.patience_min) balked++;
        else if(waitPre<=waitWalk){ pre[miPre]=false; served++; prebakedSold++; }
        else { free[miWalk]=free[miWalk]+c.bake_time_min+(c.bake_time_max-c.bake_time_min)*rng(); served++; } }
    }
  }
  const baked=served+waste, revenue=served*c.price;
  return {served,prebakedSold,balked,waste,revenue,profit:revenue-baked*c.cog,lost_margin:balked*margin(c)};
}

function evaluateDow(lam,c,schedule,sims,rng){
  const starts=scheduleToStarts(schedule,c);
  const keys=["served","balked","waste","profit","revenue","prebakedSold"];
  const base={},pol={}; keys.forEach(k=>{base[k]=0;pol[k]=0;});
  for(let s=0;s<sims;s++){ const arr=drawArrivals(lam,c,rng);
    const b=simulateDay(c,{},rng,arr), p=simulateDay(c,starts,rng,arr);
    keys.forEach(k=>{base[k]+=b[k];pol[k]+=p[k];}); }
  keys.forEach(k=>{base[k]/=sims;pol[k]/=sims;});
  const wasteCost=pol.waste*c.cog, gain=pol.profit-base.profit;
  const totalPre=pol.prebakedSold+pol.waste;
  return {base,pol,daily_gain:gain,waste_cost:wasteCost,
    success:totalPre?pol.prebakedSold/totalPre:0,
    roi:wasteCost>1e-9?gain/wasteCost:null, prebakes:totalPre};
}

/* ---------- verdict + orchestration ---------- */
function verdict(annual,c){ const gain=annual.gain,wc=annual.waste_cost,rec=annual.recovered,pre=annual.prebaked;
  if(pre<1) return {recommend:false,headline:"Pre-baking won't help at these settings.",
    detail:`Whenever demand is dense enough that a toast reliably sells within the ${c.fresh_window}-min freshness window, your oven (${c.oven_slots} slots) is already at capacity — so there's no spare slot to pre-bake into. The rush queue is a capacity limit, not a scheduling problem; adding oven capacity is the real fix.`};
  if(gain>0 && gain>Math.max(500,0.5*wc)) return {recommend:true,
    headline:`Pre-baking looks worth it: about +${Math.round(gain).toLocaleString()} THB/year.`,
    detail:`You'd waste ~${Math.round(annual.waste_units).toLocaleString()} toasts/year (~${Math.round(wc).toLocaleString()} THB) but recover ~${Math.round(rec).toLocaleString()} walk-out sales worth more. Follow the schedule below during the marked rush windows.`};
  return {recommend:false,headline:"Pre-baking roughly breaks even — not worth the effort here.",
    detail:`It would cost ~${Math.round(wc).toLocaleString()} THB/year in wasted toasts to recover only ~${Math.round(rec).toLocaleString()} sales. The freshness window is too short to build a buffer, so there's little upside at this volume.`};
}

function demandBuckets(lam,c,width=10){ const open=openMinutes(c),out=[];
  for(let m=0;m<open;m+=width){ let s=0,n=0; for(let i=m;i<Math.min(open,m+width);i++){s+=lam[i];n++;}
    out.push({clock:clock(c,m),rate:Math.round((n?s/n:0)*1000)/1000}); } return out; }

function runAnalysis(rows,c,sims){
  const seed=12345; const rng=mulberry32(seed);
  const profiles=estimateDemand(rows,c), counts=dowCounts(rows);
  const by_dow=[],schedule=[],demand_chart={};
  const annual={gain:0,waste_cost:0,recovered:0,base_profit:0,pol_profit:0,base_balk:0,pol_balk:0,prebaked:0,waste_units:0};
  for(const dow of DOW_ORDER){ const lam=profiles[dow]; demand_chart[dow]=demandBuckets(lam,c);
    const candidate=buildSchedule(lam,c,false);
    const policy=buildSchedule(lam,c,true); policy.forEach(r=>schedule.push({day_of_week:dow,...r}));
    const ev=evaluateDow(lam,c,policy,sims,rng);
    const n=counts[dow]||0, recovered=ev.base.balked-ev.pol.balked;
    const candUnits=candidate.reduce((s,r)=>s+r.qty,0);
    const candP=candidate.length?candidate.reduce((s,r)=>s+r.p_first_sells,0)/candidate.length:0;
    by_dow.push({day_of_week:dow,days_in_data:n,avg_sold:Math.round(ev.base.served*10)/10,
      candidate_units:candUnits,candidate_success:Math.round(candP*1000)/1000,
      prebake_units:Math.round(ev.prebakes*10)/10,success_rate:Math.round(ev.success*1000)/1000,
      recovered:Math.round(recovered*10)/10,daily_gain:Math.round(ev.daily_gain*10)/10,
      roi:ev.roi==null?null:Math.round(ev.roi*10)/10});
    annual.gain+=ev.daily_gain*n; annual.waste_cost+=ev.waste_cost*n; annual.recovered+=recovered*n;
    annual.base_profit+=ev.base.profit*n; annual.pol_profit+=ev.pol.profit*n;
    annual.base_balk+=ev.base.balked*n; annual.pol_balk+=ev.pol.balked*n;
    annual.prebaked+=ev.prebakes*n; annual.waste_units+=ev.pol.waste*n;
  }
  const totalToasts=rows.reduce((s,r)=>s+r.qty,0);
  const nDays=new Set(rows.map(r=>r.date.getFullYear()+"-"+r.date.getMonth()+"-"+r.date.getDate())).size;
  const summary={avg_per_day:nDays?Math.round(totalToasts/nDays*10)/10:0,
    annual_extra_profit:Math.round(annual.gain),annual_waste_cost:Math.round(annual.waste_cost),
    annual_recovered:Math.round(annual.recovered),
    baseline_walkouts:Math.round(annual.base_balk),policy_walkouts:Math.round(annual.pol_balk),
    total_toasts:totalToasts,n_days:nDays};
  return {meta:{capacity_per_min:Math.round(c.oven_slots/bakeNominal(c)*1000)/1000,
      freshness_floor_per_min:Math.round(-Math.log(1-c.success_threshold)/c.fresh_window*1000)/1000,sims},
    summary,verdict:verdict(annual,c),by_dow,schedule,demand_chart};
}

/* =========================================================================
   UI wiring
   ========================================================================= */
const $=id=>document.getElementById(id);
const KNOBS=["price","cog","oven_slots","fresh_window","patience_min","order_service_min",
  "bake_time_min","bake_time_max","open_hour","close_hour","success_threshold","sims"];
let STATE={rawRows:null, cols:[], distinct:{}, day:"Sunday", chart:null, dataReady:false};

function getConfig(){ const c={}; KNOBS.forEach(k=>c[k]=parseFloat($(k).value));
  c.oven_slots=Math.round(c.oven_slots); c.open_hour=Math.round(c.open_hour);
  c.close_hour=Math.round(c.close_hour); c.sims=Math.round(c.sims); return c; }

function syncReadouts(){ KNOBS.forEach(k=>{ const o=$(k+"_out"); if(o) o.textContent=$(k).value; }); }

/* CSV upload */
$("file").addEventListener("change",e=>{ const f=e.target.files[0]; if(!f) return;
  $("uploadStatus").textContent="Reading "+f.name+" ...";
  Papa.parse(f,{header:true,skipEmptyLines:true,complete:res=>{
    ingest(res.data,res.meta.fields,f.name); }}); });

$("sampleBtn").addEventListener("click",()=>{ const {rows,cols}=makeSample(); ingest(rows,cols,"sample data"); });

function ingest(data,fields,name){
  STATE.cols=fields.map(String);
  STATE.parsed=data;
  // guess columns
  const g=(pats)=>{ for(const p of pats) for(const col of STATE.cols) if(new RegExp(p,"i").test(col)) return col; return null; };
  let dt=g(["date.?time","timestamp","\\btime\\b","\\bdate\\b"]);
  if(!dt){ let best=null,sc=0; for(const col of STATE.cols){ let ok=0,tot=0;
      for(const r of data.slice(0,200)){ tot++; if(parseDate(r[col])) ok++; } if(tot&&ok/tot>sc){best=col;sc=ok/tot;} }
    dt=sc>0.5?best:STATE.cols[0]; }
  const item=g(["item","product","menu","\\bname\\b","description"]);
  const qty=g(["qty","quantit","count","units","\\bpcs\\b","amount"]);
  // distinct values per column (for item dropdown)
  STATE.distinct={}; for(const col of STATE.cols){ const set=new Set();
    for(const r of data){ const v=r[col]; if(v!=null&&v!=="") set.add(String(v)); if(set.size>300) break; }
    if(set.size<=300) STATE.distinct[col]=[...set].sort(); }
  fillMapping(dt,item,qty);
  $("uploadStatus").textContent=`Loaded ${data.length.toLocaleString()} rows from ${name}.`;
  $("mapCard").classList.remove("hidden"); $("knobCard").classList.remove("hidden");
  reanalyze();
}

function fillMapping(dt,item,qty){
  const opt=(v,t)=>`<option value="${v}">${t||v}</option>`;
  const none='<option value="">— none —</option>';
  $("mDatetime").innerHTML=STATE.cols.map(c=>opt(c)).join("");
  $("mItem").innerHTML=none+STATE.cols.map(c=>opt(c)).join("");
  $("mQty").innerHTML=none+STATE.cols.map(c=>opt(c)).join("");
  if(dt)$("mDatetime").value=dt; if(item)$("mItem").value=item; if(qty)$("mQty").value=qty;
  fillItems(); $("mItem").onchange=()=>{fillItems();reanalyze();};
  $("mDatetime").onchange=reanalyze; $("mQty").onchange=reanalyze; $("mItemName").onchange=reanalyze;
}
function fillItems(){ const col=$("mItem").value, vals=STATE.distinct[col]||[];
  const opt=(v,t)=>`<option value="${v}">${t||v}</option>`;
  $("mItemName").innerHTML=opt("","(analyze every row)")+vals.map(v=>opt(v)).join("");
  $("rowInfo").textContent=col&&vals.length?`${vals.length} products in “${col}”. Pick one, or analyze all rows.`
    :"No item column — every row counts as the product."; }

/* build the filtered/cleaned rows for the current mapping */
function buildRows(c){ const dt=$("mDatetime").value,item=$("mItem").value,qty=$("mQty").value,
    want=$("mItemName").value.trim().toLowerCase(); const out=[];
  for(const r of STATE.parsed){ const d=parseDate(r[dt]); if(!d) continue;
    if(item && want && String(r[item]).trim().toLowerCase()!==want) continue;
    let q=qty?parseFloat(r[qty]):1; if(!isFinite(q)) q=1; out.push({date:d,qty:q}); }
  return out; }

/* debounced re-analysis on any knob/mapping change */
let timer=null;
function reanalyze(){ if(!STATE.parsed) return; syncReadouts();
  clearTimeout(timer); $("runStatus").textContent="Updating…"; $("err").textContent="";
  timer=setTimeout(()=>{ try{
      const c=getConfig(); const errs=validate(c);
      if(errs.length){ $("err").textContent=errs.join("; "); $("runStatus").textContent=""; return; }
      const rows=buildRows(c);
      if(!rows.length){ $("err").textContent="No rows match — check the timestamp column and item filter."; $("runStatus").textContent=""; return; }
      const res=runAnalysis(rows,c,c.sims); render(res,c);
      $("results").classList.remove("hidden"); $("runStatus").textContent="";
    }catch(ex){ $("err").textContent=String(ex); $("runStatus").textContent=""; } },250); }

KNOBS.forEach(k=>$(k).addEventListener("input",reanalyze));

/* rendering */
const fmt=n=>Math.round(n).toLocaleString();
function render(res,c){
  const v=res.verdict;
  $("verdict").innerHTML=`<div class="banner ${v.recommend?'yes':'no'}"><h2>${v.recommend?'✓ ':''}${v.headline}</h2><p>${v.detail}</p></div>`;
  const s=res.summary,g=s.annual_extra_profit;
  const cards=[["Toasts / day (avg)",fmt(s.avg_per_day),""],
    ["Extra profit / year",(g>=0?"+":"−")+fmt(Math.abs(g))+" ฿",g>0?"pos":(g<0?"neg":"")],
    ["Wasted toasts / yr",fmt(s.annual_waste_cost)+" ฿",""],
    ["Walk-out sales recovered",fmt(s.annual_recovered)+"/yr",s.annual_recovered>0?"pos":""],
    ["Walk-outs now → plan",fmt(s.baseline_walkouts)+" → "+fmt(s.policy_walkouts),""]];
  $("metrics").innerHTML=cards.map(x=>`<div class="metric"><div class="k">${x[0]}</div><div class="v ${x[2]}">${x[1]}</div></div>`).join("");
  $("dowTable").innerHTML=`<tr><th>Day</th><th>Sold/day</th><th>Fresh-able windows</th><th>Safe pre-bake/day</th><th>Sales saved/day</th><th>Gain/day</th></tr>`+
    res.by_dow.map(r=>`<tr><td>${r.day_of_week}</td><td>${fmt(r.avg_sold)}</td><td>${r.candidate_units} @ ${Math.round(r.candidate_success*100)}%</td><td>${r.prebake_units}</td><td>${r.recovered}</td><td>${r.daily_gain>=0?'+':'−'}${fmt(Math.abs(r.daily_gain))} ฿</td></tr>`).join("");
  const busiest=[...res.by_dow].sort((a,b)=>b.avg_sold-a.avg_sold)[0].day_of_week;
  if(!STATE.dataReady){ STATE.day=busiest; STATE.dataReady=true; }
  $("dayBtns").innerHTML=DOW_ORDER.map(d=>`<button data-d="${d}">${d.slice(0,3)}</button>`).join("");
  $("dayBtns").querySelectorAll("button").forEach(b=>b.onclick=()=>{STATE.day=b.dataset.d;drawDay(res,c);});
  drawDay(res,c);
}
function drawDay(res,c){ const day=STATE.day;
  $("dayBtns").querySelectorAll("button").forEach(b=>b.classList.toggle("active",b.dataset.d===day));
  $("schedDay").textContent="— "+day;
  const pts=res.demand_chart[day];
  const labels=pts.map(p=>p.clock.endsWith(":00")?p.clock:"");
  const cap=pts.map(()=>res.meta.capacity_per_min), floor=pts.map(()=>res.meta.freshness_floor_per_min);
  if(STATE.chart) STATE.chart.destroy();
  STATE.chart=new Chart($("chart"),{type:"line",
    data:{labels,datasets:[
      {data:pts.map(p=>p.rate),borderColor:"#2a78d6",backgroundColor:"rgba(42,120,214,.08)",borderWidth:2,fill:true,tension:.35,pointRadius:0},
      {data:cap,borderColor:"#c0392b",borderWidth:2,borderDash:[6,4],pointRadius:0,fill:false},
      {data:floor,borderColor:"#ba7517",borderWidth:2,borderDash:[6,4],pointRadius:0,fill:false}]},
    options:{responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{title:it=>pts[it[0].dataIndex].clock,
        label:it=>it.datasetIndex===0?it.formattedValue+" toasts/min":null}}},
      scales:{y:{beginAtZero:true,title:{display:true,text:"toasts / min"}},x:{ticks:{autoSkip:false,maxRotation:0}}}}});
  const sched=res.schedule.filter(r=>r.day_of_week===day);
  const range=(a,b)=>a===b?a:`${a}–${b} (1 at a time)`;
  $("schedTable").innerHTML=sched.length?
    `<tr><th>Start baking</th><th>Ready</th><th>Qty</th><th>Expected customers</th><th>Chance it sells fresh</th></tr>`+
     sched.map(r=>`<tr><td>${range(r.start_clock,r.start_clock_last)}</td><td>${range(r.ready_clock,r.ready_clock_last)}</td><td>${r.qty}</td><td>${r.arrivals_expected}</td><td>${Math.round(r.p_marginal_sells*100)}%</td></tr>`).join("")
    :`<tr><td style="color:#6b6a65">No windows this day are dense enough to pre-bake within the freshness limit.</td></tr>`;
}

/* sample data generator (so you can try it without a file) */
function makeSample(){ const rng=mulberry32(7); const two=n=>String(n).padStart(2,"0");
  const rows=[]; const start=new Date(2025,0,6); const sumW=314;
  for(let d=0; d<120; d++){ const day=new Date(start.getTime()+d*86400000); const dow=day.getDay();
    const base=(dow===0||dow===6)?330:(dow===5?290:205);
    for(let m=0;m<600;m++){ const w=0.25+Math.exp(-Math.pow((m-75)/28,2))+1.3*Math.exp(-Math.pow((m-480)/50,2));
      if(rng()<base*w/sumW){ rows.push({datetime:`${day.getFullYear()}-${two(day.getMonth()+1)}-${two(day.getDate())} ${two(11+Math.floor(m/60))}:${two(m%60)}:${two(Math.floor(rng()*60))}`,item:"Toast",quantity:1}); } } }
  return {rows,cols:["datetime","item","quantity"]}; }

syncReadouts();
