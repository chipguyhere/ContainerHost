# House rules for a Linux service host

This is a way of organizing a Linux box, typically a Raspberry Pi or another
always-on machine, that runs many small 24/7 services. Each service is a Docker
container, and the services talk to each other and to the outside world over
MQTT. The rules are written down in [`CLAUDE.md`](CLAUDE.md), so that an AI
coding agent such as [Claude Code](https://claude.com/claude-code) follows them
whenever it creates or changes a service. A person can read them just as well.

The "house" in "house rules" is the establishment the machine serves, whether
that is a home, a business or several sites. The rules are how that
establishment builds and runs its services.

## Is this useful to you?

It probably is if you:

- run a home-automation, building-control or monitoring setup made of many
  small bridges and scripts, rather than one big application;
- already use MQTT as your message bus, or want to;
- use an AI agent to write and maintain those services, and want each one built
  the same way without having to explain your conventions every time;
- want a single directory that you can back up to recover the whole machine.

It probably isn't if you want a ready-made platform such as Home Assistant or
Node-RED, or you run services on Kubernetes or across a fleet. This is a set of
conventions for one machine, not software to install.

## What the rules cover

- **Layout.** Every service, active or retired, has its own directory directly
  under `~/Documents`. Listing that directory lists every service on the
  machine.
- **Docker by default.** Each service has a `docker-compose.yml`, a
  `Dockerfile` and usually one Python script. The script is bind-mounted into
  the container rather than built into the image, so a code change only needs
  a restart, not a rebuild. Every container restarts automatically, has its
  logs capped at 30 MB, and carries a label describing what it does.
- **systemd when Docker won't do.** A service that can't run in a container,
  or that has to run on machines without Docker, carries its own
  `--installservice` installer. The unit file never has to be backed up.
- **MQTT discipline.** Messages always use QoS 0. State topics are retained;
  commands are never retained and go under `/control/`. A command handler
  ignores retained messages. Every service publishes an online/offline status
  topic, backed by both a Last Will and an explicit "offline" on shutdown. A
  service never exits because the broker is down; it keeps retrying.
- **Health checks.** Docker health reports whether a service is connected right
  now, not just whether its process is running. There are three states
  (starting, healthy, unhealthy), with grace periods so that a restart doesn't
  raise false alarms.
- **Backups.** Each service keeps its state inside its own directory. Services
  that can't do that, typically third-party images that use Docker volumes,
  write timestamped backups into their directory on a fixed schedule with
  pruning, and ship a `restore.py`.

## What's in `house/`

`house/` holds the shared reference files that services copy from:

| File | Purpose |
|---|---|
| `healthcheck.py` | Health-check module, copied unchanged into each service |
| `ca.crt` | The CA certificate for the MQTT broker's TLS |
| `mqtt-broker.txt` | Which broker to use and how to reach it |
| `example_systemd_service.py` | Template for a systemd service that installs its own unit |
| `services` | Interactive menu for managing the house's systemd units |

## Adopting it

Copy `CLAUDE.md` and `house/` into the directory where your services will live.
Then change everything that is specific to this setup:

- the broker in `house/mqtt-broker.txt`;
- your own CA in place of `house/ca.crt`;
- the `mcw` prefix for systemd unit names;
- the `~/venv` path and the `/home/pi` shebang in the example script;
- the backup schedule, which is timed around this owner's backup running at
  :15 and :45.

`CLAUDE.md` is written in the owner's voice ("unless I ask"), because it gives
an agent instructions on the owner's behalf. When you adopt it, "I" means you.
