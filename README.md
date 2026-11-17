# ex3301-godmode — persistent root + full Web UI for the Zyxel EX3301-T0

One Python script. No blobs, no firmware mods. It turns the ISP-locked
EX3301-T0 (Hyperoptic Hyperhub and siblings) into a box you fully control:
UID-0 SSH on the LAN, every hidden admin page restored, and a **config-only
root that survives reboots without a USB stick or a Deck-side watcher**.

## What it does

- **Root (one-time):** flips the live `admin` account to UID 0 / `/bin/sh`
  via the authenticated TR-369 certificate-copy endpoint, starts Dropbear SSH
  bound to the LAN plus a firewall rule. Verified: `uid=0(root) gid=0(root)`.
- **Unhide:** patches the served `app.js` in memory (bind-mount) — forces the
  `hide*` GUI flags off and re-adds all 45 server-stripped menu entries whose
  pages exist in the build (Remote Management with FTP/TELNET/SSH rows, xDSL
  Statistics, SIP/Phone/Call Rule, Broadband, Multicast, Time, SNMP, Firmware
  Upgrade, …). 12 entries whose routes are compiled out of this ISP build are
  deliberately omitted so every visible menu item works. The UI patch is a
  per-boot tmpfs/bind-mount operation; `persist` does not make it permanent
  (see “What persists and what doesn't”).
- **Persist (config-only):** `persist` writes a clean root shadow and a
  LAN-only SSH binding into `/data/zcfg_config.json`. Stock dropbear starts
  at boot, so root SSH comes back with **no USB stick, no logrotate hook and
  no Deck watcher**. Verified across a full reboot: stock `/etc/passwd`
  (`admin:x:21:21:...:/usr/bin/zysh`), no `/tmp/root_boot.log`, stock
  logrotate config, and `uid=0(root)`.
- **Undo:** `depersist` restores the pre-persist config and reboots.

## Requirements

- The router, a USB stick for the one-time root (any size, FAT32), this
  machine on the LAN.
- Python 3, `requests`, `pycryptodome`: `pip install requests pycryptodome`
- `openssl` on this machine (used by `persist` to generate the root hash).
- Your router's **admin** password (asked interactively, never stored —
  or pass `--password-file` / `$ZYXEL_ADMIN_PASSWORD` for automation).
- A **root** password for `persist`/`status`/`depersist`: generated on the
  first `persist` and saved to `~/.zyxel-root` (mode 0600), or supplied with
  `--root-password-file` / `$ZYXEL_ROOT_PASSWORD`.

## Tested on / firmware

Developed against a second-hand **EX3301-T0 bought on eBay**, stock
Hyperoptic (ISP) firmware:

| Item | Value |
|---|---|
| Firmware | `V5.50(ABVY.5.5)b6_Y0` |
| Kernel | 3.18.21 (EcoNet EN751627, dual MIPS 1004Kc) |
| Rootfs | 25 MB LZMA squashfs on SPI NAND, tmpfs `/etc` |
| Build | OpenWrt-derived (GCC 4.8.1), 14 MTD partitions |

The TR-369 handler and the `app.js` mutation/menu structures are what the
patch depends on. `stage` verifies both anchors and aborts cleanly if a
different firmware build doesn't match — it will not write a payload it
can't verify. Newer ABVY builds (6.x+) are reported to drop the endpoint
entirely, so this is likely a 5.x-only technique.

## Hardware

![EX3301-T0 with lid off](hardware-ex3301-t0-open.jpg)

Board notes: EcoNet EN751627 SoC under the central heatsink, MT7915D WiFi
under the shield can (yellow lead = antenna), Winbond W25N01G 128 MB SPI
NAND as the 8-pin flash chip, 4× GbE LAN + 1× WAN RJ45, 2× FXS phone jacks,
USB3, and **no DSL jack** on this ETH-WAN variant despite DSL code in the
firmware. The rainbow Dupont wires are the UART tap used for the serial
console.

## Usage

```bash
# 1. Stick in the ROUTER, copy the live account file onto it:
./zyxel-godmode.py extract

# 2. Stick in THIS machine, build the payload (needs the router for stock UI files):
./zyxel-godmode.py stage /run/media/$USER/ROUTERSTICK

# 3. Stick back in the ROUTER, install the hook and wait for root (~5 min):
./zyxel-godmode.py restore

# 4. Make it persistent. No USB/Deck is needed after this:
./zyxel-godmode.py persist

# Check state / undo:
./zyxel-godmode.py status
./zyxel-godmode.py depersist
```

Global options `--host` / `--password-file` / `--root-password-file` /
`--yes` go before the command.

After step 3: `ssh admin@192.168.1.1` → `#` (admin is temporarily UID 0
from the USB hook). After `persist`: `ssh root@192.168.1.1` with the
password in `~/.zyxel-root` (or the file passed with
`--root-password-file`). The admin web password is unchanged.

`persist` reads the config over the privileged shell, so it can use either
the one-time admin UID 0 shell or an existing root password. Useful flags:

```bash
# Show what would change, without writing or rebooting:
./zyxel-godmode.py persist --dry-run

# Write the config but do not reboot:
./zyxel-godmode.py persist --no-reboot

# Rotate the root password (writes a new ~/.zyxel-root):
./zyxel-godmode.py --root-password-file ~/.zyxel-root persist --rotate-root-password
```

## Entry: the one-time root vector

This is how the first privileged shell is obtained. It is **not** the
persistence mechanism.

1. **Authenticated handler.** The web UI exposes
   `/cgi-bin/TR369Certificates?action=download&name=...&sessionkey=...`.
   The handler builds a `cp` command from the 128-byte `name` field into a
   `/data/usp/cert/...` source and a temp destination. A name padded with
   spaces is interpreted as `cp <src> <dst>`; the trailing temp path is
   truncated by the 128-byte buffer. The command runs as root because the
   web/TR-369 service does.
2. **What we copy.** `extract` copies live `/etc/passwd` to the USB root.
   `stage` builds `payload/` on the USB with a prepared passwd (admin UID 0,
   `/bin/sh`), `boot.sh`, a logrotate hook, a trigger file, and the patched
   `index.html`/`app.js`.
3. **Hook install.** `restore` logs in as admin and uses the same handler to
   copy:
   - `payload/sys_logrotate.conf` to `/var/logrotate.d/`
   - `payload/root_trigger.log` to `/tmp/`

   Both are tmpfs paths.
4. **Stock logrotate executes it.** The stock cron runs logrotate every five
   minutes. The hook's `postrotate` section loops over `/mnt/usb*` and runs
   `/bin/sh "$d/payload/boot.sh"` as root.
5. **What `boot.sh` does.** It finds the USB volume with `payload/passwd`,
   copies the prepared passwd over `/etc/passwd` (admin becomes
   `0:0:/bin/sh`), starts `dropbear -p 192.168.1.1:22`, adds a LAN-only
   `iptables` ACCEPT for SSH, and bind-mounts the patched `/web` tree over
   `/web` so the hidden pages render. It writes `/tmp/root_boot.log`.
6. **Result.** `ssh admin@192.168.1.1` is UID 0 and the full UI is served.

Why this is not persistence: `/etc/passwd`, `/var/logrotate.d`, `/tmp`,
and the `/web` bind mount are all tmpfs. A power cycle removes every one.
The USB stick by itself is inert: nothing executes `payload/boot.sh` until
the tmpfs logrotate hook is re-installed. The old Deck watcher did exactly
that re-installation every boot; it is a third-party caller, not on-router
persistence, and is deprecated.

## Persistence: what `persist` changes

`persist` uses the one-time privileged shell (admin UID 0, or an existing
root login) to edit `/data/zcfg_config.json`, the router's persistent
user-data config on the yaffs2 `/data` partition. It makes two changes:

1. **Root shadow.** The `root` account's `shadow` value is replaced with a
   clean single-hash line:
   `root:$6$<hash>:<lastchange>:0:99999:7:::`. The hash is generated with
   `openssl passwd -6`; the plaintext root password never goes into the
   config. The stock line had a duplicated hash, which made root password
   auth fail.
2. **SSH service.** The `X_ZYXEL_RemoteManagement` service named `SSH` is
   set to `Enable=true`, `Port=22`, `Mode="LAN_ONLY"`,
   `OldMode="LAN_ONLY"`, `RestartDeamon=true`,
   `DisableSshPasswordLogin=false`.

Then `persist` writes the config, reboots, and waits for root SSH. The boot
chain that makes it persistent:

```text
SPI NAND bootloader
  -> kernel + squashfs rootfs (read-only)
  -> /data yaffs2 (mtd9, read-write)
  -> rcS / zcmd loads /data/zcfg_config.json
  -> zcmd generates /tmp/var/passwd and /tmp/var/shadow
     (/etc/passwd and /etc/shadow are symlinks to those tmpfs files)
  -> libzcfg_be remote-management starts
     dropbear -p 22 -P /var/run/dropbear.pid
  -> dropbear authenticates root against /etc/shadow
  -> uid=0 root shell, every boot, no USB and no Deck
```

The root account is stock `root:x:0:0:root:/home/root:/bin/sh`; its shell
is already `/bin/sh`. `persist` only gives it a known password and makes
stock dropbear start at boot. The admin account returns to its stock
`admin:x:21:21:admin:/home/admin:/usr/bin/zysh`; use **root** after
`persist`, not admin.

## What persists and what doesn't

**Survives a reboot (config-only):**

- root SSH on port 22, LAN-only;
- the root password hash in `/data/zcfg_config.json`;
- the stock `dropbear` service start from remote-management config;
- the stock `root` account (UID 0, `/bin/sh`).

**Does not survive a clean reboot without the USB hook:**

- the UI unhide (the `/web` bind-mount and patched `app.js`/`index.html`);
- the temporary admin UID 0 / `/bin/sh` rewrite;
- the hook's `iptables` rule and `/var/logrotate.d` hook;
- `/tmp/root_boot.log`.

This is the deliberate trade-off: the config-only method gives true
on-router root persistence with no third-party caller, but the full UI
patch remains a per-boot tmpfs/bind-mount operation. After a clean reboot
without the hook, the web UI is stock (hidden pages), while root SSH still
works. To re-apply the UI patch, run the one-time hook again or copy the
patched files and bind-mount `/web` from a root shell:

```sh
MNT=$(for d in /mnt/usb*; do
  [ -f "$d/payload/index_hidden.html" ] && echo "$d" && break
done)
cp -a /web /tmp/web
cp "$MNT/payload/index_hidden.html" /tmp/web/index.html
cp "$MNT/payload/app_patched.js" /tmp/web/static/js/app.js
mount --bind /tmp/web /web
```

The hook does this automatically when it runs.

## Verification after a full reboot

The no-Deck test removed the logrotate hook and stopped the watcher, then
rebooted. After boot:

- `ssh root@192.168.1.1` -> `uid=0(root) gid=0(root)`
- `/etc/passwd` -> `admin:x:21:21:admin:/home/admin:/usr/bin/zysh`
  (stock admin; the hook did not run)
- `/tmp/root_boot.log` -> absent
- `/var/logrotate.d/sys_logrotate.conf` -> stock 60-byte logrotate file,
  not the 173-byte payload hook
- `ps` -> `dropbear -p 22 -P /var/run/dropbear.pid` (stock config start,
  not the hook's `dropbear -p 192.168.1.1:22`)
- `/etc/shadow` root line -> one `$6$` hash, no duplicate
- `/data/zcfg_config.json` SSH service -> `Mode="LAN_ONLY"`

## Backups and recovery

`persist` saves the old config in three places:

- `~/.zyxel-godmode/zcfg_config.<timestamp>.json` on this machine;
- `/data/zcfg_config.pre_persist.bak` on the router (first run only);
- `/mnt/usb*/zcfg_config.pre_persist.bak` if a USB volume is mounted.

`depersist` copies the on-router backup back and reboots. If you are locked
out after a bad write, the web UI may still be reachable; use the one-time
USB hook/`restore` to get a privileged shell, then copy
`/data/zcfg_config.pre_persist.bak` back to `/data/zcfg_config.json` and
reboot. A factory reset or a firmware upgrade that wipes `/data` also
removes persistent root.

## Boundaries

- This is a **config/root persistence** technique, not a bootloader or
  firmware exploit. The signed firmware and A/B boot validation are not
  bypassed.
- `persist` writes only `/data/zcfg_config.json`; it does not write the
  kernel, rootfs, bootloader, or NVRAM.
- The root password is stored locally in `~/.zyxel-root` (0600) or the file
  passed with `--root-password-file`; the admin web password is separate and
  should be changed.
- TR-069/ACS settings are not modified by this tool. If you do not need
  remote management, disable it or firewall the ACS/connection-request
  paths separately.

The old Deck-side watcher (`watch` / `install`) is not part of the
persistence method. It is kept only as a legacy fallback for firmware where
`persist` does not work; it is deprecated.

## OpenWrt: what you'd gain, what you'd lose

This project keeps the **stock firmware** and re-enables what the ISP build
hides. The alternative is replacing it with OpenWrt. Both are valid; they
trade different things.

**Why OpenWrt is attractive here**

- True package management (`opkg`), current kernel, upstream WiFi drivers.
- No ISP customisation flags, no CWMP/TR-069 unless you install it.
- Full root by design — no per-boot restore dance.
- The box is already OpenWrt-derived internally (kernel built with OpenWrt
  GCC 4.8.1, OpenWrt-style init/crontab), so porting is plausible.

**Why it's not a drop-in swap on this unit**

- **VoIP would be lost.** The stock FXS stack (`icf.exe`, `mm.exe`,
  `voiceApp`) is Zyxel-proprietary; there's no upstream OpenWrt support for
  two FXS ports on EcoNet silicon. Your phone ports go dead.
- **DSL support** — no upstream driver for this EcoNet VDSL/ADSL PHY. On
  this ETH-WAN variant the jack isn't even populated, but other ABVY units
  would lose xDSL entirely.
- **MT7915D WiFi** is supported upstream in principle, but the DBDC /
  Econet-specific calibration data on this board may need work.
- **Dual-boot A/B slots and the ZHAL bootloader** aren't understood well
  enough to promise a safe in-place install; a bad flash here is a hardware
  recovery job (SPI NAND programmer / ZHAL `ATUR` over UART).
- **Signed firmware.** Stock `.bin` images end in a 64-byte signature, so
  modified Zyxel images can't be flashed through the normal web upgrade —
  which is exactly why this project stays in the config/rootfs rather than
  the bootloader.

**Practical recommendation**

- Keep stock + this tool if you want **VoIP, xDSL compatibility, and the
  ISP's line provisioning**.
- Consider OpenWrt only if this unit will be a **pure router/AP with no
  phone or DSL requirement**, and only after a full flash backup (all 14
  partitions) plus a proven UART recovery path.
- The two can coexist in spirit: use this tool to grab full flash backups
  and explore the hardware *before* deciding, since those backups are also
  what make an OpenWrt attempt recoverable.

## Safety notes

- **Ownership first.** Check whether you or your ISP owns the device before
  running anything. If the router is ISP-supplied (typical for Hyperoptic
  units), modifying it may breach your contract — see the legal section.
  This repository was developed and tested on a second-hand unit bought on
  eBay specifically to avoid that situation.
- The **one-time root path** writes no firmware or bootloader data; the
  passwd change, SSH service and UI patch live in RAM (tmpfs) and on the
  removable USB stick.
- `persist` writes **only the user-data config partition**
  (`/data/zcfg_config.json`), not firmware, bootloader, or NVRAM. It saves
  backups locally, on the router, and on the USB stick if present.
  `depersist` restores the on-router backup; a factory reset also clears it.
- Before `persist`, unplug the stick and reboot and the router returns to
  stock (plus your config). After `persist`, root is intentionally
  persistent — use `depersist` or a factory reset to remove it.
- Every state-changing command (`extract`, `stage`, `restore`, `persist`,
  `depersist`) requires typing `I OWN THIS DEVICE` unless `--yes` is passed.
- Back up first: `stage` refuses to run without a valid base passwd, and
  the script never overwrites existing payload files blindly — re-run from
  a fresh stick if in doubt.
- Change the **admin** web password afterwards (it was used for the
  one-time login). The **root** password is separate; rotate it with
  `persist --rotate-root-password`.

## Legal — read this first

**Full notice: [LEGAL.md](LEGAL.md). It is not legal advice — if you're unsure, ask a lawyer.**

- **Ownership first.** Only use this on devices you own or are expressly
  authorised to administer. Many units of this model are ISP-owned; check
  your contract. If in doubt, use a lawfully purchased unit (this project was
  developed on a second-hand eBay unit).
- **Unauthorised access is a crime** in most jurisdictions (UK Computer
  Misuse Act 1990, US CFAA, EU equivalents). So is evading billing or
  bypassing access controls you are not entitled to.
- **Anti-circumvention laws may apply** (e.g. DMCA §1201, EU Directive
  2001/29/EC Art. 6). Some countries exempt repair/interoperability; many do
  not. Checking is your responsibility.
- **Reversible does not mean consequence-free.** The one-time root path
  writes no firmware; `persist` writes only the config partition. That may
  still breach your contract, void a warranty, or breach provider terms.
- **No warranty, no liability, you indemnify the authors.** See LEGAL.md
  §§5–7. Bricked devices, lost service, legal costs — your risk.
- Published for **education, research, interoperability, and administration
  of equipment you own**. Not for attacking or disrupting anyone's network.
