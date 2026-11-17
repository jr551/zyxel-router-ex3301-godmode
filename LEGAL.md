# LEGAL NOTICE, ACCEPTABLE USE POLICY AND DISCLAIMER

**This document is not legal advice.** It is a plain-language risk notice
written by non-lawyers. Laws differ by country and change over time. If you
are unsure whether you may use this software on a particular device, consult
a qualified solicitor/lawyer in your jurisdiction before running it.

---

## 1. Scope of permitted use

This software is published **solely** for:

- security research and education;
- interoperability and repairability of equipment **you own**;
- administration of networks and devices **you own or are expressly
  authorised in writing to administer**;
- recovery of hardware you have lawfully acquired (including second-hand
  and e-waste devices).

## 2. Prohibited use

You **must not** use this software, in whole or in part, to:

- access, modify, or interfere with any device, network, or account that you
  do not own or lack documented authorisation to administer;
- bypass access controls, authentication, or billing systems of any third
  party, including an internet service provider;
- obtain service without payment, or evade network management, fair-use, or
  traffic-shaping policies;
- attack, scan, probe, or disrupt any network or system;
- intercept, decrypt, or disclose the communications of any person who has
  not consented;
- violate any applicable export control, sanctions, or telecommunications
  regulation.

## 3. Ownership of ISP-supplied equipment

Many routers of this model are supplied by an internet service provider and
**remain the property of that provider**, often under a loan, rental, or
"equipment remains ours" clause. Before running anything:

1. Check your service agreement and any equipment terms.
2. If the device is provider-owned, obtain written permission, or use a
   lawfully purchased unit instead (this project was developed on a
   second-hand unit bought on eBay for exactly this reason).

Consequences of proceeding without permission may include breach of
contract, early-termination fees, recovery charges, suspension of service,
and in some jurisdictions criminal liability. You accept those consequences
entirely.

**Note on reversibility:** this tool deliberately never writes router flash
or NVRAM. Changes live in volatile memory and on a removable USB stick, so a
power cycle restores the device to its stock configuration. That reduces
technical risk; it does **not** reduce legal or contractual risk.

## 4. Anti-circumvention and firmware law

Depending on your jurisdiction, modifying device firmware or bypassing
software restrictions may engage anti-circumvention provisions — for example
17 U.S.C. §1201 (DMCA) in the United States, or Article 6 of the EU
Directive 2001/29/EC as implemented nationally. Some jurisdictions provide
limited exemptions for repair, interoperability, security research, or
personal use; many do not. It is your responsibility to determine whether an
exemption applies to you. The authors neither encourage nor condone
circumvention of technical protection measures on equipment you do not own.

## 5. No warranty

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE, TITLE, AND NON-INFRINGEMENT. THE AUTHORS DO
NOT WARRANT THAT THE SOFTWARE WILL BE ERROR-FREE, SECURE, OR COMPATIBLE WITH
ANY PARTICULAR FIRMWARE, HARDWARE REVISION, OR SERVICE PROVIDER.

## 6. Limitation of liability

TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT SHALL THE
AUTHORS, CONTRIBUTORS, OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES,
OR OTHER LIABILITY — WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE —
ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR ITS USE. THIS
INCLUDES, WITHOUT LIMITATION: BRICKED OR DAMAGED DEVICES; LOSS OF DATA,
CONFIGURATION, OR BACKUPS; LOSS OF INTERNET, TELEPHONY, OR TELEVISION
SERVICE; VOIDED WARRANTIES; TERMINATION OF SERVICE; REGULATORY FINES; LEGAL
COSTS; AND ANY INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

## 7. Indemnity

You agree to indemnify, defend, and hold harmless the authors and
contributors from and against any and all claims, liabilities, damages,
losses, costs, and expenses (including reasonable legal fees) arising from
your use of the software, your breach of this notice, or your violation of
any law or third-party right.

## 8. Assumption of risk

You acknowledge that:

- modifying networking equipment can render it inoperable;
- recovery may require physical access, a UART adapter, or a flash
  programmer;
- your service provider may detect changes and may react by pushing
  configuration, resetting the device, restricting service, or terminating
  your contract;
- you are solely responsible for complying with your provider's terms.

You assume all of these risks voluntarily.

## 9. Third-party rights and trademarks

Zyxel, EcoNet, Hyperoptic, OpenWrt, and all other product and company names
are trademarks or registered trademarks of their respective owners. This
project is **not affiliated with, endorsed by, or sponsored by** any of them.
Firmware, binaries, calibration data, and configuration files remain the
property of their respective rights holders and are **not** redistributed by
this repository.

This repository contains no provider credentials, no device secrets, and no
proprietary code. It implements a documented technique against an
authentication-protected endpoint on firmware the user already possesses.

## 10. Responsible disclosure

If you believe this technique affects devices you do not own, or that it
could be used to harm third parties at scale, report it to the vendor and/or
the relevant national CERT before publishing further. Do not test it against
systems you do not control.

## 11. Severability and governing law

If any provision of this notice is held unenforceable, the remaining
provisions remain in full force. Nothing in this notice excludes liability
that cannot lawfully be excluded in your jurisdiction.

## 12. Acceptance

By downloading, copying, or running any part of this software, you confirm
that you have read and understood this notice, that you own or are
authorised to modify the target device, and that you accept all risk and
responsibility for your actions.
