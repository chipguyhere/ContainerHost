# Container host — house rules

"House" here means the house in the old sense, the *oikos*: the
establishment these machines serve and the way it keeps its affairs in order.
Sometimes that is literally a home. Just as often it is a business, a site or
a collection of sites. The **house rules** are how this establishment builds
and runs its services, whatever building they happen to be in.

This machine is one of the house's service hosts. It exists to run small
services 24/7. Almost every one of them is a Docker container, and almost
every one of them talks to everything else over **MQTT, which is the house's
primary communications bus**. Services bridge devices and outside systems
(AV gear, lighting, access control, sensors, web APIs) onto MQTT topics, and
they coordinate with each other by publishing and subscribing there rather
than calling each other directly. A service is expected to start at boot, run
unattended indefinitely, ride out broker and network outages on its own, and
say clearly, on MQTT and to Docker, when it is not working.

Docker is installed and `docker.service` is enabled at boot.

The design decisions below are the house rules. Follow them when creating a
new service or modifying an existing one, unless I explicitly ask for
something different. Older services may predate some of them; bring a service
in line when you are already working on it, and mention where it falls short.

## Where services live: one subdirectory of `~/Documents` each

This directory, `~/Documents`, is the root of that collection, and **every
service lives in its own subdirectory directly under it** — active or
inactive, Docker or not. Listing `~/Documents` should therefore show every
service on the machine, alongside `CLAUDE.md` and `house/`. A few other
things may sit here too, but a service never lives anywhere else: not nested
inside another service, not in a grouping folder, not elsewhere on the host.

- **When I ask for a new service without saying where, create it at
  `~/Documents/<new-service-name>/`.** Use a lowercase, hyphenated name
  (`spa-jets-switch`), which also serves as the `container_name` and the
  root of its MQTT topics.
- **Before creating one, list `~/Documents` and check `docker ps -a`** so the
  name does not collide with an existing service directory or container.
- **A service that is retired stays in its directory** rather than being
  deleted or moved away, so the listing remains the full record. Mark it
  inactive (`docker compose down`, or disable the systemd unit) and say so in
  its description label or a note in the directory.

## Docker by default

**A new service is a Docker container unless one of these applies:**

- I ask for it to be a systemd service, or ask for it not to use Docker;
- it cannot practically work inside a container — for example it configures
  the host itself (the `nftables` rules in `mcw-firewall/`), needs to manage
  host services or hardware in ways a container cannot reasonably be granted,
  or must run before Docker is up;
- it is meant to be identical on several machines, some of which do not run
  Docker (`rpihealth/`, which runs the same script on every Pi).

"Needs the LAN", "needs a serial port" or "needs mDNS" are not reasons on
their own: `network_mode: host`, `devices:` and similar options cover those.
If you conclude a service cannot run in Docker, say why before building it
another way.

A Docker service's directory holds a `docker-compose.yml`, a `Dockerfile`, and
usually a single Python script that does the actual work, laid out under
"Layout of a service directory" below.

## systemd services

The few services that are not containers run as systemd units. They follow
every house rule that is not Docker-specific (where they live, MQTT
behaviour, LWT and the `"0"` on exit, logging to stdout, paths resolved from
the script, state kept in the service directory). Under systemd, stdout goes
to the journal (`journalctl -u <unit>`), which caps its own size.

**The script installs itself.** A unit file has to live in
`/etc/systemd/system`, outside the `~/Documents` backup tree. Rather than
back it up, the service script carries an installer that writes the unit file
again, so the script alone is enough to recreate the service on a rebuilt or
new machine:

```bash
sudo ~/venv/bin/python3 <service_name>.py --installservice           # install + enable
sudo ~/venv/bin/python3 <service_name>.py --installservice --anyway  # overwrite existing
```

`house/example_systemd_service.py` is the template, and `rpihealth/rpihealth.py`
is a real service built this way. Start a new systemd service by copying the
template into its own directory. The template shows how the installer works:

- it runs at the very top of the script, before any third-party import, so it
  works before the dependencies are installed;
- it refuses unless run as root, and won't overwrite an existing unit without
  `--anyway`;
- the unit it writes runs the script with the interpreter that ran the
  installer (`sys.executable`), so install with `~/venv`'s python. It uses the
  script's absolute path, runs as the user who invoked sudo, has
  `WorkingDirectory` set to the service directory, and has
  `Restart=always`;
- after writing the file it runs `systemctl daemon-reload` and `enable`, but
  does not start the service.

Don't write a separate unit file that someone would then have to back up and
copy into place. `mcw-firewall/` is an older variation that predates this
pattern: its unit file sits in its directory and is `systemctl link`ed from
there by `install-firewall.py`. That keeps it in the backup too, but a new
service should use the self-installing script.

**Name units with an `mcw` prefix** (`mcw_rpihealth`,
`mcw-custom-firewall`). That prefix is how `house/services` finds them.

**`house/services` is the manager for these units.** It is an interactive
menu that lists the house's own systemd units with their status. For a chosen
unit it can start, restart, stop, enable or disable it at boot, reload it, or
show its journal (recent or following). Run it as `~/Documents/house/services`;
symlink to it from a folder in PATH so it is accessible to the shell,
it calls `sudo` itself for actions that need it. Docker services are not in
it; manage those with `docker compose`.

## The `house/` directory

`house/` is not a service. It holds the reference copies that services copy
from:

- `house/healthcheck.py` — the shared health module (see Health checks).
- `house/ca.crt` — the CA certificate the MQTT broker's TLS certificate
  chains to.
- `house/mqtt-broker.txt` — which broker to use
- `house/example_systemd_service.py` — the template for a service that runs
  under systemd and installs its own unit (see systemd services).

It also holds `house/services`, the menu for managing the systemd services.
That is a tool you run from `house/`, not something to copy into services.

When a service needs one of these, copy it **from `house/`**, never from
another service, which may hold an older or modified copy. When a reference
copy changes, update `house/` first and then every service's copy. New shared
reference material belongs here too.

## Layout of a service directory

```
<service-name>/
  docker-compose.yml
  Dockerfile
  <service_name>.py      # the one script the container runs
  healthcheck.py         # copied verbatim from house/ (see Health checks)
  requirements.txt       # only if there are pip deps
  ca.crt                 # copied verbatim from house/, for MQTT over TLS
  data/                  # any persistent state the service keeps
```

Keep it flat. One script per container is the norm; reach for extra modules
only when the script genuinely outgrows a single file. `healthcheck.py` and
`ca.crt` are the standing exceptions — the same file in every service, copied
from `house/` rather than shared, because a service directory has to be
self-contained.

**Everything a service needs lives in its own directory.** Backing up this
one directory must be enough to back up every service — code, config,
secrets, certs, and state. So when a service stores anything of its own
(a SQLite file, a JSON cache, tokens, a last-seen marker), it goes in the
service directory or a subdirectory of it, reachable through the `/app` bind
mount. Do not use Docker named volumes for service state, and do not write to
paths elsewhere on the host: both live outside this tree and would be silently
missed by a backup. Logs are the exception — those belong to Docker's rotated
json-file driver, not to the directory.

## docker-compose.yml

The baseline every service should start from:

```yaml
services:
  my-service:
    build: .
    container_name: my-service
    working_dir: /app
    # Bind-mount the project directory over /app so the container runs the
    # host's code: edit the .py here, `docker compose restart`, done.
    volumes:
      - .:/app
    # Auto-start at boot and after a crash, but stay down if I explicitly
    # `docker compose stop` it.
    restart: unless-stopped
    # Cap logs at 30 MB total (3 files x 10 MB), rotated automatically.
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    # What this service is, for whoever is looking at the container list and
    # has no other clue. One paragraph, 5-7 sentences — see below.
    labels:
      org.opencontainers.image.description: >-
        One paragraph saying what this connects to, what it publishes or
        consumes and on which topics, how it behaves when something is down,
        and what stops working if it stops.
```

House rules baked into that template — apply them by default:

- **Auto-start is required.** Always set a restart policy;
  `restart: unless-stopped` is the default choice. Use `restart: always` only
  when I want the container to come back even after a manual stop.
- **Log usage is capped at 30 MB per service** (`max-size: "10m"` x
  `max-file: "3"`). Without a `logging:` block Docker's json-file driver grows
  without bound and eventually fills the filesystem. Only exceed 30 MB if I ask.
- **Bind-mount the service directory to `/app`** with `working_dir: /app`.
  This is the whole point of the setup: updating a script means editing the
  file in this directory and restarting the container — no image rebuild.
  It is also how persistent state stays inside the backup tree, so a service
  that keeps state writes it under `/app` and needs no other volume.
- **Networking:** use `network_mode: host` when the service talks to LAN
  devices, needs mDNS/`.local` resolution, or listens on a raw TCP port.
  Otherwise the default bridge is fine. On a bridge network `.local` names do
  not resolve — pin the broker with `extra_hosts:` (the current IP is in
  `house/mqtt-broker.txt`) and keep connecting by name, because TLS verifies
  the broker's certificate against the hostname `mqtt.local`.
- **Every service carries a description label.** A container list shows a name
  and an image, neither of which says what the thing does or what breaks if it
  stops. Write the paragraph described below.

### The description label

Use `org.opencontainers.image.description`. It is the conventional key, it is
namespaced so it cannot collide with `com.docker.compose.*`, and the container
UI reads it. A kilobyte of text costs nothing: it lands in the container's
config JSON once and is held in the daemon's memory, so it is a few kB across
every service on the host.

**Aim for one paragraph of 5 to 7 sentences.** Not a title, not a one-liner —
enough that someone who has never seen the service can tell what it is for.
Cover, in about this order:

- what it connects to, and how (protocol, host, port);
- what it publishes or consumes, naming the actual topics;
- anything non-obvious about *how* it does that — the reconciliation, the
  filtering, the rate limit, whatever the interesting part is;
- how it reports its own state (status topics, will, health check);
- what it does when something is down (retry, restart delay, give up);
- what stops working if the service stops.

Do not describe the credential rotation scheme as it is provided by a
shared library that is documented elsewhere and not material to the specific
service being documented. If this has already been documented, consider
removing that and replacing the space with something relevant to the specific
service.

Two mechanical rules:

- **One paragraph, no line breaks.** The UI ignores newlines, so a hard-wrapped
  paragraph arrives as one run-on line with the breaks silently gone. Write it
  with YAML's folded `>-` scalar, which keeps the compose file readable while
  storing a single line. Never `|` here — it preserves breaks that then do not
  render, and blank lines inside a folded scalar become newlines too, so leave
  none.
- **Labels are fixed when the container is created.** Editing the text needs
  `docker compose up -d` to recreate; `docker compose restart` keeps the old
  label. The same is true of `container_name`.

Container names, incidentally, cannot help here: the daemon allows only
`[a-zA-Z0-9][a-zA-Z0-9_.-]*`, so no spaces and nothing descriptive. Without
`container_name:` a service is named `<project>-<service>-<index>`, where the
project is this directory's name lowercased and the `-1` is the replica
ordinal, which is why an explicit `container_name` is in the template.

## Dockerfile

```dockerfile
FROM python:3.14-slim

# Install deps into the image's site-packages, NOT into /app — at runtime the
# host directory is bind-mounted over /app, which would hide anything copied
# there.
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Unbuffered stdout so `docker logs` is live.
ENV PYTHONUNBUFFERED=1

# Forward SIGTERM to the script so it can shut down cleanly (see below).
STOPSIGNAL SIGTERM

CMD ["python3", "-u", "my_service.py"]
```

- Do **not** `COPY` the application script into the image. It comes from the
  bind mount. Copying it creates two copies that silently drift.
- **`python:<latest stable>-slim` is the default base**, currently
  `python:3.14-slim`. I have no attachment to a particular version — when
  starting a new service, check what the current stable release is and use it
  rather than copying the version out of an older service here. Pin the minor
  version in the tag (`3.14-slim`, not `3-slim`) so a rebuild of an existing
  service never jumps interpreter versions on its own; bumping it is a
  deliberate edit. Pin pip versions in `requirements.txt`
  (e.g. `paho-mqtt==2.1.0`).
- Rebuilding (`docker compose up -d --build`) is only needed when the
  Dockerfile or dependencies change. Code changes need only
  `docker compose restart <service>`.

## MQTT scripts

MQTT is the bus that ties the services on this machine together, so most
scripts are MQTT clients first and device bridges second. The primary broker
is identified in `house/mqtt-broker.txt`. Default to it for every new service,
with the host and port overridable from the compose file's environment, and copy
`house/ca.crt` into the service directory as `ca.crt`. Use plain 1883 or
another broker only when I ask. Use `paho-mqtt` 2.x with
`mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)`, and verify the certificate
(`client.tls_set(ca_certs=str(BASE_DIR / "ca.crt"))`) — do not turn
verification off.

### QoS, retain, and command topics

- **Always QoS 0**, on every publish, subscribe, and will. Never 1 or 2.
- **Retain is a deliberate choice, made per topic.** The default is retained:
  state topics describe how something *is*, and a subscriber that connects
  later should learn the current value without waiting for the next change.
- **Commands and their responses are never retained.** They describe a moment,
  not a state; a retained command is a landmine that re-fires at every
  subscriber reconnect and every broker restart.
- **Commands and responses live under a topic containing `/control/`** — e.g.
  `my-service/<device>/control/reboot` for the command and
  `my-service/<device>/control/rebooted` for the response. The segment is what
  makes "this is an action, not a state" visible in the topic itself.
- **Never act on a message flagged as retained** in a command handler. On
  subscribe the broker replays whatever retained value is sitting on the topic;
  acting on it means executing a stale command on every reconnect. Check the
  flag and drop it unless I've specifically asked for replay-on-start:

```python
def on_message(client, userdata, msg):
    if msg.retain:
        log.info("Ignoring retained command on %s", msg.topic)
        return
    ...
```

Three more things are non-negotiable in any script that connects to MQTT:

**1. Survive broker outages.** The broker restarts, the network blips, the machine
reboots before the broker does. The script must never exit because the broker
was unreachable — it retries with a delay, forever.

```python
client.reconnect_delay_set(min_delay=1, max_delay=30)  # paho auto-reconnects

while True:                     # initial connect can also fail; keep trying
    try:
        client.connect(MQTT_BROKER, MQTT_PORT)
        break
    except Exception:
        log.exception("MQTT connection failed, retrying in 5s")
        time.sleep(5)
```

Then `client.loop_forever()`, which reconnects on its own after a drop. Log
disconnects in `on_disconnect` so the reason is visible in `docker logs`.

**2. Publish a Last Will and Testament (LWT).** The house rule is a retained status
topic carrying `"1"` when alive and `"0"` when not:

```python
STATUS_TOPIC = "my-service/online-status"

# Must be set BEFORE connect().
client.will_set(STATUS_TOPIC, "0", qos=0, retain=True)

def on_connect(client, userdata, flags, reason_code, properties):
    # Re-assert "1" on every connect, not just the first — a reconnect
    # follows the broker having published our will.
    client.publish(STATUS_TOPIC, "1", qos=0, retain=True)
```

**3. Deliberately publish `"0"` on the way out.** This is the part that is
easy to get wrong. The broker only fires the LWT on an *unclean* disconnect. A
script that crashes with an unhandled exception, gets `SIGTERM` from
`docker stop`, or exits on a fatal error can still tear down the TCP session
cleanly — in which case the will is discarded and the status topic is left
stuck at `"1"` while nothing is running. So publish the `"0"` yourself
whenever you are still able to:

```python
def graceful_shutdown():
    log.info("Shutting down …")
    client.publish(STATUS_TOPIC, "0", qos=0, retain=True)
    time.sleep(0.5)          # let the publish actually leave the socket
    client.disconnect()

def sig_handler(signum, frame):
    graceful_shutdown()
    raise SystemExit(0)      # unwinds to the finally block below

signal.signal(signal.SIGTERM, sig_handler)   # docker stop / compose restart
signal.signal(signal.SIGINT, sig_handler)    # Ctrl-C

try:
    client.loop_forever()
except (KeyboardInterrupt, SystemExit):
    pass
finally:
    graceful_shutdown()      # also covers exit via unhandled exception
```

Cover every exit path — signal handler, `finally`, and any explicit
`sys.exit()` in error handling. If the process is `SIGKILL`ed or loses power
there is nothing to be done; that is exactly the case the LWT exists for. The
two mechanisms are complements, not alternatives — always implement both.

## Logging in Python

Log to stdout/stderr and let Docker's capped json-file driver handle rotation.
Do not write log files into the bind-mounted directory unless I ask — those
are not covered by the 30 MB cap and will grow until the filesystem fills.

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("my-service")
```

## Health checks

Any service that depends on a network connection should report a Docker health
check. It is not redundant with the MQTT LWT: the will tells other *MQTT
subscribers* that the service died, the health check tells *Docker and
`docker ps`* that it is not working. Implement both where both apply.

**Healthy means "connected right now to every network service I need."** Not
"the process is running" — that is what `restart:` already covers, and the
failure that actually happens is a process still very much alive with its
connections gone. Derive the state from the live connection flags, not from
the fact that the process got as far as starting.

**Report three states, not two: `starting`, `healthy`, `unhealthy`.** Reserve
unhealthy for a step that has *actually failed*, so that seeing it always means
something is wrong and worth looking at:

- a connect that was refused or a link that dropped;
- **the pause between retry attempts** — the one that is easy to forget. A
  service sleeping 30 seconds before its next attempt is not healthy; it is
  doing nothing;
- a deadline blown — a subscribed feed that has gone quiet, a device that has
  stopped answering.

Everything before one of those is `starting`. A service that is merely coming
up has not failed at anything, and flagging it red is noise that trains me to
ignore the state that matters. Concretely: do not report unhealthy while the
first connection attempt is still in flight, and do not report it for a feed
that simply has not produced its first message yet — give that a deadline and
report starting until the deadline passes.

**Give every "am I hearing from it" check a grace window, measured from the
more recent of the last message and the current connection.** A reconnect
brings the link back with the last message potentially minutes old, and a fresh
subscription takes up to one publish interval to produce a live one. Timing
only from the last message means every reconnect emits a burst of unhealthy for
something that is merely waiting. Size the window at ~3 missed intervals, and
say both numbers in the reason.

**Do not count a retained MQTT message as a sign of life.** The broker replays
whatever is retained the instant you subscribe, and it may have been sitting
there since the publisher died. Only a live (unretained) message proves the
other end is alive *now*. This is why the first-message grace above is needed
at all.

**The mechanism is `healthcheck.py`** — a module with two jobs, imported by the
service script it is the writer thread, run directly by Docker's `test:` it is
the probe that reads what the writer wrote. Copy the file into the service
directory unchanged (`house/healthcheck.py` is the reference copy) and read its
docstring for the rest; it explains its own design, including the RAM-backed
state file, the staleness rule, and why only the containerized copy writes.
Do not reimplement any of that per service.

All a service owes it is a `current_health()` returning `(state, reason)` or
`(state, reason, details)`, written as a pure reading of the connection flags:
it is called from another thread every few seconds, so it must not block,
connect to anything, or take a lock a wedged worker might be holding. Name the
host and port in the reason — `docker inspect` keeps the last few probe
results, and "UNHEALTHY: no connection to MQTT 192.168.x.x:1883" ends an
investigation that "unhealthy" only starts.

```python
import healthcheck                 # sits beside the service script under /app

health = healthcheck.start(current_health, log=log.info)   # writer thread
...
health.stop()        # on orderly shutdown, beside the LWT "0"
```

```yaml
    healthcheck:
      # start_period must outlast first connect + any first-message grace, so
      # a normal start reads "starting" rather than "unhealthy" — but no
      # longer, since it also delays a real failure becoming visible.
      test: ["CMD", "python3", "/app/healthcheck.py"]
      interval: 15s
      timeout: 10s
      retries: 3
      start_period: 60s
```

Note that **Docker does not restart an unhealthy container** — `restart:` reacts
to a process exiting, not to health. This reports; it does not act. Say so
rather than implying a service will heal itself.

`healthcheck.py` is copied, not shared, so the copies can drift. A change to it
that is a genuine improvement rather than something specific to one service
belongs in every copy — say so, make it in `house/healthcheck.py` first, and
update the service copies together.

## Running a script outside its container

We often test a script by running it directly from a shell, with the container
stopped. The interpreter for that is the virtualenv at `~/venv`:

```bash
docker compose stop my-service        # don't run two copies at once
source ~/venv/bin/activate
cd ~/Documents/my-service && python my_service.py
```

- **New or changed dependencies go into `~/venv` as well as
  `requirements.txt`** — `source ~/venv/bin/activate && pip install -r
  requirements.txt`. The container and the venv are separate environments;
  installing in only one of them means the script works in one place and
  `ImportError`s in the other. Mention when a dep needs adding to both.
- **Stop the container first.** Two instances would fight over the same MQTT
  client id, publish contradictory status, and double-drive whatever hardware
  the service controls.
- **Keep the script runnable from both places.** Resolve data and config file
  paths relative to the script's own location, not the process's working
  directory, so the same code works under `/app` in the container and under
  `~/Documents/<service>` from the shell:

```python
BASE_DIR = pathlib.Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "state.json"
```

## Backups

**Backing up `~/Documents` must be enough to recover every service.** A
service that keeps state in its own directory under `/app` is already covered.
A container that keeps state where the backup can't see it — typically a
third-party image that stores its data in a Docker named volume — must instead
drop backups of that state into its own service directory, and must ship with
a restore script. `dockhand/` is the reference implementation
(`start.sh`, `dockhand_backup.mjs`, `restore.py`, and the `entrypoint:` and
`/srv/dockhand` mount in its compose file).

My host-level backup of `~/Documents` runs at :15 and :45. Service backups
therefore run at **:00 and :30 every hour**, so the two never overlap, and
whatever my backup collects is at most 30 minutes old.

- **Where:** `<service-dir>/backups/`, via the `/app` bind mount.
- **What:** one timestamped item per backup, named
  `<service>-<YYYYMMDDTHHMMSSZ>` (UTC). Support both forms, chosen by
  `BACKUP_FORMAT` in the compose file:
  - `tgz` (the default): `<name>.tgz`;
  - `dir`: a plain directory, easier to browse.

  Always build the backup as a directory first, and for `tgz` compress it
  straight away. If compression fails, keep the directory and log why, rather
  than losing the backup. Retention and pruning treat both forms alike.
- **Never visible half-written:** build each backup under a hidden temporary
  name (`.<name>.partial` for the directory, `.<name>.tgz.partial` for the
  archive), then rename it into place in one step. Delete leftover partials
  at startup.
- **Consistent, not just copied:** do not copy a live database file. For
  SQLite use the online backup API (`sqlite3.Connection.backup`) and leave out
  `-wal`/`-shm`. For other databases use their dump tool.
- **Readable by me:** hand the result to the owner of the service directory
  (`chown`), so I can browse and delete backups without sudo. Make anything
  holding keys or credentials owner-only.
- **Retention: 48 hours, plus always the newest 3.** Prune after each run.
  Delete backups older than 48 hours, but never any of the newest 3, whatever
  their age. Both numbers go in the compose file as environment variables
  (`BACKUP_KEEP_HOURS: "48"`, `BACKUP_KEEP_MIN: "3"`). The newest-3 rule only
  matters when no recent backups exist: the service was down for days, or
  the directory was carried to another machine. It keeps the last good
  backups instead of pruning to nothing. Otherwise these backups are only a
  staging area: my `~/Documents` backup at :15/:45 copies them off the
  machine well within 48 hours, and that is where history is kept. Don't go
  beyond this (longer windows, more safety nets) unless I ask. Do say what
  the retention costs in disk space.
- **Moving a service to another machine:** run its `restore.py` there
  *before* the first `docker compose up`. Starting first gives a blank
  instance, and its new backups of that blank state start to displace the
  real ones from the newest 3.
- **How:** run the backup engine inside the service's own container, not in a
  separate one. Keep the stock third-party image and don't build a derived
  one, so updates stay a plain pull. Use a wrapper entrypoint from the bind
  mount that starts the backup loop in the background, then `exec`s the
  image's original entrypoint unchanged. It should fail loudly if that
  entrypoint has moved. Write the engine in whatever runtime the image is
  guaranteed to ship (Dockhand uses Node because its image has no Python),
  restart it if it dies, and prefix its log lines so they stand out in
  `docker logs`. Mount the service directory somewhere other than `/app`
  (Dockhand uses `/srv/<service>`) when the image keeps its own code in
  `/app`. Fall back to a sidecar container only when the image has no
  usable runtime at all.

**Every such service ships a `restore.py`** that takes one backup as its
argument, either a `.tgz` or a directory, whatever `BACKUP_FORMAT` is
currently set to. Check an archive before stopping anything, and unpack it
before wiping the old data, so a damaged archive fails with nothing changed.
Its docstring and `--help` say plainly:

- whether to run it **on the host or inside the container**. Prefer the host,
  because restoring usually means stopping the container that would otherwise
  run the script. It should refuse, with a clear message, when run in the
  wrong place;
- whether the containers must be **running or stopped** beforehand, and what
  state it leaves them in. Prefer a script that handles both itself: it stops
  what it needs, restores, and starts the service again;
- that it takes a **pre-restore backup** of the current state before
  overwriting anything, so the restore itself can be undone.

Test a backup and a restore once before calling the service done.

## Working here

- `docker compose up -d --build` to create, `docker compose restart` after a
  code edit, `docker compose logs -f` to watch.
- Check `docker ps` before assuming a service name or port is free.
- Backing up this directory must capture everything. If something you add
  would live outside it, say so instead of quietly putting it there.
- This directory is not a git repository; there is no undo. Read a file before
  overwriting it.
