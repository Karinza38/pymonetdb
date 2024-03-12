# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0.  If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright 1997 - July 2008 CWI, August 2008 - 2016 MonetDB B.V.

import logging
from collections import namedtuple
import struct
from typing import List, Optional, Dict, Tuple, Type
from pymonetdb.policy import BatchPolicy
import pymonetdb.sql.connections
from pymonetdb.sql.debug import debug, export
from pymonetdb.sql import monetize, pythonize, pythonizebin
from pymonetdb.exceptions import Error, ProgrammingError, InterfaceError
from pymonetdb import mapi

logger = logging.getLogger("pymonetdb")

Description = namedtuple('Description', ('name', 'type_code', 'display_size', 'internal_size', 'precision', 'scale',
                                         'null_ok'))


class Cursor(object):
    """This object represents a database cursor, which is used to manage
    the context of a fetch operation. Cursors created from the same
    connection are not isolated, i.e., any changes done to the
    database by a cursor are immediately visible by the other
    cursors"""

    connection: 'pymonetdb.sql.connections.Connection'
    _policy: BatchPolicy
    operation: str

    arraysize: int
    """Default value for the size parameter of :func:`~pymonetdb.sql.cursors.Cursor.fetchmany`. """

    rowcount: int
    description: Optional[List[Description]]
    _can_bindecode: Optional[bool]
    _bindecoders: Optional[List['pythonizebin.BinaryDecoder']]
    rownumber: int
    _executed: Optional[str]
    _offset: int
    _rows: List[Tuple]
    _resultsets_to_close: List[str]
    _query_id: Optional[str]
    messages: List[Tuple[Type[Exception], str]]
    lastrowid: Optional[int]
    _unpack_int64: str

    _next_result_sets: List[Tuple[str, int, List[Description], List[Tuple]]]

    def __init__(self, connection: 'pymonetdb.sql.connections.Connection'):
        """This read-only attribute return a reference to the Connection
        object on which the cursor was created."""
        self.connection = connection
        self._policy = connection._policy.clone()

        # last executed operation (query)
        self.operation = ""

        # This read/write attribute specifies the number of rows to
        # fetch at a time with .fetchmany()
        self.arraysize = self._policy.decide_arraysize()

        # This read-only attribute specifies the number of rows that
        # the last .execute*() produced (for DQL statements like
        # 'select') or affected (for DML statements like 'update' or
        # 'insert').
        #
        # The attribute is -1 in case no .execute*() has been
        # performed on the cursor or the rowcount of the last
        # operation is cannot be determined by the interface.
        self.rowcount = -1

        # This read-only attribute is a sequence of 7-item
        # sequences.
        #
        # Each of these sequences contains information describing
        # one result column:
        #
        #   (name,
        #    type_code,
        #    display_size,
        #    internal_size,
        #    precision,
        #    scale,
        #    null_ok)
        #
        # This attribute will be None for operations that
        # do not return rows or if the cursor has not had an
        # operation invoked via the .execute*() method yet.
        self.description = None

        # When the opportunity presents itself to fetch a result set in binary
        # we need to know if we can handle the result and if so, how.
        #
        # These attributes are cleared by execute() and set by _nextchunk()
        self._can_bindecode = None
        self._bindecoders = None

        # This read-only attribute indicates at which row
        # we currently are
        self.rownumber = -1

        self._executed = None

        # the offset of the current resultset in the total resultset
        self._offset = 0

        # the resultset
        self._rows = []

        # ids of result sets that must eventually be closed on the server
        self._resultsets_to_close = []

        # used to identify a query during server contact.
        # Only select queries have query ID
        self._query_id = None

        # This is a Python list object to which the interface appends
        # tuples (exception class, exception value) for all messages
        # which the interfaces receives from the underlying database for
        # this cursor.
        #
        # The list is cleared by all standard cursor methods calls (prior
        # to executing the call) except for the .fetch*() calls
        # automatically to avoid excessive memory usage and can also be
        # cleared by executing "del cursor.messages[:]".
        #
        # All error and warning messages generated by the database are
        # placed into this list, so checking the list allows the user to
        # verify correct operation of the method calls.
        self.messages = []

        # This read-only attribute provides the rowid of the last
        # modified row (most databases return a rowid only when a single
        # INSERT operation is performed). If the operation does not set
        # a rowid or if the database does not support rowids, this
        # attribute should be set to None.
        #
        # The semantics of .lastrowid are undefined in case the last
        # executed statement modified more than one row, e.g. when
        # using INSERT with .executemany().
        self.lastrowid = None

        self._next_result_sets = []

        # This is used to unpack binary result sets
        server_endian = self.connection.mapi.server_endian
        if server_endian == 'little':
            unpacker = '<q'
        elif server_endian == 'big':
            unpacker = '>q'
        self._unpack_int64 = unpacker

    def _check_executed(self):
        if not self._executed:
            self._exception_handler(ProgrammingError, "do a execute() first")

    def _close_earlier_resultsets(self):
        for rs in self._resultsets_to_close:
            command = 'Xclose %s' % rs
            self.connection.command(command)
        del self._resultsets_to_close[:]

    def close(self):
        """ Close the cursor now (rather than whenever __del__ is
        called).  The cursor will be unusable from this point
        forward; an Error (or subclass) exception will be raised
        if any operation is attempted with the cursor."""

        try:
            self._close_earlier_resultsets()
        except Error:
            pass
        self.connection = None

    def __enter__(self):
        """This method is invoked when this Cursor is used in a with-statement.
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """This method is invoked when this Cursor is used in a with-statement.
        """
        try:
            self.close()
        except Error:
            pass
        # Propagate any errors
        return False

    def execute(self, operation: str, parameters: Optional[Dict] = None):  # noqa C901
        """Prepare and execute a database operation (query or
        command).  Parameters may be provided as mapping and
        will be bound to variables in the operation.
        """

        if not self.connection:
            self._exception_handler(ProgrammingError, "cursor is closed")

        # clear message history
        self.messages = []

        self._close_earlier_resultsets()

        # set the number of rows to fetch
        desired_replysize = self._policy.new_query()
        if self.connection._current_replysize != desired_replysize:
            self.connection._change_replysize(desired_replysize)

        if operation == self.operation:
            # same operation, DBAPI mentioned something about reuse
            # but monetdb doesn't support this
            pass
        else:
            self.operation = operation

        query = ""
        if parameters:
            if isinstance(parameters, dict):
                if pymonetdb.paramstyle == 'pyformat':
                    query = operation % {
                        k: monetize.convert(v)
                        for (k, v) in parameters.items()
                    }
                elif pymonetdb.paramstyle == 'named':
                    args = []
                    for k, v in parameters.items():
                        args.append('%s %s' % (k, monetize.convert(v)))
                    query = operation + ' : ( ' + ','.join(args) + ' )'
            elif isinstance(parameters, list) or isinstance(parameters, tuple):
                query = operation % tuple(
                    [monetize.convert(item) for item in parameters])
            elif isinstance(parameters, str):
                query = operation % monetize.convert(parameters)
            else:
                msg = "Parameters should be None, dict or list, now it is %s"
                self._exception_handler(ValueError, msg % type(parameters))
        else:
            query = operation

        block = self.connection.execute(query)
        self._store_result(block, update_existing=False)
        self.nextset()
        self._executed = operation
        return self.rowcount if self.rowcount >= 0 else None

    def executemany(self, operation, seq_of_parameters):
        """Prepare a database operation (query or command) and then
        execute it against all parameter sequences or mappings
        found in the sequence seq_of_parameters.

        It will return the number or rows affected
        """

        count = 0
        for parameters in seq_of_parameters:
            count += self.execute(operation, parameters)
        self.rowcount = count
        return count

    def debug(self, query, fname, sample=-1):
        """ Locally debug a given Python UDF function in a SQL query
            using the PDB debugger. Optionally can run on only a
            sample of the input data, for faster data export.
        """
        debug(self, query, fname, sample)

    def export(self, query, fname, sample=-1, filespath='./'):
        return export(self, query, fname, sample, filespath)

    def fetchone(self):
        """Fetch the next row of a query result set, returning a
        single sequence, or None when no more data is available."""

        self._check_executed()
        if self._query_id is None:
            msg = "query didn't result in a resultset"
            self._exception_handler(ProgrammingError, msg)

        cache_end = self._offset + len(self._rows)
        if self.rownumber >= cache_end:
            if self.rownumber >= self.rowcount:
                return None
            self._populate_cache(0, self.rownumber + 1)

        result = self._rows[self.rownumber - self._offset]
        self.rownumber += 1
        return result

    def fetchmany(self, size=None):
        """Fetch the next set of rows of a query result, returning a
        sequence of sequences (e.g. a list of tuples). An empty
        sequence is returned when no more rows are available.

        The number of rows to fetch per call is specified by the
        parameter.  If it is not given, the cursor's arraysize
        determines the number of rows to be fetched.

        A :class:`~pymonetdb.ProgrammingError` is raised if the previous
        call to .execute*() did not produce any result set or no
        call was issued yet."""

        self._check_executed()
        if self._query_id is None:
            msg = "query didn't result in a resultset"
            self._exception_handler(ProgrammingError, msg)

        if size is None:
            size = self.arraysize

        cache_end = self._offset + len(self._rows)
        requested_end = min(self.rownumber + size, self.rowcount)

        if requested_end <= cache_end:
            result = self._rows[self.rownumber - self._offset:requested_end - self._offset]
            self.rownumber = requested_end
        else:
            result = self._rows[self.rownumber - self._offset:cache_end - self._offset]
            self.rownumber = cache_end
            self._populate_cache(len(result), requested_end)
            result += self._rows[self.rownumber - self._offset:requested_end - self._offset]
            self.rownumber = requested_end

        return result

    def fetchall(self):
        """Fetch all remaining rows of a query result, returning
        them as a sequence of sequences (e.g. a list of tuples).

        A :class:`~pymonetdb.ProgrammingError` is raised if the previous
        call to .execute*() did not produce any result set or no
        call was issued yet."""

        return self.fetchmany(self.rowcount)

    def nextset(self) -> Optional[bool]:
        if not self._next_result_sets:
            return None

        (self._query_id, self.rowcount, self.description, self._rows) = self._next_result_sets[0]
        del self._next_result_sets[0]

        self._policy.new_query()
        self._offset = 0
        self.rownumber = 0
        self._can_bindecode = None
        self._bindecoders = None

        return True

    def _populate_cache(self, already_used, requested_end):
        del self._rows[:]
        self._offset = self.rownumber

        rows_to_fetch = self._policy.batch_size(
            already_used,
            self.rownumber, requested_end,
            self.rowcount)

        if self._can_bindecode is None:
            self._check_bindecode_possible()
        if self._can_bindecode:
            command = 'Xexportbin %s %s %s' % (self._query_id, self.rownumber, rows_to_fetch)
            binary_block = self.connection.binary_command(command)
            self._store_binary_result(binary_block)
        else:
            command = 'Xexport %s %s %s' % (self._query_id, self.rownumber, rows_to_fetch)
            block = self.connection.command(command)
            self._store_result(block, update_existing=True)

    def _check_bindecode_possible(self):
        self._can_bindecode = False
        decoders = []
        if not self.connection._policy.use_binary():
            return
        for i in range(len(self.description)):
            dec = pythonizebin.get_decoder(self, i)
            if not dec:
                return
            decoders.append(dec)
        # if we get here, all columns have a decoder
        self._can_bindecode = True
        self._bindecoders = decoders

    def setinputsizes(self, sizes):
        """
        This method would be used before the .execute*() method
        is invoked to reserve memory. This implementation doesn't
        use this.
        """
        pass

    def setoutputsize(self, size, column=None):
        """
        Set a column buffer size for fetches of large columns
        This implementation doesn't use this
        """
        pass

    def __iter__(self):
        return self

    def next(self):
        row = self.fetchone()
        if not row:
            raise StopIteration
        return row

    def __next__(self):
        return self.next()

    def _store_result(self, block, *, update_existing: bool):  # noqa: C901
        """ parses the mapi result into a resultset"""

        if not update_existing:
            self._next_result_sets = []

        if not block:
            block = ""

        columns = 0
        column_name: List[Optional[str]] = []
        scale: List[Optional[int]] = []
        display_size: List[Optional[int]] = []
        internal_size: List[Optional[int]] = []
        precision: List[Optional[int]] = []
        null_ok: List[Optional[bool]] = []
        type_: List[Optional[str]] = []

        msg_tuple = mapi.MSG_TUPLE
        assert len(msg_tuple) == 1
        msg_header = mapi.MSG_HEADER
        assert len(msg_header) == 1

        for line in block.split("\n"):
            first = line[:1]

            if first == msg_tuple:
                self._rows.append(self._parse_tuple(line))

            elif first == msg_header:
                (data, identity) = line[1:].split("#")
                values = [x.strip() for x in data.split(",")]
                identity = identity.strip()

                if identity == "name":
                    column_name = values
                elif identity == "table_name":
                    _ = values  # not used
                elif identity == "type":
                    type_ = values
                elif identity == "length":
                    _ = values  # not used
                elif identity == "typesizes":
                    typesizes = [[int(j) for j in i.split()] for i in values]
                    internal_size = [x[0] for x in typesizes]
                    for num, typeelem in enumerate(type_):
                        if typeelem in ['decimal']:
                            precision[num] = typesizes[num][0]
                            scale[num] = typesizes[num][1]
                else:
                    msg = "unknown header field: {}".format(identity)
                    logger.warning(msg)
                    self.messages.append((Warning, msg))

                description = self.description if self.description is not None else []
                description[:] = []
                for i in range(columns):
                    description.append(Description(column_name[i], type_[i], display_size[i], internal_size[i],
                                                   precision[i], scale[i], null_ok[i]))
                self.description = description
                self._offset = 0

            elif line.startswith(mapi.MSG_INFO):
                logger.info(line[1:])
                self.messages.append((Warning, line[1:]))

            elif line.startswith(mapi.MSG_QTABLE) or line.startswith(mapi.MSG_QPREPARE):
                query_id, rowcount, columns, tuples = line[2:].split()[:4]
                self._query_id = query_id

                columns = int(columns)  # number of columns in result
                tuples = int(tuples)     # number of rows in this set
                if tuples < self.rowcount:
                    self._resultsets_to_close.append(query_id)

                self.description = []
                self.rowcount = int(rowcount)  # total number of rows
                self._rows = []
                if not update_existing:
                    self._next_result_sets.append((query_id, self.rowcount, self.description, self._rows))

                # set up fields for description
                # table_name = [None] * columns
                column_name = [None] * columns
                type_ = [None] * columns
                display_size = [None] * columns
                internal_size = [None] * columns
                precision = [None] * columns
                scale = [None] * columns
                null_ok = [None] * columns
                # typesizes = [(0, 0)] * columns

                self._offset = 0
                if line.startswith(mapi.MSG_QPREPARE):
                    self.lastrowid = int(query_id)
                else:
                    self.lastrowid = None

            elif line.startswith(mapi.MSG_TUPLE_NOSLICE):
                self._rows.append((line[1:],))

            elif line.startswith(mapi.MSG_QBLOCK):
                self._rows = []

            elif line.startswith(mapi.MSG_QSCHEMA):
                self._offset = 0
                self.lastrowid = None
                self._rows = []
                self.description = None
                self.rowcount = -1

            elif line.startswith(mapi.MSG_QUPDATE):
                (affected, identity) = line[2:].split()[:2]
                self._offset = 0
                self._rows = []
                self.description = None
                self.rowcount = int(affected)
                self.lastrowid = int(identity)
                self._query_id = None

            elif line.startswith(mapi.MSG_QTRANS):
                self._offset = 0
                self.lastrowid = None
                self._rows = []
                self.description = None
                self.rowcount = -1

            elif line == mapi.MSG_PROMPT:
                return

            elif line.startswith(mapi.MSG_ERROR):
                self._exception_handler(ProgrammingError, line[1:])

        self._exception_handler(InterfaceError, "Unknown state, %s" % block)

    def _store_binary_result(self, block: memoryview):
        assert self._bindecoders is not None
        if len(block) < 8:
            self._exception_handler(InterfaceError, "binary response too short")

        toc_pos = struct.unpack_from(self._unpack_int64, block, len(block) - 8)[0]
        if toc_pos < 0:
            # It actually points to the error message.
            # The message ends at the first \x00.
            bmsg = bytes(block[toc_pos:-8])
            bmsg = bmsg.split(b'\x00', 1)[0]
            try:
                msg = str(bmsg, 'utf-8')
            except UnicodeDecodeError:
                self._exception_handler(InterfaceError, "invalid utf-8 in error message")
            self._exception_handler(ProgrammingError, msg)

        # if we get here toc_pos actually points to the toc.
        ncols = len(self._bindecoders)
        cols = []
        for i in range(ncols):
            # TODO fix endianness
            start_pos = toc_pos + 16 * i
            length_pos = start_pos + 8
            start = struct.unpack_from(self._unpack_int64, block, start_pos)[0]
            length = struct.unpack_from(self._unpack_int64, block, length_pos)[0]
            slice = block[start:start + length]
            decoder = self._bindecoders[i]
            col = decoder.decode(self.connection.mapi.server_endian, slice)
            cols.append(col)
        rows = list(zip(*cols))
        self._rows = rows

    def _parse_tuple(self, line):
        """
        parses a mapi data tuple, and returns a list of python types
        """
        elements = line[1:-1].split(',\t')
        if len(elements) == len(self.description):
            return tuple([pythonize.convert(element.strip(), description[1])
                          for (element, description) in zip(elements,
                                                            self.description)])
        else:
            self._exception_handler(InterfaceError, "length of row doesn't match header")

    def scroll(self, value, mode='relative'):
        """
        Scroll the cursor in the result set to a new position according
        to mode.

        If mode is 'relative' (default), value is taken as offset to
        the current position in the result set, if set to 'absolute',
        value states an absolute target position.

        An IndexError is raised in case a scroll operation would
        leave the result set.
        """
        self._check_executed()

        if mode not in ['relative', 'absolute']:
            msg = "unknown mode '%s'" % mode
            self._exception_handler(ProgrammingError, msg)

        if mode == 'relative':
            value += self.rownumber

        if self._offset <= value < self._offset + len(self._rows):
            self.rownumber = value
            return

        if value > self.rowcount:
            self._exception_handler(IndexError, "value beyond length of resultset")

        self.rownumber = value
        self._offset = value
        self._rows = []
        self._policy.scroll()

    def _exception_handler(self, exception_class, message):
        """
        raises the exception specified by exception, and add the error
        to the message list
        """
        self.messages.append((exception_class, message))
        raise exception_class(message)

    def get_replysize(self) -> int:
        return self._policy.replysize

    def set_replysize(self, replysize: int):
        self._policy.replysize = replysize

    replysize = property(get_replysize, set_replysize)

    def get_maxprefetch(self) -> int:
        return self._policy.maxprefetch

    def set_maxprefetch(self, maxprefetch: int):
        self._policy.maxprefetch = maxprefetch

    maxprefetch = property(get_maxprefetch, set_maxprefetch)

    def get_binary(self) -> int:
        return self._policy.binary_level

    def set_binary(self, level: int):
        self._policy.binary_level = level

    binary = property(get_binary, set_binary)

    def used_binary_protocol(self) -> bool:
        """Pymonetdb-specific. Return True if the last fetch{one,many,all}
        for the current statement made use of the binary protocol.

        Primarily used for testing.

        Note that the binary protocol is never used for the first few rows
        of a result set. Exactly when it kicks in depends on the
        `replysize` setting.
        """
        return self._can_bindecode is True  # True as opposed to False or None
