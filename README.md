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
  deliberately omitted so every visible menu item works.
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

## What persists, exactly

`persist` writes **only** `/data/zcfg_config.json`, the router's user-data
configuration file. It makes two changes:

1. The `root` account's `shadow` value becomes a clean single-hash line:
   `root:$6$...:<lastchange>:0:99999:7:::`. The stock line contained a
   duplicated hash, which made root password auth fail.
2. The `X_ZYXEL_RemoteManagement` SSH service is set to
   `Enable=true`, `Mode="LAN_ONLY"`, `OldMode="LAN_ONLY"`,
   `RestartDeamon=true`, `DisableSshPasswordLogin=false`.

At boot, `zcmd` generates `/etc/shadow` from that config line and stock
`dropbear -p 22 -P /var/run/dropbear.pid` starts from the remote-management
config. No hook, watcher, or USB stick is involved. The firmware, bootloader,
and NVRAM are not modified.

`persist` saves the old config in three places:

- `~/.zyxel-godmode/zcfg_config.<timestamp>.json` on this machine,
- `/data/zcfg_config.pre_persist.bak` on the router (first run only),
- `/mnt/usb*/zcfg_config.pre_persist.bak` if a USB volume is mounted.

`depersist` copies the on-router backup back and reboots. A factory reset or
firmware upgrade that wipes `/data` also removes persistent root.

## One-time root: how it works (30 seconds)

The `/cgi-bin/TR369Certificates?action=download` endpoint builds
`cp /data/usp/cert/<name> <tmp>` with a 128-byte buffer. A padded `name`
with spaces turns it into `cp <src> <dst>` with the temp destination
truncated away — an authenticated arbitrary file copy. We use it only to
copy router files onto the removable USB stick and to drop a logrotate
config + trigger there. The stock 5-minute logrotate job then runs our
USB payload as root, which restores the UID-0 passwd, SSH, and the UI
patch. Live `/etc/passwd` is a tmpfs file; the one-time path writes no
firmware or bootloader data.

The old Deck-side watcher (`watch` / `install`) is **not** part of the
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
