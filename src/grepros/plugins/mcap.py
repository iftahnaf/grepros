# -*- coding: utf-8 -*-
"""
MCAP input and output.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     14.10.2022
@modified    24.12.2022
------------------------------------------------------------------------------
"""
## @namespace grepros.plugins.mcap
from __future__ import absolute_import
import atexit
import copy
import io
import os
import time
import types

from .. import api as rosapi

try: import mcap, mcap.reader
except ImportError: mcap = None
if rosapi.ROS1:
    import genpy.dynamic
    try: import mcap_ros1 as mcap_ros, mcap_ros1.decoder, mcap_ros1.writer
    except ImportError: mcap_ros = None
elif rosapi.ROS2:
    try: import mcap_ros2 as mcap_ros, mcap_ros2.decoder, mcap_ros2.writer
    except ImportError: mcap_ros = None
else: mcap_ros = None
import yaml

from .. common import PATH_TYPES, ConsolePrinter, \
                      ensure_namespace, format_bytes, makedirs, plural, unique_path, verify_writable
from .. outputs import BaseSink


class McapBag(rosapi.Bag):
    """
    MCAP bag interface, providing most of rosbag.Bag interface.

    Bag cannot be appended to, and cannot be read and written at the same time
    (MCAP API limitation).
    """

    ## Supported opening modes
    MODES = ("r", "w")

    ## MCAP file header magic start bytes
    MCAP_MAGIC = b"\x89MCAP\x30\r\n"

    def __init__(self, filename, mode="r", **__):
        """Opens file and populates metadata."""
        if mode not in self.MODES: raise ValueError("invalid mode %r" % mode)

        if "w" == mode: makedirs(os.path.dirname(filename))
        self._mode           = mode
        self._topics         = {}     # {(topic, typename, typehash): message count}
        self._types          = {}     # {(typename, typehash): message type class}
        self._typedefs       = {}     # {(typename, typehash): type definition text}
        self._schemas        = {}     # {(typename, typehash): mcap.records.Schema}
        self._schematypes    = {}     # {mcap.records.Schema.id: (typename, typehash)}
        self._qoses          = {}     # {(topic, typename): [{qos profile dict}]}
        self._typefields     = {}     # {(typename, typehash): {field name: type name}}
        self._type_subtypes  = {}     # {(typename, typehash): {typename: typehash}}
        self._field_subtypes = {}     # {(typename, typehash): {field name: (typename, typehash)}}
        self._temporal_ctors = {}     # {typename: time/duration constructor}
        self._start_time     = None   # Bag start time, as UNIX timestamp
        self._end_time       = None   # Bag end time, as UNIX timestamp
        self._file           = None   # File handle
        self._reader         = None   # mcap.McapReader
        self._decoder        = None   # mcap_ros.Decoder
        self._writer         = None   # mcap_ros.Writer
        self._ttinfo         = None   # Cached result for get_type_and_topic_info()
        self._opened         = False  # Whether file has been opened at least once
        self._filename       = filename

        if rosapi.ROS2 and "r" == mode: self._temporal_ctors.update(
            (t, c) for c, t in rosapi.ROS_TIME_CLASSES.items() if rosapi.get_message_type(c) == t
        )

        self.open()


    def get_message_count(self, topic_filters=None):
        """
        Returns the number of messages in the bag.

        @param   topic_filters  list of topics or a single topic to filter by, if any
        """
        if topic_filters:
            topics = topic_filters
            topics = topics if isinstance(topics, (dict, list, set, tuple)) else [topics]
            return sum(c for (t, _, _), c in self._topics.items() if t in topics)
        return sum(self._topics.values())


    def get_start_time(self):
        """Returns the start time of the bag, as UNIX timestamp."""
        return self._start_time


    def get_end_time(self):
        """Returns the end time of the bag, as UNIX timestamp."""
        return self._end_time


    def get_message_class(self, typename, typehash=None):
        """
        Returns ROS message class for typename, or None if unknown type.

        @param   typehash  message type definition hash, if any
        """
        typehash = typehash or next((t for n, t in self._types if n == typename), None)
        typekey = (typename, typehash)
        if typekey not in self._types and typekey in self._typedefs:
            if rosapi.ROS2:
                name = typename.split("/")[-1]
                fields = rosapi.parse_definition_fields(typename, self._typedefs[typekey])
                self._types[typekey] = type(name, (types.SimpleNamespace, ), {
                    "__name__": name, "__slots__": list(fields),
                    "__repr__": message_repr, "__str__": message_repr
                })
            else:
                typeclses = genpy.dynamic.generate_dynamic(typename, self._typedefs[typekey])
                self._types[typekey] = typeclses[typename]

        return self._types.get(typekey)


    def get_message_definition(self, msg_or_type):
        """Returns ROS message type definition full text from bag, including subtype definitions."""
        if rosapi.is_ros_message(msg_or_type):
            return self._typedefs.get((rosapi.get_message_type(msg_or_type),
                                       rosapi.get_message_type_hash(msg_or_type)))
        typename = msg_or_type
        return next((d for (n, h), d in self._typedefs.items() if n == typename), None)


    def get_message_type_hash(self, msg_or_type):
        """Returns ROS message type MD5 hash."""
        typename = rosapi.get_message_type(msg_or_type)
        return next((h for n, h in self._typedefs if n == typename), None)


    def get_qoses(self, topic, typename):
        """Returns topic Quality-of-Service profiles as a list of dicts, or None if not available."""
        return self._qoses.get((topic, typename))


    def get_topic_info(self, *_, **__):
        """Returns topic and message type metainfo as {(topic, typename, typehash): count}."""
        return dict(self._topics)


    def get_type_and_topic_info(self, topic_filters=None):
        """
        Returns thorough metainfo on topic and message types.

        @param   topic_filters  list of topics or a single topic to filter by, if at all
        @return                 TypesAndTopicsTuple(msg_types, topics) namedtuple,
                                msg_types as dict of {typename: typehash},
                                topics as a dict of {topic: TopicTuple() namedtuple}.
        """
        if self._ttinfo: return self._ttinfo
        if self.closed: raise ValueError("I/O operation on closed file.")

        topics = topic_filters
        topics = topics if isinstance(topics, (list, set, tuple)) else [topics] if topics else []
        msgtypes = {n: h for t, n, h in self._topics if not topics or t in topics}
        topicdict = {}

        def median(vals):
            """Returns median value from given sorted numbers."""
            vlen = len(vals)
            return None if not vlen else vals[vlen // 2] if vlen % 2 else \
                   float(vals[vlen // 2 - 1] + vals[vlen // 2]) / 2

        for (t, n, _), c in sorted(self._topics.items()):
            if topics and t not in topics: continue  # for
            mymedian = None
            if c > 1 and self._reader:
                stamps = sorted(m.publish_time / 1E9 for _, _, m in self._reader.iter_messages([t]))
                mymedian = median(sorted(s1 - s0 for s1, s0 in zip(stamps[1:], stamps[:-1])))
            freq = 1.0 / mymedian if mymedian else None
            topicdict[t] = self.TopicTuple(n, c, len(self._qoses.get((t, n)) or []), freq)
        self._ttinfo = self.TypesAndTopicsTuple(msgtypes, topicdict)
        return self._ttinfo


    def read_messages(self, topics=None, start_time=None, end_time=None, raw=False):
        """
        Yields messages from the bag, optionally filtered by topic and timestamp.

        @param   topics      list of topics or a single topic to filter by, if at all
        @param   start_time  earliest timestamp of message to return, as ROS time or convertible
                             (int/float/duration/datetime/decimal)
        @param   end_time    latest timestamp of message to return, as ROS time or convertible
                             (int/float/duration/datetime/decimal)
        @param   raw         if true, then returned messages are tuples of
                             (typename, bytes, typehash, typeclass)
        @return              generator of (topic, message, ROS timestamp) tuples
        """
        if self.closed: raise ValueError("I/O operation on closed file.")
        if "w" == self._mode: raise io.UnsupportedOperation("read")

        topics = topics if isinstance(topics, list) else [topics] if topics else []
        start_ns, end_ns = (rosapi.to_nsec(rosapi.to_time(x)) for x in (start_time, end_time))
        for schema, channel, message in self._reader.iter_messages(topics, start_ns, end_ns):
            if raw:
                typekey = (typename, typehash) = self._schematypes[schema.id]
                if typekey not in self._types:
                    self._types[typekey] = self._make_message_class(schema, message)
                msg = (typename, message.data, typehash, self._types[typekey])
            else: msg = self._decode_message(message, channel, schema)
            yield self.BagMessage(channel.topic, msg, rosapi.make_time(nsecs=message.publish_time))


    def write(self, topic, msg, t=None, raw=False, **__):
        """
        Writes out message to MCAP file.

        @param   topic   name of topic
        @param   msg     ROS1 message
        @param   t       message timestamp if not using current ROS time,
                         as ROS time type or convertible (int/float/duration/datetime/decimal)
        @param   raw     if true, `msg` is in raw format, (typename, bytes, typehash, typeclass)
        """
        if self.closed: raise ValueError("I/O operation on closed file.")
        if "r" == self._mode: raise io.UnsupportedOperation("write")

        if raw:
            typename, binary, typehash = msg[:3]
            cls = msg[-1]
            typedef = self._typedefs.get((typename, typehash)) or rosapi.get_message_definition(cls)
            msg = rosapi.deserialize_message(binary, cls)
        else:
            with rosapi.TypeMeta.make(msg, topic) as meta:
                typename, typehash, typedef = meta.typename, meta.typehash, meta.definition
        topickey, typekey = (topic, typename, typehash), (typename, typehash)

        nanosec = (time.time_ns() if hasattr(time, "time_ns") else int(time.time() * 10**9)) \
                  if t is None else rosapi.to_nsec(rosapi.to_time(t))
        if rosapi.ROS2:
            if typekey not in self._schemas:
                fullname = rosapi.make_full_typename(typename)
                schema = self._writer.register_msgdef(fullname, typedef)
                self._schemas[typekey] = schema
            schema, data = self._schemas[typekey], rosapi.message_to_dict(msg)
            self._writer.write_message(topic, schema, data, publish_time=nanosec)
        else:
            self._writer.write_message(topic, msg, publish_time=nanosec)

        sec = nanosec / 1E9
        self._start_time = sec if self._start_time is None else min(sec, self._start_time)
        self._end_time   = sec if self._end_time   is None else min(sec, self._end_time)
        self._topics[topickey] = self._topics.get(topickey, 0) + 1
        self._types.setdefault(typekey, type(msg))
        self._typedefs.setdefault(typekey, typedef)
        self._ttinfo = None


    def open(self):
        """Opens the bag file if not already open."""
        if self._file: return
        if self._opened and "w" == self._mode:
            raise io.UnsupportedOperation("Cannot reopen bag for writing.")

        self._file    = open(self._filename, "%sb" % self._mode)
        self._reader  = mcap.reader.make_reader(self._file) if "r" == self._mode else None
        self._decoder = mcap_ros.decoder.Decoder()          if "r" == self._mode else None
        self._writer  = mcap_ros.writer.Writer(self._file)  if "w" == self._mode else None
        if "r" == self._mode: self._populate_meta()
        self._opened = True


    def close(self):
        """Closes the bag file."""
        if self._file:
            self._file.close()
            if self._writer: self._writer.finish()
            self._file, self._reader, self._writer = None, None, None


    @property
    def closed(self):
        """Returns whether file is closed."""
        return not self._file


    @property
    def topics(self):
        """Returns the list of topics in bag, in alphabetic order."""
        return sorted((t for t, _, _ in self._topics), key=str.lower)


    @property
    def filename(self):
        """Returns bag file path."""
        return self._filename


    @property
    def size(self):
        """Returns current file size."""
        return os.path.getsize(self._filename) if os.path.isfile(self._filename) else None


    @property
    def mode(self):
        """Returns file open mode."""
        return self._mode


    def __contains__(self, key):
        """Returns whether bag contains given topic."""
        return any(key == t for t, _, _ in self._topics)


    def _decode_message(self, message, channel, schema):
        """
        Returns ROS message deserialized from binary data.

        @param   message  mcap.records.Message instance
        @param   channel  mcap.records.Channel instance for message
        @param   shcema   mcap.records.Schema instance for message type
        """
        cls = self._make_message_class(schema, message, generate=False)
        if rosapi.ROS2 and not isinstance(cls, types.SimpleNamespace):
            msg = rosapi.deserialize_message(message.data, cls)
        else:
            msg = self._decoder.decode(schema=schema, message=message)
            if rosapi.ROS2:  # MCAP ROS2 message classes need monkey-patching with expected API
                msg = self._patch_message(msg, *self._schematypes[schema.id])
                # Register serialized binary, as MCAP does not support serializing its own creations
                rosapi.TypeMeta.make(msg, channel.topic, data=message.data)
        typekey = self._schematypes[schema.id]
        if typekey not in self._types: self._types[typekey] = type(msg)
        return msg


    def _make_message_class(self, schema, message, generate=True):
        """
        Returns message type class, generating if not already available.

        @param   shcema    mcap.records.Schema instance for message type
        @param   message   mcap.records.Message instance
        @param   generate  generate message class dynamically if not available
        """
        typekey = (typename, typehash) = self._schematypes[schema.id]
        if rosapi.ROS2 and typekey not in self._types:
            try:  # Try loading class from disk for full compatibility
                cls = rosapi.get_message_class(typename)
                clshash = rosapi.get_message_type_hash(cls)
                if typehash == clshash: self._types[typekey] = cls
            except Exception: pass  # ModuleNotFoundError, AttributeError etc
        if typekey not in self._types and generate:
            if rosapi.ROS2:  # MCAP ROS2 message classes need monkey-patching with expected API
                msg = self._decoder.decode(schema=schema, message=message)
                self._types[typekey] = self._patch_message_class(type(msg), typename, typehash)
            else:
                typeclses = genpy.dynamic.generate_dynamic(typename, schema.data.decode())
                self._types[typekey] = typeclses[typename]
        return self._types.get(typekey)


    def _patch_message_class(self, cls, typename, typehash):
        """
        Patches MCAP ROS2 message class with expected attributes and methods, recursively.

        @param   cös       ROS message class as returned from mcap_ros2.decoder
        @param   typename  ROS message type name, like "std_msgs/Bool"
        @param   typehash  ROS message type hash
        @return            patched class
        """
        typekey = (typename, typehash)
        if typekey not in self._typefields:
            fields = rosapi.parse_definition_fields(typename, self._typedefs[typekey])
            self._typefields[typekey] = fields
            self._field_subtypes[typekey] = {n: (s, h) for n, t in fields.items()
                                             for s in [rosapi.scalar(t)]
                                             if s not in rosapi.ROS_BUILTIN_TYPES
                                             for h in [self._type_subtypes[typekey][s]]}

        # mcap_ros2 creates a dynamic class for each message, having __slots__
        # but no other ROS2 API hooks; even the class module is "mcap_ros2.dynamic".
        cls.__module__ = typename.split("/", maxsplit=1)[0]
        cls.SLOT_TYPES = ()  # rosidl_runtime_py.utilities.is_message() checks for presence
        cls._fields_and_field_types = dict(self._typefields[typekey])
        cls.get_fields_and_field_types = message_get_fields_and_field_types
        cls.__copy__     = copy_with_slots  # MCAP message classes lack support for copy
        cls.__deepcopy__ = deepcopy_with_slots

        return cls


    def _patch_message(self, message, typename, typehash):
        """
        Patches MCAP ROS2 message with expected attributes and methods, recursively.

        @param   message   ROS message instance as returned from mcap_ros2.decoder
        @param   typename  ROS message type name, like "std_msgs/Bool"
        @param   typehash  ROS message type hash
        @return            original message patched, or new instance if ROS2 temporal type
        """
        result = message
        # [(field value, (field type name, field type hash), parent, (field name, ?array index))]
        stack = [(message, (typename, typehash), None, ())]
        while stack:
            msg, typekey, parent, path = stack.pop(0)
            mycls, typename = type(msg), typekey[0]

            if typename in self._temporal_ctors:
                # Convert temporal types to ROS2 temporal types for grepros to recognize
                msg2 = self._temporal_ctors[typename](sec=msg.sec, nanosec=msg.nanosec)
                if msg is message: result = msg2                     # Replace input message
                elif len(path) == 1: setattr(parent, path[0], msg2)  # Set scalar field
                else: getattr(parent, path[0])[path[1]] = msg2       # Set array field element
                continue  # while stack

            self._patch_message_class(mycls, *typekey)

            for name, subtypekey in self._field_subtypes[typekey].items():
                v = getattr(msg, name)
                if isinstance(v, list):  # Queue each array element for patching
                    stack.extend((x, subtypekey, msg, (name, i)) for i, x in enumerate(v))
                else:  # Queue scalar field for patching
                    stack.append((v, subtypekey, msg, (name, )))

        return result


    def _populate_meta(self):
        """Populates bag metainfo."""
        summary = self._reader.get_summary()
        self._start_time = summary.statistics.message_start_time / 1E9
        self._end_time   = summary.statistics.message_end_time   / 1E9

        defhashes = {}  # Cached {type definition full text: type hash}
        for cid, channel in summary.channels.items():
            schema = summary.schemas[channel.schema_id]
            topic, typename = channel.topic, rosapi.canonical(schema.name)

            typedef = schema.data.decode("utf-8")  # Full definition including subtype definitions
            subtypedefs, nesting = rosapi.parse_definition_subtypes(typedef, nesting=True)
            typehash = channel.metadata.get("md5sum") or \
                       rosapi.calculate_definition_hash(typename, typedef,
                                                        tuple(subtypedefs.items()))
            topickey, typekey = (topic, typename, typehash), (typename, typehash)

            qoses = None
            if channel.metadata.get("offered_qos_profiles"):
                try: qoses = yaml.safe_load(channel.metadata["offered_qos_profiles"])
                except Exception as e:
                    ConsolePrinter.warn("Error parsing topic QoS profiles from %r: %s.",
                                        channel.metadata["offered_qos_profiles"], e)

            self._topics.setdefault(topickey, 0)
            self._topics[topickey] += summary.statistics.channel_message_counts[cid]
            self._typedefs[typekey] = typedef
            defhashes[typedef] = typehash
            for t, d in subtypedefs.items():  # Populate subtype definitions and hashes
                if d in defhashes: h = defhashes[d]
                else: h = rosapi.calculate_definition_hash(t, d, tuple(subtypedefs.items()))
                self._typedefs.setdefault((t, h), d)
                self._type_subtypes.setdefault(typekey, {})[t] = h
                defhashes[d] = h
            for t, subtypes in nesting.items():  # Populate all nested type references
                h = self._type_subtypes[typekey][t]
                for t2 in subtypes:
                    h2 = self._type_subtypes[typekey][t2]
                    self._type_subtypes.setdefault((t, h), {})[t2] = h2

            if qoses: self._qoses[topickey] = qoses
            self._schemas[typekey] = schema
            self._schematypes[schema.id] = typekey


    @classmethod
    def autodetect(cls, filename):
        """Returns whether file is readable as MCAP format."""
        result = os.path.isfile(filename)
        if result and os.path.getsize(filename):
            with open(filename, "rb") as f:
                result = (f.read(len(cls.MCAP_MAGIC)) == cls.MCAP_MAGIC)
        else:
            ext = os.path.splitext(filename or "")[-1].lower()
            result = ext in McapSink.FILE_EXTENSIONS
        return result



def copy_with_slots(self):
    """Returns a shallow copy, with slots populated manually."""
    result = self.__class__.__new__(self.__class__)
    for n in self.__slots__:
        setattr(result, n, copy.copy(getattr(self, n)))
    return result


def deepcopy_with_slots(self, memo):
    """Returns a deep copy, with slots populated manually."""
    result = self.__class__.__new__(self.__class__)
    for n in self.__slots__:
        setattr(result, n, copy.deepcopy(getattr(self, n), memo))
    return result


def message_get_fields_and_field_types(self):
    """Returns a map of message field names and types (patch method for MCAP message classes)."""
    return self._fields_and_field_types.copy()


def message_repr(self):
    """Returns a string representation of ROS message."""
    fields = ", ".join("%s=%r" % (n, getattr(self, n)) for n in self.__slots__)
    return "%s(%s)" % (self.__name__, fields)



class McapSink(BaseSink):
    """Writes messages to MCAP file."""

    ## Auto-detection file extensions
    FILE_EXTENSIONS = (".mcap", )

    ## Constructor argument defaults
    DEFAULT_ARGS = dict(META=False, WRITE_OPTIONS={}, VERBOSE=False)


    def __init__(self, args=None, **kwargs):
        """
        @param   args                 arguments as namespace or dictionary, case-insensitive;
                                      or a single path as the file to write
        @param   args.META            whether to print metainfo
        @param   args.WRITE           base name of MCAP files to write
        @param   args.WRITE_OPTIONS   {"overwrite": whether to overwrite existing file
                                                    (default false)}
        @param   args.VERBOSE         whether to print debug information
        @param   kwargs               any and all arguments as keyword overrides, case-insensitive
        """
        args = {"WRITE": str(args)} if isinstance(args, PATH_TYPES) else args
        args = ensure_namespace(args, McapSink.DEFAULT_ARGS, **kwargs)
        super(McapSink, self).__init__(args)

        self._filename      = None  # Output filename
        self._file          = None  # Open file() object
        self._writer        = None  # mcap_ros.writer.Writer object
        self._schemas       = {}    # {(typename, typehash): mcap.records.Schema}
        self._overwrite     = (args.WRITE_OPTIONS.get("overwrite") in (True, "true"))
        self._close_printed = False

        atexit.register(self.close)


    def validate(self):
        """
        Returns whether required libraries are available (mcap, mcap_ros1/mcap_ros2)
        and overwrite is valid and file is writable.
        """
        ok, mcap_ok, mcap_ros_ok = True, bool(mcap), bool(mcap_ros)
        if self.args.WRITE_OPTIONS.get("overwrite") not in (None, True, False, "true", "false"):
            ConsolePrinter.error("Invalid overwrite option for MCAP: %r. "
                                 "Choose one of {true, false}.",
                                 self.args.WRITE_OPTIONS["overwrite"])
            ok = False
        if not mcap_ok:
            ConsolePrinter.error("mcap not available: cannot work with MCAP files.")
        if not mcap_ros_ok:
            ConsolePrinter.error("mcap_ros%s not available: cannot work with MCAP files.",
                                 rosapi.ROS_VERSION or "")
        if not verify_writable(self.args.WRITE):
            ok = False
        return ok and mcap_ok and mcap_ros_ok


    def emit(self, topic, msg, stamp=None, match=None, index=None):
        """Writes out message to MCAP file."""
        self._ensure_open()
        stamp, index = self._ensure_stamp_index(topic, msg, stamp, index)
        kwargs = dict(publish_time=rosapi.to_nsec(stamp), sequence=index)
        if rosapi.ROS2:
            with rosapi.TypeMeta.make(msg, topic) as m:
                typekey = m.typekey
                if typekey not in self._schemas:
                    fullname = rosapi.make_full_typename(m.typename)
                    self._schemas[typekey] = self._writer.register_msgdef(fullname, m.definition)
            schema, data = self._schemas[typekey], rosapi.message_to_dict(msg)
            self._writer.write_message(topic, schema, data, **kwargs)
        else:
            self._writer.write_message(topic, msg, **kwargs)
        super(McapSink, self).emit(topic, msg, stamp, match, index)


    def close(self):
        """Closes output file if open."""
        if self._writer:
            self._writer.finish()
            self._file.close()
            self._file, self._writer = None, None
        if not self._close_printed and self._counts:
            self._close_printed = True
            ConsolePrinter.debug("Wrote %s in %s to %s (%s).",
                                 plural("message", sum(self._counts.values())),
                                 plural("topic", self._counts), self._filename,
                                 format_bytes(os.path.getsize(self._filename)))
        super(McapSink, self).close()


    def _ensure_open(self):
        """Opens output file if not already open."""
        if self._file: return

        self._filename = self.args.WRITE if self._overwrite else unique_path(self.args.WRITE)
        makedirs(os.path.dirname(self._filename))
        if self.args.VERBOSE:
            sz = os.path.exists(self._filename) and os.path.getsize(self._filename)
            action = "Overwriting" if sz and self._overwrite else "Creating"
            ConsolePrinter.debug("%s %s.", action, self._filename)
        self._file = open(self._filename, "wb")
        self._writer = mcap_ros.writer.Writer(self._file)


def init(*_, **__):
    """Adds MCAP support to reading and writing. Raises ImportWarning if libraries not available."""
    if not mcap or not mcap_ros:
        ConsolePrinter.error("mcap libraries not available: cannot work with MCAP files.")
        raise ImportWarning()
    from .. import plugins  # Late import to avoid circular
    plugins.add_write_format("mcap", McapSink, "MCAP", [
        ("overwrite=true|false",  "overwrite existing file in MCAP output\n"
                                  "instead of appending unique counter (default false)"),
    ])
    rosapi.BAG_EXTENSIONS += McapSink.FILE_EXTENSIONS
    rosapi.Bag.READER_CLASSES.add(McapBag)
    rosapi.Bag.WRITER_CLASSES.add(McapBag)


__all__ = ["McapBag", "McapSink", "init"]
