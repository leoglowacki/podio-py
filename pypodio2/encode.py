"""multipart/form-data encoding module

This module provides functions that faciliate encoding name/value pairs
as multipart/form-data suitable for a HTTP POST or PUT request.

multipart/form-data is the standard way to upload files over HTTP"""

import mimetypes
import os
import re
import urllib.parse
from email.header import Header

# Python 2->3 quick compatability shims
try:
    unicode
except NameError:            # running on Py-3
    unicode = str

# re-introduce the old cmp(a, b) helper
try:
    cmp  # Python 2 already has it
except NameError:                 # Python 3 – define it
    def cmp(a, b):
        """Return -1 if a < b, 0 if a == b, +1 if a > b."""
        return (a > b) - (a < b)


__all__ = ['gen_boundary', 'encode_and_quote', 'MultipartParam',
           'encode_string', 'encode_file_header', 'get_body_size', 'get_headers',
           'multipart_encode']

try:
    from io import UnsupportedOperation
except ImportError:
    UnsupportedOperation = None

try:
    import uuid

    def gen_boundary():
        """Returns a random string to use as the boundary for a message"""
        return uuid.uuid4().hex

except ImportError:
    import random
    import sha

    def gen_boundary():
        """Returns a random string to use as the boundary for a message"""
        bits = random.getrandbits(160)
        return sha.new(str(bits)).hexdigest()


def encode_and_quote(data):
    """If ``data`` is unicode, return urllib.parse.quote_plus(data.encode("utf-8"))
    otherwise return urllib.parse.quote_plus(data)"""
    if data is None:
        return None

    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return urllib.parse.quote_plus(data)


def _strify(s):
    """Return *s* coerced to str (UTF-8)."""
    if s is None:
        return None
    if isinstance(s, bytes):          # accept raw bytes from callers
        return s.decode('utf-8')
    return str(s)


class MultipartParam(object):
    """Represents a single parameter in a multipart/form-data request

    ``name`` is the name of this parameter.

    If ``value`` is set, it must be a string or unicode object to use as the
    data for this parameter.

    If ``filename`` is set, it is what to say that this parameter's filename
    is.  Note that this does not have to be the actual filename any local file.

    If ``filetype`` is set, it is used as the Content-Type for this parameter.
    If unset it defaults to "text/plain; charset=utf8"

    If ``filesize`` is set, it specifies the length of the file ``fileobj``

    If ``fileobj`` is set, it must be a file-like object that supports
    .read().

    Both ``value`` and ``fileobj`` must not be set, doing so will
    raise a ValueError assertion.

    If ``fileobj`` is set, and ``filesize`` is not specified, then
    the file's size will be determined first by stat'ing ``fileobj``'s
    file descriptor, and if that fails, by seeking to the end of the file,
    recording the current position as the size, and then by seeking back to the
    beginning of the file.

    ``cb`` is a callable which will be called from iter_encode with (self,
    current, total), representing the current parameter, current amount
    transferred, and the total size.
    """

    def __init__(self, name, value=None, filename=None,
                 filetype="text/plain; charset=utf-8",
                 filesize=None, fileobj=None, cb=None):

        # Header-name must be pure ASCII; let email.Header handle RFC-2047
        self.name = str(Header(name, "utf-8"))     # ← text, not bytes

        # Normalise simple text fields
        self.value     = _strify(value)
        self.filetype  = _strify(filetype)

        # -------- filename handling ----------
        if filename is None:
            self.filename = None
        else:
            # Ensure we start with text
            if isinstance(filename, bytes):
                filename = filename.decode("utf-8", "replace")

            # XML-escape any lone non-ASCII code points → ASCII text
            xml_safe = filename.encode("ascii", "xmlcharrefreplace")  \
                               .decode("ascii")

            # Backslash-escape for the multipart header value
            self.filename = (xml_safe
                             .encode("unicode_escape")  # Py-3 codec
                             .decode("ascii")           # back to str
                             .replace('"', r'\"'))

            # Alternatively, for modern user-agents:
            # self.filename = urllib.parse.quote(filename, safe="")

        # -------- binary payload ----------
        if self.value is not None and fileobj is not None:
            raise ValueError("Only one of value or fileobj may be specified")

        self.fileobj  = fileobj
        self.cb       = cb

        # Probe size if caller did not supply it
        if fileobj is not None and filesize is None:
            try:
                self.filesize = os.fstat(fileobj.fileno()).st_size
            except (OSError, AttributeError, UnsupportedOperation):
                try:
                    fileobj.seek(0, os.SEEK_END)
                    self.filesize = fileobj.tell()
                    fileobj.seek(0)
                except Exception as exc:
                    raise ValueError("Could not determine filesize") from exc
        else:
            self.filesize = filesize


    def __cmp__(self, other):
        attrs = ['name', 'value', 'filename', 'filetype', 'filesize', 'fileobj']
        myattrs = [getattr(self, a) for a in attrs]
        oattrs = [getattr(other, a) for a in attrs]
        return cmp(myattrs, oattrs)

    def reset(self):
        if self.fileobj is not None:
            self.fileobj.seek(0)
        elif self.value is None:
            raise ValueError("Don't know how to reset this parameter")

    @classmethod
    def from_file(cls, paramname, filename):
        """Returns a new MultipartParam object constructed from the local
        file at ``filename``.

        ``filesize`` is determined by os.path.getsize(``filename``)

        ``filetype`` is determined by mimetypes.guess_type(``filename``)[0]

        ``filename`` is set to os.path.basename(``filename``)
        """

        return cls(paramname, filename=os.path.basename(filename),
                   filetype=mimetypes.guess_type(filename)[0],
                   filesize=os.path.getsize(filename),
                   fileobj=open(filename, "rb"))

    @classmethod
    def from_params(cls, params):
        """Returns a list of MultipartParam objects from a sequence of
        name, value pairs, MultipartParam instances,
        or from a mapping of names to values

        The values may be strings or file objects, or MultipartParam objects.
        MultipartParam object names must match the given names in the
        name,value pairs or mapping, if applicable."""
        if hasattr(params, 'items'):
            params = params.items()

        retval = []
        for item in params:
            if isinstance(item, cls):
                retval.append(item)
                continue
            name, value = item
            if isinstance(value, cls):
                assert value.name == name
                retval.append(value)
                continue
            if hasattr(value, 'read'):
                # Looks like a file object
                filename = getattr(value, 'name', None)
                if filename is not None:
                    filetype = mimetypes.guess_type(filename)[0]
                else:
                    filetype = None

                retval.append(cls(name=name, filename=filename,
                                  filetype=filetype, fileobj=value))
            else:
                retval.append(cls(name, value))
        return retval

    def encode_hdr(self, boundary) -> bytes:
        """Return the multipart header for this parameter as raw bytes."""
        boundary = encode_and_quote(boundary)

        headers = [f"--{boundary}"]

        # Content-Disposition
        if self.filename:
            disposition = (
                f'form-data; name="{self.name}"; filename="{self.filename}"'
            )
        else:
            disposition = f'form-data; name="{self.name}"'
        headers.append(f"Content-Disposition: {disposition}")

        # Content-Type
        headers.append(f"Content-Type: {self.filetype or 'text/plain; charset=utf-8'}")

        # Blank line separates header from body
        headers.append("")
        headers.append("")

        # Join with CRLF and return as ASCII bytes
        return "\r\n".join(headers).encode("ascii")

    def encode(self, boundary):
        """Returns the string encoding of this parameter"""
        if self.value is None:
            value = self.fileobj.read()
        else:
            value = self.value.encode("utf-8")

            # Prevent boundary-smuggling in text fields
            if re.search(rb"^--" + re.escape(boundary.encode()) + rb"$", value, re.M):
                raise ValueError("boundary found in encoded string")

        return self.encode_hdr(boundary) + value + b"\r\n"

    def iter_encode(self, boundary, blocksize=4096):
        """Yields the encoding of this parameter
        If self.fileobj is set, then blocks of ``blocksize`` bytes are read and
        yielded."""
        total = self.get_size(boundary)
        current = 0
        if self.value is not None:
            block = self.encode(boundary)
            current += len(block)
            yield block
            if self.cb:
                self.cb(self, current, total)
        else:
            block = self.encode_hdr(boundary)
            current += len(block)
            yield block
            if self.cb:
                self.cb(self, current, total)
            last_block = b""
            encoded_boundary = b"--" + encode_and_quote(boundary).encode("ascii")
            boundary_exp = re.compile(b"^" + re.escape(encoded_boundary) + b"$", re.M)
            while True:
                block = self.fileobj.read(blocksize)
                if not block:
                    current += 2
                    yield b"\r\n"
                    if self.cb:
                        self.cb(self, current, total)
                    break
                last_block += block
                if boundary_exp.search(last_block):
                    raise ValueError("boundary found in file data")
                last_block = last_block[-len(encoded_boundary) - 2:]
                current += len(block)
                yield block
                if self.cb:
                    self.cb(self, current, total)

    def get_size(self, boundary):
        """Return the number of bytes this part will occupy."""
        if self.filesize is not None:  # a real file, we already know
            valuesize = self.filesize
        else:  # simple text field → UTF-8 bytes
            valuesize = len(self.value.encode("utf-8"))

        return len(self.encode_hdr(boundary)) + 2 + valuesize


def encode_string(boundary, name, value):
    """Returns ``name`` and ``value`` encoded as a multipart/form-data
    variable.  ``boundary`` is the boundary string used throughout
    a single request to separate variables."""

    return MultipartParam(name, value).encode(boundary)


def encode_file_header(boundary, paramname, filesize, filename=None,
                       filetype=None):
    """Returns the leading data for a multipart/form-data field that contains
    file data.

    ``boundary`` is the boundary string used throughout a single request to
    separate variables.

    ``paramname`` is the name of the variable in this request.

    ``filesize`` is the size of the file data.

    ``filename`` if specified is the filename to give to this field.  This
    field is only useful to the server for determining the original filename.

    ``filetype`` if specified is the MIME type of this file.

    The actual file data should be sent after this header has been sent.
    """

    return MultipartParam(paramname, filesize=filesize, filename=filename,
                          filetype=filetype).encode_hdr(boundary)


def get_body_size(params, boundary):
    """Returns the number of bytes that the multipart/form-data encoding
    of ``params`` will be."""
    size = sum(p.get_size(boundary) for p in MultipartParam.from_params(params))
    return size + len(boundary) + 6


def get_headers(params, boundary):
    """Returns a dictionary with Content-Type and Content-Length headers
    for the multipart/form-data encoding of ``params``."""
    headers = {}
    boundary = urllib.parse.quote_plus(boundary)
    headers['Content-Type'] = "multipart/form-data; boundary=%s" % boundary
    headers['Content-Length'] = str(get_body_size(params, boundary))
    return headers


class MultipartYielder:
    def __init__(self, params, boundary, cb):
        self.params = params
        self.boundary = boundary
        self.cb = cb

        self.i = 0
        self.p = None
        self.param_iter = None
        self.current = 0
        self.total = get_body_size(params, boundary)

    def __iter__(self):
        return self

    # ---- NEW: Python-3 iterator hook -----------------------------

    def __next__(self):
        """Return the next data block for str.join()."""
        # *** this is just your old `next()` logic, unchanged ***
        if self.param_iter is not None:
            try:
                block = next(self.param_iter)  # use built-in next()
                self.current += len(block)
                if self.cb:
                    self.cb(self.p, self.current, self.total)
                return block
            except StopIteration:
                self.p = self.param_iter = None

        if self.i is None:
            raise StopIteration
        elif self.i >= len(self.params):
            self.param_iter = self.p = None
            self.i = None
            block = f"--{self.boundary}--\r\n".encode("ascii")
            self.current += len(block)
            if self.cb:
                self.cb(self.p, self.current, self.total)
            return block

        self.p = self.params[self.i]
        self.param_iter = self.p.iter_encode(self.boundary)
        self.i += 1
        return self.__next__()  # tail-recursion to fetch first chunk

    # ---- OPTIONAL: keep Py-2 compatibility -----------------------
    next = __next__

    def reset(self):
        self.i = 0
        self.current = 0
        for param in self.params:
            param.reset()


def multipart_encode(params, boundary=None, cb=None):
    """Encode ``params`` as multipart/form-data.

    ``params`` should be a sequence of (name, value) pairs or MultipartParam
    objects, or a mapping of names to values.
    Values are either strings parameter values, or file-like objects to use as
    the parameter value.  The file-like objects must support .read() and either
    .fileno() or both .seek() and .tell().

    If ``boundary`` is set, then it as used as the MIME boundary.  Otherwise
    a randomly generated boundary will be used.  In either case, if the
    boundary string appears in the parameter values a ValueError will be
    raised.

    If ``cb`` is set, it should be a callback which will get called as blocks
    of data are encoded.  It will be called with (param, current, total),
    indicating the current parameter being encoded, the current amount encoded,
    and the total amount to encode.

    Returns a tuple of `datagen`, `headers`, where `datagen` is a
    generator that will yield blocks of data that make up the encoded
    parameters, and `headers` is a dictionary with the assoicated
    Content-Type and Content-Length headers.

    Examples:

    >>> datagen, headers = multipart_encode( [("key", "value1"), ("key", "value2")] )
    >>> s = "".join(datagen)
    >>> assert "value2" in s and "value1" in s

    >>> p = MultipartParam("key", "value2")
    >>> datagen, headers = multipart_encode( [("key", "value1"), p] )
    >>> s = "".join(datagen)
    >>> assert "value2" in s and "value1" in s

    >>> datagen, headers = multipart_encode( {"key": "value1"} )
    >>> s = "".join(datagen)
    >>> assert "value2" not in s and "value1" in s

    """
    if boundary is None:
        boundary = gen_boundary()
    else:
        boundary = urllib.parse.quote_plus(boundary)

    headers = get_headers(params, boundary)
    params = MultipartParam.from_params(params)

    return MultipartYielder(params, boundary, cb), headers
