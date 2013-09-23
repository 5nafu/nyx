"""
Fetches connection data (IP addresses and ports) associated with a given
process. This sort of data can be retrieved via a variety of common *nix
utilities:
- netstat   netstat -np | grep "ESTABLISHED <pid>/<process>"
- sockstat  sockstat | egrep "<process> *<pid>.*ESTABLISHED"
- lsof      lsof -wnPi | egrep "^<process> *<pid>.*((UDP.*)|(\(ESTABLISHED\)))"
- ss        ss -nptu | grep "ESTAB.*\"<process>\",<pid>"

all queries dump its stderr (directing it to /dev/null). Results include UDP
and established TCP connections.

FreeBSD lacks support for the needed netstat flags and has a completely
different program for 'ss'. However, lsof works and there's a couple other
options that perform even better (thanks to Fabian Keil and Hans Schnehl):
- sockstat    sockstat -4c | grep '<process> *<pid>'
- procstat    procstat -f <pid> | grep TCP | grep -v 0.0.0.0:0
"""

import os
import time
import threading

from stem.util import conf, connection, log, system

# If true this provides new instantiations for resolvers if the old one has
# been stopped. This can make it difficult ensure all threads are terminated
# when accessed concurrently.
RECREATE_HALTED_RESOLVERS = False

RESOLVERS = []                      # connection resolvers available via the singleton constructor
RESOLVER_FAILURE_TOLERANCE = 3      # number of subsequent failures before moving on to another resolver
RESOLVER_SERIAL_FAILURE_MSG = "Unable to query connections with %s, trying %s"
RESOLVER_FINAL_FAILURE_MSG = "We were unable to use any of your system's resolvers to get tor's connections. This is fine, but means that the connections page will be empty. This is usually permissions related so if you would like to fix this then run arm with the same user as tor (ie, \"sudo -u <tor user> arm\")."

def conf_handler(key, value):
  if key.startswith("port.label."):
    portEntry = key[11:]

    divIndex = portEntry.find("-")
    if divIndex == -1:
      # single port
      if portEntry.isdigit():
        PORT_USAGE[portEntry] = value
      else:
        msg = "Port value isn't numeric for entry: %s" % key
        log.notice(msg)
    else:
      try:
        # range of ports (inclusive)
        minPort = int(portEntry[:divIndex])
        maxPort = int(portEntry[divIndex + 1:])
        if minPort > maxPort: raise ValueError()

        for port in range(minPort, maxPort + 1):
          PORT_USAGE[str(port)] = value
      except ValueError:
        msg = "Unable to parse port range for entry: %s" % key
        log.notice(msg)

CONFIG = conf.config_dict("arm", {
  "queries.connections.minRate": 5,
}, conf_handler)

PORT_USAGE = {}

def getPortUsage(port):
  """
  Provides the common use of a given port. If no useage is known then this
  provides None.

  Arguments:
    port - port number to look up
  """

  return PORT_USAGE.get(port)

def isResolverAlive(processName, processPid = ""):
  """
  This provides true if a singleton resolver instance exists for the given
  process/pid combination, false otherwise.

  Arguments:
    processName - name of the process being checked
    processPid  - pid of the process being checked, if undefined this matches
                  against any resolver with the process name
  """

  for resolver in RESOLVERS:
    if not resolver._halt and resolver.processName == processName and (not processPid or resolver.processPid == processPid):
      return True

  return False

def getResolver(processName, processPid = "", alias=None):
  """
  Singleton constructor for resolver instances. If a resolver already exists
  for the process then it's returned. Otherwise one is created and started.

  Arguments:
    processName - name of the process being resolved
    processPid  - pid of the process being resolved, if undefined this matches
                  against any resolver with the process name
    alias       - alternative handle under which the resolver can be requested
  """

  # check if one's already been created
  requestHandle = alias if alias else processName
  haltedIndex = -1 # old instance of this resolver with the _halt flag set
  for i in range(len(RESOLVERS)):
    resolver = RESOLVERS[i]
    if resolver.handle == requestHandle and (not processPid or resolver.processPid == processPid):
      if resolver._halt and RECREATE_HALTED_RESOLVERS: haltedIndex = i
      else: return resolver

  # make a new resolver
  r = ConnectionResolver(processName, processPid, handle = requestHandle)
  r.start()

  # overwrites halted instance of this resolver if it exists, otherwise append
  if haltedIndex == -1: RESOLVERS.append(r)
  else: RESOLVERS[haltedIndex] = r
  return r

class ConnectionResolver(threading.Thread):
  """
  Service that periodically queries for a process' current connections. This
  provides several benefits over on-demand queries:
  - queries are non-blocking (providing cached results)
  - falls back to use different resolution methods in case of repeated failures
  - avoids overly frequent querying of connection data, which can be demanding
    in terms of system resources

  Unless an overriding method of resolution is requested this defaults to
  choosing a resolver the following way:

  - Checks the current PATH to determine which resolvers are available. This
    uses the first of the following that's available:
      netstat, ss, lsof (picks netstat if none are found)

  - Attempts to resolve using the selection. Single failures are logged at the
    INFO level, and a series of failures at NOTICE. In the later case this
    blacklists the resolver, moving on to the next. If all resolvers fail this
    way then resolution's abandoned and logs a WARN message.

  The time between resolving connections, unless overwritten, is set to be
  either five seconds or ten times the runtime of the resolver (whichever is
  larger). This is to prevent systems either strapped for resources or with a
  vast number of connections from being burdened too heavily by this daemon.

  Parameters:
    processName       - name of the process being resolved
    processPid        - pid of the process being resolved
    resolveRate       - minimum time between resolving connections (in seconds,
                        None if using the default)
    * defaultRate     - default time between resolving connections
    lastLookup        - time connections were last resolved (unix time, -1 if
                        no resolutions have yet been successful)
    overwriteResolver - method of resolution (uses default if None)
    * defaultResolver - resolver used by default (None if all resolution
                        methods have been exhausted)
    resolverOptions   - resolvers to be cycled through (differ by os)

    * read-only
  """

  def __init__(self, processName, processPid = "", resolveRate = None, handle = None):
    """
    Initializes a new resolver daemon. When no longer needed it's suggested
    that this is stopped.

    Arguments:
      processName - name of the process being resolved
      processPid  - pid of the process being resolved
      resolveRate - time between resolving connections (in seconds, None if
                    chosen dynamically)
      handle      - name used to query this resolver, this is the processName
                    if undefined
    """

    threading.Thread.__init__(self)
    self.setDaemon(True)

    self.processName = processName
    self.processPid = processPid
    self.resolveRate = resolveRate
    self.handle = handle if handle else processName
    self.defaultRate = CONFIG["queries.connections.minRate"]
    self.lastLookup = -1
    self.overwriteResolver = None

    self.defaultResolver = None
    self.resolverOptions = connection.get_system_resolvers()

    log.info("Operating System: %s, Connection Resolvers: %s" % (os.uname()[0], ", ".join(self.resolverOptions)))

    if self.resolverOptions:
      self.defaultResolver = self.resolverOptions[0]

    self._connections = []        # connection cache (latest results)
    self._resolutionCounter = 0   # number of successful connection resolutions
    self._isPaused = False
    self._halt = False            # terminates thread if true
    self._cond = threading.Condition()  # used for pausing the thread
    self._subsiquentFailures = 0  # number of failed resolutions with the default in a row
    self._resolverBlacklist = []  # resolvers that have failed to resolve

    # Number of sequential times the threshold rate's been too low. This is to
    # avoid having stray spikes up the rate.
    self._rateThresholdBroken = 0

  def getOverwriteResolver(self):
    """
    Provides the resolver connection resolution is forced to use. This returns
    None if it's dynamically determined.
    """

    return self.overwriteResolver

  def setOverwriteResolver(self, overwriteResolver):
    """
    Sets the resolver used for connection resolution, if None then this is
    automatically determined based on what is available.

    Arguments:
      overwriteResolver - connection resolver to be used
    """

    self.overwriteResolver = overwriteResolver

  def run(self):
    while not self._halt:
      minWait = self.resolveRate if self.resolveRate else self.defaultRate
      timeSinceReset = time.time() - self.lastLookup

      if self._isPaused or timeSinceReset < minWait:
        sleepTime = max(0.2, minWait - timeSinceReset)

        self._cond.acquire()
        if not self._halt: self._cond.wait(sleepTime)
        self._cond.release()

        continue # done waiting, try again

      isDefault = self.overwriteResolver == None
      resolver = self.defaultResolver if isDefault else self.overwriteResolver

      # checks if there's nothing to resolve with
      if not resolver:
        self.lastLookup = time.time() # avoids a busy wait in this case
        continue

      try:
        resolveStart = time.time()
        time.sleep(2)
        from stem.util import log
        connResults = [(conn.local_address, conn.local_port, conn.remote_address, conn.remote_port) for conn in connection.get_connections(resolver, process_pid = self.processPid, process_name = self.processName)]

        lookupTime = time.time() - resolveStart

        self._connections = connResults
        self._resolutionCounter += 1

        newMinDefaultRate = 100 * lookupTime
        if self.defaultRate < newMinDefaultRate:
          if self._rateThresholdBroken >= 3:
            # adding extra to keep the rate from frequently changing
            self.defaultRate = newMinDefaultRate + 0.5

            log.trace("connection lookup time increasing to %0.1f seconds per call" % self.defaultRate)
          else: self._rateThresholdBroken += 1
        else: self._rateThresholdBroken = 0

        if isDefault: self._subsiquentFailures = 0
      except (ValueError, IOError), exc:
        # this logs in a couple of cases:
        # - special failures noted by getConnections (most cases are already
        # logged via system)
        # - note fail-overs for default resolution methods
        if str(exc).startswith("No results found using:"):
          log.info(exc)

        if isDefault:
          self._subsiquentFailures += 1

          if self._subsiquentFailures >= RESOLVER_FAILURE_TOLERANCE:
            # failed several times in a row - abandon resolver and move on to another
            self._resolverBlacklist.append(resolver)
            self._subsiquentFailures = 0

            # pick another (non-blacklisted) resolver
            newResolver = None
            for r in self.resolverOptions:
              if not r in self._resolverBlacklist:
                newResolver = r
                break

            if newResolver:
              # provide notice that failures have occurred and resolver is changing
              log.notice(RESOLVER_SERIAL_FAILURE_MSG % (resolver, newResolver))
            else:
              # exhausted all resolvers, give warning
              log.notice(RESOLVER_FINAL_FAILURE_MSG)

            self.defaultResolver = newResolver
      finally:
        self.lastLookup = time.time()

  def getConnections(self):
    """
    Provides the last queried connection results, an empty list if resolver
    has been halted.
    """

    if self._halt: return []
    else: return list(self._connections)

  def getResolutionCount(self):
    """
    Provides the number of successful resolutions so far. This can be used to
    determine if the connection results are new for the caller or not.
    """

    return self._resolutionCounter

  def getPid(self):
    """
    Provides the pid used to narrow down connection resolution. This is an
    empty string if undefined.
    """

    return self.processPid

  def setPid(self, processPid):
    """
    Sets the pid used to narrow down connection resultions.

    Arguments:
      processPid - pid for the process we're fetching connections for
    """

    self.processPid = processPid

  def setPaused(self, isPause):
    """
    Allows or prevents further connection resolutions (this still makes use of
    cached results).

    Arguments:
      isPause - puts a freeze on further resolutions if true, allows them to
                continue otherwise
    """

    if isPause == self._isPaused: return
    self._isPaused = isPause

  def stop(self):
    """
    Halts further resolutions and terminates the thread.
    """

    self._cond.acquire()
    self._halt = True
    self._cond.notifyAll()
    self._cond.release()

class AppResolver:
  """
  Provides the names and pids of appliations attached to the given ports. This
  stops attempting to query if it fails three times without successfully
  getting lsof results.
  """

  def __init__(self, scriptName = "python"):
    """
    Constructs a resolver instance.

    Arguments:
      scriptName - name by which to all our own entries
    """

    self.scriptName = scriptName
    self.queryResults = {}
    self.resultsLock = threading.RLock()
    self._cond = threading.Condition()  # used for pausing when waiting for results
    self.isResolving = False  # flag set if we're in the process of making a query
    self.failureCount = 0     # -1 if we've made a successful query

  def getResults(self, maxWait=0):
    """
    Provides the last queried results. If we're in the process of making a
    query then we can optionally block for a time to see if it finishes.

    Arguments:
      maxWait - maximum second duration to block on getting results before
                returning
    """

    self._cond.acquire()
    if self.isResolving and maxWait > 0:
      self._cond.wait(maxWait)
    self._cond.release()

    self.resultsLock.acquire()
    results = dict(self.queryResults)
    self.resultsLock.release()

    return results

  def resolve(self, ports):
    """
    Queues the given listing of ports to be resolved. This clears the last set
    of results when completed.

    Arguments:
      ports - list of ports to be resolved to applications
    """

    if self.failureCount < 3:
      self.isResolving = True
      t = threading.Thread(target = self._queryApplications, kwargs = {"ports": ports})
      t.setDaemon(True)
      t.start()

  def _queryApplications(self, ports=[]):
    """
    Performs an lsof lookup on the given ports to get the command/pid tuples.

    Arguments:
      ports - list of ports to be resolved to applications
    """

    # atagar@fenrir:~/Desktop/arm$ lsof -i tcp:51849 -i tcp:37277
    # COMMAND  PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
    # tor     2001 atagar   14u  IPv4  14048      0t0  TCP localhost:9051->localhost:37277 (ESTABLISHED)
    # tor     2001 atagar   15u  IPv4  22024      0t0  TCP localhost:9051->localhost:51849 (ESTABLISHED)
    # python  2462 atagar    3u  IPv4  14047      0t0  TCP localhost:37277->localhost:9051 (ESTABLISHED)
    # python  3444 atagar    3u  IPv4  22023      0t0  TCP localhost:51849->localhost:9051 (ESTABLISHED)

    if not ports:
      self.resultsLock.acquire()
      self.queryResults = {}
      self.isResolving = False
      self.resultsLock.release()

      # wakes threads waiting on results
      self._cond.acquire()
      self._cond.notifyAll()
      self._cond.release()

      return

    results = {}
    lsofArgs = []

    # Uses results from the last query if we have any, otherwise appends the
    # port to the lsof command. This has the potential for persisting dirty
    # results but if we're querying by the dynamic port on the local tcp
    # connections then this should be very rare (and definitely worth the
    # chance of being able to skip an lsof query altogether).
    for port in ports:
      if port in self.queryResults:
        results[port] = self.queryResults[port]
      else: lsofArgs.append("-i tcp:%s" % port)

    if lsofArgs:
      lsofResults = system.call("lsof -nP " + " ".join(lsofArgs))
    else: lsofResults = None

    if not lsofResults and self.failureCount != -1:
      # lsof query failed and we aren't yet sure if it's possible to
      # successfully get results on this platform
      self.failureCount += 1
      self.isResolving = False
      return
    elif lsofResults:
      # (iPort, oPort) tuple for our own process, if it was fetched
      ourConnection = None

      for line in lsofResults:
        lineComp = line.split()

        if len(lineComp) == 10 and lineComp[9] == "(ESTABLISHED)":
          cmd, pid, _, _, _, _, _, _, portMap, _ = lineComp

          if "->" in portMap:
            iPort, oPort = portMap.split("->")
            iPort = iPort.split(":")[1]
            oPort = oPort.split(":")[1]

            # entry belongs to our own process
            if pid == str(os.getpid()):
              cmd = self.scriptName
              ourConnection = (iPort, oPort)

            if iPort.isdigit() and oPort.isdigit():
              newEntry = (iPort, oPort, cmd, pid)

              # adds the entry under the key of whatever we queried it with
              # (this might be both the inbound _and_ outbound ports)
              for portMatch in (iPort, oPort):
                if portMatch in ports:
                  if portMatch in results:
                    results[portMatch].append(newEntry)
                  else: results[portMatch] = [newEntry]

      # making the lsof call generated an extraneous sh entry for our own connection
      if ourConnection:
        for ourPort in ourConnection:
          if ourPort in results:
            shIndex = None

            for i in range(len(results[ourPort])):
              if results[ourPort][i][2] == "sh":
                shIndex = i
                break

            if shIndex != None:
              del results[ourPort][shIndex]

    self.resultsLock.acquire()
    self.failureCount = -1
    self.queryResults = results
    self.isResolving = False
    self.resultsLock.release()

    # wakes threads waiting on results
    self._cond.acquire()
    self._cond.notifyAll()
    self._cond.release()

