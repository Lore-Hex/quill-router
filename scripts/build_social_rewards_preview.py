"""Regenerate the self-contained, simulated social rewards UI preview."""

import argparse
import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src/trusted_router/static"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Destination for the generated HTML")
    args = parser.parse_args()
    css = "\n".join(
        (STATIC / name).read_text() for name in ("tokens.css", "dashboard.css", "console.css")
    )
    font = base64.b64encode((STATIC / "homepage/archivo.woff2").read_bytes()).decode()
    css += f'\n@font-face{{font-family:Archivo;src:url(data:font/woff2;base64,{font})}}'
    dialog = (ROOT / "src/trusted_router/templates/console/_social_rewards.html").read_text()
    dialog = re.sub(r"<script.*?</script>", "", dialog, flags=re.S)
    dialog = re.sub(r"\{\{.*?\}\}", "preview-user", dialog)
    script = (STATIC / "social-rewards.js").read_text()
    child = (
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<style>{css}</style><body class=console><main style=padding:24px>"
        "<h1>Credits</h1><p>Simulated balance: $0.150000</p>"
        '<button class=btn data-rewards-open hidden>Social rewards</button>'
        '<div id=preview-key hidden class=console-key-reveal>One-time API key reveal</div>'
        "</main>" + dialog + "<script>MOCK_SCRIPT</script>"
        f"<script>{script}</script></body>"
    )
    mock = """
const state = {eligible:true,enabled:true,first_call_complete:true,dismissed:false,
 workspace_name:'Personal workspace', actions:[
 ['github','Star TrustedRouter on GitHub'],['x_trustedrouter','Follow TrustedRouter on X'],
 ['x_founder','Follow founder @jperla on X'],['linkedin','Follow TrustedRouter on LinkedIn']
 ].map(([id,label])=>({id,label,url:'#',claimed:false}))};
const scenario = SCENARIO;
const key = document.getElementById('preview-key');
if(scenario === 'key-reveal') key.hidden=false; else key.remove();
if(scenario === 'before-call') state.first_call_complete=false;
if(scenario === 'paused') state.enabled=false;
if(scenario === 'ineligible') state.eligible=false;
if(scenario === 'claimed') state.actions.slice(0,2).forEach(a=>a.claimed=true);
window.fetch=async(url,options)=>{
 if(url.endsWith('/shown')) {state.show_now=!state.dismissed;state.dismissed=true;}
 if(url.endsWith('/dismiss')) state.dismissed=true;
 if(url.endsWith('/claim')) {
  if(scenario === 'pending') await new Promise(resolve=>setTimeout(resolve,5000));
  if(scenario === 'error') return {ok:false};
  state.actions.find(a=>a.id===JSON.parse(options.body).action).claimed=true;
 }
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(state))};
};
window.open=()=>null;
"""
    outer = """<!doctype html><html lang=en><meta charset=utf-8>
<title>TrustedRouter social rewards — local simulated preview</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#18211c;color:#edf8f5;font:16px system-ui}
header{padding:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
p{margin:0;width:100%;font-size:13px}select{font:inherit;padding:8px}
iframe{display:block;border:1px solid #536459;margin:0 auto;max-width:100%;height:1050px}
</style><header><strong>Local UI preview · simulated rewards only</strong>
<label>Width <select id=width><option>1440</option><option>390</option><option>320</option></select></label>
<label>State <select id=scenario>
<option value=available>Available</option><option value=claimed>Two claimed</option>
<option value=pending>Pending (click a claim)</option><option value=error>Error (click a claim)</option>
<option value=before-call>Before first successful call</option><option value=key-reveal>Key reveal</option>
<option value=paused>Campaign paused</option><option value=ineligible>Legacy/ineligible account</option>
</select></label><p>No network requests, real accounts, social actions or rewards. Changes reset when
switching state. Open Social rewards manually for locked/paused states. Production authorization and
saved cross-session progress require the server; this preview only exercises the actual UI assets.</p>
</header><iframe title="Social rewards preview"></iframe><script>
const child = CHILD_JSON;
const mock = MOCK_JSON;
const frame = document.querySelector('iframe');
function render(){
 const width=document.getElementById('width').value;
 frame.style.width=width+'px';frame.style.height=width==='1440'?'1050px':'844px';
 const script=mock.replace('SCENARIO',JSON.stringify(document.getElementById('scenario').value));
 frame.srcdoc=child.replace('MOCK_SCRIPT',script);
}
document.querySelectorAll('select').forEach(select=>select.addEventListener('change',render));
render();</script></html>"""
    # Escape script closing tags because the child source lives inside a script string.
    outer = outer.replace("CHILD_JSON", json.dumps(child).replace("</", "<\\/"))
    outer = outer.replace("MOCK_JSON", json.dumps(mock).replace("</", "<\\/"))
    args.output.write_text(outer)


if __name__ == "__main__":
    main()
