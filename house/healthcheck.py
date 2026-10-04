#!/usr/bin/env python3

'''
Docker health reporting -- the writer and the probe in one file.

Deliberately named for the job and not for the service.  Every container here
runs a single script, and each container gets its own private filesystem and
its own private /dev/shm, so there is nothing to collide with: this file can be
dropped into any service directory unchanged.

USING IT FROM A SERVICE SCRIPT
------------------------------

    import healthcheck

    # Return ('healthy'|'starting'|'unhealthy', reason[, extra_fields]).
    def current_health():
        if not connected:
            return 'unhealthy', f'no connection to MQTT {BROKER}:{PORT}'
        return 'healthy', f'connected to MQTT {BROKER}:{PORT}'

    health = healthcheck.start(current_health)      # writer thread, running
    ...
    health.stop()                                   # on orderly shutdown

`start()` logs one line when the state or the substance of the reason changes,
and nothing in between -- "health check healthy (connected to ...)" -- so the
history is visible in `docker logs` without a heartbeat every few seconds
burying it.  Numbers inside the reason do not count as a change, so a reason
may carry a live count ("15s of 60s") without it costing a log line per pass.

`log=` routes that somewhere other than print(); a service using the logging
module passes `log=log.info`.  It keeps being called even when the file is not
being written (a manual ~/venv run), because the state is worth seeing there
too.

AND FROM docker-compose.yml
---------------------------

    healthcheck:
      test: ["CMD", "python3", "/app/healthcheck.py"]
      interval: 15s
      timeout: 10s
      retries: 3
      start_period: 60s

Run directly, this file is the probe: it reads what the service wrote and exits
0 (healthy) or 1 (starting or unhealthy), printing the reason either way.
Writer and probe therefore cannot disagree about the path or the format.

THE THREE STATES
----------------

'unhealthy' is for something that has actually FAILED -- a connect refused, a
link dropped, a deadline blown, or the pause between retry attempts (a service
sleeping before its next try is not healthy, it is doing nothing).  Everything
before a first failure is 'starting': a service still coming up has not failed
at anything, and colouring it red is noise.

Docker's health protocol has only two exit codes, so 'starting' also exits 1.
Docker supplies the third state itself: inside `start_period` a failing probe
leaves the container "starting" rather than marking it unhealthy.  The probe
still labels the two differently, so the history `docker inspect` keeps does
not read like a fault when nothing was wrong.
'''

import datetime
import json
import os
import pathlib
import re
import sys
import threading
import time

STATES = ('healthy', 'starting', 'unhealthy')

# RAM-backed on purpose.  /dev/shm is a tmpfs and Docker gives every container
# its OWN empty one, so the file genuinely does not exist until the service
# writes it -- "missing" unambiguously means "has not reported in yet" -- and
# nothing stale can survive a restart and lie about a process that is not
# running.  It is also the one thing that deliberately does not live under
# /app: it is rewritten every few seconds, is meaningless the moment the
# process exits, and must not land in the backup tree.
HEALTH_FILE = pathlib.Path(os.environ.get('HEALTH_FILE',
                                          '/dev/shm/healthcheck.health'))

# The writer rewrites the file this often; the probe rejects a file older than
# MAX_AGE whatever its contents claim, so a wedged process reads as unhealthy
# instead of reporting healthy forever on its last good write.
WRITE_INTERVAL = 5
MAX_AGE = 30

# Only the containerized copy writes the file.  Nothing reads it when a script
# is run by hand from ~/venv, and the host's /dev/shm -- unlike the container's
# -- persists between runs, so writing there would leave litter that outlives
# the process that meant it.
#
# A low PID is the test.  The service script is the container's entrypoint so
# it is normally PID 1, but the threshold is loose rather than `== 1`: adding
# `init: true` puts docker-init at PID 1 and demotes it to 2, and an entrypoint
# wrapper that does not `exec` could push it to 3 or 4.  An exact test would
# silently switch reporting off in those cases and leave the container reported
# unhealthy forever.
#
# The reverse mistake cannot realistically happen: PIDs below ~15 are held
# permanently by init and kernel threads, and once the PID counter passes 300
# the kernel never allocates below it again (on wraparound alloc_pid() restarts
# from RESERVED_PIDS, 300, not 1).  Nothing launched from a shell lands here.
#
# All of that assumes the container has its own PID namespace, which is the
# default and which `network_mode: host` does NOT change.  Adding `pid: host`
# would, and the service would then take a host PID, fail this test, and be
# reported unhealthy for as long as it runs -- while the log line below insists
# the PID is "too high to be inside the container", which in that one case is
# true and useless.  Set HEALTH_FILE explicitly if a service ever needs
# `pid: host`.
#
# Setting HEALTH_FILE explicitly forces reporting on regardless, which is how
# the writer can be exercised outside the container.
ENABLED = (os.getpid() < 10) or ('HEALTH_FILE' in os.environ)


def _normalize(result):
    '''Unpack whatever the caller's health function returned into
    (state, reason, extra fields).  Accepts ('healthy', why), a bool in place
    of the state, and an optional third dict of diagnostics to fold into the
    file for whoever reads it with `docker exec cat`.'''
    if not isinstance(result, tuple) or not (2 <= len(result) <= 3):
        raise ValueError('health function must return (state, reason[, details])'
                         f', got {result!r}')
    state, reason = result[0], result[1]
    details = result[2] if len(result) == 3 else None
    if state is True:
        state = 'healthy'
    elif state is False:
        state = 'unhealthy'
    if state not in STATES:
        raise ValueError(f'health state must be one of {STATES}, got {state!r}')
    return state, str(reason), details or {}


class Reporter:
    '''Owns the health file for the life of the process.

    One writer on its own clock, deliberately, rather than write calls
    scattered through the connection code: if a worker thread wedges or dies
    this one keeps telling the truth about it, and if this thread itself dies
    the file simply goes stale and the probe catches that.'''

    def __init__(self, source, log=print, path=HEALTH_FILE,
                 interval=WRITE_INTERVAL, max_age=None, enabled=ENABLED):
        self.source = source
        self.log = log
        self.path = pathlib.Path(path)
        self.interval = interval
        # ~6 missed writes.  Derived rather than fixed so that changing the
        # interval cannot leave the staleness rule behind at a threshold that
        # no longer means the same thing.
        self.max_age = max_age if max_age is not None else 6 * interval
        self.enabled = enabled
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last = None

    # -- writing ---------------------------------------------------------
    def sample(self):
        '''Ask the caller how things are, and log the answer if it changed.'''
        try:
            state, reason, details = _normalize(self.source())
        except Exception as e:
            # A broken health function is itself a fault worth reporting, but
            # it must not stop the file being written.
            state = 'unhealthy'
            reason = f'health function raised {type(e).__name__}: {e}'
            details = {}
        # Log transitions only.  "Transition" ignores the numbers inside the
        # reason, because a good reason carries live counts -- "waiting for the
        # first heartbeat (15s of 60s)", "3 consecutive failures" -- and those
        # tick on every pass.  Comparing the reason verbatim would put a line in
        # the log every few seconds for a service that is simply still waiting,
        # which is exactly what transition-only logging exists to avoid.  A
        # reason that changes in substance ("no connection to MQTT" becoming
        # "no connection to the device") still logs.  The live numbers are
        # never lost -- they are in the file, and the probe prints them into
        # the history `docker inspect` keeps.
        key = (state, re.sub(r'\d+', '#', reason))
        if key != self._last:
            self._last = key
            self.log(f'health check {state} ({reason})')
        return state, reason, details

    def write(self, state, reason, details=None):
        '''Replace the file atomically -- the probe may read at any instant and
        must never see a half-written file, so write a sibling temp file and
        rename over the target.  Every write also refreshes the mtime, which is
        the heartbeat the probe checks, so this keeps being called even when
        nothing has changed.'''
        if not self.enabled:
            return
        payload = dict(details or {})       # caller's diagnostics first ...
        payload.update({                    # ... ours always win
            'state': state,
            'healthy': state == 'healthy',
            'reason': reason,
            'pid': os.getpid(),
            'written': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            # The probe is a separate process and knows only what it reads
            # here, so the writer states its own clock rather than letting the
            # probe assume the defaults.  A service that passes a different
            # interval= therefore cannot end up being judged stale against a
            # threshold meant for a different cadence.
            'interval': self.interval,
            'max_age': self.max_age,
        })
        tmp = self.path.with_suffix(self.path.suffix + f'.tmp{os.getpid()}')
        with self._lock:
            if self._stop.is_set():
                return                      # stop() has cleared it; stay gone
            try:
                tmp.write_text(json.dumps(payload, indent=2) + '\n')
                os.replace(tmp, self.path)
            except Exception as e:
                # Reporting health must never be what takes the service down.
                self.log(f'Could not write health file {self.path}: {e}')
                try:
                    tmp.unlink()
                except Exception:
                    pass

    def clear(self):
        '''Remove the file entirely.  Absence is the strongest available
        statement that nothing is running -- there are no contents left to be
        misread as stale good news.'''
        if not self.enabled:
            return
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                self.log(f'Could not remove health file {self.path}: {e}')

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if self.enabled:
            # Clear before anything can read it.  The container's /dev/shm is
            # already empty; this matters when HEALTH_FILE was set by hand.
            self.clear()
            self.log(f'health check reporting ON (pid {os.getpid()}): {self.path} '
                     f'rewritten every {self.interval}s, '
                     f'considered stale after {self.max_age}s')
        else:
            # Expected when running by hand from ~/venv.  NOT expected in the
            # container -- if this shows up in `docker logs`, something now
            # sits between the container and the script deep enough to push it
            # past PID 10, and the container will be reported unhealthy for as
            # long as it runs.
            self.log(f'health check reporting OFF: running as pid {os.getpid()}, '
                     f'which is too high to be inside the container, and '
                     f'HEALTH_FILE is not set. Nothing will be written to '
                     f'{self.path}, but state changes are still logged.')
        # The thread runs either way.  Only the file is gated: the transition
        # log is the useful half of health reporting during a manual run, and
        # switching it off with the file would make the shell copy quieter
        # about its own state than the container is.
        threading.Thread(target=self._run, name='healthcheck',
                         daemon=True).start()
        return self

    def stop(self):
        '''Orderly shutdown: stop writing and take the file away.'''
        self._stop.set()
        self.clear()

    def _run(self):
        while not self._stop.is_set():
            state, reason, details = self.sample()
            self.write(state, reason, details)
            self._stop.wait(self.interval)


def start(source, **kwargs):
    '''The whole import-side footprint: one call, returns a live Reporter.'''
    return Reporter(source, **kwargs).start()


def probe(path=HEALTH_FILE, max_age=None, interval=None):
    '''The other half of the contract: read the file and turn it into an exit
    status.  Runs as its own short-lived process and shares nothing with the
    service but the file.  Prints the reason either way -- `docker inspect`
    keeps the last few, and "UNHEALTHY: no connection to MQTT 192.168.x.x:1883"
    ends an investigation that "unhealthy" only starts.'''
    path = pathlib.Path(path)
    try:
        payload = json.loads(path.read_text())
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        # Absence covers both ends of the life cycle: not started yet, and
        # stopped cleanly (stop() removes the file).  Say both, rather than
        # asserting the first and being wrong every time a container shuts down.
        print(f'STARTING: no {path} -- the service has not reported in yet, '
              f'or has shut down and cleared it')
        return 1
    except Exception as e:
        print(f'UNHEALTHY: could not read {path}: {e}')
        return 1

    # The writer records its own cadence in the file, so a service that keeps a
    # different clock is judged against the threshold it actually meant.  The
    # arguments override that; the module defaults are the last resort, for a
    # file written before these fields existed.
    if max_age is None:
        max_age = payload.get('max_age') or MAX_AGE
    if interval is None:
        interval = payload.get('interval') or WRITE_INTERVAL

    if age > max_age:
        # Staleness outranks the contents: a wedged process would otherwise
        # keep reporting whatever it last managed to write.
        print(f'UNHEALTHY: {path} is {int(age)}s old (expected an update every '
              f'{interval}s) -- the service is wedged or gone. '
              f'Last said: {payload.get("reason")}')
        return 1

    state = payload.get('state')
    reason = payload.get('reason')
    if state == 'healthy':
        print(f'HEALTHY: {reason}')
        return 0
    if state == 'starting':
        print(f'STARTING: {reason}')
        return 1
    print(f'UNHEALTHY: {reason}')
    return 1


if __name__ == '__main__':
    sys.exit(probe())
