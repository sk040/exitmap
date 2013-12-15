import functools
import threading
import multiprocessing
import socket

import stem
from stem import StreamStatus
from stem import CircStatus

import command
import mysocks
import util
import log

logger = log.getLogger()

class EventHandler( object ):

    """
    Implement a handler for asynchronous Tor events.

    The handler processes only stream and circuit events.  New streams are
    attached to their corresponding circuits since exitmap's Tor process leaves
    new streams unattached.
    """

    def __init__( self, torCtrl, probingModule, stats ):
        """
        Initialise an EventHandler object.
        """

        self.stats = stats
        self.attachers = {}
        self.torCtrl = torCtrl
        self.probingModule = probingModule
        self.finishedStreams = 0
        self.origsock = socket.socket

        self.ourStreamEvents = [ StreamStatus.NEW, StreamStatus.NEWRESOLVE,
                                 StreamStatus.CLOSED, StreamStatus.FAILED,
                                 StreamStatus.DETACHED]
        self.ourCircuitEvents = [ CircStatus.BUILT, CircStatus.FAILED,
                                  CircStatus.CLOSED ]

        self.manager = multiprocessing.Manager()
        self.queue = self.manager.Queue()

        threading.Thread(target=self.queueReader, args=()).start()

        mysocks.setdefaultproxy(mysocks.PROXY_TYPE_SOCKS5, "127.0.0.1", 10000)

    def queueReader( self ):
        """
        Read (circuit ID, sockname) tuples from invoked probing modules.

        These tuples are then used to attach streams to their corresponding
        circuits.
        """

        logger.info("Starting to read from IPC queue.")

        while True:
            circID, sockname = self.queue.get()
            # This is our signal to stop.
            if (circID == None) and (sockname == None):
                break

            _, port = sockname[0], int(sockname[1])
            logger.debug("Read from queue: %s, %s" % (circID, str(sockname)))

            # If we already have the corresponding stream ID, we can attach
            # right now.  Otherwise, we store an attaching function for later
            # use.
            if self.attachers.has_key(port):
                attacher = self.attachers[port]
                attacher(circuitID=circID)
                del self.attachers[port]
            else:
                self.attachers[port] = functools.partial(self._attachStream,
                                                         circuitID=circID)

        logger.info("Stopping to read from IPC queue.")

    def isFinished( self ):
        """
        Check if the scan is finished and if it is, shut down exitmap.
        """

        # Did all circuits either build or fail?
        circsDone = (self.stats.failedCircuits + \
                     self.stats.successfulCircuits) == self.stats.totalCircuits

        # Was every built circuit attached to a stream?
        streamsDone = (self.finishedStreams >= self.stats.successfulCircuits)

        logger.debug("failedCircs=%d, builtCircs=%d, totalCircs=%d, " \
                     "finishedStreams=%d" % (self.stats.failedCircuits,
                      self.stats.successfulCircuits, self.stats.totalCircuits,
                      self.finishedStreams))

        if circsDone and streamsDone:
            # Terminate the thread which handles the queue.
            socket.socket = mysocks._orgsocket
            self.queue.put((None, None))
            logger.info("Finished scan: %s" % self.stats)
            exit(0)

    def newCircuit( self, circEvent ):
        """
        Invoke a new probing module when a new circuit becomes ready.
        """

        if circEvent.status not in self.ourCircuitEvents:
            return

        # Keep track of how many circuits are already finished.
        if circEvent.status in [CircStatus.FAILED,  CircStatus.CLOSED]:
            logger.info("Circuit closed because: %s" % str(circEvent.reason))
            self.stats.failedCircuits += 1
            return

        self.stats.successfulCircuits += 1
        exitFpr = circEvent.path[-1][0]
        logger.info("Circuit for exit relay \"%s\" is built.  " \
                    "Now invoking probing module." % exitFpr)

        cmd = command.Command("/tmp/torsocks.conf", self.queue, circEvent.id,
                               self.origsock)
        socket.socket = mysocks.socksocket
        mysocks.setqueue(self.queue, circEvent.id)

        # Invoke the module in a dedicated process.
        proc = multiprocessing.Process(target=self.probingModule,
                                       args=(exitFpr,cmd,))
        proc.start()

    def newStream( self, streamEvent ):
        """
        Create a function which is later used to attach a stream to a circuit.

        The attaching cannot be done right now as we do not know the stream's
        desired circuit ID at this point.  So we set up all we can at this
        point and wait for the attaching to be done in queueReader().
        """

        if streamEvent.status not in self.ourStreamEvents:
            return

        # Keep track of how many streams are already finished.
        if streamEvent.status in [StreamStatus.CLOSED, StreamStatus.FAILED,
                                  StreamStatus.DETACHED]:
            self.finishedStreams += 1
            return

        sourcePort = util.getSourcePort(str(streamEvent))
        if not sourcePort:
            logger.error("Couldn't extract source port from stream event: %s" %
                         str(streamEvent))
            return

        logger.debug("Adding attacher for new stream %s." % streamEvent.id)

        if self.attachers.has_key(sourcePort):
            attacher = self.attachers[sourcePort]
            attacher(streamID=streamEvent.id)
            del self.attachers[port]
        else:
            # We maintain a dictionary of source ports which points to the
            # according attaching function.  We already know the stream ID but
            # not yet the circuit ID which is the reason why we cannot attach
            # at this point.
            self.attachers[sourcePort] = functools.partial(self._attachStream,
                                         streamID=streamEvent.id)

    def _attachStream( self, streamID=None, circuitID=None ):
        """
        Attach a stream to a circuit.
        """

        logger.debug("Attaching stream %s to circuit %s." %
                     (streamID, circuitID))

        try:
            self.torCtrl.attach_stream(streamID, circuitID)
        except stem.OperationFailed as err:
            logger.error("Couldn't attach stream: %s" % str(err))

    def newEvent( self, event ):
        """
        Dispatches new Tor controller events to the appropriate handlers.
        """

        if isinstance(event, stem.response.events.CircuitEvent):
            self.newCircuit(event)

        elif isinstance(event, stem.response.events.StreamEvent):
            self.newStream(event)

        else:
            logger.warning("Received unexpected event: " % str(event))

        self.isFinished()
