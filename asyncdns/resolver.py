#
# Python-based async DNS implementation
#

import asyncio
import struct
import time
import random
import weakref
import socket
import ipaddress
import encodings.idna
import sys
import pkg_resources
import re

from .constants import *
from . import rr
from .utils import decode_domain, decode_pascal_string, rcode_to_string

TIMEOUT         = 1000
MAX_TRIES       = 5
MAX_PACKET_SIZE = 4000
MAX_TTL         = 3 * 3600

# These are for DNS messages
QR          = 0x8000
OPCODE_MASK = 0x7800
QUERY       = 0x0000
IQUERY      = 0x0800
STATUS      = 0x1000
#           = 0x1800
NOTIFY      = 0x2000
UPDATE      = 0x2800

AA          = 0x0400
TC          = 0x0200
RD          = 0x0100
RA          = 0x0080
Z           = 0x0040
AD          = 0x0020
CD          = 0x0010

DO          = 0x8000

RCODE_MASK  = 0x000f

_rng = random.SystemRandom()

# Load and parse the named.root file
_ipv4_roots = []
_ipv6_roots = []

_named_root = pkg_resources.resource_string('asyncdns', 'named.root')\
                           .decode('ascii')
_space_re = re.compile(r'\s+')
for line in _named_root.splitlines():
    line = line.strip()
    if line.startswith(';'):
        continue

    line = _space_re.split(line)
    if line[2] == 'A':
        addr = ipaddress.ip_address(line[3])
        _ipv4_roots.append((addr, 53))
    elif line[2] == 'AAAA':
        addr = ipaddress.ip_address(line[3])
        _ipv6_roots.append((addr, 53))

_all_roots = _ipv4_roots + _ipv6_roots

class Timeout(object):
    def __init__(self, timeout, callback):
        loop = asyncio.get_event_loop()
        loop.call_later(timeout, self.fire)
        self.callback = callback
        self.cancelled = False

    def fire(self):
        if self.cancelled:
            return
        self.callback()

    def cancel(self):
        self.cancelled = True
        self.callback = None

class Query(object):
    """Represents a DNS query."""

    def __init__(self, name, q_type, q_class):
        # The name being queried
        if isinstance(name, str):
            name = name.rstrip('.')
            if name == '':
                self.name = b''
            else:
                self.name = b'.'.join([encodings.idna.ToASCII(label)
                                       for label in name.split('.')])
        else:
            name = name.rstrip(b'.')
            self.name = name

        # The query type (e.g. A)
        self.q_type = q_type

        # The query class (e.g. IN)
        self.q_class = q_class

    def __lt__(self, other):
        return (self.name < other.name
                or (self.name == other.name
                    and (self.q_type < other.q_type
                         or (self.q_type == other.q_type
                             and self.q_class < other.q_class))))

    def __eq__(self, other):
        return (self.name == other.name
                and self.q_type == other.q_type
                and self.q_class == other.q_class)

    def __ne__(self, other):
        return (self.name != other.name
                or self.q_type != other.q_type
                or self.q_class != other.q_class)

    def __gt__(self, other):
        return other < self

    def __ge__(self, other):
        return not self < other

    def __le__(self, other):
        return not other < self

    def __hash__(self):
        return hash((self.name, self.q_type, self.q_class))

    def __repr__(self):
        return 'Query({!r}, {!r}, {!r})'.format(self.name,
                                                self.q_type,
                                                self.q_class)

class Reply(object):
    def __init__(self, flags, rcode, answers, authorities, additional):
        self.flags = flags
        self.rcode = rcode
        self.answers = answers
        self.authorities = authorities
        self.additional = additional

    def update_ttls(ttl):
        for answer in self.answers:
            answer.ttl = ttl
        for auth in self.authorities:
            auth.ttl = ttl
        for add in self.additional:
            add.ttl = ttl

    def __str__(self):
        flags = []
        for name,mask in (('AA', AA), ('TC', TC), ('RD', RD), ('RA', RA),
                          ('Z', Z), ('AD', AD), ('CD', CD)):
            if self.flags & mask:
                flags.append(name)

        header = ';; {} ({})'.format(rcode_to_string(self.rcode), ', '.join(flags))

        lines = [header]
        lines.append('; {} answers:'.format(len(self.answers)))
        for answer in self.answers:
            lines.append(str(answer))
        lines.append('; {} authorities:'.format(len(self.authorities)))
        for auth in self.authorities:
            lines.append(str(auth))
        lines.append('; {} additional:'.format(len(self.additional)))
        for add in self.additional:
            lines.append(str(add))
        return '\n'.join(lines)

class BadReply(Exception):
    pass

class RoundRobinServer(object):
    def __init__(self, servers):
        self.servers = servers
        self.server_ndx = 0

    def next_server(self):
        server = self.servers[self.server_ndx]
        self.server_ndx = (self.server_ndx + 1) % len(self.servers)
        return server

class RandomServer(object):
    def __init__(self, servers):
        self.servers = servers

    def next_server(self):
        server_ndx = random.randrange(0, len(self.servers))
        return self.servers[server_ndx]

class DNSProtocol(object):

    def __init__(self, resolver, query, server_selector,
                 uid, using_tcp, prefer_ipv6, should_cache, recursive):
        self.resolver = weakref.ref(resolver)
        self.query = query
        self.server_selector = server_selector
        self.server = None
        self.uid = uid
        self.using_tcp = using_tcp
        self.prefer_ipv6 = prefer_ipv6
        self.should_cache = should_cache
        self.recursive = recursive
        self.transport = None
        self._retry_count = 0
        self._waiters = []
        self._timeout = None

    def set_timeout(self, timeout):
        self.cancel_timeout()
        self._timeout = Timeout(timeout, self.timed_out)

    def cancel_timeout(self):
        if self._timeout is not None:
            self._timeout.cancel()
            self._timeout = None

    def build_dns_packet(self, uid, query, wants_recursion=False):
        flags = QUERY
        if wants_recursion:
            flags |= RD
        header = struct.pack(b'>HHHHHH', uid, flags, 1, 0, 0, 1)
        packet = [header]

        for label in query.name.split(b'.'):
            if len(label) > 63:
                raise ValueError('DNS label too long')

            if len(label) == 0:
                continue

            packet.append(struct.pack(b'>B', len(label)))
            packet.append(label)

        packet.append(struct.pack(b'>BHH', 0, query.q_type, query.q_class))

        # Add an OPT record to indicate EDNS support
        packet.append(struct.pack(b'>BHHLH', 0, OPT, MAX_PACKET_SIZE,
                                  DO, 0))

        return b''.join(packet)

    def bind_random_port(self, sock):
        while True:
            port = _rng.randrange(1024, 65536)

            try:
                sock.bind(('0.0.0.0', port))
                break
            except OSError as e:
                if e.errno not in (errno.EADDRINUSE, EADDRNOTAVAIL):
                    raise
        return port

    def retry(self):
        self.cancel_timeout()

        self._retry_count += 1
        if self._retry_count >= MAX_TRIES:
            self.fail_waiters(OSError(errno.ETIMEDOUT))
            if self.transport is not None:
                self.transport.abort()
            self.resolver().protocol_done(self)
            return

        if self.transport is not None:
            self.transport.abort()

        self.set_timeout(TIMEOUT)

        loop = asyncio.get_event_loop()

        self.server = self.server_selector.next_server()

        server_addr = (str(self.server[0]), self.server[1])

        if not self.using_tcp:
            packet = self.build_dns_packet(self.uid, self.query,
                                           not self.recursive)

            if len(packet) > MAX_PACKET_SIZE:
                self.using_tcp = True
                del packet

        if self.using_tcp:
            self._buffer = b''

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.bind_random_port(sock)
            f = asyncio.ensure_future(loop.create_connection(lambda: self,
                                                             host=server_addr,
                                                             sock=sock))

            def callback(f):
                if f.cancelled():
                    self.cancel_waiters()
                    return

                exc = f.exception()
                if exc is not None:
                    self.fail_waiters(exc)
                    return

                packet = self.build_dns_packet(uid, query, not self.recursive)
                len_bytes = struct.pack(b'>H', len(packet))

                transport, _ = f.result()
                try:
                    transport.writelines(len_bytes, packet)
                except:
                    e = sys.exc_info()[0]
                    self.fail_waiters(e)

            f.add_done_callback(callback)
        else:
            self._buffer = None

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.bind_random_port(sock)
            f = asyncio.ensure_future(loop.create_datagram_endpoint(lambda: self,
                                                                    sock=sock))

            def callback(f):
                if f.cancelled():
                    self.cancel_waiters()
                    return

                exc = f.exception()
                if exc is not None:
                    self.fail_waiters(exc)
                    return

                transport, _ = f.result()
                try:
                    transport.sendto(packet, addr=server_addr)
                except:
                    e = sys.exc_info()[0]
                    self.fail_waiters(e)

            f.add_done_callback(callback)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Ignore responses from the wrong server
        if addr != (str(self.server[0]), self.server[1]):
            return

        try:
            if self.process_packet(data):
                self.resolver().protocol_done(self)
            else:
                # This means we're re-querying using TCP, or recursing
                self._buffer = b''
        except:
            raise
            e = sys.exc_info()[0]
            self.fail_waiters(e)

    def data_received(self, data):
        self._buffer += data

        # Accumulate data until we have enough for the packet length
        if len(self._buffer) > 2:
            packet_len, = struct.unpack(b'>H', self._buffer[:2])

            # When we have enough for the entire packet, process it
            if len(self._buffer) >= 2 + packet_len:
                try:
                    done = self.process_packet(self._buffer[2:])
                except:
                    e = sys.exc_info()[0]
                    self.fail_waiters(e)
                finally:
                    self.transport.close()

                if not done:
                    # This means we're recursing
                    self._buffer = b''
                else:
                    self.resolver().protocol_done(self)

    def decode_rr(self, packet, ptr, rcode):
        name, ptr = decode_domain(packet, ptr)

        utype, uclss, ttl, rdlen = struct.unpack(b'>HHLH', packet[ptr:ptr+10])

        ptr += 10

        if utype == OPT:
            rcode |= (ttl >> 20) & 0xff0
            ptr += rdlen
            return (rcode, None, None)

        result = rr.RR.decode(name, utype, uclss, ttl, packet, ptr, rdlen)
        ptr += rdlen

        return (rcode, result, ptr)

    def process_packet(self, packet):
        self.cancel_timeout()

        # Decode the header
        uid, flags, qdcount, ancount, nscount, arcount \
            = struct.unpack(b'>HHHHHH', packet[:12])

        # Ignore packets that aren't query responses
        if (flags & QR) == 0:
            raise BadReply('Received a packet that isn\'t a response')

        if (flags & OPCODE_MASK) != QUERY:
            raise BadReply('Received a packet that isn\'t a query response')

        # Ignore packets with the wrong question count
        if qdcount != 1:
            raise BadReply('Too many questions in query response')

        # Ignore packets with the wrong uid
        if uid != self.uid:
            raise BadReply('Wrong UID in query response')

        # Decode the query
        domain, ptr = decode_domain(packet, 12)

        qtype, qclss = struct.unpack(b'>HH', packet[ptr:ptr+4])
        ptr += 4

        q = Query(domain, qtype, qclss)

        # Ignore packets containing the wrong query
        if q != self.query:
            raise BadReply('Query mismatch in query response - {} {}'.format(q, self.query))

        # Check the TC flag and re-query using TCP
        if (flags & TC) != 0:
            if self.using_tcp:
                raise BadReply('Query using TCP asks to re-do using TCP!')

            self.using_tcp = True
            self._retry_count = 0
            self.retry()

            return False

        # Find the rcode and decode the answer
        rcode = flags & RCODE_MASK

        answers = []
        authorities = []
        additional = []

        if rcode in (NOERROR, NXDOMAIN):
            for count, array in [(ancount, answers),
                                 (nscount, authorities),
                                 (arcount, additional)]:
                for n in range(0, count):
                    rcode, rr, ptr = self.decode_rr(packet, ptr, rcode)
                    if rr is not None:
                        array.append(rr)

            reply = Reply(flags,
                          rcode,
                          answers,
                          authorities,
                          additional)
        else:
            # Look for an OPT entry in the additional area to get the rcode

            # First skip the answer and authority parts
            for n in range(0, ancount + nscount):
                _, ptr = decode_domain(packet, ptr)
                utype, uclss, ttl, rdlen = struct.unpack(b'>HHLH',
                                                         packet[ptr:ptr+10])
                ptr += 10 + rdlen

            # Now read the additional entries
            for n in range(0, arcount):
                _, ptr = decode_domain(packet, ptr)
                utype, uclss, ttl, rdlen = struct.unpack(b'>HHLH',
                                                         packet[ptr:ptr+10])
                if utype == OPT:
                    rcode |= (ttl >> 20) & 0xff0

                ptr += 10 + rdlen

            reply = Reply(False, rcode, [], [], [])

        # Finally, check to see if we need to recurse
        if self.recursive \
           and (flags & (RA|RD)) != (RA|RD) \
           and rcode == NOERROR \
           and len(answers) == 0 and len(authorities) != 0:
            # Check to see if we have addresses in the additional section
            addrs = {}
            for addl in additional:
                if addl.rr_type not in (A, AAAA):
                    continue

                l = addrs.get(addl.name, None)
                if l is None:
                    l = list()
                    addrs[addl.name] = l
                l.append(addl.address)

            # Find the list of servers
            servers = []
            for authrr in authorities:
                if authrr.rr_type != NS:
                    continue
                addresses = addrs.get(authrr.host, None)
                got_v6 = False
                if self.prefer_ipv6:
                    for addr in addresses:
                        if isinstance(addr, ipaddress.IPv6Address):
                            servers.append((addr, 53))
                            got_v6 = True
                if not got_v6:
                    for addr in addresses:
                        if self.prefer_ipv6 is None \
                           or not isinstance(addr, ipaddress.IPv6Address):
                            servers.append((addr, 53))

            if len(servers):
                self.server_selector = RandomServer(servers)
                self.using_tcp = False
                self._retry_count = 0

                self.retry()

                return False

        self.fire_waiters(reply)

        return True

    def timed_out(self):
        self.fail_waiters(OSError(errno.ETIMEDOUT))

    def error_received(self, exc):
        self.fail_waiters(exc)

    def connection_lost(self, exc):
        if exc is not None:
            self.fail_waiters(exc)

    def add_waiter(self, future):
        self._waiters.append(future)

    def fire_waiters(self, reply):
        self.cancel_timeout()
        if self.should_cache:
            self.resolver().cache_reply(self.query, reply)
        for f in self._waiters:
            f.set_result(reply)

    def cancel_waiters(self):
        self.cancel_timeout()
        for f in self._waiters:
            f.cancel()

    def fail_waiters(self, exc):
        self.cancel_timeout()
        for f in self._waiters:
            f.set_exception(exc)

class Resolver(object):

    def __init__(self):
        self._cache = {}
        self._queries = {}

    def close(self):
        """Cancel any in-progress queries."""
        if len(self._queries):
            for q,p in self._queries.items():
                p.cancel_waiters()

    def __del__(self):
        self.close()

    def check_domain(self, query):
        for label in query.name.split(b'.'):
            if len(label) > 63:
                raise ValueError('DNS label too long')

    def lookup(self, query, servers=None, prefer_ipv6=False, should_cache=True,
               recursive=False):
        """Perform a DNS query."""
        f = asyncio.Future()

        # First see if the result is in the cache - if so, return it
        r = self._cache.get(query, None)
        if r is not None:
            (expiry, reply) = r

            now = time.time()

            if now >= expiry:
                del _cache[query]
            else:
                ttl_ms = expiry - now
                ttl_s = (ttl_ms + 999) / 1000

                reply.update_ttls(ttl_s)

                f.set_result(reply)

                return f

        # Now, look to see if we're already doing this lookup; if we are,
        # add this future to the list waiting on the query.
        r = self._queries.get(query, None)
        if r is not None:
            r.add_waiter(f)
            return f

        # Finally, fire off a new query
        uid = _rng.randrange(0, 65536)
        self.check_domain(query)

        if servers is None:
            recursive = True
            if prefer_ipv6:
                servers = _ipv6_roots
            elif prefer_ipv6 is None:
                servers = _all_roots
            else:
                servers = _ipv4_roots

        if isinstance(servers, list):
            servers = RandomServer(servers)
        elif isinstance(servers, tuple):
            servers = RoundRobinServer([servers])

        protocol = DNSProtocol(self, query, servers, uid, False,
                               prefer_ipv6, should_cache, recursive)
        self._queries[query] = protocol
        protocol.add_waiter(f)
        protocol.retry()

        return f

    def protocol_done(self, protocol):
        del self._queries[protocol.query]

    def flush_cache(self):
        self._cache = {}

    def cache_reply(self, query, reply):
        now = time.time()
        has_ttl = False
        min_ttl = 0

        # Find the minimum ttl
        for collection in (reply.answers, reply.authorities, reply.additional):
            for rr in collection:
                ttl = rr.ttl
                if not has_ttl or min_ttl > ttl:
                    has_ttl = True
                    min_ttl = ttl

        # Can't cache things with no TTL or with TTL zero
        if not has_ttl:
            return

        if min_ttl > MAX_TTL:
            min_ttl = MAX_TTL

        self._cache[query] = (now + min_ttl, reply)
