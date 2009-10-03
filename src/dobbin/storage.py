import logging
import mmap
import os
import re
import shutil
import threading
# import cPickle as pickle
import pickle
from cStringIO import StringIO

from fcntl import flock
from fcntl import LOCK_EX
from fcntl import LOCK_UN
from fcntl import LOCK_NB

from dobbin.exc import IntegrityError
from dobbin.persistent import Local
from dobbin.persistent import Persistent
from dobbin.persistent import PersistentFile
from dobbin.persistent import undo_persistent_local
from dobbin.utils import make_timestamp

# transaction log segment types
LOG_VERSION = 0
LOG_RECORD = 1
LOG_STREAM = 2

re_id = re.compile(r'(?P<protocol>[a-z]+)://(?P<token>[0-9:]+)')
logger = logging.getLogger('dobbin.storage')

class TransactionRecord(object):
    def __init__(self, tid, status):
        self.tid = tid
        self.status = status

class TransactionLog(object):
    """Log transactions to a single file.

    Note that ``_transaction`` is set to the transaction currently
    being committed if and only if we hold the commit-lock.
    """

    _offset = 0
    _tx_count = 0
    _transaction = None
    _rstream = None
    _wstream = None
    _oid = 0
    _writer = None
    _written = None

    def __init__(self, path, buffer_size=4194304):
        self.path = path

        # acquire locks
        l = threading.RLock()
        self._lock_acquire = l.acquire
        self._lock_release = l.release

        # open stream for reading
        self._open()

        # set configuration
        self._soft_buffer_size = buffer_size

        # pickle writer
        self._buffer = StringIO()
        self._pickler = pickle.Pickler(self._buffer)

    def __len__(self):
        """Return transaction count.

        We hold the lock to compare the actual transaction log
        filesize with our offset. If the storage is up-to-date, the
        transaction count is returned; else a runtime error is raised.

        Production code should not query the storage length under
        normal circumstances.
        """

        self._lock_acquire()

        try:
            if self._rstream is None and not self._open():
                return 0
            size = os.path.getsize(self._rstream.name)
            if size > self._offset:
                raise RuntimeError("Storage not up-to-date; length unknown.")
            return self._tx_count
        finally:
            self._lock_release()

    def read(self, jar):
        """Read transactions.

        This method yields tuples of (oid, class, state, timestamp).
        """

        self._lock_acquire()
        try:
            if self._rstream is None and not self._open():
                return

            try:
                _map = mmap.mmap(self._rstream.fileno(), 0, mmap.PROT_READ)
                _map.seek(self._offset)
            except (ValueError, mmap.error):
                return

            last_record = None
            try:
                for record, items in self._read(jar, _map):
                    self._tx_count += 1
                    for oid, cls, state in items:
                        yield oid, cls, state, record.tid
            finally:
                self._offset = _map.tell()
                _map.close()
        finally:
            self._lock_release()

    def commit(self, obj, transaction):
        """Serialize object and write to database.

        Note that oids are assigned when objects are committed to the
        database for the first time, e.g. with a call to this
        method.

        This method should only be called when the commit-lock is
        held.
        """

        # this asserts that we hold the commit-lock
        assert transaction is self._transaction
        assert isinstance(obj, Local)

        jar = obj._p_jar
        oid = obj._p_oid
        state = obj.__getstate__()
        cls = undo_persistent_local(obj.__class__)

        if oid is None:
            oid = self._new_oid(obj)

        deferred = []

        def persistent_id(obj):
            """This closure provides persistent identifier tokens for
            persistent objects and files.
            """

            if isinstance(obj, Persistent):
                if obj._p_jar is None:
                    jar.add(obj)

                oid = obj._p_oid
                if oid is None:
                    oid = self._new_oid(obj)

                return "oid://%d" % oid

            if isinstance(obj, PersistentStream):
                return "file://%d:%d" % (obj.offset, obj.length)

            if isinstance(obj, PersistentFile):
                # compute file length
                offset = obj.tell()
                obj.seek(0, os.SEEK_END)
                length = obj.tell() - offset
                obj.seek(offset)

                # write transaction log segment
                offset = self._write_unbuffered(LOG_STREAM, (obj.name, length))
                self._write_stream(obj, length)

                # switch identity to transaction stream
                obj.__dict__.clear()
                obj.__class__ = PersistentStream
                obj.__init__(self._opener, offset, length)

                return "file://%d:%d" % (offset, length)

            if isinstance(obj, file):
                raise TypeError(
                    "Can't persist files; use the ``PersistentFile`` wrapper.")

        self._pickler.persistent_id = persistent_id

        # pickle object state; note that the pickler instance is set
        # up to write to a buffer in memory --- the reason being that
        # pickling may fail, which is likely to result in integrity
        # errors if we write directly to disk (another critical
        # benefit is that the ``persistent_id`` method is free to
        # write data to disk, circumventing the pickle buffer; this is
        # used to write file streams in parallel with the pickle
        # operation); all in all: brittle machinery.
        self._write(LOG_VERSION, (oid, cls, state))

    def tpc_abort(self, transaction, timestamp):
        self._lock_acquire()
        try:
            if transaction is not self._transaction:
                return
            try:
                # write transaction record
                self._write(LOG_RECORD, TransactionRecord(timestamp, False))
                self._flush()
            finally:
                # update transaction state
                self._transaction = None
                self._tx_count +=1

                # update file pointer and close stream
                self._offset = self._wstream.tell()

                # release commit-lock
                self._commit_lock_release()

                # close stream
                self._wstream.close()
        finally:
            self._lock_release()

    def tpc_begin(self, transaction):
        self._lock_acquire()
        try:
            if self._transaction is transaction:
                return
            self._lock_release()

            # open write stream
            wstream = self._wstream = file(self.path, 'ab+')

            # acquire commit lock
            fd = wstream.fileno()
            self._commit_lock_acquire = lambda: flock(fd, LOCK_EX | LOCK_NB)
            self._commit_lock_release = lambda: flock(fd, LOCK_UN)

            try:
                self._commit_lock_acquire()
            except IOError:
                self._lock_acquire()
                self._transaction = None
                raise

            # acquire lock and store transaction
            self._lock_acquire()
            self._transaction = transaction
            self._tid = make_timestamp()

            # clear pickle memory; we shouldn't actually have to do
            # this---since we're anyway reading the log from the
            # beginning; XXX: look into this further
            self._pickler.clear_memo()
        finally:
            self._lock_release()

    def tpc_vote(self, transaction):
        self._lock_acquire()
        try:
            if transaction is not self._transaction:
                return
        finally:
            self._lock_release()

    def tpc_finish(self, transaction, timestamp):
        self._lock_acquire()
        try:
            if transaction is not self._transaction:
                return
            try:
                # write transaction record
                self._write(LOG_RECORD, TransactionRecord(timestamp, True))
                self._flush()
            finally:
                # update transaction state
                self._transaction = None
                self._tx_count +=1

                # update file pointer and close stream
                self._offset = self._wstream.tell()

                # release commit-lock
                self._commit_lock_release()

                # close stream
                self._wstream.close()
        finally:
            self._lock_release()

    def _flush(self, offset=0):
        stream = self._buffer

        # persist changes on disk
        stream.seek(offset)
        bytes = stream.read()
        self._wstream.write(bytes)

        # truncate stream
        stream.seek(offset)
        stream.truncate()

        return self._wstream.tell()

    def _open(self):
        if os.path.exists(self.path):
            f = self._rstream = file(self.path, 'rb+')
            return f

    def _opener(self):
        return file(self.path, 'rb')

    def _new_oid(self, obj):
        oid = obj._p_oid = self._oid + 1
        self._oid = oid
        return oid

    def _read(self, jar, stream):
        offset = stream.tell()
        size = stream.size()
        unpickler = pickle.Unpickler(stream)

        def load(oid):
            match = re_id.match(oid)
            if match is None:
                raise ValueError('Protocol mismatch: %s.' % oid)

            protocol = match.group('protocol')
            token = match.group('token')

            if protocol == 'oid':
                oid = int(token)
                return jar.get(oid)

            if protocol == 'file':
                offset, length = map(int, token.split(':'))
                return PersistentStream(self._opener, offset, length)

            raise ValueError('Unknown protocol: %s.' % protocol)

        unpickler.persistent_load = load

        entries = []
        while size > offset:
            segment_type, segment = unpickler.load()

            if segment_type == LOG_VERSION:
                entries.append(segment)

            elif segment_type == LOG_RECORD:
                yield segment, entries
                del entries[:]

            elif segment_type == LOG_STREAM:
                name, length = segment
                stream.seek(length, os.SEEK_CUR)

            offset = stream.tell()

        if entries:
            raise IntegrityError(
                "Transaction record not found for %d entries." % len(entries))

    def _write(self, segment_type, data):
        try:
            self._pickler.dump((segment_type, data))
        except:
            logger.critical("Could not pickle data: %s (type %d)." % (
                repr(data), segment_type))
            self._flush()
            raise

    def _write_unbuffered(self, segment_type, data):
        offset = self._buffer.tell()
        self._write(segment_type, data)
        return self._flush(offset)

    def _write_stream(self, stream, length):
        shutil.copyfileobj(stream, self._wstream, length)

class PersistentStream(threading.local):
    """Binary stream persisted in the transaction log.

    Features a file-like API as well as iteration (independent from
    each other; iteration will always acquire its own file handle).
    """

    stream = None
    chunk_size = 32768

    def __init__(self, opener, offset, length):
        self._opener = opener
        self.offset = offset
        self.length = length

    def __deepcopy__(self, memo):
        return self

    def __iter__(self):
        """Iterate through stream.

        We always open a new file handle, detached entirely from the
        instance. It's automatically closed when the handle is
        garbage-collected since it falls out of scope at the end of
        the method.
        """

        f = self._opener()
        f.seek(self.offset)

        remaining = self.length
        chunk_size = self.chunk_size
        read = self.read

        while remaining > 0:
            count = min(chunk_size, remaining)
            bytes = read(remaining, f)
            remaining -= len(bytes)
            yield bytes

    @property
    def closed(self):
        stream = self.stream
        if stream is None:
            return True
        return self.stream.closed

    @property
    def name(self):
        return self.stream.name

    def close(self):
        if self.stream is None:
            raise RuntimeError("File already closed.")

        self.stream.close()
        self.stream = None

    def open(self):
        if self.stream is not None:
            raise RuntimeError("File already open.")

        # open file for reading
        self.stream = self._opener()

        # seek to offset, if required
        if self.offset is not None:
            self.stream.seek(self.offset)

    def read(self, size=None, stream=None):
        if stream is None:
            stream = self.stream
            if stream is None:
                raise ValueError("File not open for reading.")
        if size is None:
            size = self.length
        return stream.read(min(size, self.length))

    def seek(self, offset, whence=os.SEEK_SET):
        if self.offset is not None:
            offset += self.offset
        self.stream.seek(offset, whence)

    def tell(self):
        offset = self.stream.tell()
        if self.offset is not None:
            offset -= self.offset
        return offset
