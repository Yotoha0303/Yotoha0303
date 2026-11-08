# Xiaomi Pad 6 account isolation SOP

Scope: Android Tablet (Xiaomi Pad 6 `pipa` or compatible HyperOS devices), target ADB serial.
This procedure is for dedicated testing profiles during account observation and isolation periods.
It reduces local account mixing and optional OEM telemetry collection surfaces. It cannot guarantee
that Google will accept the session, prevent a restriction, or hide device and network signals from
services the user signs into.

## 1. Current read-only findings

Observed baseline findings on the reference tablet:

| Item | Finding | Treatment |
| --- | --- | --- |
| `com.miui.misightservice` | Installed, enabled in user 0; system extension app | Exact-match candidate for reversible user-0 disable |
| `com.xiaomi.security.onetrack` | Installed, enabled in user 0; product app | Exact-match candidate for reversible user-0 disable |
| `com.qualcomm.qti.services.systemhelper` | Installed, enabled; privileged system-extension app | Inspect and report only; never auto-disable |
| `com.microsoft.emmx` | Not installed in user 0 | Install in the dedicated Android user |
| `com.taobao.idlefish`, `com.taobao.taobao` | Not installed in user 0 | AppOps rules are generated only if found |
| Private DNS | Mode and hostname both reported `null` | Configure on device after choosing a reachable provider |
| Android users | Only owner/user 0 reported | Create a separate Android user before account sign-in |

System Helper's package metadata lists `MANAGE_USERS`,
`INTERACT_ACROSS_USERS`, and `READ_PRIVILEGED_PHONE_STATE`. Its path is
under `/system_ext/priv-app/`. Those facts justify caution, but do not prove
that it sends account IDs or network logs. If this package needs further
assessment, inspect its network behavior and ROM dependencies separately.
Do not infer that disabling it is safe.

## 2. Audit and review

Requires Python 3 and Android Platform Tools (`adb`). Authorize USB debugging
on the tablet. The script accepts an attached authorized device for audit and plan generation.
Applying changes requires that device to report `pipa` and `23043RP34C`.
No account names, passwords, tokens, or Android account database are read.

```bash
adb devices -l
python3 scripts/tablet_device_isolation.py --audit
python3 scripts/tablet_device_isolation.py --prepare-env --dry-run
python3 scripts/tablet_device_isolation.py --prepare-env
```

`--audit` writes `var/tablet-device-isolation/audit.json`;
`--prepare-env` writes `plan.json` with commands and per-action rollback
commands. Both are read-only on the device. `--dry-run` prints JSON and
writes nothing. `--output DIR` changes the report directory. Reports are
created with owner-only file permissions because they contain device
identifiers and app inventory.

After creating the dedicated secondary Android user, find its numeric ID with
`adb shell pm list users`. Repeat audit and planning with
`--user-id ID`; this writes `audit-user-ID.json` and
`plan-user-ID.json`, and targets only that Android user. Review those
actions independently. The default remains user 0.

For an offline fixture, put `package:com.example.name` lines in a text file
and run:

```bash
python3 scripts/tablet_device_isolation.py --prepare-env --mock-packages packages.txt --dry-run
```

Mock input cannot be applied to a device. An absent or ambiguous ADB target
is an error rather than an implicit target switch.

## 3. Account isolation checklist

1. Keep the designated account out of Android Settings > Accounts,
   Google Play, Gmail, Chrome, and other apps on the owner profile. Do not
   add it as a device-wide Google account.
2. Create a dedicated Android secondary user from Settings > Additional
   settings > Multiple users (menu names vary by HyperOS version). Give that
   user a screen lock. Install only Edge and essentials needed for this account.
   Do not sign other Google or Microsoft accounts into that user. After the
   user exists, audit its numeric ID as described above.
3. If this ROM does not offer a secondary user, use a separate Android device
   for strict isolation. A separate Edge tab, InPrivate window, or browser
   profile inside the existing owner user does not isolate Android app data,
   system services, notifications, clipboard, or device identifiers.
4. In the dedicated user, install Microsoft Edge from a trusted store and
   confirm package name `com.microsoft.emmx`. Leave Edge/Microsoft sync off.
   Disable saved payment details, unnecessary autofill, and notification
   permission. Use only this account in Edge.
5. Navigate to Google's sign-in page in Edge's address bar. Decline prompts
   to add the account to Android or open it in another app. Disable supported
   link opening for Google apps in Android settings if links are being handed
   off. Keep recovery and two-step verification methods current.
6. Avoid repeatedly switching accounts, browsers, IP regions, VPN endpoints,
   or devices during the observation period. These controls do not bypass
   Google's risk decisions. If sign-in is unnecessary during the cooling period,
   wait until the period ends.
7. Keep the tablet updated, use a strong device lock, and revoke USB debugging
   authorization after setup. Review active sessions and security alerts in
   the Google account's Security page.

Edge in a dedicated Android user is the browser container. Normal browsing
retains its cookies for the next session; InPrivate clears local session data
when all private tabs close. Neither mode prevents the signed-in site from
seeing that device or its network.

## 4. OEM and AppOps plan

Review `plan.json` before applying. The only automatic package actions are:

```bash
adb shell pm disable-user --user 0 com.miui.misightservice
adb shell pm disable-user --user 0 com.xiaomi.security.onetrack
```

The script does not uninstall packages. It never targets `android`,
`com.android.systemui`, `com.android.settings`, telephony/emergency
components, or Qualcomm System Helper. Disabling OEM services can still
affect diagnostics or other ROM behavior; reboot and check normal operation
after changes. User-0 controls do not automatically apply to a newly
created secondary Android user. Use `--user-id ID` for that user's package
and AppOps plan, then review its `plan-user-ID.json`.

When Xianyu or Taobao is installed and enabled in user 0, the plan includes
`cmd appops set --user 0 PACKAGE READ_CLIPBOARD ignore` and
`cmd appops set --user 0 PACKAGE SYSTEM_ALERT_WINDOW ignore`. It probes
support and records each prior mode for rollback. Clipboard restrictions
can break paste flows; overlay restrictions can break floating UI. Android
versions may enforce clipboard access differently, so verify behavior on
device. Package updates may reset AppOps.

To apply only the allowlisted actions after review:

```bash
python3 scripts/tablet_device_isolation.py --prepare-env --apply
# After reviewing the separate user's plan:
python3 scripts/tablet_device_isolation.py --prepare-env --apply --user-id ID
```

The plan is written before mutation. The script rechecks device identity,
package state, and AppOps state before executing. If an action fails midway,
inspect the applicable plan file and reverse only actions that actually
succeeded.

Rollback for the two OEM candidates:

```bash
adb shell pm enable --user 0 com.miui.misightservice
adb shell pm enable --user 0 com.xiaomi.security.onetrack
```

For AppOps, use the corresponding `rollback` array in `plan.json`, which
restores the mode captured immediately before applying. Rerun
`--audit` after a reboot and confirm affected apps and system functions.

## 5. Private DNS

On the tablet, open Settings > Connection & sharing > Private DNS (or search
Settings for "Private DNS"). Choose **Private DNS provider hostname**, enter
the hostname from a trusted DNS-over-TLS provider that works on the current
network, save, and test web browsing. `dns.google` and
`one.one.one.one` are example provider hostnames, not endorsements or
account-specific filters. If the network blocks DNS-over-TLS, choose a
reachable provider or return to Automatic. A custom blocklist needs a
provider that supports it; the Private DNS field accepts only a hostname.

Verify the setting and connectivity:

```bash
adb shell settings get global private_dns_mode
adb shell settings get global private_dns_specifier
```

Expected mode for a configured hostname is `hostname`; the specifier should
match the hostname entered. A VPN may handle DNS separately. Private DNS
does not isolate Google accounts or prove that OEM telemetry is blocked.

## 6. Test command

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```
