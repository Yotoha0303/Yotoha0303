# Android privacy guard

Requires Python 3 and ADB for a live device. All package inventory commands are
read-only. The default debloat mode is reversible `pm disable-user --user 0`.
`--apply-lockdown` is the only flag that changes device state. Use `--serial` if
more than one authorized ADB device is attached.

```bash
python3 scripts/android_privacy_guard.py --scan --dry-run
python3 scripts/android_privacy_guard.py --scan --generate-script
python3 scripts/android_privacy_guard.py --apply-lockdown --dry-run
python3 scripts/android_privacy_guard.py --apply-lockdown
python3 scripts/android_privacy_guard.py --scan --ingest-feed
```

Artifacts default to `var/android-privacy-guard/`: `report.json`, an executable
`lockdown.sh`, and `adguard.txt`, `nextdns.txt`, and `clash.rules`. Set `--output`
to another directory. The generated script requires `ANDROID_SERIAL` in its
environment. `--debloat-mode uninstall` selects `pm uninstall -k --user 0`; use
it only after reviewing the generated plan. `--domain` adds an exact endpoint
to the exports. These rules are candidate blocks, not observed traffic.

Android's Private DNS setting accepts a provider hostname, not a blocklist file.
Import `adguard.txt` or `nextdns.txt` into the relevant provider account and set
that provider's hostname on Android. `clash.rules` contains rules for a local
Clash configuration. Provider syntax and availability can vary by plan/version.

The guard only auto-selects two exact OEM packages for debloat. Xianyu and
Taobao receive four AppOps controls when installed. Other matched OEM services
are reported for review; package presence alone does not prove telemetry or
notification access. Unsupported AppOps are skipped. Blocking clipboard access
can also affect normal paste behavior, and app updates may reset AppOps.

For CI or an unplugged device, pass `--mock-packages PATH` with `pm list packages`
style lines such as `package:com.miui.daemon`. It never calls ADB. Combine it
with `--dry-run` to simulate an apply. Feed ingestion is explicit with
`--ingest-feed [PATH]`; the default path is
`data/instabox-personal-privacy-feed.json`. The sanitizer validates, redacts,
and checksums the resulting threat records.

Run the repository tests with:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```
