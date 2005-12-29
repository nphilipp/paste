# (c) 2005 Ian Bicking, Clark C. Evans and contributors
# This module is part of the Python Paste Project and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php
# Some of this code was funded by http://prometheusresearch.com
"""
HTTP Message Headers

This contains general support for HTTP/1.1 message headers [1] in a
manner that supports WSGI ``environ`` [2] and ``response_headers``[3].
Specifically, this module defines a ``HTTPHeader`` class whose instances
correspond to field-name items.  The actual field-content for the
message-header is stored in the appropriate WSGI collection (either
the ``environ`` for requests, or ``response_headers`` for responses).

Each ``HTTPHeader`` instance is a callable (defining ``__call__(``)
that takes one of the following:

  - an ``environ`` dictionary, returning the corresponding header
    value by according to the WSGI's HTTP_ prefix mechanism, e.g.,
    ``UserAgent(environ)`` returns ``environ.get('HTTP_USER_AGENT')``

  - a ``response_headers`` list, giving a comma-delimited string for
    each corresponding ``header_value`` tuple entries (see below).

  - a sequence of string ``*args`` that are comma-delimited into a
    single string value, e.g. ``ContentType("text/html","text/plain")``
    returns ``"text/html, text/plain"``

  - a set of ``*kwargs`` keyword arguments that are used to create
    a header value, in a manner dependent upon the particular header in
    question (to make value construction easier and error-free):
    ``ContentDisposition(max_age=ContentDisposition.ONEWEEK)`` returns
    ``"public, max-age=60480"``

Each ``HTTPHeader`` instance also provides several methods wich act on
a WSGI collection, for removing and setting header values.

  ``delete(collection)``

    This method removes all entries of the corresponding header from
    the given collection (``environ`` or ``response_headers``), e.g.,
    ``UserAgent.remove(environ)`` deletes the 'HTTP_USER_AGENT' entry
    from the ``environ``.

  ``update(collection, *args, **kwargs)``

    This method does an in-place replacement of the given header entry,
    for example: ``ContentLength(response_headers,len(body))``

    The first argument is a valid ``environ`` dictionary or
    ``response_headers`` list; remaining arguments are passed on to
    ``__call__(*args, **kwargs)`` for value construction.

  ``apply(collection, **kwargs)``

    This method is similar to update, only that it may affect other
    headers.  For example, according to recommendations in RFC 2616,
    certain Cache-Control configurations should also set the ``Expires``
    header for HTTP/1.0 clients. By default, ``apply()`` is simply
    ``update()`` but limited to keyword arguments.

This particular approach to managing headers within a WSGI collection
has several advantages:

  1. Typos in the header name are easily detected since they become a
     ``NameError`` when executed.  The approach of using header strings
     directly can be problematic; for example, the following should
     allways return ``None``: ``environ.get("HTTP_ACCEPT_LANGUAGES")``

  2. For specific headers with validation, using ``__call__`` will
     result in an automatic header value check.  For example, the
     ContentDisposition header will reject a value having ``maxage``
     or ``max_age`` (the appropriate parameter is ``max-age``).

  3. When appending/replacing headers, the field-name has the suggested
     RFC capitalization (e.g. ``Content-Type`` or ``ETag``) for
     user-agents that incorrectly use case-sensitive matches.

  4. Some headers (such as ``Content-Type``) are singeltons, that is,
     only one entry of this type may occur in a given set of
     ``response_headers``.  This module knows about those cases and
     enforces this cardnality constraint.

  5. The exact details of WSGI header management are abstracted so
     the programmer need not worry about operational differences
     between ``environ`` dictionary or ``response_headers`` list.

  6. Sorting of ``HTTPHeaders`` is done following the RFC suggestion
     that general-headers come first, followed by request and response
     headers, and finishing with entity-headers.

  7. Special care is given to exceptional cases such as ``Set-Cookie``
     which violates the RFC's recommendation about combining header
     content into a single entry using comma separation [1]

A particular difficulty with HTTP message headers is a categorization of
sorts as described in section 4.2:

    Multiple message-header fields with the same field-name MAY be
    present in a message if and only if the entire field-value for that
    header field is defined as a comma-separated list [i.e., #(values)].
    It MUST be possible to combine the multiple header fields into one
    "field-name: field-value" pair, without changing the semantics of
    the message, by appending each subsequent field-value to the first,
    each separated by a comma.

This creates three fundamentally different kinds of headers:

  - Those that do not have a #(values) production, and hence are
    singular and may only occur once in a set of response fields;
    this case is handled by the ``SingleValueHeader`` subclass.

  - Those which have the #(values) production and follow the
    combining rule outlined above; our ``MultiValueHeader`` case.

  - Those which are multi-valued, but cannot be combined (such as the
    ``Set-Cookie`` header due to its ``Expires`` parameter); or where
    combining them into a single header entry would cause common
    user-agents to fail (``WWW-Authenticate``, ``Warning``) since
    they fail to handle dates even when properly quoted. This case
    is handled by ``MultiEntryHeader``.

Since this project does not have time to provide rigorous support
and validation for all headers, it does a basic construction of
headers listed in RFC 2616 (plus a few others) so that they can
be obtained by simply doing ``from paste.httpheaders import *``;
the name of the header instance is the "common name" less any
dashes to give CamelCase style names.

[1] http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.2
[2] http://www.python.org/peps/pep-0333.html#environ-variables
[3] http://www.python.org/peps/pep-0333.html#the-start-response-callable
"""

from mimetypes import guess_type
from rfc822 import formatdate, parsedate_tz, mktime_tz
from time import time as now
from httpexceptions import HTTPBadRequest

__all__ = ['get_header', 'HTTPHeader', 'normalize_headers'
           # additionally, all headers are exported
]

_headers = {}

class HTTPHeader(object):
    """
    HTTPHeader instances represent a particular ``field-name`` of an
    HTTP message header. They do not hold a field-value, but instead
    provide operations that work on is corresponding values.  Storage of
    the actual field valies is done with WSGI ``environ`` or
    ``response_headers`` as appropriate.  Typically, a sub-classes that
    represent a specific HTTP header, such as ContentDisposition, are
    singeltons.  Once constructed the HTTPHeader instances themselves
    are immutable and stateless.

    For purposes of documentation a "container" refers to either a
    WSGI ``environ`` dictionary, or a ``response_headers`` list.

    Member variables (and correspondingly constructor arguments).

      ``name``         the ``field-name`` of the header, in "common form"
                       as presented in RFC 2616; e.g. 'Content-Type'

      ``category``     one of 'general', 'request', 'response', or 'entity'

      ``version``      version of HTTP (informational) with which the
                       header should be recognized

      ``sort_order``   sorting order to be applied before sorting on
                       field-name when ordering headers in a response

    Special Methods:

       ``__call__``    The primary method of the HTTPHeader instance is
                       to make it a callable, it takes either a collection,
                       a string value, or keyword arguments and attempts
                       to find/construct a valid field-value

       ``__lt__``      This method is used so that HTTPHeader objects
                       can be sorted in a manner suggested by RFC 2616.

       ``__str__``     The string-value for instances of this class is
                       the ``field-name``.

    Collection Methods:

       ``delete()``    remove the all occurances (if any) of the given
                       header in the collection provided

       ``update()``    replaces (if they exist) all field-value items
                       in the given collection with the value provided

       ``apply()``     similar to ``update`` only that keyword arguments
                       are used and that other headers may be updated

       ``tuples()``    returns a set of (field-name, field-value) tuples
                       sutable for extending ``response_headers``

    The collected versions of initialized header instances are immediately
    registered and accessable through the ``get_header`` function.  Do not
    inherit from this directly, use one of ``SingleValueHeader``,
    ``MultiValueHeader``, or ``MultiEntryHeader`` as appropriate.
    """
    #@@: add field-name validation
    def __new__(cls, name, category=None):
        """
        We use the ``__new__`` operator to ensure that only one
        ``HTTPHeader`` instance exists for each field-name, and to
        register the header so that it can be found/enumerated.
        """
        self = get_header(name, raiseError=False)
        if self:
            # Allow the registration to happen again, but assert
            # that everything is identical.
            assert self.name == name, \
                "duplicate registration with different capitalization"
            assert self.category == category, \
                "duplicate registration with different category"
            assert cls == self.__class__, \
                "duplicate registration with different class"
            return self

        self = object.__new__(cls)
        self.name = name
        self.version  = getattr(self,'version','1.1')
        self.category = getattr(self,'category',category or 'general')
        assert isinstance(self.name,str)
        self.sort_order = {'general': 1, 'request': 2,
                           'response': 3, 'entity': 4 }[self.category]
        self.extensions = {}
        _headers[self.name.lower()] = self
        self._environ_name = 'HTTP_'+ self.name.upper().replace("-","_")
        self._headers_name = self.name.lower()
        assert self.version in ('1.1','1.0','0.9')
        assert isinstance(self,(SingleValueHeader,MultiValueHeader,
                                MultiEntryHeader))

    def __str__(self):
        return self.name

    def __lt__(self, other):
        """
        Re-define sorting so that general headers are first, followed
        by request/response headers, and then entity headers.  The
        list.sort() methods use the less-than operator for this purpose.
        """
        if isinstance(other,HTTPHeader):
            if self.sort_order != other.sort_order:
                return self.sort_order < other.sort_order
            return self.name < other.name
        return False

    def __repr__(self):
        return '<HTTPHeader %s>' % self.name

    def compose(self, **kwargs):
        """
        construct field-value(s) via keyword arguments
        """
        raise NotImplementedError()

    def format(self, *values):
        """ produce a return value appropriate for this kind of header """
        if not values:
           return None
        raise NotImplementedError()

    def __call__(self, *args, **kwargs):
        """
        This finds/constructs field-value(s) for the given header
        depending upon the arguments:

        - If only keyword arguments are given, then this is equivalent
          to ``format(*compose(**kwargs))``.

        - If the first (and only) argument is a dict, it is assumed
          to be a WSGI ``environ`` and the result of the corresponding
          HTTP_ entry is returned.

        - If the first (and only) argument is a list, it is assumed
          to be a WSGI ``response_headers`` and the field-value(s)
          for this header are collected and returned.

        - In all other cases, the arguments are collected, checked that
          they are string values, possibly verified by the header's
          logic, and returned.

        At this time it is an error to provide keyword arguments if args
        is present (this might change).  It is an error to provide both
        a WSGI object and also string arguments.  If no arguments are
        provided, then ``compose()`` is called to provide a default
        value for the header; if there is not default it is an error.
        """
        if not args:
            return self.format(*self.compose(**kwargs))
        if list == type(args[0]):
            assert 1 == len(args)
            result = []
            name = self.name.lower()
            for value in [value for header, value in args[0]
                         if header.lower() == name]:
                result.append(value)
            return self.format(*result)
        if dict == type(args[0]):
            assert 1 == len(args) and 'wsgi.version' in args[0]
            value = args[0].get(self._environ_name)
            if value is None:
               return None
            return self.format(value)
        for item in args:
           assert not type(item) in (dict, list)
        return self.format(*args)

    def delete(self, collection):
        """
        This method removes all occurances of the header in the
        given collection.  It does not return the value removed.
        """
        if type(collection) == dict:
            if self._environ_name in collection:
                del collection[self._environ_name]
            return self
        assert list == type(collection)
        i = 0
        while i < len(collection):
            if collection[i][0].lower() == self._headers_name:
                del collection[i]
                continue
            i += 1
        return self

    def update(self, collection, *args, **kwargs):
        """
        This method replaces (in-place when possible) all occurances of
        the given header with the provided value.  If no value is
        provided, this is the same as ``remove`` (note that this case
        can only occur if the target is a collection w/o a corresponding
        header value). The return value is the new header value (which
        could be a list for ``MultiEntryHeader`` instances).
        """
        value = self.__call__(*args, **kwargs)
        if value is None:
            self.remove(connection)
            return
        if type(collection) == dict:
            collection[self._environ_name] = value
            return value
        assert list == type(collection)
        i = 0
        found = False
        while i < len(collection):
            if collection[i][0].lower() == self._headers_name:
                if found:
                    del collection[i]
                    continue
                collection[i] = (self.name, value)
                found = True
            i += 1
        if not found:
            collection.append((self.name, value))
        return value

    def apply(self, collection, **kwargs):
        """
        This method is similar to ``update`` only that usage may result
        in other headers being changed as recommended by the corresponding
        specification.  The return value is defined by the particular
        sub-class, but defaults to the same as ``update()``.
        """
        return self.update(collection, **kwargs)

    def tuples(self, *args, **kwargs):
        value = self.__call__(*args, **kwargs)
        if not value:
            return ()
        return ((self.name, value),)

class SingleValueHeader(HTTPHeader):
    """
    The field-value is a single value and therefore all results
    constructed or obtained from a collection are asserted to ensure
    that only one result was there.
    """

    def format(self, *values):
        if not values:
           return None
        assert len(values) == 1, "more than one value: %s" % repr(values)
        return str(values[0]).strip()

class MultiValueHeader(HTTPHeader):
    """
    This header is multi-valued and values can be combined by
    concatinating with a comma, as described by section 4.2 of RFC 2616.
    """

    def format(self, *values):
        if not values:
           return None
        return ", ".join([str(v).strip() for v in values])

class MultiEntryHeader(HTTPHeader):
    """
    This header is multi-valued, but the values should not be combined
    with a comma since the header is not in compliance with RFC 2616
    (Set-Cookie due to Expires parameter) or which common user-agents do
    not behave well when the header values are combined.

    The values returned for this case are _always_ a list instead
    of a string.
    """

    def update(self, collection, *args, **kwargs):
        #@@: This needs to be implemented to handle lists
        raise NotImplementedError()

    def format(self, *values):
        if not values:
           return None
        return list([str(v).strip() for v in values])

    def tuples(self, *args, **kwargs):
        values = self.__call__(*args, **kwargs)
        if not values:
            return ()
        return tuple([(self.name, value) for value in values])

def get_header(name, raiseError=True):
    """
    This function finds the corresponding ``HTTPHeader`` for the
    ``name`` provided.  So that python-style names can be used,
    underscores are converted to dashes before the lookup.
    """
    if isinstance(name,HTTPHeader):
        return name
    retval = _headers.get(name.strip().lower().replace("_","-"))
    if not retval and raiseError:
        raise AssertionError("'%s' is an unknown header" % name)
    return retval

def list_headers(general=True, request=True, response=True, entity=True):
    " list all headers for a given category "
    search = []
    for (bool,strval) in ((general,'general'), (request,'request'),
                         (response,'response'), (entity,'entity')):
        if bool:
            search.append(strval)
    search = tuple(search)
    for head in _headers.values():
        if head.category in search:
            retval.append(head)
    retval.sort()
    return retval

def normalize_headers(response_headers, strict=True):
    """
    This alters the underlying response_headers to use the common
    name for each header; as well as sorting them with general
    headers first, followed by request/response headers, then
    entity headers, and unknown headers last.
    """
    category = {}
    for idx in range(len(response_headers)):
        (key,val) = response_headers[idx]
        head = get_header(key, strict)
        if not head:
            newhead = '-'.join(x.capitalize() for x in \
                               key.replace("_","-").split("-"))
            response_headers[idx] = (newhead,val)
            category[newhead] = 4
            continue
        response_headers[idx] = (str(head),val)
        category[str(head)] = head.sort_order
    def compare(a,b):
        ac = category[a[0]]
        bc = category[b[0]]
        if ac == bc:
            return cmp(a[0],b[0])
        return cmp(ac,bc)
    response_headers.sort(compare)

class DateHeader(SingleValueHeader):
    """
    This extends the ``SingleValueHeader`` object with specific
    treatment of time values.

    - It overrides ``compose`` to provide a sole keyword argument
      ``time`` which is an offset in seconds from the current time.

    - A ``time`` method is provided which parses the given value
      and returns the current time value.
    """

    def compose(self, time=None, delta=None):
        time = time or now()
        if delta:
            assert type(delta) == int
            time += delta
        return (formatdate(time),)

    def time(self, *args, **kwargs):
        """ return the time value (in seconds since 1970) """
        value = self.__call__(*args, **kwargs)
        if value is None:
            return None
        try:
            return mktime_tz(parsedate_tz(value))
        except TypeError:
            raise HTTPBadRequest((
                "Received an ill-formed timestamp for %s: %s\r\n") %
                (self.name, value))

#
# Following are specific HTTP headers. Since these classes are mostly
# singletons, there is no point in keeping the class around once it has
# been instantiated, so we use the same name.
#

class CacheControl(MultiValueHeader):
    """
    Cache-Control, RFC 2616 section 14.9

    This header can be constructed (using keyword arguments), by
    first specifying one of the following mechanisms:

      ``public``        if True, this argument specifies that the
                        response, as a whole, may be cashed.

      ``private``       if True, this argument specifies that the
                        response, as a whole, may be cashed; this
                        implementation does not support the
                        enumeration of private fields


      ``no_cache``      if True, this argument specifies that the
                        response, as a whole, may be cashed; this
                        implementation does not support the
                        enumeration of private fields

    In general, only one of the above three may be True, the other 2
    must then be False or None.  If all three are None, then the cashe
    is assumed to be ``public``.  Following one of these mechanism
    specifiers are various modifiers:

      ``no_store``      indicates if content may be stored on disk;
                        otherwise cashe is limited to memory (note:
                        users can still save the data, this applies
                        to intermediate caches)

      ``max_age``       the maximum duration (in seconds) for which
                        the content should be cached; if ``no-cache``
                        is specified, this defaults to 0 seconds

      ``s_maxage``      the maximum duration (in seconds) for which the
                        content should be allowed in a shared cache.

      ``no_transform``  specifies that an intermediate cache should
                        not convert the content from one type to
                        another (e.g. transform a BMP to a PNG).

      ``extensions``    gives additional cache-control extensionsn,
                        such as items like, community="UCI" (14.9.6)

    The usage of ``apply()`` on this header has side-effects. As
    recommended by RFC 2616, if ``max_age`` is provided, then then the
    ``Expires`` header is also calculated for HTTP/1.0 clients and
    proxies (this is done at the time ``apply()`` is called).  For
    ``no-cache`` and for ``private`` cases, we either do not want the
    response cached or do not want any response accidently returned to
    other users; so to prevent this case, we set the ``Expires`` header
    to the time of the request, signifying to HTTP/1.0 transports that
    the content isn't to be cached.  If you are using SSL, your
    communication is already "private", so to work with HTTP/1.0
    browsers over SSL, consider specifying your cache as ``public`` as
    the distinction between public and private is moot.
    """

    # common values for max-age; "good enough" approxmiates
    ONE_HOUR  = 60*60
    ONE_DAY   = ONE_HOUR * 24
    ONE_WEEK  = ONE_DAY * 7
    ONE_MONTH = ONE_DAY * 30
    ONE_YEAR  = ONE_WEEK * 52

    def _compose(self, public=None, private=None, no_cache=None,
                 no_store=False, max_age=None, s_maxage=None,
                 no_transform=False, **extensions):
        assert isinstance(max_age,(type(None),int))
        assert isinstance(s_maxage,(type(None),int))
        expires = 0
        result = []
        if private is True:
            assert not public and not no_cache and not s_maxage
            result.append('private')
        elif no_cache is True:
            assert not public and not private and not max_age
            result.append('no-cache')
        else:
            assert public is None or public is True
            assert not private and not no_cache
            expires = max_age
            result.append('public')
        if no_store:
            result.append('no-store')
        if no_transform:
            result.append('no-transform')
        if max_age is not None:
            result.append('max-age=%d' % max_age)
        if s_maxage is not None:
            result.append('s-maxage=%d' % s_maxage)
        for (k,v) in extensions.items():
            if k not in self.extensions:
                raise AssertionError("unexpected extension used: '%s'" % k)
            result.append('%s="%s"' % (k.replace("_","-"),v))
        return (result, expires)

    def compose(self, **kwargs):
        (result, expires) = self._compose(**kwargs)
        return result

    def apply(self, collection, **kwargs):
        """ returns the offset expiration in seconds """
        (result, expires) = self._compose(**kwargs)
        if expires is not None:
            Expires.update(collection, delta=expires)
        self.update(collection, *result)
        return expires

CacheControl = CacheControl('Cache-Control','general')

class SingleValueCGIHeader(SingleValueHeader):
    """
    This is a base class for Content-Type and Content-Length headers,
    which besides their HTTP_ entries may also have a CGI version.
    The logic is to only use the CGI version when the HTTP_ version is
    missing.  Hopefully this can be removed.
    """
    def __call__(self, *args, **kwargs):
        if args and dict == type(args[0]):
            if not args[0].get(self._environ_name):
                cgi_name = self._environ_name[5:]
                return self.format(args[0].get(cgi_name))
        return SingleValueHeader.__call__(self, *args, **kwargs)

class ContentType(SingleValueCGIHeader):
    """
    Content-Type, RFC 2616 section 14.17

    If the 'Content-Type' does not appear in the ``environ`` the
    corresponding CGI variable is searched.
    """
    version = '1.0'

    # common mimetype constants
    UNKNOWN    = 'application/octet-stream'
    TEXT_PLAIN = 'text/plain'
    TEXT_HTML  = 'text/html'
    TEXT_XML   = 'text/xml'

    def compose(self, major=None, minor=None, charset=None):
        if not major:
            if minor in ('plain','html','xml'):
                major = 'text'
            else:
                assert not minor and not charset
                return (self.UNKNOWN,)
        if not minor:
            minor = "*"
        result = "%s/%s" % (major,minor)
        if charset:
            result += "; charset=%s" % charset
        return (result,)
ContentType = ContentType('Content-Type','entity')

class ContentLength(SingleValueCGIHeader):
    """
    Content-Length, RFC 2616 section 14.13
    """
    version = "1.0"
ContentLength = ContentLength('Content-Length','entity')

class ContentDisposition(SingleValueHeader):
    """
    Content-Disposition, RFC 2183

    This header can be constructed (using keyword arguments), by
    first specifying one of the following mechanisms:

      ``attachment``    if True, this specifies that the content
                        should not be shown in the browser and
                        should be handled externally, even if the
                        browser could render the content

      ``inline``        exclusive with attachment; indicates that the
                        content should be rendered in the browser if
                        possible, but otherwise it should be handled
                        externally

    Only one of the above 2 may be True.  If both are None, then
    the disposition is assumed to be an ``attachment``. These are
    distinct fields since support for field enumeration may be
    added in the future.

      ``filename``      the filename parameter, if any, to be reported;
                        if this is None, then the current object's
                        'filename' attribute is used

    The usage of ``apply()`` on this header has side-effects. If
    filename is provided, and Content-Type is not set or is
    'application/octet-stream', then the mimetypes.guess is used to
    upgrade the Content-Type setting.
    """
    version = '1.1'

    def _compose(self, attachment=None, inline=None, filename=None):
        result = []
        if inline is True:
            assert not attachment
            result.append('inline')
        else:
            assert not inline
            result.append('attachment')
        if filename:
            assert '"' not in filename
            filename = filename.split("/")[-1]
            filename = filename.split("\\")[-1]
            result.append('filename="%s"' % filename)
        return (("; ".join(result),), filename)

    def compose(self, **kwargs):
        (result, mimetype) = self._compose(**kwargs)
        return result

    def apply(self, collection, **kwargs):
        """ return the new Content-Type side-effect value """
        (result, filename) = self._compose(**kwargs)
        mimetype = ContentType(collection)
        if filename and (not mimetype or ContentType.UNKNOWN == mimetype):
            mimetype, _ = guess_type(filename)
            if mimetype and ContentType.UNKNOWN != mimetype:
                ContentType.update(collection, mimetype)
        self.update(collection, *result)
        return mimetype
ContentDisposition = ContentDisposition('Content-Disposition','entity')

class IfModifiedSince(DateHeader):
    """
    If-Modified-Since, RFC 2616 section 14.25
    """
    version = '1.0'
    def time(self, *args, **kwargs):
        value = DateHeader.time(self, *args, **kwargs)
        if value and value > now():
            raise HTTPBadRequest((
              "Please check your system clock.\r\n"
              "According to this server, the time provided in the\r\n"
              "%s header is in the future.\r\n") % self.name)
        return value
IfModifiedSince = IfModifiedSince('If-Modified-Since','request')

#
# For now, construct a minimalistic version of the field-names; at a
# later date more complicated headers may sprout content constructors.
#
for (name,              category, version, style,      comment) in \
(("Accept"             ,'request' ,'1.1','multi-value','RFC 2616 $14.1' )
,("Accept-Charset"     ,'request' ,'1.1','multi-value','RFC 2616 $14.2' )
,("Accept-Encoding"    ,'request' ,'1.1','multi-value','RFC 2616 $14.3' )
,("Accept-Language"    ,'request' ,'1.1','multi-value','RFC 2616 $14.4' )
,("Accept-Ranges"      ,'response','1.1','multi-value','RFC 2616 $14.5' )
,("Age"                ,'response','1.1','singular'   ,'RFC 2616 $14.6' )
,("Allow"              ,'entity'  ,'1.0','multi-value','RFC 2616 $14.7' )
,("Authorization"      ,'request' ,'1.0','singular'   ,'RFC 2616 $14.8' )
#,("Cache-Control"      ,'general' ,'1.1','multi-value','RFC 2616 $14.9' )
,("Cookie"             ,'request' ,'1.0','multi-value','RFC 2109/Netscape')
,("Connection"         ,'general' ,'1.1','multi-value','RFC 2616 $14.10')
,("Content-Encoding"   ,'entity'  ,'1.0','multi-value','RFC 2616 $14.11')
#,("Content-Disposition",'entity'  ,'1.1','multi-value','RFC 2616 $15.5' )
,("Content-Language"   ,'entity'  ,'1.1','multi-value','RFC 2616 $14.12')
#,("Content-Length"     ,'entity'  ,'1.0','singular'   ,'RFC 2616 $14.13')
,("Content-Location"   ,'entity'  ,'1.1','singular'   ,'RFC 2616 $14.14')
,("Content-MD5"        ,'entity'  ,'1.1','singular'   ,'RFC 2616 $14.15')
,("Content-Range"      ,'entity'  ,'1.1','singular'   ,'RFC 2616 $14.16')
#,("Content-Type"       ,'entity'  ,'1.0','singular'   ,'RFC 2616 $14.17')
,("Date"               ,'general' ,'1.0','date-header','RFC 2616 $14.18')
,("ETag"               ,'response','1.1','singular'   ,'RFC 2616 $14.19')
,("Expect"             ,'request' ,'1.1','multi-value','RFC 2616 $14.20')
,("Expires"            ,'entity'  ,'1.0','date-header','RFC 2616 $14.21')
,("From"               ,'request' ,'1.0','singular'   ,'RFC 2616 $14.22')
,("Host"               ,'request' ,'1.1','singular'   ,'RFC 2616 $14.23')
,("If-Match"           ,'request' ,'1.1','multi-value','RFC 2616 $14.24')
#,("If-Modified-Since"  ,'request' ,'1.0','date-header','RFC 2616 $14.25')
,("If-None-Match"      ,'request' ,'1.1','multi-value','RFC 2616 $14.26')
,("If-Range"           ,'request' ,'1.1','singular'   ,'RFC 2616 $14.27')
,("If-Unmodified-Since",'request' ,'1.1','date-header' ,'RFC 2616 $14.28')
,("Last-Modified"      ,'entity'  ,'1.0','date-header','RFC 2616 $14.29')
,("Location"           ,'response','1.0','singular'   ,'RFC 2616 $14.30')
,("Max-Forwards"       ,'request' ,'1.1','singular'   ,'RFC 2616 $14.31')
,("Pragma"             ,'general' ,'1.0','multi-value','RFC 2616 $14.32')
,("Proxy-Authenticate" ,'response','1.1','multi-value','RFC 2616 $14.33')
,("Proxy-Authorization",'request' ,'1.1','singular'   ,'RFC 2616 $14.34')
,("Range"              ,'request' ,'1.1','multi-value','RFC 2616 $14.35')
,("Referer"            ,'request' ,'1.0','singular'   ,'RFC 2616 $14.36')
,("Retry-After"        ,'response','1.1','singular'   ,'RFC 2616 $14.37')
,("Server"             ,'response','1.0','singular'   ,'RFC 2616 $14.38')
,("Set-Cookie"         ,'response','1.0','multi-entry','RFC 2109/Netscape')
,("TE"                 ,'request' ,'1.1','multi-value','RFC 2616 $14.39')
,("Trailer"            ,'general' ,'1.1','multi-value','RFC 2616 $14.40')
,("Transfer-Encoding"  ,'general' ,'1.1','multi-value','RFC 2616 $14.41')
,("Upgrade"            ,'general' ,'1.1','multi-value','RFC 2616 $14.42')
,("User-Agent"         ,'request' ,'1.0','singular'   ,'RFC 2616 $14.43')
,("Vary"               ,'response','1.1','multi-value','RFC 2616 $14.44')
,("Via"                ,'general' ,'1.1','multi-value','RFC 2616 $14.45')
,("Warning"            ,'general' ,'1.1','multi-entry','RFC 2616 $14.46')
,("WWW-Authenticate"   ,'response','1.0','multi-entry','RFC 2616 $14.47')):
    cname = name.replace("-","")
    bname = { 'multi-value': 'MultiValueHeader',
              'multi-entry': 'MultiEntryHeader',
              'date-header': 'DateHeader',
              'singular'   : 'SingleValueHeader'}[style]
    exec """\
class %(cname)s(%(bname)s):
    "%(comment)s"
    version = "%(version)s"
%(cname)s('%(name)s','%(category)s');
""" % { 'cname': cname, 'name': name,
        'category': category, 'bname': bname,
        'comment': comment, 'version': version } in globals(), globals()

for head in _headers.values():
    headname = head.name.replace("-","")
    locals()[headname] = head
    __all__.append(headname)

