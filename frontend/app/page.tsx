'use client';
import {useEffect,useState,useRef} from 'react';
import {ShieldCheck,LayoutDashboard,Router,ClipboardCheck,GitBranch,BookOpen,Plus,Search,Upload,Download,ChevronRight,ChevronLeft,Check,AlertTriangle,Network,FileText,CheckCircle2,Loader2,X} from 'lucide-react';
import {SidebarProvider,Sidebar,SidebarContent,SidebarTrigger} from '@/components/ui/sidebar';
import {Dialog,DialogContent,DialogTitle,DialogDescription} from '@/components/ui/dialog';
import {Tabs,TabsList,TabsTrigger,TabsContent} from '@/components/ui/tabs';
import {Select,SelectTrigger,SelectValue,SelectContent,SelectItem} from '@/components/ui/select';
import {Table,TableHeader,TableBody,TableRow,TableHead,TableCell} from '@/components/ui/table';
import {Checkbox} from '@/components/ui/checkbox';
import {Toaster,toast} from 'sonner';
import {api,downloadReport} from '@/lib/api';
import type {Device,Finding} from '@/lib/types';
const icons:any={Router,Switch:Network,Firewall:ShieldCheck};
function deviceType(d:Device):string{if(d.vendor==='Fortinet')return 'Firewall';if(/switch|sw[-_]/i.test(d.name))return 'Switch';return 'Router'}
function Badge({children,type=''}:{children:React.ReactNode;type?:string}){return <span className={'badge '+type}>{children}</span>}
function Choice({value,onChange,options,label}:{value:string;onChange:(v:string)=>void;options:{value:string;label:string}[];label?:string}){return <Select value={value} onValueChange={onChange}><SelectTrigger aria-label={label} className="choice"><SelectValue/></SelectTrigger><SelectContent>{options.map(o=><SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}</SelectContent></Select>}
function Logo(){return <div className="brand"><span className="brandmark"><ShieldCheck size={22}/></span>configlens<span style={{color:'#608177',fontSize:22}}>.</span></div>}
function DeviceIcon({d}:{d:Device}){const Icon=icons[deviceType(d)]||Router;return <span className="device-icon"><Icon size={19}/></span>}
export default function App(){
 const [devices,setDevices]=useState<Device[]>([]),[view,setView]=useState('Overview'),[selected,setSelected]=useState<Device|null>(null),[tab,setTab]=useState('audit'),[busy,setBusy]=useState(false),[loadError,setLoadError]=useState('');
 const [uploadOpen,setUploadOpen]=useState(false),[files,setFiles]=useState<File[]>([]),[auditOpen,setAuditOpen]=useState(false),[auditTarget,setAuditTarget]=useState<Device|null>(null),[benches,setBenches]=useState<any[]>([]),[chosen,setChosen]=useState<string[]>([]);
 const [search,setSearch]=useState(''),[vendor,setVendor]=useState('all'),[status,setStatus]=useState('all'),[findingIndex,setFindingIndex]=useState(0),[findStatus,setFindStatus]=useState('ALL'),[key,setKey]=useState(''),[value,setValue]=useState('true');
 const [reviewIndex,setReviewIndex]=useState(0),[canonicalKeys,setCanonicalKeys]=useState<{key:string;category:string;description:string}[]>([]),[sourceText,setSourceText]=useState('');
 const inputRef=useRef<HTMLInputElement>(null);
 async function refresh(){try{const ds=await api.listDevices();setDevices(ds);setLoadError('')}catch(e:any){setLoadError(e.message)}}
 useEffect(()=>{refresh();api.listBenchmarks().then(setBenches).catch(e=>toast.error('Could not load benchmarks: '+e.message));api.listCanonicalKeys().then(setCanonicalKeys).catch(()=>{})},[]);
 async function action(fn:()=>Promise<void>){setBusy(true);try{await fn()}catch(e:any){toast.error(e.message)}finally{setBusy(false)}}
 async function openDevice(d:Device,t='audit'){await action(async()=>{const [dev,reviews]=await Promise.all([api.getDevice(d.id),api.getReviewQueue(d.id)]);let audit=null;if(dev.has_audit){try{audit=await api.getAudit(d.id)}catch{}}setSelected({...dev,reviews,audit});setView('Devices');setTab(t);setFindingIndex(0);setKey('');setFindStatus('ALL');setReviewIndex(0);setSourceText('')})}
 function navigate(v:string){setView(v);setSelected(null);setSearch('');setStatus('all');setVendor('all')}
 const unknown=devices.reduce((s,d)=>s+d.unknown_count,0),audited=devices.filter(d=>d.has_audit),sum=audited.reduce((s,d)=>({PASS:s.PASS+(d.summary?.PASS||0),FAIL:s.FAIL+(d.summary?.FAIL||0),UNKNOWN:s.UNKNOWN+(d.summary?.UNKNOWN||0),total:s.total+(d.summary?.total||0)}),{PASS:0,FAIL:0,UNKNOWN:0,total:0}),score=sum.total?Math.round(sum.PASS/sum.total*100):0;
 const currentReview=selected?.reviews?.[reviewIndex];
 const filtered=devices.filter(d=>(d.name+' '+d.vendor).toLowerCase().includes(search.toLowerCase())&&(vendor==='all'||d.vendor===vendor)&&(status==='all'||status==='review'&&d.unknown_count>0||status==='audited'&&d.has_audit||status==='pending'&&!d.has_audit));
 const findings:Finding[]=selected?.audit?.results?.filter((f:Finding)=>findStatus==='ALL'||f.status===findStatus)||[],finding=findings[findingIndex]||findings[0];
 function startAudit(d?:Device){setAuditTarget(d||null);setChosen([benches[0]?.name].filter(Boolean));setAuditOpen(true)}
 async function runAudit(){if(!auditTarget){toast.error('Choose a device to audit');return}await action(async()=>{const result=await api.runAudit(auditTarget.id,chosen);await refresh();setSelected(s=>s&&s.id===auditTarget.id?{...s,audit:result,has_audit:true,summary:result.summary}:s);setView('Devices');setTab('audit');setAuditOpen(false);setFindingIndex(0);setFindStatus('ALL');toast.success('Audit complete. Findings are ready to review.')})}
 async function report(d:Device){await action(async()=>{downloadReport(await api.getReport(d.id));toast.success('Report downloaded')})}
 async function submit(decision:'approve'|'ignore'){if(!selected||!currentReview)return;await action(async()=>{await api.submitReview(selected.id,currentReview.id,{decision,key,value_expr:value,scope_type:'GLOBAL'});const [dev,reviews]=await Promise.all([api.getDevice(selected.id),api.getReviewQueue(selected.id)]);let audit=selected.audit;if(dev.has_audit){try{audit=await api.getAudit(selected.id)}catch{}}setSelected({...dev,reviews,audit});setKey('');setValue('true');setReviewIndex(0);await refresh();toast.success(decision==='approve'?'Mapping approved. Rerun the audit to update findings.':'Command ignored.')})}
 function addFiles(incoming:File[]){setFiles(old=>[...old,...incoming].filter((f,i,a)=>a.findIndex(x=>x.name===f.name&&x.size===f.size)===i))}
 const table=(rows:Device[])=><div className="tablewrap"><Table className="table"><TableHeader><TableRow><TableHead>Device name</TableHead><TableHead>Vendor / OS</TableHead><TableHead>Configuration</TableHead><TableHead>Compliance</TableHead><TableHead><span className="sr-only">Open</span></TableHead></TableRow></TableHeader><TableBody>{rows.map(d=><TableRow key={d.id}><TableCell><button className="device-ident" onClick={()=>openDevice(d)}><DeviceIcon d={d}/><span style={{textAlign:'left'}}><span className="device-name">{d.name}</span><span className="subtext" style={{display:'block'}}>{deviceType(d)}</span></span></button></TableCell><TableCell><span>{d.vendor}</span><div className="subtext">{d.dialect}</div></TableCell><TableCell>{d.unknown_count?<button onClick={()=>openDevice(d,'review')}><Badge type="warn"><AlertTriangle size={11}/>{d.unknown_count} need review</Badge></button>:<Badge><Check size={11}/>Normalized</Badge>}</TableCell><TableCell>{d.has_audit&&d.summary?<div className="scorecell"><strong style={{fontWeight:550}}>{d.summary.score!=null?d.summary.score+'%':'—'}</strong><div className="scorebar"><i style={{width:(d.summary.score||0)+'%',background:(d.summary.score||0)<60?'#d9a34b':'#128574'}}/></div></div>:<span className="muted">Not audited</span>}</TableCell><TableCell><button className="btn quiet" aria-label={'Open '+d.name} onClick={()=>openDevice(d)}><ChevronRight size={17}/></button></TableCell></TableRow>)}</TableBody></Table>{!rows.length&&<div className="empty">No devices match your filters.</div>}</div>;
 return <SidebarProvider style={{'--sidebar-width':'232px'} as React.CSSProperties}><Toaster richColors position="top-right"/><Sidebar><SidebarContent><div className="side-inner"><Logo/><span className="side-label">WORKSPACE</span>{[{name:'Overview',icon:LayoutDashboard},{name:'Devices',icon:Router},{name:'Audits & reports',icon:ClipboardCheck},{name:'Mapping review',icon:GitBranch,count:unknown},{name:'Benchmarks',icon:BookOpen}].map(n=><button key={n.name} className={'navitem '+(view===n.name&&!selected?'active':'')} onClick={()=>navigate(n.name)}><n.icon size={18}/>{n.name}{!!n.count&&<span className="count">{n.count}</span>}</button>)}<div className="side-bottom"><div className="workspace"><strong>Hexa forge</strong><span style={{color:'#7e91a7',fontSize:12}}>Network compliance auditor</span></div></div></div></SidebarContent></Sidebar><div className="shell"><header className="topbar"><div className="crumb"><SidebarTrigger className="md:hidden"/><span>Workspace</span><ChevronRight size={12}/><strong>{selected?selected.name:view}</strong></div><div className="top-actions"><span className="tag">Hexa forge</span></div></header><main className="main">{loadError?<div className="banner" role="alert">{loadError}<button className="textlink" onClick={refresh}>Retry</button></div>:null}
 {!selected&&<div className="pagehead"><div><h1>{view==='Overview'?'Network overview':view==='Audits & reports'?'Audits & reports':view==='Mapping review'?'Command review':view==='Benchmarks'?'Benchmark library':'Device inventory'}</h1><p>{view==='Overview'?'A clear picture of your network\'s configuration health.':view==='Devices'?'Manage configurations across vendors, platforms and locations.':view==='Mapping review'?'Turn unfamiliar commands into trusted, normalized observations.':view==='Benchmarks'?'Choose the controls that define your security baseline.':'Evidence-backed findings and downloadable audit reports.'}</p></div><div className="actions">{view==='Overview'&&<button className="btn" onClick={()=>startAudit()}><ClipboardCheck size={16}/>Run audit</button>}<button className="btn primary" onClick={()=>setUploadOpen(true)}><Plus size={17}/>Add devices</button></div></div>}
 {view==='Overview'&&!selected&&<><div className="stats">{[{label:'Total devices',value:devices.length,icon:Router,foot:<><em>{new Set(devices.map(d=>d.vendor)).size} vendors</em> across your network</>},{label:'Compliance score',value:score+'%',icon:ShieldCheck,foot:<>Across {sum.total} evaluated controls</>},{label:'Failed controls',value:sum.FAIL,icon:AlertTriangle,foot:<span className="danger">Review remediation guidance</span>},{label:'Pending mappings',value:unknown,icon:GitBranch,foot:<>Across {devices.filter(d=>d.unknown_count).length} devices</>}].map(s=><div className="stat" key={s.label}><div className="stat-label">{s.label}<s.icon size={16} color="#8897a6"/></div><div className="stat-value">{s.value}</div><div className="stat-foot">{s.foot}</div></div>)}</div><div className="overview-grid"><section className="panel"><div className="panelhead"><div><h2>Compliance posture</h2><p>Latest audit results across your fleet</p></div><Badge type="neutral">{audited.length} devices audited</Badge></div><div className="compliance-body"><div className="donut" style={{background:`conic-gradient(#128574 0 ${score}%,#e79b64 ${score}% ${sum.total?(sum.PASS+sum.FAIL)/sum.total*100:100}%,#dce3e9 0 100%)`}}><div className="donut-inner"><b>{score}<span style={{fontSize:17}}>%</span></b><span>CONTROLS PASSED</span></div></div><div className="legend">{[['Passed',sum.PASS,'#128574'],['Failed',sum.FAIL,'#e79b64'],['Needs review',sum.UNKNOWN,'#dce3e9']].map(([l,n,c])=><div key={String(l)} className="legend-row"><span><i className="dot" style={{background:String(c)}}/>{l}</span><strong>{n}</strong></div>)}<div style={{borderTop:'1px solid #edf0f3',paddingTop:12,fontSize:11,color:'#94a0ad'}}>Pass / all evaluated controls, including unknowns</div></div></div></section><section className="panel"><div className="panelhead"><h2>Needs your attention</h2><span className="badge warn">{unknown} pending</span></div><div className="attention"><GitBranch size={19}/><div><strong>{unknown?'Unknown commands need a human eye':'Your mapping queue is clear'}</strong><p>{unknown?'Review and map commands before relying on an audit.':'New configurations will appear here when review is needed.'}</p></div></div>{devices.filter(d=>d.unknown_count).slice(0,2).map(d=><button className="review-line" style={{width:'100%'}} key={d.id} onClick={()=>openDevice(d,'review')}><span>{d.name}</span><span style={{display:'flex',alignItems:'center',gap:10,color:'#aa8546'}}>{d.unknown_count} commands<ChevronRight size={14}/></span></button>)}<div style={{padding:'13px 23px'}}><button className="textlink" onClick={()=>navigate('Mapping review')}>Open mapping review</button></div></section></div><section className="panel"><div className="panelhead"><div style={{display:'flex',gap:10,alignItems:'center'}}><h2>Device inventory</h2><Badge type="neutral">{devices.length}</Badge></div><button className="textlink" onClick={()=>navigate('Devices')}>View all devices <ChevronRight size={14}/></button></div>{table(devices.slice(0,5))}<div className="tablefooter"><span>Showing {Math.min(devices.length,5)} of {devices.length} devices</span></div></section></>}
 {view==='Devices'&&!selected&&<section className="panel"><div className="panelhead"><h2>All devices <span className="muted" style={{fontWeight:400}}>({devices.length})</span></h2><span className="muted" style={{fontSize:12}}>Single and bulk imports supported</span></div><div className="filters"><div className="search"><Search size={16}/><input className="input" aria-label="Search devices" placeholder="Search devices…" value={search} onChange={e=>setSearch(e.target.value)}/></div><Choice label="Filter vendor" value={vendor} onChange={setVendor} options={[{value:'all',label:'All vendors'},...Array.from(new Set(devices.map(d=>d.vendor))).map(v=>({value:v,label:v}))]}/><Choice label="Filter status" value={status} onChange={setStatus} options={[{value:'all',label:'All statuses'},{value:'review',label:'Needs review'},{value:'audited',label:'Audited'},{value:'pending',label:'Not audited'}]}/></div>{table(filtered)}<div className="tablefooter"><span>{filtered.length} devices</span></div></section>}
 {view==='Mapping review'&&!selected&&<section className="panel"><div className="panelhead"><h2>Pending review</h2><Badge type="warn">{unknown} templates</Badge></div>{devices.filter(d=>d.unknown_count).map(d=><div className="review-line" style={{padding:23}} key={d.id}><div className="device-ident"><DeviceIcon d={d}/><div><strong>{d.name}</strong><div className="subtext">{d.vendor} · {d.unknown_count} unmapped templates</div></div></div><button className="btn" onClick={()=>openDevice(d,'review')}>Review commands<ChevronRight size={14}/></button></div>)}{!unknown&&<div className="empty"><CheckCircle2 style={{margin:'0 auto 14px'}}/>All commands have been reviewed.</div>}</section>}
 {view==='Audits & reports'&&!selected&&<section className="panel"><div className="panelhead"><h2>Latest device audits</h2><span className="muted" style={{fontSize:12}}>One current snapshot per device</span></div>{audited.map(d=><div className="review-line" style={{padding:22}} key={d.id}><div className="device-ident"><DeviceIcon d={d}/><div><button className="device-name" onClick={()=>openDevice(d)}>{d.name}</button><div className="subtext">{d.vendor} · {d.summary?.score!=null?d.summary.score+'% score':'—'}</div></div></div><div className="actions"><button className="btn small" onClick={()=>report(d)} disabled={busy}><Download size={14}/>Report</button></div></div>)}{!audited.length&&<div className="empty">Run your first device audit to create a report.</div>}</section>}
 {view==='Benchmarks'&&!selected&&<><div className="banner"><BookOpen size={18}/><span>Benchmark controls are loaded from the registry. Run an audit on any device to apply them.</span></div><div className="registry-grid">{benches.map(b=><section className="panel bench-card" key={b.name}><Badge type="neutral">{b.rule_count} rules</Badge><h2>{b.name}</h2><div className="meta"><span>{b.rule_count} controls</span></div></section>)}{!benches.length&&<div className="empty">No benchmarks loaded. Seed the registry first.</div>}</div></>}
 {selected&&<><button className="textlink" style={{marginBottom:22,color:'#718194'}} onClick={()=>setSelected(null)}><ChevronLeft size={14}/>Device inventory</button><div className="pagehead"><div><div className="devicehead"><DeviceIcon d={selected}/><div><h1>{selected.name}</h1><div className="detail-meta"><span>{selected.vendor} / {selected.dialect}</span><span>{deviceType(selected)}</span><span className="muted">{selected.status}</span></div></div></div></div><div className="actions">{selected.has_audit&&<button className="btn" onClick={()=>report(selected)} disabled={busy}><Download size={16}/>Report</button>}<button className="btn primary" onClick={()=>startAudit(selected)}><ClipboardCheck size={16}/>Run audit</button></div></div>{selected.unknown_count>0&&<div className="banner"><AlertTriangle size={17}/><span><strong>{selected.unknown_count} unknown command templates.</strong> Review mappings to improve audit coverage.</span><button className="textlink" onClick={()=>setTab('review')}>Review commands<ChevronRight size={14}/></button></div>}<Tabs value={tab} onValueChange={setTab} className="tabs"><TabsList><TabsTrigger value="audit">Audit findings</TabsTrigger><TabsTrigger value="review">Mapping review {selected.unknown_count>0&&<Badge type="warn">{selected.unknown_count}</Badge>}</TabsTrigger><TabsTrigger value="normalized">Normalized config</TabsTrigger><TabsTrigger value="source">Source config</TabsTrigger></TabsList><TabsContent value="audit">{selected.audit?<><div className="stats">{[['Compliance',selected.audit.summary?.score!=null?selected.audit.summary.score+'%':'—'],['Passed',selected.audit.summary?.PASS??0],['Failed',selected.audit.summary?.FAIL??0],['Unknown',selected.audit.summary?.UNKNOWN??0]].map(([l,n])=><div className="stat" key={String(l)}><div className="stat-label">{l}</div><div className="stat-value">{n}</div></div>)}</div><div className="audit-grid"><section className="panel"><div className="panelhead"><h2>Controls</h2><Choice label="Finding status" value={findStatus} onChange={v=>{setFindStatus(v);setFindingIndex(0)}} options={['ALL','FAIL','PASS','UNKNOWN'].map(v=>({value:v,label:v==='ALL'?'All results':v}))}/></div><div className="findings">{findings.map((f,i)=><button className={'finding '+(i===findingIndex?'selected':'')} key={f.rule} onClick={()=>setFindingIndex(i)}><div className="finding-top"><span className="mono">{f.rule}</span><Badge type={f.status==='FAIL'?'fail':f.status==='UNKNOWN'?'warn':''}>{f.status}</Badge></div><strong>{f.title}</strong><p>{f.severity} severity · {f.scope||'global'} scope</p></button>)}{!findings.length&&<div className="empty">No matching findings</div>}</div></section>{finding&&<section className="panel evidence-content"><div className="actions"><Badge type={finding.status==='FAIL'?'fail':finding.status==='UNKNOWN'?'warn':''}>{finding.status}</Badge><span className="muted" style={{fontSize:12}}>{finding.severity} severity</span></div><h2 className="evidence-title">{finding.title}</h2><div className="compare"><div className="codecard bad"><div className="codehead"><FileText size={14}/>Observed value</div><pre>{JSON.stringify(finding.actual,null,2)??'No evidence'}</pre></div><div className="codecard good"><div className="codehead"><CheckCircle2 size={14}/>{finding.status==='PASS'?'Expected (satisfied)':'Expected value'}</div><pre>{JSON.stringify(finding.expected,null,2)}</pre></div></div>{finding.status==='FAIL'&&finding.fix&&<div style={{marginTop:20,borderTop:'1px solid #e9edf1',paddingTop:20}}><div style={{display:'flex',alignItems:'center',gap:8,marginBottom:12,fontSize:13,fontWeight:600,color:'#1a2332'}}><AlertTriangle size={14} color="#e79b64"/>Suggested fix</div><div className="codecard" style={{background:'#f0f9f6',borderColor:'#b8e0d8'}}><div className="codehead" style={{color:'#128574'}}><CheckCircle2 size={14}/>Remediation command</div><pre style={{whiteSpace:'pre-wrap',wordBreak:'break-all'}}>{finding.fix}</pre></div></div>}<div style={{display:'grid',gap:14,fontSize:13,borderTop:'1px solid #e9edf1',paddingTop:20,marginTop:20}}><div className="muted">Canonical key <code style={{display:'block',color:'#304f58',marginTop:8,overflowWrap:'anywhere'}}>{finding.key||'—'}</code></div><div className="actions"><Badge type="neutral">Actual: {JSON.stringify(finding.actual)}</Badge><Badge>Expected: {JSON.stringify(finding.expected)}</Badge></div><p>Findings are based on normalized observations mapped to the canonical registry.</p></div></section>}</div></>:<div className="panel empty"><ClipboardCheck size={30} style={{margin:'0 auto 15px'}}/><h2>No audit yet</h2><p style={{margin:'10px 0 20px'}}>Select a benchmark to evaluate this configuration.</p><button className="btn primary" onClick={()=>startAudit(selected)}>Run first audit</button></div>}</TabsContent>
 <TabsContent value="review">
  {currentReview ? (
    <div className="mapping-review">

      {/* Template navigation */}
      {selected.reviews && selected.reviews.length > 1 && (
        <div
          className="review-navigation"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 20,
            padding: '0 0 4px',
          }}
        >
          <button
            className="btn quiet"
            disabled={reviewIndex === 0}
            onClick={() => setReviewIndex(i => i - 1)}
            aria-label="Previous template"
          >
            <ChevronLeft size={14} />
          </button>

          <span
            className="subtext"
            style={{
              flex: 1,
              textAlign: 'center',
            }}
          >
            {reviewIndex + 1} / {selected.reviews.length} template
          </span>

          <button
            className="btn quiet"
            disabled={reviewIndex >= selected.reviews.length - 1}
            onClick={() => setReviewIndex(i => i + 1)}
            aria-label="Next template"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}

      <div className="mapping">
      {/* 1st section: Understand */}
      <section className="panel review-card">
        <div className="actions">
          <Badge type="warn">Unmapped template</Badge>
          <span className="muted" style={{ fontSize: 12 }}>
            {currentReview.frequency} occurrence(s)
          </span>
        </div>

        <h2 style={{ marginTop: 20 }}>
          Understand the command
        </h2>

        <pre>{currentReview.display}</pre>

        <h3>Source evidence</h3>

        {currentReview.examples.map((e, i) => (
          <div
            key={i}
            style={{
              fontSize: 12,
              lineHeight: 1.8,
              marginTop: 15,
            }}
          >
            <span className="muted">
              Line {e.line}
            </span>

            <code
              style={{
                display: 'block',
                overflowWrap: 'anywhere',
              }}
            >
              {e.raw}
            </code>
          </div>
        ))}

        {currentReview.vars.length > 0 && (
          <div style={{ marginTop: 20 }}>
            <h3>Extracted template variables</h3>

            {currentReview.vars.map(v => (
              <div
                className="subtext"
                key={v.name}
              >
                {v.name} · {v.type}
              </div>
            ))}
          </div>
        )}

        {currentReview.suggestion && (
          <div style={{marginTop:20,border:'1px solid #f0dcc0',borderRadius:8,overflow:'hidden'}}>
            <div style={{background:'#fff8f0',borderBottom:'1px solid #f0dcc0',padding:'10px 16px',display:'flex',alignItems:'center',gap:8}}>
              <AlertTriangle size={13} color="#b97a3a"/>
              <span style={{fontSize:12,fontWeight:600,color:'#7a4f20'}}>ML-predicted — accuracy not guaranteed</span>
              <span style={{marginLeft:'auto',fontSize:11,color:'#94a0ad',whiteSpace:'nowrap'}}>
                {currentReview.suggestion.model_version&&`Model: ${currentReview.suggestion.model_version} · `}
                {Math.round(currentReview.suggestion.score*100)}% confidence
              </span>
            </div>
            <div style={{background:'#fffcf8',padding:'14px 16px'}}>
              <div style={{fontSize:11,color:'#8897a6',marginBottom:4}}>Suggested canonical key</div>
              <code style={{display:'block',fontSize:13,fontWeight:600,color:'#1a2332',marginBottom:10,overflowWrap:'anywhere'}}>{currentReview.suggestion.key}</code>
              {currentReview.suggestion.value_expr&&<>
                <div style={{fontSize:11,color:'#8897a6',marginBottom:4}}>Value expression</div>
                <code style={{display:'block',fontSize:12,color:'#304f58',marginBottom:12}}>{currentReview.suggestion.value_expr}</code>
              </>}
              <button className="textlink" onClick={()=>{setKey(currentReview.suggestion!.key);setValue(currentReview.suggestion!.value_expr||'true')}}>
                Use suggestion →
              </button>
            </div>
          </div>
        )}
      </section>

      {/* 2nd section: Map */}
      <section className="panel review-card">
        <h2>Map to the canonical model</h2>

        <p
          className="subtext"
          style={{
            margin: '9px 0 24px',
            lineHeight: 1.8,
          }}
        >
          Choose the meaning of this command. Your decision is saved
          to the registry.
        </p>

        <div className="field">
          <label>Canonical key</label>

          <Select
            value={key}
            onValueChange={setKey}
          >
            <SelectTrigger className="choice">
              <SelectValue placeholder="Select canonical key…" />
            </SelectTrigger>

            <SelectContent
              className="key-dropdown"
              style={{ maxHeight: 280 }}
            >
              {canonicalKeys.map(k => (
                <SelectItem
                  key={k.key}
                  value={k.key}
                >
                  <span
                    style={{
                      fontFamily: 'monospace',
                      fontSize: 12,
                    }}
                  >
                    {k.key}
                  </span>

                  {k.description && (
                    <span
                      style={{
                        color: '#8897a6',
                        marginLeft: 8,
                        fontSize: 11,
                      }}
                    >
                      {k.description}
                    </span>
                  )}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="field">
          <label htmlFor="mappedvalue">
            Normalized value
          </label>

          <input
            id="mappedvalue"
            className="input mono"
            value={value}
            onChange={e => setValue(e.target.value)}
          />

          <div className="subtext">
            Use true, false, or a number (JSON literal)
          </div>
        </div>

        <div className="field">
          <label>Scope</label>

          <Choice
            label="Mapping scope"
            value="global"
            onChange={() => {}}
            options={[
              {
                value: 'global',
                label: 'Global configuration',
              },
            ]}
          />
        </div>

        <div
          className="actions"
          style={{
            marginTop: 28,
            flexWrap: 'wrap',
          }}
        >
          <button
            className="btn primary"
            disabled={busy || !key}
            onClick={() => submit('approve')}
          >
            <Check size={16} />
            Approve mapping
          </button>

          <button
            className="btn"
            disabled={busy}
            onClick={() => submit('ignore')}
          >
            Ignore command
          </button>
        </div>

        <p
          className="subtext"
          style={{
            marginTop: 20,
            lineHeight: 1.7,
          }}
        >
          Ignoring a command excludes it from normalization and keeps
          unresolved checks marked Unknown.
        </p>
      </section>
      </div>
    </div>
  ) : (
    <div className="panel empty">
      <CheckCircle2
        size={32}
        style={{
          margin: '0 auto 15px',
          color: '#128574',
        }}
      />

      <h2>Review queue complete</h2>

      <p style={{ margin: '10px 0 20px' }}>
        Run an audit to evaluate the updated canonical model.
      </p>

      <button
        className="btn primary"
        onClick={() => startAudit(selected)}
      >
        Run audit
      </button>
    </div>
  )}
</TabsContent>
<TabsContent value="normalized"><section className="panel"><div className="panelhead"><h2>Canonical observations</h2><Badge type="neutral">{(selected.observations||[]).length} entries</Badge></div><Table className="table"><TableHeader><TableRow><TableHead>Canonical key</TableHead><TableHead>Value</TableHead><TableHead>Source line</TableHead><TableHead>Mapped by</TableHead></TableRow></TableHeader><TableBody>{(selected.observations||[]).map((o,i)=><TableRow key={i}><TableCell className="mono">{o.key}</TableCell><TableCell><Badge>{JSON.stringify(o.value)}</Badge></TableCell><TableCell>{o.src_line??'—'}</TableCell><TableCell>{o.source||'—'}</TableCell></TableRow>)}</TableBody></Table>{!(selected.observations||[]).length&&<div className="empty">No known observations. Review unknown commands to add mappings.</div>}</section></TabsContent><TabsContent value="source"><section className="panel"><div className="panelhead"><h2>Raw configuration</h2><span className="muted" style={{fontSize:12}}>Original uploaded file</span></div>{sourceText?<pre style={{fontFamily:'monospace',fontSize:12,lineHeight:1.6,padding:'0 20px 20px',overflowX:'auto',whiteSpace:'pre',color:'#1a2332',maxHeight:600,overflow:'auto'}}>{sourceText}</pre>:<div style={{padding:'40px 20px',textAlign:'center'}}><p className="muted" style={{marginBottom:16}}>Click to load the source configuration file</p><button className="btn" onClick={()=>api.getSource(selected.id).then(r=>setSourceText(r.text)).catch(e=>toast.error('Could not load source: '+String(e)))}>Load source config</button></div>}</section></TabsContent></Tabs></>}
 <footer className="footer"><span>ConfigLens <span style={{margin:'0 7px'}}>/</span> Multi-vendor network compliance</span><span>Hexa forge</span></footer></main></div>
 <Dialog open={uploadOpen} onOpenChange={v=>!busy&&setUploadOpen(v)}><DialogContent className="modal"><DialogTitle>Add network devices</DialogTitle><DialogDescription>Upload configuration files. Each file becomes one device in your inventory.</DialogDescription><div className="dropzone" role="button" tabIndex={0} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();inputRef.current?.click()}}} onClick={()=>inputRef.current?.click()} onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();addFiles(Array.from(e.dataTransfer.files))}}><Upload size={29}/><strong>Drop configurations here, or browse files</strong><p>.conf, .cfg, .txt, .json · 1 MB per file · 20 files per batch</p><input ref={inputRef} type="file" multiple accept=".conf,.cfg,.txt,.json,.config,.log,.set" style={{display:'none'}} onChange={e=>addFiles(Array.from(e.target.files||[]))}/></div><div style={{maxHeight:180,overflow:'auto'}}>{files.map((f,i)=><div className="file-row" key={f.name}><FileText size={16}/><span style={{flex:1,overflowWrap:'anywhere'}}>{f.name}</span><span className="muted">{Math.ceil(f.size/1024)} KB</span><button aria-label={'Remove '+f.name} className="btn quiet" onClick={()=>setFiles(files.filter((_,j)=>i!==j))}><X size={15}/></button></div>)}</div><p style={{fontSize:12}}>Files are parsed, flattened and mapped to canonical keys. Unrecognized syntax is sent to human review.</p><div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setUploadOpen(false)}>Cancel</button><button className="btn primary" disabled={busy||!files.length} onClick={()=>action(async()=>{const ds=await api.uploadDevices(files);await refresh();setUploadOpen(false);setFiles([]);navigate('Devices');toast.success(`${ds.length} device${ds.length===1?'':'s'} added to inventory`)})}>{busy?<Loader2 className="spin" size={16}/>:<Upload size={16}/>}Import {files.length||''} configuration{files.length!==1?'s':''}</button></div></DialogContent></Dialog>
 <Dialog open={auditOpen} onOpenChange={v=>!busy&&setAuditOpen(v)}><DialogContent className="modal"><DialogTitle>Run a configuration audit</DialogTitle><DialogDescription>Choose a device and the benchmarks to evaluate.</DialogDescription><div style={{marginTop:18}}><label style={{fontSize:13,display:'block',marginBottom:9}}>Device</label><Choice label="Device to audit" value={auditTarget?.id||''} onChange={id=>setAuditTarget(devices.find(d=>d.id===id)||null)} options={devices.map(d=>({value:d.id,label:d.name}))}/></div><div style={{marginTop:10}}>{benches.map(b=><label className="benchmark-option" key={b.name}><Checkbox checked={chosen.includes(b.name)} onCheckedChange={checked=>setChosen(checked?[...chosen,b.name]:chosen.filter(x=>x!==b.name))}/><div><strong>{b.name}</strong><p>{b.rule_count} controls</p></div></label>)}</div>{!!auditTarget?.unknown_count&&<div className="banner" style={{margin:0}}><AlertTriangle size={17}/>Unmapped commands may produce Unknown results.</div>}<div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setAuditOpen(false)}>Cancel</button><button className="btn primary" disabled={busy||!chosen.length||!auditTarget} onClick={runAudit}>{busy?<Loader2 className="spin" size={16}/>:<ClipboardCheck size={16}/>}Run audit</button></div></DialogContent></Dialog>
 </SidebarProvider>
}
