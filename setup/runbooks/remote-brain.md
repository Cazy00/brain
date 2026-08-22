# remote brain — the VPS operator runbook

The production brain is one always-on VPS behind Cloudflare Access. Every
client — Claude, Codex, ChatGPT, Gemini — reaches it as a remote MCP server at
`brain.qodevia.com/mcp` (read + capture) or `brain-read.qodevia.com/mcp`
(read-only). Nothing else writes to it.

**The one rule everything below protects: there is exactly one writer.** The
VPS owns `knowledge/`. A local clone is a reader and a recovery copy, never a
second authority. Two writable copies do not merge — they diverge, and the
divergence is only discovered later, in an answer that is quietly wrong. Every
recovery procedure here therefore ends with the old copy read-only, and none of
them ever ends with two.

Design: `docs/superpowers/specs/2026-08-23-remote-mcp-foundation-design.md`.
Observed behaviour and deviations:
`docs/superpowers/plans/2026-08-23-remote-mcp-foundation.md`.

## Where things live

| Path | Holds | Owner |
|---|---|---|
| `/srv/brain/engine` | this repo at a pinned tag — code, compose file, these scripts | root, `0755` |
| `/srv/brain/data` | the private knowledge git repository — **the only truth** | `10001`, `0700` |
| `/srv/brain/state` | index, `git.lock`, ledger, sessions, event log — all derived | `10001`, `0700` |
| `/srv/brain/backups` | the last three nightly encrypted archives | `10001`, `0700` |
| `/etc/brain` | config and secrets — never in either git repo | root, `0700` |
| `/var/lib/brain` | `alerts.log`, `drills/` | root, `0750` |

`/etc/brain` holds, all `0600` and root-owned unless noted:

| File | What |
|---|---|
| `brain-http.json` | endpoints, audiences, team domain, owner email, service principals |
| `backup.env` | S3-compatible endpoint, bucket, key id, secret |
| `backup-recipients.txt` | age **public** recipients, `0644` — the identities are offline |
| `deploy.env` | path overrides for the systemd units; optional |
| `tunnel-token` | the brain tunnel's connector token |
| `git-deploy-key` | write access to the private data repository, and to nothing else |

Nothing in `/etc/brain` is in any repository, and nothing in any repository
contains a value from it. `.env.example` documents names only.

## What runs when

| Timer | When (UTC) | Does |
|---|---|---|
| `brain-backup.timer` | 02:30 +30m jitter | `deploy/backup/snapshot.sh` — encrypted archive, upload, prune |
| `brain-maintenance.timer` | 03:20 +20m jitter | index, lint, doctor, in the pinned image |
| `brain-consolidate.timer` | Sun 04:10 +45m | the weekly consolidation pass, onto a branch |
| `brain-restore-drill.timer` | 2nd of the month 05:00 +1h | `restore-drill.sh --verify-only` |

Every one of them wires `OnFailure=brain-alert@%n.service`.

```sh
systemctl list-timers 'brain-*'          # next run, last run
journalctl -u brain-backup.service -n 50 # what happened
```

## Reading an alert

Start here, always. One line usually is the diagnosis.

```sh
tail -5 /var/lib/brain/alerts.log
# 2026-08-23T02:41:07Z brain-backup.service result=exit-code exit=78
```

Exit codes, from the scripts' own headers:

| Code | Meaning | Go to |
|---|---|---|
| 69 | a tool or a path is missing (data mount gone?) | procedure 6 |
| 70 | archive built and kept locally; the **upload** failed | procedure 1 step 9 |
| 70 (drill) | **the drill failed** — the backups are not proven | procedure 6 |
| 73 | the archive could not be built at all | procedure 6 |
| 75 | transient: lock held too long, or no full drill in 60 days | procedure 10 |
| 77 | refused: a credential was about to enter the archive | procedure 8 |
| 78 | offsite storage was never configured | procedure 1 step 9 |

---

## 1. First deployment

Reference host: Oracle Cloud Ampere A1, Ubuntu 24.04 LTS, aarch64.
Budget about two hours. Steps 1–6 are reversible; step 10 is the cutover.

**1. Never lose the way in.** Before touching anything, open a *second* SSH
session and leave it open. Do not close it until the end. Never close SSH
access until a second, tested way in already works.

**2. Take a boot volume backup first.** Always Free includes five.

```sh
# In Oracle Cloud Shell, not on the VPS:
oci bv boot-volume-backup create --boot-volume-id <BOOT_VOLUME_OCID> \
    --type FULL --display-name brain-pre-deploy-2026-08-23 \
    --wait-for-state AVAILABLE
```

Full, not the default incremental: this is the pre-change baseline. It does not
require stopping the instance, and it must not — **never STOP+START an A1
instance.** A stop releases the capacity allocation and it may not come back.
Reboots use SOFTRESET.

**3. Install Docker** from Docker's own repository, deb822 form:

```sh
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker --version && docker compose version
```

Pin what you installed into the deployment log. Versions verified on arm64
noble on 2026-08-23: `docker-ce 5:29.7.2-1~ubuntu.24.04~noble`,
`containerd.io 2.3.3`, `docker-compose-plugin 5.5.0`.

Add the Docker origin to unattended-upgrades — its `Release` file has
`Origin: Docker`, `Label: Docker CE`, `Suite: noble` and **no Codename**, so
match on `o=` / `l=` / `a=`, never on `n=`:

```sh
grep -n 'Docker' /etc/apt/apt.conf.d/50unattended-upgrades
# "origin=Docker,label=Docker CE,archive=noble";
```

**4. The firewall, and the reason there is no port to open.**

Docker's published ports bypass the host firewall: it DNATs before `INPUT`, so
an `iptables` REJECT in the filter table never sees the packet. Filtering for
container traffic belongs in `DOCKER-USER`, matched with conntrack because the
DNAT has already happened.

This deployment sidesteps all of it by publishing **no host port at all**.
`cloudflared` runs inside the compose project and reaches `brain:8787` over the
private compose network. Verify, from a machine that is not the VPS:

```sh
nmap -Pn -p 8787 <VPS_IP>      # expect: filtered/closed, never open
```

> **Footgun.** Never run `netfilter-persistent save` or
> `iptables-save > /etc/iptables/rules.v4` while Docker is running. It
> snapshots Docker's generated chains into the persisted rules, and the next
> boot replays stale NAT rules for containers that no longer exist. Edit the
> rules files by hand, or save them with Docker stopped.

**5. Users and directories.**

```sh
sudo groupadd -g 10001 brain || true
sudo useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin -d /srv/brain brain || true
sudo mkdir -p /srv/brain/{data,state,backups} /etc/brain /var/lib/brain/drills
sudo chown -R 10001:10001 /srv/brain/{data,state,backups}
sudo chmod 700 /srv/brain/{data,state,backups} /etc/brain
sudo chmod 750 /var/lib/brain
```

`/var/lib/brain/drills` must exist before the drill timer runs: it is in the
unit's `ReadWritePaths=`, and systemd refuses to start a unit whose
`ReadWritePaths=` names a path that is not there.

**6. The engine, at a tag — never a branch.**

```sh
sudo git clone --branch <RELEASE_TAG> --depth 1 \
     https://github.com/<owner>/brain.git /srv/brain/engine
sudo git -C /srv/brain/engine rev-parse HEAD    # record this
```

**7. The private data repository.** Move the existing brain across; do not
recreate it. Preserve its history — the history *is* the audit trail.

```sh
# On the Mac, from the current brain, with everything committed and pushed:
bin/brain lint && bin/brain doctor && git push
git bundle create /tmp/brain-migrate.bundle --all
scp /tmp/brain-migrate.bundle <VPS>:/tmp/
# On the VPS:
sudo -u brain git clone /tmp/brain-migrate.bundle /srv/brain/data
sudo -u brain git -C /srv/brain/data remote set-url origin <PRIVATE_GIT_URL>
shred -u /tmp/brain-migrate.bundle
```

Compare an inventory and hashes before believing it:

```sh
# both sides, same numbers or stop:
git -C <root> rev-parse HEAD
git -C <root> rev-list --count HEAD
git -C <root> ls-files -- 'knowledge/*.md' | wc -l
git -C <root> ls-files -- 'knowledge/*.md' | sort | xargs sha256sum | sha256sum
```

**8. Configuration and secrets.** Write `/etc/brain/*` by hand, on the box, as
root, `0600`. Never through a file that touches either repository, never as a
command-line argument, never pasted into a shell that logs history.

```sh
sudo install -m 0600 /dev/null /etc/brain/brain-http.json
sudo nano /etc/brain/brain-http.json
```

```json
{
  "team_domain": "<TEAM_DOMAIN>.cloudflareaccess.com",
  "owner_email": "<OWNER_EMAIL>",
  "endpoints": [
    {"hostname": "brain.qodevia.com",      "aud": "<AUD_RW>", "profile": "capture"},
    {"hostname": "brain-read.qodevia.com", "aud": "<AUD_RO>", "profile": "read"}
  ],
  "service_principals": {"<SERVICE_TOKEN_COMMON_NAME>": "read"},
  "principal_key_file": "/run/secrets/principal-key",
  "git_push": true,
  "log_level": "info"
}
```

The age recipients file is the one thing here that is public by nature:

```sh
# On the Mac — the identity NEVER goes to the VPS:
cat setup/vault-recipient.txt        # or a second, DR-only age keypair
# On the VPS:
sudo install -m 0644 /dev/null /etc/brain/backup-recipients.txt
sudo nano /etc/brain/backup-recipients.txt      # one age1... per line
```

Put **two** recipients in it if you can — a second offline identity, kept
somewhere the first one is not. `age -R` encrypts to all of them, and one lost
identity then costs nothing instead of costing everything.

**9. Offsite storage.** R2 is not enabled on this account yet, so until it is,
`snapshot.sh` exits 78 every night, keeps the local encrypted archive, and
refuses to write a success stamp. That is the designed behaviour and the alert
is correct: one disk is not a backup.

```sh
# Create the bucket and an API token scoped to it alone, then:
sudo install -m 0600 /dev/null /etc/brain/backup.env
sudo nano /etc/brain/backup.env
```

```sh
BRAIN_BACKUP_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
BRAIN_BACKUP_BUCKET=<BUCKET>
BRAIN_BACKUP_REGION=auto
BRAIN_BACKUP_PREFIX=brain
BRAIN_BACKUP_ACCESS_KEY_ID=<KEY_ID>
BRAIN_BACKUP_SECRET_ACCESS_KEY=<SECRET>
```

Prove it before trusting it:

```sh
sudo -u brain BRAIN_BACKUP_CONF=/etc/brain/backup.env \
     /srv/brain/engine/deploy/backup/snapshot.sh --dry-run   # builds, no upload
sudo systemctl start brain-backup.service                     # the real thing
cat /srv/brain/state/backup-last-success.json
```

**10. Cloudflare.** The brain gets its **own** tunnel, run as a container in
this compose project. It does not join the existing host `workhorse` tunnel:
every replica of one tunnel shares one ingress list, so joining would put the
brain's routes in the same configuration as five unrelated dev hostnames — and
the first step of incident containment is stopping the brain's connector
without touching anything else.

Rules that break deployments, in the order they bite:

- The tunnel ingress `PUT` **replaces the entire list.** Send every hostname
  every time, with the catch-all `{"service": "http_status:404"}` **last**.
- DNS records for tunnel hostnames must be `proxied: true`. Unproxied, Access
  never sees the request and the origin is exposed.
- Access applications: `PATCH` is rejected under API-token auth. Use `PUT`,
  which **replaces the whole object** — so `GET` first and reproduce every
  field you are not changing. A `PUT` that omits `policies` leaves policies
  intact; an application with **no** policies fails closed.
- Never send both `self_hosted_domains` and `destinations` on one application.
  They count additively against a five-destination cap.
- The two hostnames must be **two applications with two audiences.** There are
  no OAuth scopes to express read vs write, and a token issued through any one
  domain of a multi-domain application is valid for all of them — merging them
  would make a read-only token work against the capture endpoint.
- Access takes about **85 seconds** to begin enforcing after an application is
  created. A probe that succeeds immediately afterwards has proven nothing.

Managed OAuth settings, per the design: access token `15m`, grant session
`336h`, and the application-level `session_duration` also `336h` (the shorter
of the two wins, and a refresh that outlives the identity session forces a
re-login no client can explain). Dynamic client registration enabled, with
localhost and loopback redirects on — Claude Code, `mcp-remote` and the MCP
Inspector all use them.

**11. Bring it up.**

```sh
cd /srv/brain/engine/deploy
sudo docker compose --project-name brain up -d
sudo docker compose --project-name brain ps
curl -fsS localhost:8787/healthz     # from inside the VPS only
sudo docker compose --project-name brain exec brain /opt/brain/bin/brain doctor
```

`cloudflared` in a container binds its metrics server to `0.0.0.0` on
20241–20245 and serves `/ready` and `/healthcheck`. Reach it from the compose
network, not from the host, and never publish it.

**12. Install the timers.**

```sh
sudo cp /srv/brain/engine/deploy/systemd/*.service /etc/systemd/system/
sudo cp /srv/brain/engine/deploy/systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain-backup.timer brain-maintenance.timer \
     brain-consolidate.timer brain-restore-drill.timer
systemctl list-timers 'brain-*'
```

**13. Acceptance, before any client is repointed.** Every one of these, in
order, and stop at the first failure:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' https://brain.qodevia.com/mcp   # 302/401 from Access, never 200
```

- OAuth login works on both hostnames, as the owner and as nobody else.
- `tools/list` on `brain-read` returns exactly four tools; on `brain` exactly
  five.
- A hand-built `brain_capture` call against `brain-read` is refused.
- A canary capture commits, comes back `provisional`, survives a
  `docker compose restart brain`, and reaches the private git remote.
- `sudo systemctl start brain-backup.service` writes a success stamp.
- Procedure 10 (a full restore drill) passes on the Mac.

**14. Cutover.** Repoint every client to its endpoint, then make the local copy
read-only — see procedure 5, step 8. Do not skip it: two writable copies is the
one failure this whole design exists to prevent.

---

## 2. Upgrade to a new image

Never deploy a moving branch. Always a tag, always with a snapshot in front.

```sh
# 1. Confirm the ground is solid.
sudo docker compose -p brain exec brain /opt/brain/bin/brain doctor
sudo -u brain git -C /srv/brain/data status --porcelain      # expect empty
sudo -u brain git -C /srv/brain/data log origin/main..HEAD   # expect empty
cat /srv/brain/state/backup-last-success.json                # expect < 24h old

# 2. Pre-upgrade snapshot, on purpose and by hand.
sudo systemctl start brain-backup.service

# 3. Record what is running now — this is the rollback target.
sudo docker compose -p brain images --format '{{.Service}} {{.Repository}}@{{.ID}}'

# 4. Fetch the new engine and the new image.
sudo git -C /srv/brain/engine fetch --tags
sudo git -C /srv/brain/engine checkout <NEW_TAG>
sudo docker compose -p brain pull

# 5. Preflight against the real data, without serving traffic.
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     brain-maintenance lint
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     brain-maintenance doctor

# 6. Restart, and prove it.
sudo docker compose -p brain up -d
curl -fsS localhost:8787/readyz
sudo docker compose -p brain logs --since 5m brain
```

Then re-run the acceptance probes from procedure 1 step 13 — at minimum: OAuth
on both hostnames, the four/five tool split, and one canary capture.

Keep the previous tag and the pre-upgrade archive until the next drill passes.

---

## 3. Roll back a deployment

Rolling back software is safe. Rolling back *knowledge* is not, and these are
not the same operation.

```sh
sudo git -C /srv/brain/engine checkout <PREVIOUS_TAG>
sudo docker compose -p brain up -d
curl -fsS localhost:8787/readyz
```

**Do not restore the data volume as part of a software rollback.** Notes
captured by the newer version are real notes; discarding them to undo a code
change destroys knowledge to fix a bug. If the older engine genuinely cannot
read what the newer one wrote, that is a data migration and it needs its own
approved design — stop here and say so.

If the rollback is because the new version wrote something *wrong*, roll the
software back first, then fix the content as content: `bin/brain supersede`, or
a revert commit in `/srv/brain/data`. Never a destructive `git reset` on the
data repository.

---

## 4. Rotate an Access service token

Service credentials expire after 90 days. Rotate at day 83, not at day 90 —
the old token stays valid throughout, so there is no outage window.

```sh
# 1. Create the replacement. The secret is shown ONCE and is never retrievable.
curl -sS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/access/service_tokens" \
  -H "Authorization: Bearer $CF_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"name":"codex-cli-2026-08"}'
```

**2. Give it its own policy.** Decision `non_identity`, including exactly this
token:

```json
{"decision": "non_identity",
 "include": [{"service_token": {"token_id": "<NEW_TOKEN_UUID>"}}]}
```

Never `any_valid_service_token`. That include admits **every** service token in
the entire account — including one created next year for something unrelated —
to the brain. Ordering matters too: `service_auth_401_redirect` cannot be
enabled on the application until a Service Auth policy exists.

**3. Teach the origin the new principal.** Add the token's common name to
`service_principals` in `/etc/brain/brain-http.json`, mapped to the profile it
is allowed to use — `read` unless there is a reason it must capture — then
restart the brain service.

**4. Update the client**, and confirm it works with the new credential.

**5. Delete the old token and its policy.** Deleting a service token must block
that client immediately and affect no other credential and no OAuth session.
Verify both halves: the old credential now gets a 403, and a browser session on
the same endpoint still works.

Alert at seven days before expiry. A silently expired headless client looks
exactly like a broken server.

---

## 5. Recover a lost VPS

The target is service restored within two hours, losing at most 15 minutes of
accepted captures. Work in this order; it is ordered by what stops the bleeding
first, not by what is quickest.

**1. Do not terminate anything.** Not the instance, not its boot volume, not
the VCN, not the subnet, not the reserved IP. An A1 instance that is terminated
may be impossible to recreate at the same size. If the box is unreachable but
alive, try a SOFTRESET first — never STOP+START.

**2. Contain while you work.** If the host may be compromised rather than
merely dead, go to procedure 8 first and come back here at its step 5.
Otherwise, stop the tunnel so no client talks to a half-recovered brain:
disable the brain tunnel's route at Cloudflare, or leave the connector down.

**3. Find the newest recovery point.**

```sh
# From the Mac, where the identity lives:
BRAIN_DRILL_IDENTITY=~/.config/brain/dr-key.txt \
BRAIN_DRILL_ENGINE=~/Dev/brain \
  ~/Dev/brain/deploy/backup/restore-drill.sh --full
```

A passing full drill *is* the proof that the archive you are about to restore
is good. Do not skip it because the situation is urgent; restoring an archive
nobody has verified is how an outage becomes data loss.

**4. Prefer git over the archive when git is intact.** The private remote holds
every accepted capture, committed. The nightly archive is at most 24 hours old;
the remote is at most 15 minutes behind. Clone the remote, and use the archive
only for what the remote does not have.

**5. Build the replacement host** — procedure 1, steps 1 through 6 and 8
through 12. At step 7, instead of migrating, restore:

```sh
# from git (preferred):
sudo -u brain git clone <PRIVATE_GIT_URL> /srv/brain/data

# from the archive (when the remote is also gone — see procedure 7):
age -d -i <offline-identity> brain-<STAMP>.tar.gz.age | tar -xz -C /tmp/restore
sudo rsync -a --numeric-ids /tmp/restore/./ /srv/brain/data/
sudo chown -R 10001:10001 /srv/brain/data
rm -rf /tmp/restore
```

`/srv/brain/state` is **not** restored. Index, locks, ledger and sessions are
derived; `brain index` rebuilds them, and a restored lock file or a restored
idempotency ledger describes a machine that no longer exists.

**6. Rotate every secret the old host held.** A dead host's secrets are not
known-safe, and rotating them costs an hour once: tunnel credential, git deploy
key, S3 keys, and any service token. The age recipients file is public and
needs no rotation.

**7. Verify before announcing.** `doctor`, `lint`, both endpoints, the tool
split, a canary capture, and a git push.

**8. Make the old copy read-only, and prove it.** This is a required step, not
a tidy-up. Whatever the old data was — a rescued volume, the Mac's clone, a
copy on a laptop — it must not be able to write:

```sh
git -C <old-copy> remote remove origin          # it cannot push
chmod -R a-w <old-copy>                         # it cannot be edited
mv <old-copy> <old-copy>-READONLY-2026-08-23    # and it is named as what it is
```

No recovery step may leave the old local copy writable alongside the
replacement VPS. Two writers do not merge; they diverge silently and the brain
starts answering with whichever half it happens to read.

---

## 6. Recover corrupt data

"Corrupt" means one of four different things and they have four different
remedies. Identify which before touching anything.

```sh
sudo -u brain git -C /srv/brain/data status --porcelain
sudo -u brain git -C /srv/brain/data fsck --no-progress
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     brain-maintenance lint
ls -l /srv/brain/state
```

**a. The index is wrong or missing.** Not corruption. The index is derived and
disposable, and it is never truth.

```sh
sudo rm -f /srv/brain/state/index.db
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     brain-maintenance index
```

**b. Lint fails on content.** The notes are readable but malformed — usually a
dangling wikilink or a half-finished supersede. The system is already in
fail-closed maintenance mode for mutations, and reads keep working. Fix it as
content, on a branch, with `bin/brain supersede` or an ordinary commit. Never
delete an archived note to make lint pass; archived history is append-only.

**c. The working tree has unexpected changes.** An interrupted capture, or a
consolidation branch left checked out. Do **not** run a broad `git reset`, and
do not `git checkout -- .`: the uncommitted file may be the one capture that
was never committed anywhere else.

```sh
sudo -u brain git -C /srv/brain/data status
sudo -u brain git -C /srv/brain/data diff        # read it before deciding
sudo -u brain git -C /srv/brain/data stash -u    # preserve, do not discard
```

Then decide, note by note, what to keep.

**d. The git object store is damaged** (`fsck` reports missing or broken
objects). This is the real case. Stop the brain immediately so nothing writes
on top of it, then restore — the remote first, the archive second:

```sh
sudo docker compose -p brain stop brain
sudo mv /srv/brain/data /srv/brain/data.damaged-2026-08-23
sudo -u brain git clone <PRIVATE_GIT_URL> /srv/brain/data
sudo chmod -R a-w /srv/brain/data.damaged-2026-08-23   # keep it, read-only
sudo docker compose -p brain up -d brain
```

Keep the damaged copy read-only until a full drill has passed on the
replacement. It may hold the last few captures that never reached the remote,
and a damaged repository is still readable file by file.

If the drill (procedure 10) is what failed rather than the live data, the live
data is fine and the *backups* are not: fix the archive path before the next
incident needs it.

---

## 7. Lost private git remote

The remote is layer three: server-pushed history, audit, and offsite backup. It
is not the truth. `/srv/brain/data` is.

```sh
sudo -u brain git -C /srv/brain/data log origin/main..HEAD   # what is unpushed
```

**1. Do not panic-push anywhere.** A repository this size fits in a bundle, and
pushing to a hastily-created public repository is how the whole brain leaks.

**2. Create the replacement private repository.** Private, empty, no README.
Confirm it is private *before* the first push:

```sh
gh repo create <owner>/<new-private-name> --private
gh repo view <owner>/<new-private-name> --json visibility
```

**3. Repoint and push.**

```sh
sudo -u brain git -C /srv/brain/data remote set-url origin <NEW_PRIVATE_GIT_URL>
sudo -u brain git -C /srv/brain/data push --all
sudo -u brain git -C /srv/brain/data push --tags
```

**4. New deploy key**, scoped to that repository and nothing else, written to
`/etc/brain/git-deploy-key`, `0600`, root-owned. Restart the brain service so
the push queue picks it up.

**5. Drain the queue and confirm.** A capture accepted while the remote was
gone was still committed locally and reported `backup_pending`; the queue
retries with backoff.

```sh
sudo -u brain git -C /srv/brain/data log origin/main..HEAD   # expect empty
sudo docker compose -p brain exec brain /opt/brain/bin/brain doctor
```

**6. If the old remote was deleted rather than lost**, assume its contents were
readable by whoever deleted it. Treat it as procedure 8.

---

## 8. Compromised credentials

Any of: a leaked service token, an OAuth session on a device you no longer
control, a stolen Cloudflare API token, a suspected VPS compromise. Assume the
worst reading of the evidence and work down this list **in this order**. Speed
matters more than tidiness until step 4 is done.

**1. Disable the tunnel.** This is first because it is the only step that stops
an attacker mid-request, and it needs no decision about which credential leaked.

```sh
sudo docker compose -p brain stop cloudflared
```

Then, at Cloudflare, delete the brain tunnel's ingress routes or the tunnel
itself. Do not touch the host `workhorse` tunnel — it serves five unrelated
hostnames and it is not part of this deployment.

**2. Revoke access, both kinds.** OAuth sessions and service credentials are
separate systems and revoking one does nothing to the other:

- revoke the user's OAuth grants / active sessions for both Access
  applications;
- delete every Access service token that could reach either endpoint;
- if the Cloudflare API token may be involved, roll it too — it can rewrite the
  Access policies you are relying on.

An application with no policies fails closed, which makes "delete every policy"
a legitimate emergency brake.

**3. Preserve the evidence, encrypted, before changing anything else.**

```sh
sudo systemctl start brain-backup.service       # today's state, encrypted
sudo journalctl -u 'brain-*' --since '7 days ago' > /tmp/brain-journal.txt
sudo docker compose -p brain logs --no-color --since 168h > /tmp/brain-logs.txt
sudo tar -cz -C /tmp brain-journal.txt brain-logs.txt \
  | age -R /etc/brain/backup-recipients.txt -o /tmp/brain-incident-2026-08-23.tar.gz.age
shred -u /tmp/brain-journal.txt /tmp/brain-logs.txt
```

Encrypted to the same offline recipients, for the same reason: a diagnostic
bundle from a compromised host is a second copy of everything the attacker was
after. Move it off the box, then remove it.

**4. Rotate every secret.** Not the suspected one — every one:
tunnel credential, git deploy key, S3 access keys, service tokens, the
Cloudflare API token. Anything that was on that host is now public until proven
otherwise, and proving it is more expensive than rotating.

**5. Restore onto a clean host.** Do not clean the old one in place. Build a
new VPS by procedure 5 and restore from git or from an archive created
*before* the compromise window — the drill (procedure 10) is how you find out
which archives predate it.

**6. Keep the old host, powered off, until the incident is closed.** Never
terminate it: it is the evidence, and terminating an A1 instance may mean never
getting the capacity back.

**7. The old data copy is read-only.** Same rule as everywhere else, and it
matters most here: a compromised copy that can still push is a compromised
remote.

**8. Rebuild the trust boundary before reopening.** Both Access applications
recreated with two distinct audiences, owner-only policies, Managed OAuth
settings restored, tunnel routes proxied. Wait the ~85 seconds for Access to
start enforcing, then re-run the acceptance probes from procedure 1 step 13.
Only then bring `cloudflared` back up.

---

## 9. Bad software deployment

The service is up but wrong: refusing valid tokens, returning errors, capturing
into the wrong place, or leaking something it should not.

**1. Decide in one minute whether it is serving or mutating.** A read bug is
bad. A write bug is urgent, because every minute of it is committed history.

```sh
sudo docker compose -p brain logs --since 15m brain | tail -50
sudo -u brain git -C /srv/brain/data log --oneline -20
```

**2. If it is mutating wrongly, stop the writer, not the reader.** The
read-only endpoint can keep serving:

```sh
sudo docker compose -p brain stop brain
```

**3. Roll the software back** — procedure 3. Do not roll the data back.

**4. Assess what the bad version wrote.** Every accepted capture is a commit,
which is exactly why this is recoverable:

```sh
sudo -u brain git -C /srv/brain/data log --since '<when the deploy happened>' \
    --stat --oneline
```

Wrong notes are fixed as content — supersede them, or revert those specific
commits. Never a destructive reset, and never an edit to an archived note's
body.

**5. Confirm the credential scanner did its job.** If the bad version could
have written a secret into a note, treat it as procedure 8 as well: git history
makes it effectively permanent, and the remote is a second copy.

**6. Write down what happened before you sleep.** The deploy that broke it, the
symptom, the fix, and the check that would have caught it. That last one is the
note worth having — capture the why, not the what.

---

## 10. Full restore drill (monthly, on the machine with the identity)

The VPS runs a verify-only drill monthly and can do no more: nothing on that
host can decrypt what it uploads, which is the property that makes the archives
worth having. The real drill runs where the offline identity is.

```sh
BRAIN_DRILL_IDENTITY=~/.config/brain/dr-key.txt \
BRAIN_DRILL_ENGINE=~/Dev/brain \
BRAIN_BACKUP_CONF=~/.config/brain/backup.env \
BRAIN_DRILL_REPORT_DIR=~/.config/brain/drills \
  ~/Dev/brain/deploy/backup/restore-drill.sh --full
```

It downloads the newest archive, restores it into a scratch directory the live
brain cannot see, lints it, rebuilds the index, checks git history against the
manifest, runs read/links/search canaries and a trust-boundary canary, destroys
the restored copy, and publishes a verdict — counts and pass/fail only, never a
note id or a query — back to the bucket. The VPS reads that verdict; a full
drill older than 60 days turns the monthly verify-only run red.

A failing drill means the backups are not proven. Fix it the same week: an
untested backup is a belief, and the day you need it is the worst possible day
to discover which one it was.

---

## Never, on this deployment

- Never terminate the instance, or delete a boot volume, VCN, subnet or
  reserved IP. Reboot with SOFTRESET; **never STOP+START** an A1 instance.
- Never close SSH until a second, tested way in already works.
- Never publish a host port for the brain, and never widen a rule to
  `0.0.0.0/0` without asking.
- Never run `netfilter-persistent save` while Docker is running.
- Never put a credential, a real owner email, an account id, an audience or an
  application id into either git repository.
- Never put the age recovery identity on the VPS. `restore-drill.sh` refuses
  one stored under `/etc/brain`, `/srv/brain` or `/run/secrets`, and that
  refusal is load-bearing.
- Never leave two writable copies of `knowledge/` in existence.
- Never make the current private data repository public in place. Publication
  is an allowlist export into fresh history — deleted files stay in git
  history forever.
