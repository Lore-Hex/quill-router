"""Add NBXplorer to the isolated dashboard, reusing the funded Bitcoin/LND node."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import time
from typing import Any

from scripts.lightning.activate_web import Operator
from scripts.lightning.btcpay.deploy import PRIVATE_IP, committed_bundle, ssh

# NBXplorer probes createwallet during startup. With disablewallet=1 it returns
# method-not-found. No wallet, transaction-broadcast, or node-admin RPC is enabled.
RPC_METHODS = (
    "getblockchaininfo", "getnetworkinfo", "getpeerinfo", "getindexinfo",
    "getbestblockhash", "getblockhash", "getblockheader", "getblock", "getblockcount",
    "getrawmempool", "getmempoolinfo", "getmempoolentry", "getrawtransaction",
    "gettxout", "estimatesmartfee", "validateaddress", "createwallet",
)


def bitcoin_configuration(current: str) -> str:
    if "disablewallet=1" not in current.splitlines() or "[main]" not in current.splitlines():
        raise ValueError("Require existing mainnet node with Core wallet disabled")
    if "listen=0" in current.splitlines():
        current = current.replace("\nlisten=0\n", "\nlisten=1\n")
    elif "listen=1" not in current.splitlines():
        raise ValueError("Unexpected Bitcoin listener configuration")
    include = "includeconf=/etc/bitcoin/btcpay.conf"
    if include not in current.splitlines():
        current = current.replace("[main]", include + "\n[main]", 1)
    return current


def remote(operator: Operator, code: str, payload: dict[str, Any], *, node: str = "tr-btcpay-1") -> str:
    wrapped = "import json,sys\np=json.load(sys.stdin)\n" + code
    return ssh(operator, "sudo python3 -c " + shlex.quote(wrapped), data=json.dumps(payload), node=node)


NODE_PREPARE = r'''
import hashlib,hmac,os,pwd,secrets
from pathlib import Path
conf=Path('/etc/bitcoin/bitcoin.conf')
current=conf.read_text()
if 'disablewallet=1' not in current.splitlines():
    raise SystemExit('Refusing Core wallet-enabled node')
password=Path('/etc/bitcoin/btcpay-password')
os.umask(0o077)
if not password.exists(): password.write_text(secrets.token_hex(32))
fragment=Path('/etc/bitcoin/btcpay.conf')
if not fragment.exists():
    salt=secrets.token_hex(16)
    digest=hmac.new(salt.encode(),password.read_bytes(),'sha256').hexdigest()
    fragment.write_text('rpcauth=tr_nbxplorer:'+salt+'$'+digest+'\n'
      +'rpcwhitelistdefault=0\nrpcwhitelist=tr_nbxplorer:'+','.join(p['rpc_methods'])+'\n'
      +'[main]\nrpcbind=10.92.0.2\nrpcallowip=10.92.2.2/32\n'
      +'bind=10.92.0.2:8333\nwhitelist=download,noban,mempool,relay@10.92.2.2/32\n')
    os.chown(fragment,0,pwd.getpwnam('bitcoin').pw_gid)
    fragment.chmod(0o640)
expected='rpcwhitelist=tr_nbxplorer:'+','.join(p['rpc_methods'])
if expected not in fragment.read_text().splitlines(): raise SystemExit('Unexpected existing NBXplorer permissions')
print(json.dumps({'cookie':'tr_nbxplorer:'+password.read_text(),'configuration':current}))
'''

NODE_ACTIVATE = r'''
import socket,subprocess,time
from pathlib import Path
conf=Path('/etc/bitcoin/bitcoin.conf')
if conf.read_text()!=p['previous']: raise SystemExit('Bitcoin config changed since inspection')
def run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=600)
    if result.returncode: raise RuntimeError('Node operation failed: '+args[0])
    return result.stdout
def cli(method):
    return json.loads(run(['/opt/lnd-v0.21.3-beta/lncli','--lnddir=/srv/lnd',method]))
before=cli('getinfo')
if not before['synced_to_chain']: raise SystemExit('Existing Lightning node is not synced')
if any(c.get('pending_htlcs') for c in cli('listchannels')['channels']):
    raise SystemExit('In-flight Lightning payments; retry during idle window')
run(['systemctl','start','tr-lnd-backup.service'])
try:
    with socket.create_connection(('10.92.0.2',8333),timeout=2): listening=True
except OSError: listening=False
changed=conf.read_text()!=p['configuration']
if changed or not listening:
    backup=Path('/etc/bitcoin/bitcoin.conf.before-btcpay')
    if not backup.exists(): backup.write_bytes(conf.read_bytes()); backup.chmod(0o600)
    conf.write_text(p['configuration'])
    try:
        run(['systemctl','restart','tr-bitcoin.service','tr-lnd.service'])
        for attempt in range(90):
            try:
                after=cli('getinfo')
                if after['synced_to_chain'] and after['identity_pubkey']==before['identity_pubkey']: break
            except Exception: pass
            time.sleep(2)
        else: raise RuntimeError('Existing LND identity did not recover')
    except Exception:
        conf.write_text(p['previous'])
        run(['systemctl','restart','tr-bitcoin.service','tr-lnd.service'])
        raise
print(json.dumps({'core_listener_ready':True,'node_identity_preserved':True,'restarted':changed or not listening}))
'''

DATABASE_SETUP = r'''
import base64,os,secrets,subprocess
from pathlib import Path
root=Path('/opt/tr-btcpay')
os.umask(0o077)
for name in ('data/nbxplorer','secrets'): (root/name).mkdir(parents=True,exist_ok=True)
(root/'secrets/nbxplorer-bitcoin.cookie').write_text(p['cookie'])
env=root/'.env'
values=dict(line.split('=',1) for line in env.read_text().splitlines() if '=' in line)
def sql(text):
    result=subprocess.run(['docker-compose','-f',str(root/'compose.yaml'),'exec','-T','postgres',
      'psql','-U','btcpay','-d','postgres','-v','ON_ERROR_STOP=1','-At'],input=text,text=True,capture_output=True)
    if result.returncode: raise RuntimeError('NBXplorer database setup failed')
    return result.stdout.strip()
if 'NBXPLORER_DATABASE_CONNECTION' not in values:
    password=secrets.token_hex(32)
    if sql("SELECT 1 FROM pg_roles WHERE rolname='nbxplorer'"): raise SystemExit('Unexpected existing NBXplorer role')
    sql("CREATE ROLE nbxplorer LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
      +"CREATE DATABASE nbxplorer OWNER nbxplorer;\nREVOKE ALL ON DATABASE nbxplorer FROM PUBLIC;")
    with env.open('a') as f:
        f.write('\nNBXPLORER_DATABASE_CONNECTION=User ID=nbxplorer;Password='+password
          +';Host=postgres;Port=5432;Database=nbxplorer;MaxPoolSize=10\n')
compose=root/'compose.yaml'
backup=root/'compose.before-nbxplorer.yaml'
if not backup.exists(): backup.write_bytes(compose.read_bytes())
compose.write_bytes(base64.b64decode(p['compose']))
print('NBXplorer database and private configuration prepared')
'''

EXPLORER_CHECK = r'''
import base64,urllib.request
from pathlib import Path
root=Path('/opt/tr-btcpay')
# Address discovered from the private Docker network; never publish this API.
import subprocess
address=subprocess.check_output(['docker','inspect','-f','{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}','tr-btcpay_nbxplorer_1'],text=True).split()[0]
cookie=(root/'data/nbxplorer/Main/.cookie').read_text().strip()
request=urllib.request.Request('http://'+address+':24444/v1/cryptos/BTC/status',
  headers={'Authorization':'Basic '+base64.b64encode(cookie.encode()).decode()})
with urllib.request.urlopen(request,timeout=10) as response: data=json.load(response)
print(json.dumps({'isFullySynched':data.get('isFullySynched'),'chainHeight':data.get('chainHeight'),
 'syncHeight':data.get('syncHeight'),'bitcoinStatus':data.get('bitcoinStatus')}))
'''


BTCPAY_CHECK = r'''
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:49392/api/v1/health',timeout=10) as response:
    print(json.dumps(json.load(response)))
'''


def validate_firewall(rule: dict[str, Any]) -> None:
    if (rule.get("direction") != "INGRESS" or rule.get("disabled", False)
            or rule.get("sourceRanges") != [PRIVATE_IP + "/32"]
            or rule.get("targetTags") != ["tr-lightning"]
            or not rule.get("network", "").endswith("/networks/tr-lightning")
            or rule.get("sourceTags") or rule.get("sourceServiceAccounts")
            or rule.get("allowed") != [{"IPProtocol": "tcp", "ports": ["8332", "8333"]}]):
        raise ValueError("Unexpected Bitcoin firewall scope; refusing deployment")


def apply(operator: Operator) -> None:
    files = committed_bundle()
    core = json.loads(remote(operator, NODE_PREPARE, {"rpc_methods": RPC_METHODS}, node="tr-bitcoin-1"))
    configuration = bitcoin_configuration(core["configuration"])
    name = "tr-btcpay-bitcoin"
    rules = operator.gc("compute", "firewall-rules", "list", "--filter=name=" + name, "--format=value(name)")
    if not rules.strip():
        operator.gc("compute", "firewall-rules", "create", name, "--network=tr-lightning", "--direction=INGRESS",
                    "--source-ranges=" + PRIVATE_IP + "/32", "--target-tags=tr-lightning",
                    "--allow=tcp:8332,tcp:8333", "--enable-logging")
    validate_firewall(json.loads(operator.gc("compute", "firewall-rules", "describe", name, "--format=json")))
    print(remote(operator, DATABASE_SETUP, {"cookie": core["cookie"], "compose": files["compose.yaml"]}))
    print(ssh(operator, "sudo docker-compose -f /opt/tr-btcpay/compose.yaml pull nbxplorer"))
    print(remote(operator, NODE_ACTIVATE, {"previous": core["configuration"], "configuration": configuration}, node="tr-bitcoin-1"))
    ssh(operator, "sudo docker-compose -f /opt/tr-btcpay/compose.yaml up -d nbxplorer")
    for _attempt in range(60):
        try:
            status = json.loads(remote(operator, EXPLORER_CHECK, {}))
            if status.get("isFullySynched"):
                print(json.dumps(status))
                break
        except (RuntimeError, ValueError):
            pass
        time.sleep(5)
    else:
        raise RuntimeError("NBXplorer not synced; BTCPay configuration has not been activated")
    ssh(operator, "sudo bash -s -- --configure-only", data=base64.b64decode(files["install.sh"]).decode())
    ssh(operator, "sudo systemctl start tr-btcpay-backup.service")
    ssh(operator, "sudo docker-compose -f /opt/tr-btcpay/compose.yaml up -d --no-deps btcpay")
    for _attempt in range(60):
        try:
            if json.loads(remote(operator, BTCPAY_CHECK, {})).get("synchronized") is True:
                print("BTCPay and NBXplorer synchronized; both databases backed up")
                return
        except (RuntimeError, ValueError):
            pass
        time.sleep(5)
    raise RuntimeError("BTCPay did not report synchronized; inspect dashboard before calling deployment healthy")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        apply(Operator(args.account))
    else:
        print("Add private NBXplorer, scoped Bitcoin RPC and private P2P. Back up and restart existing Core/LND only if needed. No new wallet or chain.")
