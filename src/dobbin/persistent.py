import copy
import os
import threading
import transaction

from dobbin.exc import ObjectGraphError

MARKER = object()
DELETE = object()
IGNORE = object()

setattr = object.__setattr__

def checkout(obj):
    """Puts the object in local state, such that the object can be
    changed without breaking the data integrity of other threads."""

    if not isinstance(obj, Persistent):
        raise TypeError("Object is not persistent.")

    # upgrade shared objects to local; note that it's not an error to
    # check out an already local object
    if not isinstance(obj, Local):
        d = obj.__dict__
        cls = make_persistent_local(obj.__class__)
        setattr(obj, '__class__', cls)
        obj._p_init_(d)

    if obj._p_jar is None:
        sync(obj)
    else:
        if obj._p_oid is None:
            obj._p_jar.add(obj)
        else:
            obj._p_jar.save(obj)

def retract(obj):
    """Returns the object to shared state."""

    if not isinstance(obj, Local):
        raise TypeError("Object is not local-persistent.")

    cls = undo_persistent_local(type(obj))
    object.__setattr__(obj, "__class__", cls)
    del obj._p_local
    return obj

def make_persistent_local(cls):
    """Returns a class that derives from ``Local``."""

    return type(cls.__name__, (Local, cls), {})

def undo_persistent_local(cls):
    """Returns the class that was made local."""

    return cls.__bases__[1]

def update_local(inst, _p_local_dict):
    """Updates local dictionary with a deep copy of the shared state."""

    __dict__ = object.__getattribute__(inst, "__dict__")
    _p_local_dict.update(copy.deepcopy(__dict__))

class Persistent(object):
    """Persistent base class.

    The methods provided by this class are mostly there to protect
    users from using the database in a way that could cause integrity
    errors.

    It's also a marker for the database to know that this object
    should have its own identity in the database.
    """

    _p_jar = None
    _p_oid = None
    _p_serial = None
    _p_resolve_conflict = None

    def __new__(cls, *args, **kwargs):
        inst = object.__new__(cls)
        checkout(inst)
        return inst

    @property
    def _p_shared(self):
        return self.__dict__

    def __deepcopy__(self, memo):
        # persistent objects are never deep-copied
        return self

    def __getstate__(self):
        raise RuntimeError(
            "Shared persistent objects are not serializable.")

    def __setattr__(self, key, value):
        raise RuntimeError("Can't set attribute in read-only mode.")

    def __setitem__(self, key, value):
        raise RuntimeError("Can't set item in read-only mode.")

    def __setstate__(self, new_state):
        state = self._p_shared
        for key, value in new_state.items():
            if value is DELETE:
                del state[key]
            if value is IGNORE:
                continue
            else:
                state[key] = value

class Broken(Persistent):
    def __new__(cls, oid):
        inst = object.__new__(cls)
        inst.__dict__['_p_oid'] = oid
        return inst

class Local(Persistent):
    """Local persistent.

    This class is used internally be the database.

    Objects that derive from this class have thread-local state, which
    is prepared on-demand on a per-thread basis.

    Note that the ``__dict__`` attribute returns the thread-local
    dictionary. Applications get access the shared state from the
    ``_p_shared`` attribute; in general, they should not.
    """

    _p_count = 0
    _p_local = None
    _p_shared = None

    @property
    def __dict__(self):
        return self._p_local.__dict__

    def __getstate__(self):
        return self._p_local.__dict__

    def __setattr__(self, key, value):
        if isinstance(key, basestring):
            if key.startswith('_p_') or key.startswith('__'):
                self._p_shared[key] = value
                return

        self._p_local[key] = value

    def __getattr__(self, key):
        if isinstance(key, basestring):
            if key.startswith('_p_') or key.startswith('__'):
                return object.__getattribute__(self, key)

        local = self._p_local
        try:
            value = local[key]
            if value is DELETE:
                raise AttributeError(key)
            if value is not IGNORE:
                return value
        except KeyError:
            pass

        try:
            return self._p_shared[key]
        except KeyError:
            raise AttributeError(key)

    def _p_init_(self, shared):
        shared['_p_local'] = _local(shared)
        shared['_p_shared'] = shared

class _local(threading.local):
    """Thread local which proxies a shared dictionary."""

    __slots__ = "_p_dict"

    def __init__(self, d):
        self._p_dict = d

    def __delitem__(self, key):
        self[key] = DELETE

    def __getitem__(self, key):
        return self.__dict__[key]

    def __setitem__(self, key, value):
        self.__dict__[key] = value

class PersistentDict(Persistent):
    """Persistent dictionary.

    We make sure mutable objects (both keys and values) are
    deep-copied when accessed from the proxied dictionary. This
    guarantees that items in the thread-local dictionary are
    always deep-copied for use by that thread.

    Caution: This means that iterating through a checked out
    ``PersistentDict`` can be expensive, if keys and/or values are
    non-persistent, mutable objects.
    """

    def __init__(self, state=None):
        if state is not None:
            self.__dict__.update(state)

    def __iter__(self):
        shared = self._p_shared
        local = shared.get("_p_local")

        # if there's no thread-local dictionary, just iterate through
        # the shared dictionary
        if local is None:
            for key in shared:
                if isinstance(key, tuple):
                    yield key[0]
            return

        # first iterate over local entries; we record each entry so
        # avoid duplicates when later iterating over shared entries
        keys = []
        d = local.__dict__
        for key in d:
            if isinstance(key, tuple):
                key = key[0]
                if key is DELETE:
                    continue

                keys.append(key)
                yield key

        for key in shared:
            if isinstance(key, tuple):
                key = key[0]
                if key not in keys:
                    # deep-copy the key; if it's not the same object, we
                    # set it on the local copy, with a marker value
                    new_key = copy.deepcopy(key)
                    if new_key is not key:
                        self[new_key] = IGNORE
                    yield new_key

    def __getattr__(self, key):
        try:
            return self._p_shared[key]
        except KeyError:
            raise AttributeError(key)

    def __setitem__(self, key, value):
        self.__setattr__((key,), value)

    def __getitem__(self, key):
        return self.__getattr__((key,))

    def get(self, key, default=None):
        shared = self._p_shared
        local = shared.get("_p_local")
        key = (key,)

        if local is not None:
            value = local.__dict__.get(key, MARKER)
            if value is not MARKER:
                return value

        return shared.get(key, default)

    def items(self):
        return [(key, self[key]) for key in self]

    def keys(self):
        return [key for key in self]

    def setdefault(self, key, default):
        value = self.get(key, MARKER)
        if value is not MARKER:
            return value
        self[key] = default
        return default

class PersistentFile(threading.local):
    """Persistent file.

    Pass an open file to persist it in the database. The file you pass
    in should not closed before the transaction ends (usually it will
    fall naturally out of scope, which prompts Python to close it).

    :param stream: open stream-like object

    Typical usage is the input-stream of an HTTP request.
    """

    def __init__(self, stream):
        self.stream = stream

    @property
    def name(self):
        return self.stream.name

    def tell(self):
        return self.stream.tell()

    def seek(self, offset, whence=os.SEEK_SET):
        return self.stream.seek(offset, whence)

    def read(self, size=-1):
        return self.stream.read(size)

class UnconnectedSync(threading.local):
    def __init__(self):
        transaction.manager.registerSynch(self)
        self._unconnected = set()

    def __call__(self, obj):
        self._unconnected.add(obj)

    def abort(self, tx):
        pass

    def afterCompletion(self, tx):
        pass

    def beforeCompletion(self, tx):
        if self._unconnected:
            transaction.get().join(self)

    def newTransaction(self, tx):
        pass

    def commit(self, tx):
        pass

    def sortKey(self):
        return -1

    def tpc_begin(self, tx):
        pass

    def tpc_abort(self, tx):
        self._unconnected.clear()

    def tpc_vote(self, tx):
        unconnected = self._unconnected
        while unconnected:
            obj = unconnected.pop()
            if obj._p_jar is None:
                unconnected.clear()
                raise ObjectGraphError(
                    "%s not connected to graph." % repr(obj))

    def tpc_finish(self, tx):
        self._unconnected.clear()

sync = UnconnectedSync()
