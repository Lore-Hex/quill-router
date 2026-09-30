(() => {
"use strict";
const DATA=JSON.parse(document.querySelector("#homepage-data").textContent);
const MIGSRC=DATA.migration.from_sources;
const TRB=DATA.migration.base_url,TRK=DATA.migration.api_key_env;
const escH=t=>t.replace(/&/g,"&amp;").replace(/</g,"&lt;");
function migCode(lang,src){const o=MIGSRC[src];const D=t=>`<span class="del">- ${escH(t)}</span>`,A=t=>`<span class="add">+ ${escH(t)}</span>`,L=t=>`  ${escH(t)}\n`,C=t=>`<span class="cm">  ${escH(t)}</span>\n`;
 if(lang==="py")return L("from openai import OpenAI")+L("import os")+"\n"+L("client = OpenAI(")+D(`    base_url="${o.b}",`)+D(`    api_key=os.environ["${o.k}"],`)+A(`    base_url="${TRB}",`)+A(`    api_key=os.environ["${TRK}"],`)+L(")")+"\n"+L("response = client.chat.completions.create(")+L('    model="z-ai/glm-5.3-flash",')+L('    messages=[{"role": "user", "content": "Hello"}],')+C('    # optional: extra_body={"provider": {"min_privacy": "confidential"}}')+L(")");
 if(lang==="ts")return L('import OpenAI from "openai";')+"\n"+L("const client = new OpenAI({")+D(`  baseURL: "${o.b}",`)+D(`  apiKey: process.env.${o.k},`)+A(`  baseURL: "${TRB}",`)+A(`  apiKey: process.env.${TRK},`)+L("});")+"\n"+L("const res = await client.chat.completions.create({")+L('  model: "z-ai/glm-5.3-flash",')+L('  messages: [{ role: "user", content: "Hello" }],')+C('  // optional: provider: { min_privacy: "zdr" }')+L("});");
 return D(`curl ${o.b}/chat/completions \\`)+D(`  -H "Authorization: Bearer $${o.k}" \\`)+A(`curl ${TRB}/chat/completions \\`)+A(`  -H "Authorization: Bearer $${TRK}" \\`)+L('  -H "Content-Type: application/json" \\')+L(`  -d '{"model": "z-ai/glm-5.3-flash",`)+L(`       "messages": [{"role": "user", "content": "Hello"}]}'`);}
const AGENT=DATA.migration.agent_prompt;

const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
// One HTML content tree: collapse supporting examples on phones, expose them on desktop.
// Without JavaScript these native disclosures start open and still work.
const compactContent=matchMedia('(max-width:600px)');
const mobileDisclosures=$$('[data-mobile-disclosure]');
const mobileOpen=new Map();
function syncDisclosures(){
 mobileDisclosures.forEach(detail=>{
  const summary=$('summary',detail);
  const focused=document.activeElement;
  detail.open=compactContent.matches?(mobileOpen.get(detail.id)??false):true;
  summary.tabIndex=compactContent.matches?0:-1;
  if(compactContent.matches&&!detail.open&&detail.contains(focused))summary.focus({preventScroll:true});
  if(!compactContent.matches&&focused===summary&&!detail.classList.contains('footer-disclosure')){
   const content=$('.disclosure-body',detail);content.tabIndex=-1;content.focus({preventScroll:true});
  }
 });
 document.documentElement.classList.add('disclosures-ready');
}
mobileDisclosures.forEach(detail=>detail.addEventListener('toggle',()=>{
 if(compactContent.matches)mobileOpen.set(detail.id,detail.open);
}));
compactContent.addEventListener('change',syncDisclosures);
syncDisclosures();
// Homepage-specific events remain local hooks pending the analytics contract.
// Existing dashboard.js retains the application acquisition/sign-in events.
function track(name, properties={}) {
 document.dispatchEvent(new CustomEvent('trustedrouter:analytics', {detail:{name, properties}}));
}
let feedbackTimer;
function feedback(message) {
 const el=$('.copy-feedback'); el.textContent=message;
 clearTimeout(feedbackTimer); feedbackTimer=setTimeout(()=>el.textContent='',2400);
}
async function copyText(text, button) {
 const original=button.textContent;
 try {
  await navigator.clipboard.writeText(text);
  button.textContent='Copied'; feedback('Copied to clipboard');
  setTimeout(()=>button.textContent=original,1600); return true;
 } catch {
  feedback('Clipboard unavailable. Select the text and copy it manually.'); return false;
 }
}
$$('.trc .fl button').forEach(button=>button.addEventListener('click',()=>{
 $$('.trc .fl button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
 $$('.trc tbody').forEach(t=>t.hidden=t.dataset.f!==button.dataset.f);
 const count=$$('.trc tbody:not([hidden]) tr').length;
 $('.trc .cnt').textContent=button.dataset.f==='all'?`Featured · ${count} of ${DATA.catalog_total}`:`${button.textContent.trim()} · ${count} shown`;
 track('home.catalog_filter',{filter:button.textContent.toLowerCase()});
}));
$$('[data-alias]').forEach(button=>button.addEventListener('click',async()=>{
 if(await copyText(button.dataset.alias,button)){
  feedback('Copied '+button.dataset.alias);
  track('home.alias_copied',{alias:button.dataset.alias});
 }
}));
let lang='py', source='openai';
const migration=$('.trm'), pre=$('.mcode');
function renderCode(){
 $$('.tab',migration).forEach(tab=>{const selected=tab.dataset.lang===lang;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;});
 $$('.from button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.src===source)));
 $('.from').hidden=lang==='agent';
 pre.setAttribute('aria-labelledby','code-tab-'+lang);
 if(lang==='agent'){pre.className='mcode prompt';pre.textContent=AGENT;}
 else{pre.className='mcode';pre.innerHTML=migCode(lang,source);}
 $('.pf .hint').textContent=lang==='agent'?DATA.migration.agent_hint:DATA.migration.code_hint;
}
function setLang(value){lang=value;renderCode();track('home.migration_tab',{tab:value,from:source});}
$$('.tab').forEach((tab,index,tabs)=>{
 tab.addEventListener('click',()=>setLang(tab.dataset.lang));
 tab.addEventListener('keydown',e=>{
  let next;
  if(e.key==='ArrowRight')next=(index+1)%tabs.length;
  if(e.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;
  if(e.key==='Home')next=0;if(e.key==='End')next=tabs.length-1;
  if(next!==undefined){e.preventDefault();setLang(tabs[next].dataset.lang);tabs[next].focus();}
 });
});
$$('.from button').forEach(button=>button.addEventListener('click',()=>{source=button.dataset.src;renderCode();track('home.migration_tab',{tab:lang,from:source});}));
$('[data-agent]').addEventListener('click',()=>{$('#migration-example').open=true;setLang('agent');$('#code-tab-agent').focus();track('home.agent_prompt_opened',{source:'link'});});
$('[data-copy]').addEventListener('click',async e=>{if(await copyText(TRB,e.currentTarget))track('home.base_url_copied');});
$('[data-copycode]').addEventListener('click',async e=>{
 const clean=pre.cloneNode(true);$$('.del',clean).forEach(n=>n.remove());$$('.add',clean).forEach(n=>n.append('\n'));
 const text=lang==='agent'?AGENT:clean.textContent.split('\n').map(line=>line.replace(/^[+ ] /,'')).join('\n');
 if(await copyText(text,e.currentTarget))track(lang==='agent'?'home.agent_prompt_copied':'home.code_copied',{tab:lang,from:source});
});
renderCode();
const menu=$('.menu'), nav=$('#homepage-nav');
function closeMenu(){nav.classList.remove('open');menu.setAttribute('aria-expanded','false');}
menu.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')!=='true';nav.classList.toggle('open',open);menu.setAttribute('aria-expanded',String(open));});
nav.addEventListener('click',e=>{if(e.target.closest('a'))closeMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&menu.getAttribute('aria-expanded')==='true'){closeMenu();menu.focus();}});
const search=$('#model-search'), query=$('#model-query');
let searchTrigger;
let searchModels=null, searchLoading=false, searchFailed=false;
function renderSearch(){
 const results=$('#search-results');results.replaceChildren();
 $('#search-retry').hidden=!searchFailed;
 if(searchLoading){$('#search-count').textContent='Loading the model catalog…';return;}
 if(searchFailed){$('#search-count').textContent='The catalog could not be loaded. Try again or browse all models.';return;}
 const needle=query.value.trim().toLowerCase();
 const models=(searchModels||[]).filter(m=>(m.name+' '+m.id).toLowerCase().includes(needle));
 $('#search-count').textContent=models.length?`${models.length} ${models.length===1?'match':'matches'}${models.length>50?' · showing the first 50':''}`:'No matching models. Try another name or browse all models.';
 for(const model of models.slice(0,50)){
  const a=document.createElement('a');a.href='/models/'+model.id.split('/').map(encodeURIComponent).join('/');
  const icon=document.createElement('img');icon.className='model-lab-icon';icon.src=DATA.publisher_icons[model.id.split('/')[0]]||'/static/homepage/mark.svg';icon.alt='';icon.width=30;icon.height=30;
  const label=document.createElement('span');label.className='model-label';label.textContent=model.name;
  const id=document.createElement('small');id.textContent=model.id;label.append(id);a.append(icon,label);results.append(a);
 }
}
async function loadSearch(){
 if(searchLoading||searchModels)return;
 searchLoading=true;searchFailed=false;renderSearch();
 try{
  const response=await fetch('/v1/models/picker',{headers:{Accept:'application/json'},signal:AbortSignal.timeout(10000)});
  if(!response.ok)throw new Error('Catalog unavailable');
  const payload=await response.json();if(!Array.isArray(payload.data))throw new Error('Invalid catalog');
  searchModels=payload.data.filter(m=>typeof m.id==='string'&&typeof m.name==='string'&&!m.trustedrouter?.internal_only);
 }catch{searchFailed=true;}finally{searchLoading=false;renderSearch();}
}
function openSearch(trigger){searchTrigger=trigger;closeMenu();search.showModal();query.value='';renderSearch();query.focus();void loadSearch();}
$('#search-retry').addEventListener('click',()=>void loadSearch());
$$('[data-open-search]').forEach(button=>button.addEventListener('click',()=>openSearch(button)));
$('[data-close-search]').addEventListener('click',()=>search.close());
search.addEventListener('close',()=>{const target=searchTrigger?.getClientRects().length?searchTrigger:menu;target?.focus();});
search.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();search.close();}});
search.addEventListener('click',e=>{if(e.target===search){const box=search.getBoundingClientRect();if(e.clientX<box.left||e.clientX>box.right||e.clientY<box.top||e.clientY>box.bottom)search.close();}});
query.addEventListener('input',renderSearch);
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openSearch(document.activeElement);}});
const tooltip=document.createElement('div');tooltip.className='page-tooltip';tooltip.id='page-tooltip';tooltip.role='tooltip';tooltip.hidden=true;document.body.append(tooltip);
let tipOwner;
function hideTip(){tooltip.hidden=true;tipOwner?.removeAttribute('aria-describedby');tipOwner=null;}
function showTip(element){tipOwner=element;tooltip.textContent=element.dataset.tip;tooltip.hidden=false;element.setAttribute('aria-describedby',tooltip.id);const r=element.getBoundingClientRect(),t=tooltip.getBoundingClientRect();tooltip.style.left=Math.max(12,Math.min(r.left,innerWidth-t.width-12))+'px';tooltip.style.top=(r.top>t.height+16?r.top-t.height-8:r.bottom+8)+'px';}
$$('[data-tip]').forEach(element=>{
 element.addEventListener('pointerenter',()=>showTip(element));element.addEventListener('pointerleave',()=>{if(document.activeElement!==element)hideTip();});element.addEventListener('focus',()=>showTip(element));element.addEventListener('blur',hideTip);element.addEventListener('click',()=>showTip(element));
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')hideTip();});
document.addEventListener('pointerdown',e=>{if(tipOwner&&!tipOwner.contains(e.target))hideTip();});
addEventListener('scroll',hideTip,{passive:true});addEventListener('resize',hideTip);
$$('#faq details').forEach((el,index)=>el.addEventListener('toggle',()=>{if(el.open)track('home.faq_opened',{question_index:index});}));
$$('.trc .m a').forEach(a=>a.addEventListener('click',()=>track('home.catalog_row_clicked',{model_id:a.href.split('/models/')[1]})));
$$('.btnp,.agent,.signin').forEach(a=>a.addEventListener('click',()=>track('home.cta_clicked',{module:a.closest('footer')?'footer':a.closest('#migrate')?'migration':'hero',cta:'api_key'})));

})();
