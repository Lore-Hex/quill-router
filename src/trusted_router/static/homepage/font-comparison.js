(() => {
 const options={archivo:['Archivo','Archivo'],jakarta:['Plus Jakarta Sans','Archivo'],plex:['IBM Plex Sans','Archivo'],manrope:['Manrope','Manrope']};
 const select=document.querySelector('#font-choice'); if(!select)return;
 const status=document.querySelector('#font-description');
 const share=document.querySelector('#font-share');
 function apply(value,updateURL=false){
  const key=Object.hasOwn(options,value)?value:'archivo';
  const x=scrollX,y=scrollY;
  document.documentElement.dataset.font=key;
  select.value=key;
  status.textContent=`Headings: ${options[key][0]} · Body / navigation: ${options[key][1]}`;
  const url=new URL(location.href);url.searchParams.set('font',key);
  if(updateURL)history.replaceState(null,'',url);
  share.href=url.href;
  document.querySelectorAll('a[href]').forEach(a=>{const u=new URL(a.href,location.href);if(u.origin===location.origin&&['/','/new-york/','/trustedrouter/'].includes(u.pathname)&&u.pathname!==location.pathname){u.searchParams.set('font',key);a.href=u.href;}});
  if(updateURL){scrollTo({left:x,top:y,behavior:'instant'});requestAnimationFrame(()=>scrollTo({left:x,top:y,behavior:'instant'}));}
 }
 apply(new URL(location.href).searchParams.get('font'));
 select.addEventListener('change',()=>apply(select.value,true));
 addEventListener('popstate',()=>apply(new URL(location.href).searchParams.get('font')));
 Promise.all(['Archivo','Plus Jakarta Sans','IBM Plex Sans','Manrope'].flatMap(f=>[400,500,550,700].map(w=>document.fonts.load(`${w} 16px "${f}"`)))).then(()=>{select.disabled=false;document.querySelector('#font-loading').hidden=true;}).catch(()=>{document.querySelector('#font-loading').textContent='A font could not load. Reload to compare the actual fonts.';});
})();
