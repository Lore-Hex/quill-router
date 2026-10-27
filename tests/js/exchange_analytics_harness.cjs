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
 vm.runInContext(source,context);
 return {requests,window,document,storage,timers,context};
}

module.exports = {setup};

// Emit real browser payloads for the Python collector/export integration test.
if (require.main === module) {
 const s=setup({pathname:process.argv[2]});
 [...s.timers.values()][0]();
 process.stdout.write(JSON.stringify(s.requests.map(request=>JSON.parse(request.body))));
}
