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
| `cf-api.env` | `CLOUDFLARE_API_TOKEN=…` — one line, for `provision.py` and the daily edge check. Scoped to this account's Access, Tunnel and DNS resources and to nothing else |
| `cloudflare.json` | the declared edge state `provision.py` reconciles against — account, zone, team domain, owner address, applications, notifications |
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
| `brain-edge-check.timer` | 06:40 +45m | `provision.py --check` — the Cloudflare side, read-only |

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

`brain-edge-check` has its own two, from `provision.py`:

| Code | Meaning | Go to |
|---|---|---|
| 2 | a human owes an action at the edge — usually a service token with seven days left | procedure 4 |
| 4 | the account has DRIFTED from `/etc/brain/cloudflare.json`. Somebody edited the dashboard, or the state file changed and was never applied. Run the same command without `--check` to read the diff before deciding which side is right | procedure 13 |

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

**10b. Render the deployment files and install the connector credential.**
Nothing in `deploy/` is usable as shipped: three files are `.example`, and
`compose.yaml` refuses to start without them. All three are gitignored because
they carry the tunnel id, the audiences and the owner's address.

```sh
cd /srv/brain/engine/deploy
sudo cp env.example .env && sudo chmod 600 .env
sudo $EDITOR .env                       # BRAIN_VERSION and the four host paths
sudo cp cloudflared/config.yml.example cloudflared/config.yml
sudo $EDITOR cloudflared/config.yml     # replace <TUNNEL_ID>
```

The connector credential is the one file an agent cannot fetch for you — it is
a credential, and retrieving it is deliberately a human action. Take it from
**Zero Trust → Networks → Tunnels → brain → Configure**, and write it as a
credentials file, not a token, because this tunnel is locally configured:

```sh
sudo install -o 65532 -g 65532 -m 0600 /dev/null /etc/brain/tunnel-credentials.json
sudo $EDITOR /etc/brain/tunnel-credentials.json
#   {"AccountTag": "...", "TunnelID": "...", "TunnelSecret": "..."}
```

`65532`, not `10001`: the `cloudflared` image is distroless and runs as its own
nonroot uid, and a bind-mounted secret carries the host's ownership straight
through. A file owned by the brain's uid is unreadable to the connector, and
the error looks like a missing file rather than a permission one.

**10c. DNS, and it goes LAST.** Everything before this is invisible to the
world; this is the step that makes the hostnames reachable, so nothing that can
fail should be left after it. Access needs its ~85 seconds *before* a record
exists, not after.

`brain.qodevia.com` is a special case: it is **already** routed, by the host
`workhorse` tunnel, to the old service on `localhost:8787`. Repointing it is the
cutover, and it has an order:

1. Bring the new stack up and prove it on `brain-read.qodevia.com` first — a
   brand-new hostname with nothing depending on it. Create its proxied CNAME
   onto `<brain-tunnel-id>.cfargotunnel.com` and run the acceptance list below.
2. Only then touch `brain.qodevia.com`. `GET` the **workhorse** tunnel's
   ingress, remove *only* its `brain.qodevia.com` entry, and `PUT` the whole
   remaining list back with the catch-all last. Removing it from the old tunnel
   before repointing DNS means the hostname 502s for a few seconds rather than
   being served by two origins at once.
3. Convert the existing `brain` Access application from its `bypass` policy to
   Managed OAuth plus an owner-email Allow policy. Its audience does not change,
   so `/etc/brain/brain-http.json` needs no edit.
4. Repoint the `brain.qodevia.com` CNAME onto the brain tunnel, proxied.
5. Stop the old service and re-sync anything it captured in the meantime — see
   the note in procedure 1 step 7. Two writable copies is the one failure this
   design exists to prevent, so this step is not optional and not deferrable.

**11. Bring it up.**

```sh
cd /srv/brain/engine/deploy
sudo docker compose up -d
sudo docker compose ps

# There is NO host port to curl -- that is the point of the design. Reach the
# container on its address on the egress bridge, which is the same path the
# host-side monitoring uses.
EG=$(sudo docker inspect -f '{{(index .NetworkSettings.Networks "brain_egress").IPAddress}}' brain-brain-1)
curl -fsS "http://$EG:8787/healthz"      # {"status": "ok", ...}
curl -fsS "http://$EG:8787/readyz"       # proves the live Cloudflare JWKS fetch

sudo docker compose --profile maintenance run --rm maintenance doctor
```

`readyz` is the one that matters here: it returns 200 only after the container
has reached `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, parsed
the RSA keys and found the data mount, git and the locks all usable. A 200 from
`healthz` alone only says a Python process is answering.

`cloudflared` in a container binds its metrics server to `0.0.0.0` on
20241–20245 and serves `/ready` and `/healthcheck`. Reach it from the compose
network, not from the host, and never publish it.

**12. Install the timers.**

```sh
sudo cp /srv/brain/engine/deploy/systemd/*.service /etc/systemd/system/
sudo cp /srv/brain/engine/deploy/systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain-backup.timer brain-maintenance.timer \
     brain-restore-drill.timer brain-edge-check.timer
systemctl list-timers 'brain-*'
```

There is deliberately **no consolidation timer**. The pinned runner is the
`claude` CLI and the production image does not contain it, so a unit would have
failed every week while looking scheduled. `deploy/systemd/README.md` has the
three options and the reason the gap is currently visible rather than silent:
`doctor` goes RED on an inbox over 25 items or an oldest item past 21 days, so
consolidation is a manual step with an alarm on it.

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

**Cutting the release, on the development machine.** One number, in one file,
and everything else reads it: `bin/brainlib/version.py`. The git tag is `v` +
that string, the image tag IS that string, and the server reports it to clients
as `serverInfo.version`. Bump it in the same commit that cuts the release —
a version claimed on a branch and never tagged is worse than none.

```sh
$EDITOR bin/brainlib/version.py            # VERSION = "0.2.0"
python3 -m unittest discover -s tests      # ReleaseVersionTests checks the agreement
python3 bin/brain lint
git commit -am "release: 0.2.0"
V=$(python3 -c 'import sys; sys.path.insert(0, "bin"); from brainlib import version; print(version.VERSION)')
git tag -a "v$V" -m "brain v$V"
git push --tags
```

Read `$V` out of the file rather than typing it twice. Typing it is how the tag
and the image come to disagree, and they disagree silently.

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

# 4. Fetch the new engine and build the new image.
#    `compose pull` is WRONG here and fails: the brain image is built on this
#    box and never pushed anywhere, so there is no registry to pull it from.
#    Only cloudflared comes from a registry, and it is pinned by tag in the
#    compose file.
sudo git -C /srv/brain/engine fetch --tags
sudo git -C /srv/brain/engine checkout <NEW_TAG>
V=$(sudo python3 -c 'import sys; sys.path.insert(0, "/srv/brain/engine/bin"); from brainlib import version; print(version.VERSION)')
sudo docker build --pull -f /srv/brain/engine/deploy/Dockerfile \
     --build-arg BRAIN_VERSION="$V" -t "brain:$V" /srv/brain/engine
sudo sed -i "s/^BRAIN_VERSION=.*/BRAIN_VERSION=$V/" /srv/brain/engine/deploy/.env
sudo docker compose -p brain pull cloudflared

# 5. Preflight against the real data, without serving traffic.
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     maintenance lint
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     maintenance doctor

# 6. Restart, and prove it. There is no host port -- that is the design -- so
#    the probe goes to the container's address on the egress bridge, which is
#    the same path the host-side monitoring uses.
sudo docker compose -p brain up -d
EG=$(sudo docker inspect -f '{{(index .NetworkSettings.Networks "brain_egress").IPAddress}}' brain-brain-1)
curl -fsS "http://$EG:8787/readyz"
sudo docker compose -p brain logs --since 5m brain
```

**7. Release evidence, before anyone is told it shipped.** A handback owes an
image identity, a bill of materials and a vulnerability scan, and all three are
about the image that is now running rather than the one that was intended.

```sh
# Identity. A LOCAL content digest -- the OCI index digest under the containerd
# image store, the config digest under the classic one. NOT a registry manifest
# digest: nothing here is pushed, so none exists. Do not write it down as one;
# that claims an immutability this deployment does not have.
sudo docker inspect -f '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}}' "brain:$V"
sudo docker inspect -f '{{index .Config.Labels "org.opencontainers.image.base.name"}}' "brain:$V"
sudo docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim-bookworm  # what the base RESOLVED to

# Bill of materials. Refuses (77) if a third-party Python package ever appears
# in the image, which is a design invariant and not a preference.
sudo /srv/brain/engine/deploy/sbom.sh "brain:$V" > "/var/lib/brain/sbom-$V.json"

# Vulnerability scan. Through a saved tarball rather than by mounting
# /var/run/docker.sock into a scanner: this needs to READ one image, and a
# scan is not worth handing a third-party container control of the daemon.
sudo docker save "brain:$V" -o /tmp/brain-image.tar
sudo docker run --rm -v /tmp:/work:ro aquasec/trivy:latest image \
     --input /work/brain-image.tar --severity HIGH,CRITICAL --scanners vuln
sudo rm -f /tmp/brain-image.tar
```

The scan reports the BASE image's Debian packages -- there is nothing else in
there to report on. **Read the fix column, not the count.** A HIGH or CRITICAL
with a fixed version available is a reason to rebuild with `--pull` and ship
again; one with `affected`, `fix_deferred` or `will_not_fix` and no fixed
version is a line in the handback, because there is nothing to apply.

```sh
# The number that decides anything, rather than the total:
sudo docker run --rm -v /tmp:/work:ro aquasec/trivy:latest image \
     --input /work/brain-image.tar --severity HIGH,CRITICAL --scanners vuln \
     --quiet --format json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
      v=[x for r in d.get("Results") or [] for x in (r.get("Vulnerabilities") or [])]; \
      print(len(v), "HIGH/CRITICAL,", len([x for x in v if x.get("FixedVersion")]), "fixable")'
```

Most of what is reported arrives with `git`, which drags in perl and
util-linux. That is the price of the spec's rule that a capture is not
accepted until it is committed, and it is worth re-examining at a base-image
change -- not at every release.

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
EG=$(sudo docker inspect -f '{{(index .NetworkSettings.Networks "brain_egress").IPAddress}}' brain-brain-1)
curl -fsS "http://$EG:8787/readyz"
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

Rotate a week before the expiry `provision.py` reports, not on the day — the
old token stays valid throughout, so there is no outage window. The duration is
whatever was chosen at creation and is recorded in `/etc/brain/cloudflare.json`;
this deployment uses one year, and `expiry_warning_days` is the seven-day
alert. Do not trust a remembered number: `provision.py --check` reads the real
`expires_at` from the account, and a token whose expiry it cannot read is
reported as blocked rather than ok, because an unreadable expiry is a
credential with no rotation alarm at all.

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

**5. Delete the old token — POLICY FIRST, and that order is not optional.**
Cloudflare refuses to delete a service token while any policy still references
it:

```
12139 access.api.error.service_token_in_use: cannot delete service token
because it is used by a policy, group, or app SCIM configuration.
```

[verified 2026-08-23]. So: delete the token's own Service Auth policy, then the
token. Reaching for the token first gets an error that reads like a permissions
problem and is not one.

```sh
# 1. the policy that names this token
curl -sS -X DELETE -H "Authorization: Bearer $CF_API_TOKEN" \
  ".../accounts/<ACCOUNT_ID>/access/apps/<APP_ID>/policies/<POLICY_ID>"
# 2. only now, the token
curl -sS -X DELETE -H "Authorization: Bearer $CF_API_TOKEN" \
  ".../accounts/<ACCOUNT_ID>/access/service_tokens/<TOKEN_UUID>"
```

If you need the client blocked *without* unpicking the policy — an incident
rather than a rotation — **rotate** the token instead
(`POST .../service_tokens/<uuid>/rotate`). That invalidates every existing
client immediately and leaves the policy alone. The secret it returns is, once
again, shown only once.

**Verify both halves, and the second half is the one that matters.** The
revoked credential must be refused and every other credential must be
untouched:

```
BEFORE   brain-read-headless      -> HTTP 200  4 tools
         brain-revocation-canary  -> HTTP 200  4 tools
AFTER    brain-read-headless      -> HTTP 200  4 tools
         brain-revocation-canary  -> HTTP 401  Unauthorized      [verified 2026-08-23]
```

Access answers **401**, not 403, for a revoked service credential on a Managed
OAuth application — the same `invalid_token` shape an unauthenticated request
gets. Both credentials in that demonstration sat on the same endpoint with the
same profile, so the only thing separating them was the per-token policy. With
`any_valid_service_token` the revocation would have broken both or neither.

Alert at seven days before expiry — `brain-edge-check.timer` does this, and
Cloudflare's own `expiring_service_token_alert` does it independently. A
silently expired headless client looks exactly like a broken server.

**One more thing that looks like an auth failure and is not.** A client whose
User-Agent looks automated gets Cloudflare **error 1010** — "blocked access
based on your browser's signature" — as a 403 from the edge, *before* Access is
consulted [verified 2026-08-23: `Python-urllib/3.12` was refused; any ordinary
User-Agent string was not]. The 403 body is a Cloudflare error page, not an
Access challenge, which is how to tell the two apart at a glance.

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
     maintenance lint
ls -l /srv/brain/state
```

**a. The index is wrong or missing.** Not corruption. The index is derived and
disposable, and it is never truth.

```sh
sudo rm -f /srv/brain/state/index.db
sudo docker compose -p brain --profile maintenance run --rm --no-deps \
     maintenance index
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

## 11. Egress allowlist for the containers

The design limits outbound access "as practical" to the tunnel, the Cloudflare
JWKS, the private git provider, backup storage, registries during controlled
updates and the consolidator provider. Docker has no per-container egress ACL,
so this is a host firewall job — `compose.yaml` says so and points here.

**Which chain sees what.** This is the whole safety argument for trying it:

| Traffic | Chain | Covered here |
|---|---|---|
| container → internet | `FORWARD` → `DOCKER-USER` | yes |
| host → internet (`docker pull`, `apt`, the host connector) | `OUTPUT` | **no** — a different chain, deliberately out of scope |
| internet → SSH | `INPUT` | no — **nothing here can lock you out** |
| brain ↔ cloudflared, the MCP path itself | never leaves `brain_mcp` | nothing to do: the network is `internal: true` and has no gateway |

Row three is why this is safe to attempt at all. A mistake breaks the tunnel —
visible within seconds, one command to undo — and cannot cost you the way in.
Row two is why "registries during controlled updates" needs no rule: `docker
pull` is dockerd on the host, not a container.

**The current state is no control at all**, and it is recorded here as a
decision rather than left as an oversight:

```sh
sudo iptables -S DOCKER-USER
# -N DOCKER-USER      <- empty. Container egress is unrestricted. [2026-08-23]
```

**What actually has to get out** [verified 2026-08-23]:

| Service | Destination | Port |
|---|---|---|
| cloudflared | the Cloudflare edge | 7844/tcp **and** 7844/udp (QUIC) |
| brain | `<team>.cloudflareaccess.com`, for the Access JWKS | 443/tcp |
| brain | the private git remote, over SSH | 22/tcp |
| maintenance | the S3-compatible backup endpoint | 443/tcp |

DNS needs no rule. A container on a user-defined bridge resolves through
Docker's embedded server at `127.0.0.11` inside its own namespace, and dockerd
makes the upstream query from the HOST namespace — those packets are `OUTPUT`,
never `FORWARD` [verified 2026-08-23: `/etc/resolv.conf` in `brain-brain-1` is
`nameserver 127.0.0.11`]. Return traffic needs no rule either: replies arrive
`-i <wan> -o brain-egress` and never match an `-i brain-egress` rule. Adding a
conntrack ACCEPT "to be safe" is the usual way this ends up allowing more than
intended.

**Pin the interface name before writing a single rule.** Docker calls a bridge
`br-<first 12 of the network id>` and regenerates the id whenever the network
is recreated. `compose.yaml` therefore pins `com.docker.network.bridge.name:
brain-egress` and the subnet `10.77.1.0/24`. Applying that pin recreates the
network, so it lands on the next deploy:

```sh
cd /srv/brain/engine/deploy
sudo docker compose down && sudo docker compose up -d
ip -br link show brain-egress          # must exist before the rules mean anything
```

**One script owns every iptables rule this deployment adds.** Not
`netfilter-persistent save` — saving while Docker is running captures Docker's
generated chains and replays them at boot before Docker starts, which is
already in the "never" list at the end of this file. Every rule is tagged with
a comment so that clearing is exact and never touches anyone else's:

```sh
sudo tee /usr/local/sbin/brain-firewall >/dev/null <<'EOF'
#!/bin/sh
# Every iptables rule the brain deployment owns, in one place.
#
# Idempotent by construction: `apply` deletes its own rules before adding them,
# so running it twice leaves one copy of each. Deletion is by RULE SPEC, never
# by line number and never by parsing `iptables -S` -- Docker rewrites its
# chains on every container start, so a line number read a second ago points at
# something else by the time it is used, and `-S` output re-quotes arguments
# (`--log-prefix "brain-egress-drop "`) in a way that does not survive being
# split back into words.
set -eu
TAG=brain-fw
EG=brain-egress            # the pinned bridge name from compose.yaml, not br-<id>

# $1 is -A to add or -D to remove. Same specs both ways: that symmetry is the
# only reason `clear` is exact.
egress_rules() {
    op=$1
    iptables "$op" DOCKER-USER -i "$EG" -p tcp --dport 7844 -m comment --comment "$TAG" -j ACCEPT
    iptables "$op" DOCKER-USER -i "$EG" -p udp --dport 7844 -m comment --comment "$TAG" -j ACCEPT
    iptables "$op" DOCKER-USER -i "$EG" -p tcp --dport 443  -m comment --comment "$TAG" -j ACCEPT
    iptables "$op" DOCKER-USER -i "$EG" -p tcp --dport 22   -m comment --comment "$TAG" -j ACCEPT
    iptables "$op" DOCKER-USER -i "$EG" -m limit --limit 6/min \
             -m comment --comment "$TAG" -j LOG --log-prefix "brain-egress-drop "
    iptables "$op" DOCKER-USER -i "$EG" -m comment --comment "$TAG" -j DROP
}

apply() {
    # Errexit is suspended for a function on the left of `||`, so every delete
    # is attempted even though the first one usually fails on a clean chain.
    { egress_rules -D; } >/dev/null 2>&1 || true
    egress_rules -A          # -A, so the ACCEPTs precede LOG and DROP in order
}

case "${1:-}" in
    apply) apply ;;
    clear) { egress_rules -D; } >/dev/null 2>&1 || true ;;
    *) echo "usage: $0 apply|clear" >&2; exit 64 ;;
esac
EOF
sudo chmod 0755 /usr/local/sbin/brain-firewall
```

The delete loop is written against the rule text rather than line numbers on
purpose: Docker rewrites its chains on every container start, and a rule number
captured a second earlier is a rule number that now points at something else.

```ini
# /etc/systemd/system/brain-firewall.service
[Unit]
Description=iptables rules owned by the brain deployment
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/brain-firewall apply
ExecStop=/usr/local/sbin/brain-firewall clear
OnFailure=brain-alert@%n.service

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now brain-firewall.service
```

**Prove it both ways, and the negative half is the half that matters.** A rule
set that allows everything passes the positive tests perfectly.

```sh
# still works: the JWKS fetch (443) and the data mount
EG=$(sudo docker inspect -f '{{(index .NetworkSettings.Networks "brain_egress").IPAddress}}' brain-brain-1)
curl -fsS "http://$EG:8787/readyz"

# still works: git push over 22
sudo docker compose --profile maintenance run --rm maintenance doctor

# still works: the tunnel — four connections, from the Cloudflare side
#   (Zero Trust > Networks > Tunnels, or provision.py's tunnel step)

# MUST FAIL: anything else outbound
sudo docker compose --profile maintenance run --rm --entrypoint python3 maintenance \
     -c "import socket; socket.create_connection(('1.1.1.1', 53), 3)"
# -> TimeoutError.  If this succeeds, the rules are not matching: check that
#    `ip -br link show brain-egress` exists and that the container is on it.
sudo iptables -L DOCKER-USER -v -n --line-numbers   # the DROP counter should move
```

**Rollback**, one line. `DOCKER-USER` is Docker's own hook chain and Docker
never puts anything in it, so flushing it restores exactly the state above:

```sh
sudo systemctl disable --now brain-firewall.service   # ExecStop clears them
sudo iptables -F DOCKER-USER                          # or, if in a hurry
```

**The stronger version, and why it is not the default.** Allowing 443 to
anywhere still allows 443 to anywhere. An ipset refreshed from Cloudflare's
published ranges (`https://www.cloudflare.com/ips-v4`) and GitHub's
(`https://api.github.com/meta`) narrows 443 to Cloudflare and 22 to the git
provider, which is most of the remaining value. It also adds a scheduled
network fetch whose failure mode is a brain that stops pushing, or an edge that
stops connecting, some hours later, for a reason that will not look like a
firewall. Take it only with the refresh unit's `OnFailure=` wired to
`brain-alert@` like every other timer, and with the last good ranges written to
a file so a failed refresh keeps them rather than emptying the set.

**If you do not apply any of this**, say what the residual is rather than
letting it read as covered: the brain container can open a connection to any
host on the internet. It runs no third-party dependency and no third-party
code, its only inputs are Access-authenticated MCP calls, and everything it
holds is already pushed to a private remote — so the realistic exposure is
exfiltration *after* a compromise of the engine itself, which single-writer and
append-only history do not defend against. That is a trade recorded, not a
trade resolved.

---

## 12. SSH — the only way in

**Before anything below: never close SSH until a second, tested way in already
works.** On this instance that second way is Oracle's serial console. It is
per-instance, it needs a key of its own, and it must be tested *before* it is
needed. Every change here is applied with a second session already open, and
reloaded rather than restarted.

**Where the settings actually live, and the trap.** `sshd_config` `Include`s
`/etc/ssh/sshd_config.d/*.conf` at the TOP of the file, and OpenSSH takes the
**first** value it sees for a keyword. Drop-ins therefore beat the main file,
and among drop-ins the **lowest-numbered** name wins — the opposite of what
`99-hardening.conf` looks like it should do. Anything added here goes in
`10-brain.conf` so it wins, and `sudo sshd -T` is the only reading that counts.

Verified with `sudo sshd -T` on 2026-08-23:

| Setting | Effective | Change to | Why |
|---|---|---|---|
| `PermitRootLogin` | `no` | keep | already right; stated in config, not inherited |
| `PasswordAuthentication` | `no` | keep | key-only, and it is set — not assumed |
| `KbdInteractiveAuthentication` | `no` | keep | leaving it on re-opens password auth through PAM |
| `PubkeyAuthentication` | `yes` | keep | |
| `MaxAuthTries` | `6` | `3` | halves the attempts a single connection buys |
| `LoginGraceTime` | `120` | `30` | an unauthenticated connection holds a slot for two minutes today |
| `ClientAliveInterval` | `0` | `300` (+ `ClientAliveCountMax 2`) | a dropped admin session otherwise holds its slot indefinitely |
| `X11Forwarding` | `yes` | `no` | there is no X on this host |
| `AllowAgentForwarding` | `yes` | `no` | forwarding your agent onto a shared host exposes every key it holds |
| `AllowTcpForwarding` | `yes` | **keep `yes`** | it is how `ssh -L` reaches cloudflared's metrics and the container's health endpoints, neither of which publishes a port. Turning it off removes the only safe way to inspect them |
| `AllowGroups` | unset | `sshadm` | three accounts on this box have a shell; one is used |

Three accounts have login shells: `root` (`/bin/bash`, no authorized key,
`PermitRootLogin no`), `ubuntu` (`/bin/bash`, one key — this is the admin
account) and `opc` (`/bin/sh`, one key — Oracle's image ships it) [verified
2026-08-23]. `opc` is the one to decide about deliberately: either add it to
`sshadm` because you use it, or leave it out and let `AllowGroups` retire it.
An account nobody has reviewed is not the same as an account nobody uses.

```sh
sudo groupadd -f sshadm
sudo usermod -aG sshadm ubuntu

sudo tee /etc/ssh/sshd_config.d/10-brain.conf >/dev/null <<'EOF'
# Brain deployment hardening. Numbered 10 because drop-ins are first-wins and
# the lowest number therefore wins; 99-hardening.conf does not.
AllowGroups sshadm
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
X11Forwarding no
AllowAgentForwarding no
# Restated so they are asserted here rather than inherited from the cloud image.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF

sudo sshd -t                       # syntax. A bad file here is how boxes are lost.
sudo sshd -T | grep -E '^(allowgroups|maxauthtries|logingracetime|clientaliveinterval|x11forwarding|allowagentforwarding|permitrootlogin|passwordauthentication)'
sudo systemctl reload ssh          # reload, never restart: open sessions survive
```

Then, **from a new terminal on the Mac and before closing anything**:

```sh
ssh workhorse true && echo "second way in still works"
```

**Rate limiting.** `INPUT` accepts 22 from anywhere and Oracle's security list
is the only thing in front of it. Add the limit to the same script that owns
every other rule here — `netfilter-persistent save` is still forbidden while
Docker is running:

```sh
# Add to /usr/local/sbin/brain-firewall and call ssh_rules from apply/clear.
# Insertion is positional (the order relative to the existing ACCEPT is the
# whole point) but removal is by spec, so the two are still symmetric.
ssh_rules() {
    if [ "$1" = "-A" ]; then
        iptables -I INPUT 4 -p tcp --dport 22 -m state --state NEW \
                 -m recent --set --name SSH -m comment --comment "$TAG"
        iptables -I INPUT 5 -p tcp --dport 22 -m state --state NEW \
                 -m recent --update --seconds 60 --hitcount 6 --name SSH \
                 -m comment --comment "$TAG" -j DROP
    else
        iptables -D INPUT -p tcp --dport 22 -m state --state NEW \
                 -m recent --set --name SSH -m comment --comment "$TAG"
        iptables -D INPUT -p tcp --dport 22 -m state --state NEW \
                 -m recent --update --seconds 60 --hitcount 6 --name SSH \
                 -m comment --comment "$TAG" -j DROP
    fi
}
```

Positions 4 and 5 put both rules immediately **above** the existing
`--dport 22 -j ACCEPT` at position 4; check with `sudo iptables -L INPUT
--line-numbers` first, because a DROP that lands below the ACCEPT is a rule
that does nothing and a DROP that lands above the conntrack ACCEPT at position
1 disconnects you.

This rate-limits *you* as well — six new connections a minute is easy to exceed
with a script that opens a session per command. Use `ControlMaster auto` +
`ControlPersist` on the Mac so repeated commands share one connection, or use
fail2ban's `sshd` jail instead (inactive on this host today) with your own
address in `ignoreip`. **Never insert either rule without a second session
open.**

---

## 13. Cloudflare drift, and what watches what

`brain-edge-check.timer` has raised. It runs
`provision.py --state /etc/brain/cloudflare.json --check` daily and, unlike a
plain dry run, it is allowed to have an opinion:

| Exit | Meaning |
|---|---|
| 0 | the account matches the declared state |
| 2 | a human owes an action — a service token with seven days left, or one that was declared and never created |
| 4 | **drift**: the live account no longer matches `/etc/brain/cloudflare.json` |

**Read the diff before deciding which side is wrong.** Drift is not
automatically the account's fault; the state file is edited by hand too.

```sh
# The token comes from the file, never from the command line: an argument is
# visible in `ps` to every user on the box and stays in shell history.
sudo sh -c 'set -a; . /etc/brain/cf-api.env; set +a; \
    exec python3 /srv/brain/engine/deploy/cloudflare/provision.py \
        --state /etc/brain/cloudflare.json'          # dry run: prints the diff
```

- The **account** is wrong (somebody edited the dashboard): re-run with
  `--apply`. It creates and updates; it never deletes, so anything it cannot
  fix is reported for you rather than removed.
- The **state file** is wrong (a deliberate change was made at the edge and
  never written down): edit `/etc/brain/cloudflare.json` to match, and say in
  the commit-less way this file allows — a comment key — why.

Exit 2 for a token is procedure 4. Exit 2 for a token that does not exist at
all means the state file declares a credential nobody ever created; either
create it, or drop it from the file.

### How the Cloudflare alerts are addressed

Email, to the account owner, and that is the only mechanism this account has:

```sh
# read-only, and the one call worth making before trusting any of it
GET /accounts/<account>/alerting/v3/destinations/eligible
#   email     eligible: true   ready: true
#   pagerduty eligible: false
#   webhooks  eligible: false        [verified 2026-08-23]
```

Two things to know before treating that as proof.

**The `/policies/{id}/test` endpoint does not work here.** It answers
`15000: An internal server error occurred` for both policies [verified
2026-08-23]. So what is proven is that the policies exist, are enabled, are
bound to the right tunnel, and that the account's email destination is ready.
Actual delivery is *not* proven, and the first real delivery will be the proof.
If a tunnel outage ever passes without an email, this is the first thing to
suspect — not the policy.

**Nothing is silenced.** `GET /alerting/v3/silences` should stay empty; a
silence is invisible from the policy itself and is the one way a correctly
configured alert reaches nobody.

### What watches what — the whole picture, in one table

| Watcher | Runs on | Sees | Blind to |
|---|---|---|---|
| `brain-maintenance` → `doctor` | the VPS | index, content, backup age, disk headroom, unpushed commits, inbox backlog | anything outside the box |
| `brain-backup` | the VPS | archive built, encrypted, uploaded, pruned | whether it restores |
| `brain-restore-drill` | the VPS | that it restores | the days between drills |
| `brain-edge-check` | the VPS | the Cloudflare side: expiring credentials, drifted apps, policies, DNS | whether the brain is answering |
| Cloudflare `tunnel_health_event` | **Cloudflare** | the connector stopped talking to the edge | whether the origin behind it is alive |
| Cloudflare `expiring_service_token_alert` | **Cloudflare** | a headless credential lapses in 7 days | everything else |
| the capture queue | the VPS | its own unpushed backlog | anything it is not asked to write |

The first four go quiet in the one failure where silence is worst: the box
being gone. The two Cloudflare policies are the only alerting that survives
that, which is the entire reason they exist — and it is also why they are
reconciled by `provision.py` rather than clicked in, so that "is the alerting
still there" is a question the daily check answers instead of a thing somebody
remembers.

**Neither of them tells you the brain is up.** Cloudflare is explicit that
tunnel status "only reflects the connection between `cloudflared` and the
Cloudflare network... A tunnel can appear Healthy while users are unable to
connect to an application" [docs 2026-08-23]. A healthy tunnel in front of a
dead origin is a 502 that nothing alerts on. The gap is closed by the container
healthcheck (which restarts a wedged process) and by `readyz` under
maintenance — both of which run on the box. Closing it from OUTSIDE the box
needs an external prober, and this deployment does not have one; that is a
known, recorded gap rather than a covered one.

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
