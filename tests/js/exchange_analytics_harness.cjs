const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../../sites/token-exchange/analytics.js'),'utf8');
function setup(options={}) {
 const requests=[], timers=new Map(), storage=options.storage||new Map(); let sequence=0;
 const document={hidden:!!options.hidden,addEventListener:(event,fn)=>document[event]=fn};
 const window={};
 const context=vm.createContext({window,document,navigator:options.navigator||{},URLSearchParams,Date,
  location:{search:'?utm_source=trustedrouter.com&utm_content=homepage-banner&email=private',pathname:options.pathname||'/'},
  crypto:{randomUUID:()=> '11111111-1111-4111-8111-111111111111'},
  sessionStorage:{getItem:k=>{if(options.noStorage)throw Error('blocked');return storage.get(k);},setItem:(k,v)=>storage.set(k,v)},
  setTimeout:fn=>{timers.set(++sequence,fn);return sequence;},clearTimeout:id=>timers.delete(id),
  fetch:async(url,opts)=>{requests.push({url,...opts});if(options.offline)throw Error('offline');},
 });
 vm.runInContext(source,context,{timeout:1000});
 return {requests,window,document,storage,timers,context};
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
