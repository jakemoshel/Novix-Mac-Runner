# The Novix Mac runner

This is the machine that does what the Linux container cannot: compile an Xcode
target, and photograph a page in real Safari.

Its source lives in the private product repository beside the backend it talks to, so
the two version together. After the main repository's gates pass, GitHub publishes
only this folder to the public
[`Novix-Mac-Runner`](https://github.com/jakemoshel/Novix-Mac-Runner) distribution
repository. It is stdlib-only Python 3 — the `python3` that comes with Xcode's Command
Line Tools runs it as it stands. No pip, no virtualenv, nothing to keep alive.

## What it does

Novix's sandbox clones a repo and runs the customer's own build and tests in an E2B
Linux container. Two jobs it can never do there:

- **Build an Apple target.** `xcodebuild` is macOS-only. A container run on an Xcode
  project stops at an honest boundary ("this is an Xcode project, and iOS and macOS
  targets build on Apple hardware") and no verdict is produced.
- **Photograph the page in Safari.** WebKit-on-Linux is a stand-in, and a stand-in
  filed under a Safari label is evidence of something that never happened.

So this Mac polls Novix for those jobs, does them, and posts the result back. Novix
never calls this machine — which is why it needs no port forwarding, no static IP, no
tunnel and no SSH key stored anywhere.

## Set it up

### 1. Set the token on the server

In Render, set `NOVIX_MAC_RUNNER_TOKEN` to a long random string. Until it is set,
every `/api/mac-runner/*` route answers 404 and this runner has nothing to talk to.

```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Install Xcode on the Mac

The full Xcode, not just the Command Line Tools — `xcodebuild -version` has to work.
Open it once and accept the licence, then:

```
sudo xcodebuild -license accept
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -runFirstLaunch
```

Install at least one iOS Simulator runtime (Xcode > Settings > Components). Without
one, builds still run against `generic/platform=iOS Simulator`, but tests have nowhere
to execute and the runner correctly reports **no test verdict** rather than a failure.

### 3. Turn on Safari automation (only if you want Safari screenshots)

```
sudo safaridriver --enable
```

Then in Safari: Settings > Advanced > "Show features for web developers", and
Develop > Allow Remote Automation.

Skip this and the runner reports `safari: false` at check-in, Novix never queues it a
screenshot job, and the "Safari on macOS" card stays hidden — or, if this repository
had already selected Safari, visible with the reason on it.

**No restart needed if the runner is already going.** It re-probes anything that is
off every ten minutes and the next check-in carries the new answer. That was NOT true
before 2026-08-31: capabilities were read once at startup, so enabling Safari did
nothing until somebody restarted it, while this file and the card both promised
otherwise.
Xcode builds are unaffected.

### 4. Run it

```
export NOVIX_URL=https://app.getnovix.ai
export NOVIX_MAC_RUNNER_TOKEN=<the same string you set in Render>
python3 runner/novix_mac_runner.py --once
```

`--once` takes at most one job and exits, which is the right way to see it work the
first time. It prints what it can do on startup:

```
[novix-mac-runner] Mac mini (mac-26b772fefd155093): xcode=Xcode 16.2 safari=True
[novix-mac-runner] connected to https://app.getnovix.ai; beating every 75s
```

Then check `https://app.getnovix.ai/api/health` — the `macRunner` block names this
machine, and the "Safari on macOS" card on the Sandbox settings page goes live.

### 5. Keep it running

`bash install.command` does all of this for you, including the one trap below. What
it writes by hand is described here so the plist is readable rather than magic.

The installed runner also keeps itself current. Every five minutes, while it is
between jobs, it checks the public runner-only repository. Public means anybody can
audit and download it, not that anybody can publish an update: the runner embeds the
Novix release public key and rejects a manifest without its RSA/SHA-256 signature.
Only the matching private signing key in the private product repository can produce
one. A newer version is then accepted only when its manifest version, embedded version
and SHA-256 all agree; the candidate must compile and answer `--version` under the
Mac's own Python before it atomically replaces `~/novix/novix_mac_runner.py`. The
previous file remains at
`~/novix/novix_mac_runner.py.previous` for manual recovery. Set
`NOVIX_MAC_RUNNER_AUTO_UPDATE=0` in the LaunchAgent only when deliberately pinning a
machine. Source checkouts never update themselves — only the installer's exact
`~/novix/novix_mac_runner.py` destination does.

This means version 1.5.0 needs one final manual reinstall to gain the signed updater. Later
runner releases do not need another AirDrop or installer run.

**NEVER RUN IT FROM ~/Desktop, ~/Documents OR ~/Downloads.** Those are TCC-protected,
and a launchd agent does not inherit the access the Terminal that started it has, so
the runner works perfectly when a person runs it and fails from launchd with

```
can't open file '/Users/you/Desktop/.../novix_mac_runner.py': [Errno 1] Operation not permitted
```

forever, with `KeepAlive` turning that into a restart loop. It is `Operation not
permitted` rather than "no such file", which is the tell. `~/novix` is not protected,
which is where the installer puts it. Measured on a real mini, 2026-08-31, after
AirDrop landed the folder on the Desktop.

A LaunchAgent, so it starts at login and comes back if it dies. Save as
`~/Library/LaunchAgents/ai.getnovix.macrunner.plist`, with the paths and the token
filled in:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.getnovix.macrunner</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/Novix-main/runner/novix_mac_runner.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NOVIX_URL</key><string>https://app.getnovix.ai</string>
    <key>NOVIX_MAC_RUNNER_TOKEN</key><string>PUT_THE_TOKEN_HERE</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/novix-mac-runner.log</string>
  <key>StandardErrorPath</key><string>/tmp/novix-mac-runner.log</string>
</dict>
</plist>
```

```
launchctl load ~/Library/LaunchAgents/ai.getnovix.macrunner.plist
tail -f /tmp/novix-mac-runner.log
```

### Running more than one

One agent takes one job at a time. On a machine that stays powered on, run several by
giving each a `--worker` name, and install one LaunchAgent per worker:

```
python3 novix_mac_runner.py --worker a
python3 novix_mac_runner.py --worker b
```

The name is folded into the runner id, so each registers as its own row and a worker
that dies shows up as gone instead of being masked by its twin. Claiming was always
safe without this — that takes the ticket row's lock — so what `--worker` buys is
throughput and visibility, not correctness. Size it by cores and disk, not by
ambition: each concurrent job is a full clone plus an Xcode build.

**Stop the Mac sleeping**, or it stops checking in and the Safari card correctly goes
back to "Connect a Mac to run this":

```
sudo pmset -a sleep 0 disablesleep 1
```

## What the Mac is trusted with

Each claimed job carries a **repository token** for the workspace it belongs to, so
this machine can clone a private repo. That is the same trust boundary the E2B
container already has and it is deliberate — but it means this Mac should be treated
as production infrastructure, not a spare laptop: full-disk encryption on, no shared
accounts, and the token in the LaunchAgent plist is a real credential.

Everything the runner posts back runs through `scrub()` before it leaves, and the
server scrubs again on arrival, because a guard on the far side of a network boundary
is not a guard.

## What it does to the machine

This is somebody's own Mac, so a job is not allowed to leave anything behind.

- **Every byte a job writes lands in its own `mkdtemp` directory**, removed in a
  `finally` whatever the outcome. That includes the two things `xcodebuild` puts
  elsewhere by default: `-derivedDataPath` and `-clonedSourcePackagesDirPath` point
  inside the job directory, because DerivedData is hundreds of megabytes per project,
  lives in `~/Library/Developer/Xcode`, and is never cleaned by anything.
- **It stops claiming below `MIN_FREE_DISK_GB` (20).** A full startup disk is the one
  failure that outlives the job causing it — it breaks the Mac for its owner, not just
  for Novix. An unclaimed job expires *unproven*, which is already the honest answer.
- **A simulator it boots for tests is shut down again.** On a machine that stays
  powered on for months, one idle simulator per tested repository adds up.
- **It never signs.** `CODE_SIGNING_ALLOWED=NO`, so it does not reach for a keychain
  or a certificate, and a signing refusal is never reported as a broken patch.
- **Nothing runs as root.** Only the one-time setup steps above use `sudo`.
- **One job at a time**, each bounded by its own timeout (clone 10 min, build and test
  25 min each), so a hung build cannot pin the machine indefinitely.

What it does *not* sandbox: an Xcode build phase is arbitrary shell, so a job really
does execute code from the repository it was given, exactly as the E2B container does.
Treat this Mac as production infrastructure — a dedicated non-admin account is the
right home for it, not your own login.

## When something looks wrong

`GET /api/health` → `macRunner` is the first place to look. It separates three states
that are easy to confuse:

| What it says | What it means |
|---|---|
| `configured: false` | `NOVIX_MAC_RUNNER_TOKEN` is not set in Render. The routes 404. |
| `connected: false`, with a `lastSeenAt` | The Mac checked in before and has stopped. Asleep, offline, or the agent died. |
| `connected: false`, no `lastSeenAt` | Nothing has ever checked in. Wrong token, wrong url, or the agent was never started. |
| `connected: true`, `canBuild: false` | The Mac is there, but Xcode is not installed on it. Screenshot jobs only. |

**A sleeping Mac is never a failing build.** A job nobody claims expires as *unproven*
and the ticket says so; it never reports `passed: false`, because nothing observed the
patch breaking. That distinction is load-bearing: Novix re-drafts a patch on a false
verdict, at the deep tier, so getting it wrong would burn a redraft on code that is
fine.

## The rules this runner holds

Four, and each is a way the feature could quietly lie:

1. **Never substitute a browser.** If Safari cannot be driven, no picture is taken.
2. **A step that did not run reports nothing, never a failure.** Every verdict is
   tri-state all the way to the server.
3. **The revert proves itself.** A `git checkout` of an unmodified file exits 0, so the
   "before" picture is only taken once `git status` confirms the tree really moved.
   Otherwise the "before" is the patched app under the wrong label.
4. **The clone token never reaches a log.**

The server-side half of all of this is `backend/app/mac_runner.py`; why macOS is not
run in the sandbox at all is settled in `backend/app/apple_ci.py`, and must not be
re-litigated.
