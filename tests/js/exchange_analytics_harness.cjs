const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../../sites/token-exchange/analytics.js'),'utf8');
function setup(options={}) {
 const requests=[], timers=new Map(), storage=options.storage||new Map(), local=options.local||new Map(); let sequence=0;
 const store=map=>({
  getItem:k=>{if(options.noStorage)throw Error('blocked');return map.has(k)?map.get(k):null;},
  setItem:(k,v)=>{if(options.noStorage)throw Error('blocked');map.set(k,v);},
  removeItem:k=>{map.delete(k);},
 });
 const hostname=options.hostname||'nytokenexchange.com', pathname=options.pathname||'/';
 const document={hidden:!!options.hidden,referrer:options.referrer||'',
  body:{dataset:{market:options.market===undefined?'new-york':options.market}},
  addEventListener:(event,fn)=>document[event]=fn};
 const window={};
 const context=vm.createContext({window,document,navigator:options.navigator||{},URLSearchParams,URL,Date,
  location:{search:options.search===undefined?'?utm_source=trustedrouter.com&utm_content=homepage-banner&email=private':options.search,
   pathname,hostname,href:'https://'+hostname+pathname},
  crypto:{randomUUID:()=> options.uuid||'11111111-1111-4111-8111-111111111111'},
  sessionStorage:store(storage),localStorage:store(local),
  setTimeout:fn=>{timers.set(++sequence,fn);return sequence;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,opts)=>{requests.push({url,...opts});if(options.offline)throw Error('offline');},
 });
 vm.runInContext(source,context,{timeout:1000});
 return {requests,window,document,storage,local,timers,context};
}

module.exports = {setup};

// Emit real browser payloads for the Python collector/export integration test.
if (require.main === module) {
 const started=performance.now();
 process.stderr.write('analytics harness: Node started\n');
 const payloads=pathname=>{
  const s=setup({pathname});
  // Run callbacks inside the VM deadline too; a broken callback must fail,
  // rather than leave the parent waiting on its process deadline.
  s.context.engage=[...s.timers.values()][0];
  vm.runInContext('engage()',s.context,{timeout:1000});
  return s.requests.map(request=>JSON.parse(request.body));
 };
 const paths=process.argv.slice(2);
 const result=paths.length===1 ? payloads(paths[0]) : Object.fromEntries(paths.map(path=>[path,payloads(path)]));
 process.stdout.write(JSON.stringify(result));
 process.stderr.write(`analytics harness: completed ${paths.length} pages in ${(performance.now()-started).toFixed(1)}ms\n`);
}
