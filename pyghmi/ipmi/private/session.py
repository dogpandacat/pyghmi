# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This represents the low layer message framing portion of IPMI

import atexit
import collections
import hashlib
import os
import random
import select
import socket
import struct
import time

from Crypto.Cipher import AES
from Crypto.Hash import HMAC
from Crypto.Hash import SHA

import pyghmi.exceptions as exc
from pyghmi.ipmi.private import constants


initialtimeout = 0.5  # minimum timeout for first packet to retry in any given
                     # session.  This will be randomized to stagger out retries
                     # in case of congestion


def _monotonic_time():
    """Provides a monotonic timer

    This code is concerned with relative, not absolute time.
    This function facilitates that prior to python 3.3
    """
    # Python does not provide one until 3.3, so we make do
    # for most OSes, os.times()[4] works well.
    # for microsoft, GetTickCount64
    if (os.name == "posix"):
        return os.times()[4]
    else:  # last resort, non monotonic time
        return time.time()
    #TODO(jbjohnso): Windows variant


def _poller(readhandles, timeout=0):
    rdylist, _, _ = select.select(readhandles, (), (), timeout)
    return rdylist


def _aespad(data):
    """ipmi demands a certain pad scheme,
    per table 13-20 AES-CBC encrypted payload fields.
    """
    newdata = list(data)
    currlen = len(data) + 1  # need to count the pad length field as well
    neededpad = currlen % 16
    if neededpad:  # if it happens to be zero, hurray, but otherwise invert the
                  # sense of the padding
        neededpad = 16 - neededpad
    padval = 1
    while padval <= neededpad:
        newdata.append(padval)
        padval += 1
    newdata.append(neededpad)
    return newdata


def call_with_optional_args(callback, *args):
    """In order to simplify things, in a number of places there is a callback
    facility and optional arguments to pass in.  An object-oriented caller may
    find the additional argument needless. Allow them to ignore it by skipping
    the argument if None.
    """
    newargs = []
    for arg in args:
        if arg is not None:
            newargs.append(arg)
    callback(*newargs)


def get_ipmi_error(response, suffix=""):
    if 'error' in response:
        return response['error'] + suffix
    code = response['code']
    if code == 0:
        return False
    command = response['command']
    netfn = response['netfn']
    if ((netfn, command) in constants.command_completion_codes
            and code in constants.command_completion_codes[(netfn, command)]):
        res = constants.command_completion_codes[(netfn, command)][code]
        res += suffix
    elif code in constants.ipmi_completion_codes:
        res = constants.ipmi_completion_codes[code] + suffix
    else:
        res = "Unknown code 0x%2x encountered" % code
    return res


class Session(object):
    """A class to manage common IPMI session logistics

    Almost all developers should not worry about this class and instead be
    looking toward ipmi.Command and ipmi.Console.

    For those that do have to worry, the main interesting thing is that the
    event loop can go one of two ways.  Either a larger manager can query using
    class methods
    the soonest timeout deadline and the filehandles to poll and assume
    responsibility for the polling, or it can register filehandles to be
    watched.  This is primarily of interest to Console class, which may have an
    input filehandle to watch and can pass it to Session.

    :param bmc: hostname or ip address of the BMC
    :param userid: username to use to connect
    :param password: password to connect to the BMC
    :param kg: optional parameter if BMC requires Kg be set
    :param port: UDP port to communicate with, pretty much always 623
    :param onlogon: callback to receive notification of login completion
    """
    _external_handlers = {}
    bmc_handlers = {}
    waiting_sessions = {}
    keepalive_sessions = {}
    peeraddr_to_nodes = {}
    iterwaiters = []
    # Upon exit of python, make sure we play nice with BMCs by assuring closed
    # sessions for all that we tracked

    @classmethod
    def _cleanup(cls):
        for session in cls.bmc_handlers.itervalues():
            session.cleaningup = True
            session.logout()

    @classmethod
    def _createsocket(cls):
        atexit.register(cls._cleanup)
        cls.socket = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)  # INET6
                                    # can do IPv4 if you are nice to it
        try:  # we will try to fixup our receive buffer size if we are smaller
             # than allowed.
            maxmf = open("/proc/sys/net/core/rmem_max")
            rmemmax = int(maxmf.read())
            rmemmax = rmemmax / 2
            curmax = cls.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            curmax = curmax / 2
            if (rmemmax > curmax):
                cls.socket.setsockopt(socket.SOL_SOCKET,
                                      socket.SO_RCVBUF,
                                      rmemmax)
        except Exception:
            # FIXME: be more selective in catching exceptions
            pass

        curmax = cls.socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        cls.readersockets = [cls.socket]
        curmax = curmax / 2
        # we throttle such that we never have no more outstanding packets than
        # our receive buffer should be able to handle
        cls.pending = 0
        cls.maxpending = curmax / 1000
        # pessimistically assume 1 kilobyte messages,
        # which is way larger than almost all ipmi datagrams.
        # For faster performance, sysadmins may want to examine and tune
        # /proc/sys/net/core/rmem_max up.  This allows the module to request
        # more, but does not increase buffers for applications that do less
        # creative things
        # TODO(jbjohnso): perhaps spread sessions across a socket pool when
        # rmem_max is small, still get ~65/socket, but avoid long queues that
        # might happen with low rmem_max and putting thousands of nodes in line

    def _sync_login(self, response):
        """Handle synchronous callers in liue of
        a client-provided callback.
        """
        if 'error' in response:
            raise exc.IpmiException(response['error'])

    def __new__(cls,
                bmc,
                userid,
                password,
                port=623,
                kg=None,
                onlogon=None):
        trueself = None
        for res in socket.getaddrinfo(bmc, port, 0, socket.SOCK_DGRAM):
            sockaddr = res[4]
            if (res[0] == socket.AF_INET):  # convert the sockaddr to AF_INET6
                newhost = '::ffff:' + sockaddr[0]
                sockaddr = (newhost, sockaddr[1], 0, 0)
            if sockaddr in cls.bmc_handlers:
                self = cls.bmc_handlers[sockaddr]
                if (self.bmc == bmc and self.userid == userid and
                        self.password == password and self.kgo == kg):
                    trueself = self
                else:
                    del cls.bmc_handlers[sockaddr]
            if trueself:
                return trueself
            return object.__new__(cls)

    def __init__(self,
                 bmc,
                 userid,
                 password,
                 port=623,
                 kg=None,
                 onlogon=None):
        if hasattr(self, 'initialized'):
            # new found an existing session, do not corrupt it
            if onlogon is None:
                while not self.logged:
                    Session.wait_for_rsp()
            else:
                if not self.logged:
                    self.logonwaiters.append(onlogon)
                else:
                    self.iterwaiters.append(onlogon)
            return
        self.incommand = False
        self.initialized = True
        self.cleaningup = False
        self.lastpayload = None
        self.bmc = bmc
        self.userid = userid
        self.password = password
        self.nowait = False
        self.pendingpayloads = collections.deque([])
        self.kgo = kg
        if kg is not None:
            self.kg = kg
        else:
            self.kg = password
        self.port = port
        if (onlogon is None):
            self.async = False
            self.logonwaiters = [self._sync_login]
        else:
            self.async = True
            self.logonwaiters = [onlogon]
        if not hasattr(Session, 'socket'):
            self._createsocket()
        self.login()
        if not self.async:
            while not self.logged:
                Session.wait_for_rsp()

    def onlogon(self, parameter):
        while self.logonwaiters:
            waiter = self.logonwaiters.pop()
            waiter(parameter)

    def _initsession(self):
        # NOTE(jbjohnso): this number can be whatever we want.
        #                 I picked 'xCAT' minus 1 so that a hexdump of packet
        #                 would show xCAT
        self.localsid = 2017673555

        # NOTE(jbjohnso): for the moment, assume admin access
        # TODO(jbjohnso): make flexible
        self.privlevel = 4

        self.confalgo = 0
        self.aeskey = None
        self.integrityalgo = 0
        self.k1 = None
        self.rmcptag = 1
        self.ipmicallback = None
        self.ipmicallbackargs = None
        self.sessioncontext = None
        self.sequencenumber = 0
        self.sessionid = 0
        self.authtype = 0
        self.ipmiversion = 1.5
        self.timeout = initialtimeout + (0.5 * random.random())
        self.seqlun = 0
        # NOTE(jbjohnso): per IPMI table 5-4, software ids in the ipmi spec may
        #                 be 0x81 through 0x8d.  We'll stick with 0x81 for now,
        #                 do not forsee a reason to adjust
        self.rqaddr = 0x81

        self.logged = 0
        # NOTE(jbjohnso): when we confirm a working sockaddr, put it here to
        #                 skip getaddrinfo
        self.sockaddr = None
        # NOTE(jbjohnso): this tracks netfn,command,seqlun combinations that
        #                 were retried so that we don't loop around and reuse
        #                 the same request data and cause potential ambiguity
        #                 in return
        self.tabooseq = {}
        # NOTE(jbjohnso): default to supporting ipmi 2.0.  Strictly by spec,
        #                 this should gracefully be backwards compat, but some
        #                 1.5 implementations checked reserved bits
        self.ipmi15only = 0
        self.sol_handler = None
        # NOTE(jbjohnso): This is the callback handler for any SOL payload

    def _checksum(self, *data):  # Two's complement over the data
        csum = sum(data)
        csum = csum ^ 0xff
        csum += 1
        csum &= 0xff
        return csum

    def _make_ipmi_payload(self, netfn, command, data=()):
        """This function generates the core ipmi payload that would be
        applicable for any channel (including KCS)
        """
        self.expectedcmd = command
        self.expectednetfn = netfn + \
            1  # in ipmi, the response netfn is always one
                                   # higher than the request payload, we assume
                                   # we are always the requestor for now
        seqincrement = 7  # IPMI spec forbids gaps bigger then 7 in seq number.
                       # Risk the taboo rather than violate the rules
        while ((netfn, command, self.seqlun) in self.tabooseq and
               self.tabooseq[(netfn, command, self.seqlun)] and seqincrement):
            self.tabooseq[(self.expectednetfn, command, self.seqlun)] -= 1
                     # Allow taboo to eventually expire after a few rounds
            self.seqlun += 4  # the last two bits are lun, so add 4 to add 1
            self.seqlun &= 0xff  # we only have one byte, wrap when exceeded
            seqincrement -= 1
        header = [0x20, netfn << 2]
            #figure 13-4, first two bytes are rsaddr and
                               # netfn, rsaddr is always 0x20 since we are
                               # addressing BMC
        reqbody = [self.rqaddr, self.seqlun, command] + list(data)
        headsum = self._checksum(*header)
        bodysum = self._checksum(*reqbody)
        payload = header + [headsum] + reqbody + [bodysum]
        return payload

    def _generic_callback(self, response):
        errorstr = get_ipmi_error(response)
        if errorstr:
            response['error'] = errorstr
        self.lastresponse = response

    def raw_command(self,
                    netfn,
                    command,
                    data=[],
                    retry=True,
                    callback=None,
                    callback_args=None,
                    delay_xmit=None):
        while self.incommand:
            Session.wait_for_rsp()
        self.incommand = True
        self.ipmicallbackargs = callback_args
        if callback is None:
            self.lastresponse = None
            self.ipmicallback = self._generic_callback
        else:
            self.ipmicallback = callback
        self._send_ipmi_net_payload(netfn, command, data, retry=retry,
                                    delay_xmit=delay_xmit)
        if retry:  # in retry case, let the retry timers indicate wait time
            timeout = None
        else:  # if not retry, give it a second before surrending
            timeout = 1
        #In the synchronous case, wrap the event loop in this call
        #The event loop is shared amongst pyghmi session instances
        #within a process.  In this way, synchronous usage of the interface
        #plays well with asynchronous use.  In fact, this produces the behavior
        #of only the constructor *really* needing a callback.  From then on,
        #synchronous usage of the class acts in a greenthread style governed by
        #order of data on the network
        if callback is None:
            while self.lastresponse is None:
                Session.wait_for_rsp(timeout=timeout)
            return self.lastresponse

    def _send_ipmi_net_payload(self, netfn, command, data, retry=True,
                               delay_xmit=None):
        ipmipayload = self._make_ipmi_payload(netfn, command, data)
        payload_type = constants.payload_types['ipmi']
        self.send_payload(payload=ipmipayload, payload_type=payload_type,
                          retry=retry, delay_xmit=delay_xmit)

    def send_payload(self, payload=None, payload_type=None, retry=True,
                     delay_xmit=None):
        if payload is not None and self.lastpayload is not None:
                             #we already have a packet outgoing, make this
                             # a pending payload
                             # this way a simplistic BMC won't get confused
                             # and we also avoid having to do more complicated
                             # retry mechanism where each payload is
                             # retried separately
            self.pendingpayloads.append((payload, payload_type, retry))
            return
        if payload_type is None:
            payload_type = self.last_payload_type
        if payload is None:
            payload = self.lastpayload
        message = [0x6, 0, 0xff, 0x07]  # constant RMCP header for IPMI
        if retry:
            self.lastpayload = payload
            self.last_payload_type = payload_type
        message.append(self.authtype)
        baretype = payload_type
        if self.integrityalgo:
            payload_type |= 0b01000000
        if self.confalgo:
            payload_type |= 0b10000000
        if (self.ipmiversion == 2.0):
            message.append(payload_type)
            if (baretype == 2):
                #TODO(jbjohnso): OEM payload types
                raise NotImplementedError("OEM Payloads")
            elif baretype not in constants.payload_types.values():
                raise NotImplementedError(
                    "Unrecognized payload type %d" % baretype)
            message += struct.unpack("!4B", struct.pack("<I", self.sessionid))
        message += struct.unpack("!4B", struct.pack("<I", self.sequencenumber))
        if (self.ipmiversion == 1.5):
            message += struct.unpack("!4B", struct.pack("<I", self.sessionid))
            if not self.authtype == 0:
                message += self._ipmi15authcode(payload)
            message.append(len(payload))
            message += payload
            totlen = 34 + \
                len(message)  # Guessing the ipmi spec means the whole
                                   # packet and assume no tag in old 1.5 world
            if (totlen in (56, 84, 112, 128, 156)):
                message.append(0)  # Legacy pad as mandated by ipmi spec
        elif self.ipmiversion == 2.0:
            psize = len(payload)
            if self.confalgo:
                pad = (
                    psize + 1) % 16  # pad has to cope with one byte field like
                                   # the _aespad function
                if pad:  # if no pad needed, then we take no more action
                    pad = 16 - pad
                newpsize = psize + pad + \
                    17  # new payload size grew according to pad
                                      # size, plus pad length, plus 16 byte IV
                                      #(Table 13-20)
                message.append(newpsize & 0xff)
                message.append(newpsize >> 8)
                iv = os.urandom(16)
                message += list(struct.unpack("16B", iv))
                payloadtocrypt = _aespad(payload)
                crypter = AES.new(self.aeskey, AES.MODE_CBC, iv)
                crypted = crypter.encrypt(struct.pack("%dB" %
                                                      len(payloadtocrypt),
                                                      *payloadtocrypt))
                crypted = list(struct.unpack("%dB" % len(crypted), crypted))
                message += crypted
            else:  # no confidetiality algorithm
                message.append(psize & 0xff)
                message.append(psize >> 8)
                message += list(payload)
            if self.integrityalgo:  # see table 13-8,
                                   # RMCP+ packet format
                                   # TODO(jbjohnso): SHA256 which is now
                                   # allowed
                neededpad = (len(message) - 2) % 4
                if neededpad:
                    neededpad = 4 - neededpad
                message += [0xff] * neededpad
                message.append(neededpad)
                message.append(7)  # reserved, 7 is the required value for the
                                  # specification followed
                integdata = message[4:]
                authcode = HMAC.new(self.k1,
                                    struct.pack("%dB" % len(integdata),
                                                *integdata),
                                    SHA).digest()[:12]  # SHA1-96
                                    # per RFC2404 truncates to 96 bits
                message += struct.unpack("12B", authcode)
        self.netpacket = struct.pack("!%dB" % len(message), *message)
        #advance idle timer since we don't need keepalive while sending packets
        #out naturally
        if self in Session.keepalive_sessions:
            Session.keepalive_sessions[self]['timeout'] = _monotonic_time() + \
                25 + (random.random() * 4.9)
        self._xmit_packet(retry, delay_xmit=delay_xmit)

    def _ipmi15authcode(self, payload, checkremotecode=False):
        #checkremotecode is used to verify remote code,
        #otherwise this function is used to general authcode for local
        if self.authtype == 0:  # Only for things before auth in ipmi 1.5, not
                                # like 2.0 cipher suite 0
            return ()
        password = self.password
        padneeded = 16 - len(password)
        if padneeded < 0:
            raise exc.IpmiException("Password is too long for ipmi 1.5")
        password += '\x00' * padneeded
        passdata = struct.unpack("16B", password)
        if checkremotecode:
            seqbytes = struct.unpack("!4B",
                                     struct.pack("<I", self.remsequencenumber))
        else:
            seqbytes = struct.unpack("!4B",
                                     struct.pack("<I", self.sequencenumber))
        sessdata = struct.unpack("!4B", struct.pack("<I", self.sessionid))
        bodydata = passdata + sessdata + tuple(payload) + seqbytes + passdata
        dgst = hashlib.md5(
            struct.pack("%dB" % len(bodydata), *bodydata)).digest()
        hashdata = struct.unpack("!%dB" % len(dgst), dgst)
        return hashdata

    def _got_channel_auth_cap(self, response):
        if 'error' in response:
            self.onlogon(response)
            return
        if response['code'] == 0xcc and self.ipmi15only is not None:
            # tried ipmi 2.0 against a 1.5 which should work, but some bmcs
            # thought 'reserved' meant 'must be zero'
            self.ipmi15only = 1
            return self._get_channel_auth_cap()
        mysuffix = " while trying to get channel authentication capabalities"
        errstr = get_ipmi_error(response, suffix=mysuffix)
        if errstr:
            self.onlogon({'error': errstr})
            return
        data = response['data']
        self.currentchannel = data[0]
        if data[1] & 0b10000000 and data[3] & 0b10:  # ipmi 2.0 support
            self.ipmiversion = 2.0
        if self.ipmiversion == 1.5:
            if not (data[1] & 0b100):
                self.onlogon(
                    {'error':
                     "MD5 required but not enabled/available on target BMC"})
                return
            self._get_session_challenge()
        elif self.ipmiversion == 2.0:
            self._open_rmcpplus_request()

    def _got_session_challenge(self, response):
        errstr = get_ipmi_error(response,
                                suffix=" while getting session challenge")
        if errstr:
            self.onlogon({'error': errstr})
            return
        data = response['data']
        self.sessionid = struct.unpack("<I", struct.pack("4B", *data[0:4]))[0]
        self.authtype = 2
        self._activate_session(data[4:])
    '''
    This sends the activate session payload.  We pick '1' as the requested
    sequence number without perturbing our real sequence number
    '''

    def _activate_session(self, data):
        rqdata = [2, 4] + list(data) + [1, 0, 0, 0]
                 # TODO(jbjohnso): this always requests admin level (1.5)
        self.ipmicallback = self._activated_session
        self._send_ipmi_net_payload(netfn=0x6, command=0x3a, data=rqdata)

    def _activated_session(self, response):
        errstr = get_ipmi_error(response)
        if errstr:
            self.onlogon({'error': errstr})
            return
        data = response['data']
        self.sessionid = struct.unpack("<I", struct.pack("4B", *data[1:5]))[0]
        self.sequencenumber = struct.unpack("<I",
                                            struct.pack("4B", *data[5:9]))[0]
        self._req_priv_level()

    def _req_priv_level(self):
        self.ipmicallback = self._got_priv_level
        self._send_ipmi_net_payload(netfn=0x6,
                                    command=0x3b,
                                    data=[self.privlevel])

    def _got_priv_level(self, response):
        mysuffix = " while requesting privelege level %d for %s" % (
            self.privlevel, self.userid)
        errstr = get_ipmi_error(response, suffix=mysuffix)
        if errstr:
            self.onlogon({'error': errstr})
            return
        self.logged = 1
        Session.keepalive_sessions[self] = {}
        Session.keepalive_sessions[self]['ipmisession'] = self
        Session.keepalive_sessions[self]['timeout'] = _monotonic_time() + \
            25 + (random.random() * 4.9)
        self.onlogon({'success': True})

    def _get_session_challenge(self):
        reqdata = [2]
        if len(self.userid) > 16:
            raise exc.IpmiException(
                "Username too long for IPMI, must not exceed 16")
        padneeded = 16 - len(self.userid)
        userid = self.userid + ('\x00' * padneeded)
        reqdata += struct.unpack("!16B", userid)
        self.ipmicallback = self._got_session_challenge
        self._send_ipmi_net_payload(netfn=0x6, command=0x39, data=reqdata)

    def _open_rmcpplus_request(self):
        self.authtype = 6
        self.localsid += 1  # have unique local session ids to ignore aborted
                            # login attempts from the past
        self.rmcptag += 1
        data = [
            self.rmcptag,
            0,  # request as much privilege as the channel will give us
            0, 0,  # reserved
        ]
        data += list(struct.unpack("4B", struct.pack("<I", self.localsid)))
        data += [
            0, 0, 0, 8, 1, 0, 0, 0,  # table 13-17, SHA-1
            1, 0, 0, 8, 1, 0, 0, 0,  # SHA-1 integrity
            2, 0, 0, 8, 1, 0, 0, 0,  # AES privacy
            #2,0,0,8,0,0,0,0, #no privacy confalgo
        ]
        self.sessioncontext = 'OPENSESSION'
        self.send_payload(
            payload=data,
            payload_type=constants.payload_types['rmcpplusopenreq'])

    def _get_channel_auth_cap(self):
        self.ipmicallback = self._got_channel_auth_cap
        if (self.ipmi15only):
            self._send_ipmi_net_payload(netfn=0x6,
                                        command=0x38,
                                        data=[0x0e, self.privlevel])
        else:
            self._send_ipmi_net_payload(netfn=0x6,
                                        command=0x38,
                                        data=[0x8e, self.privlevel])

    def login(self):
        self.logontries = 5
        self._initsession()
        self._get_channel_auth_cap()

    @classmethod
    def wait_for_rsp(cls, timeout=None, callout=True):
        """IPMI Session Event loop iteration

        This watches for any activity on IPMI handles and handles registered
        by register_handle_callback.  Callers are satisfied in the order that
        packets return from nework, not in the order of calling.

        :param timeout: Maximum time to wait for data to come across.  If
                        unspecified, will autodetect based on earliest timeout
        """
        #Assume:
        #Instance A sends request to packet B
        #Then Instance C sends request to BMC D
        #BMC D was faster, so data comes back before BMC B
        #Instance C gets to go ahead of Instance A, because
        #Instance C can get work done, but instance A cannot

        curtime = _monotonic_time()
        # There ar a number of parties that each has their own timeout
        # The caller can specify a deadline in timeout argument
        # each session with active outbound payload has callback to
        # handle retry/timout error
        # each session that is 'alive' wants to send a keepalive ever so often.
        # We want to make sure the most strict request is honored and block for
        # no more time than that, so that whatever part(ies) need to service in
        # a deadline, will be honored
        if timeout != 0:
            for session, parms in cls.waiting_sessions.iteritems():
                if parms['timeout'] <= curtime:
                    timeout = 0  # exit after one guaranteed pass
                    break
                if (timeout is not None and
                        timeout < parms['timeout'] - curtime):
                    continue  # timeout smaller than the current session needs
                timeout = parms['timeout'] - curtime  # set new timeout value
            for session, parms in cls.keepalive_sessions.iteritems():
                if parms['timeout'] <= curtime:
                    timeout = 0
                    break
                if (timeout is not None and
                        timeout < parms['timeout'] - curtime):
                    continue
                timeout = parms['timeout'] - curtime
        # If the loop above found no sessions wanting *and* the caller had no
        # timeout, exit function. In this case there is no way a session
        # could be waiting so we can always return 0
        while cls.iterwaiters:
            waiter = cls.iterwaiters.pop()
            waiter({'success': True})
        if timeout is None:
            return 0
        rdylist, _, _ = select.select(cls.readersockets, (), (), timeout)
        if len(rdylist) > 0:
            while _poller((cls.socket,)):  # if the somewhat lengthy
                        # queue # processing takes long enough for packets to
                        # come in, be eager
                pktqueue = collections.deque([])
                while _poller((cls.socket,)):  # looks rendundant, but
                              # want # to queue and process packets to keep
                              # things off RCVBUF
                    rdata = cls.socket.recvfrom(3000)
                    pktqueue.append(rdata)
                while len(pktqueue):
                    (data, sockaddr) = pktqueue.popleft()
                    cls._route_ipmiresponse(sockaddr, data)
                    while _poller((cls.socket,)):  # seems ridiculous,
                         #but between every callback, check for packets again
                        rdata = cls.socket.recvfrom(3000)
                        pktqueue.append(rdata)
            for handlepair in _poller(cls.readersockets):
                if isinstance(handlepair, int):
                    myhandle = handlepair
                else:
                    myhandle = handlepair.fileno()
                if myhandle != cls.socket.fileno() and callout:
                    myfile = cls._external_handlers[myhandle][1]
                    cls._external_handlers[myhandle][0](myfile)
        sessionstodel = []
        for session, parms in cls.keepalive_sessions.iteritems():
            if parms['timeout'] < curtime:
                cls.keepalive_sessions[session]['timeout'] = 25 + \
                    (random.random() * 4.9)
                session._keepalive()
        for session, parms in cls.waiting_sessions.iteritems():
            if parms['timeout'] < curtime:  # timeout has expired, time to
                                            # give up on it and trigger timeout
                                            # response in the respective
                                            # session
                sessionstodel.append(
                    session)  # defer deletion until after loop
                                              # to avoid confusing the for loop
        for session in sessionstodel:
            cls.pending -= 1
            cls.waiting_sessions.pop(session, None)
            session._timedout()
        return len(cls.waiting_sessions)

    def _keepalive(self):
        """Performs a keepalive to avoid idle disconnect
        """
        if self.incommand:  # if currently in command, no cause to keepalive
            return
        self.raw_command(netfn=6, command=1)

    @classmethod
    def register_handle_callback(cls, handle, callback):
        """Add a handle to be watched by Session's event loop

        In the event that an application would like IPMI Session event loop
        to drive things while adding their own filehandle to watch for events,
        this class method will register that.

        :param handle: filehandle too watch for input
        :param callback: function to call when input detected on the handle.
                         will receive the handle as an argument
        """
        if isinstance(handle, int):
            cls._external_handlers[handle] = (callback, handle)
        else:
            cls._external_handlers[handle.fileno()] = (callback, handle)
        #If we don't have a socket yet, we need one for the code to behave
        #correctly from this point forward
        if not hasattr(Session, 'socket'):
            cls._createsocket()
        cls.readersockets += [handle]

    @classmethod
    def _route_ipmiresponse(cls, sockaddr, data):
        if not (data[0] == '\x06' and data[2:4] == '\xff\x07'):  # not ipmi
            return
        try:
            cls.bmc_handlers[sockaddr]._handle_ipmi_packet(data,
                                                           sockaddr=sockaddr)
            cls.pending -= 1
        except KeyError:
            pass

    def _handle_ipmi_packet(self, data, sockaddr=None):
        if self.sockaddr is None and sockaddr is not None:
            self.sockaddr = sockaddr
        elif (self.sockaddr is not None and
              sockaddr is not None and
              self.sockaddr != sockaddr):
            return  # here, we might have sent an ipv4 and ipv6 packet to kick
                   # things off ignore the second reply since we have one
                   # satisfactory answer
        if data[4] in ('\x00', '\x02'):  # This is an ipmi 1.5 paylod
            remsequencenumber = struct.unpack('<I', data[5:9])[0]
            if (hasattr(self, 'remsequencenumber') and
                    remsequencenumber < self.remsequencenumber):
                return -5  # remote sequence number is too low, reject it
            self.remsequencenumber = remsequencenumber
            if ord(data[4]) != self.authtype:
                return -2  # BMC responded with mismatch authtype, for
                          # mutual authentication reject it. If this causes
                          # legitimate issues, it's the vendor's fault
            remsessid = struct.unpack("<I", data[9:13])[0]
            if remsessid != self.sessionid:
                return -1  # does not match our session id, drop it
            # now we need a mutable representation of the packet, rather than
            # copying pieces of the packet over and over
            rsp = list(struct.unpack("!%dB" % len(data), data))
            authcode = False
            if data[4] == '\x02':  # we have authcode in this ipmi 1.5 packet
                authcode = data[13:29]
                del rsp[13:29]
                    # this is why we needed a mutable representation
            payload = list(rsp[14:14 + rsp[13]])
            if authcode:
                expectedauthcode = self._ipmi15authcode(payload,
                                                        checkremotecode=True)
                expectedauthcode = struct.pack("%dB" % len(expectedauthcode),
                                               *expectedauthcode)
                if expectedauthcode != authcode:
                    return
            self._parse_ipmi_payload(payload)
        elif data[4] == '\x06':
            self._handle_ipmi2_packet(data)
        else:
            return  # unrecognized data, assume evil

    def _handle_ipmi2_packet(self, rawdata):
        data = list(struct.unpack("%dB" % len(rawdata), rawdata))
                    #now need mutable array
        ptype = data[5] & 0b00111111
        # the first 16 bytes are header information as can be seen in 13-8 that
        # we will toss out
        if ptype == 0x11:  # rmcp+ response
            return self._got_rmcp_response(data[16:])
        elif ptype == 0x13:
            return self._got_rakp2(data[16:])
        elif ptype == 0x15:
            return self._got_rakp4(data[16:])
        elif ptype == 0 or ptype == 1:  # good old ipmi payload or sol
            # If endorsing a shared secret scheme, then at the very least it
            # needs to do mutual assurance
            if not (data[5] & 0b01000000):  # This would be the line that might
                                         # trip up some insecure BMC
                                         # implementation
                return
            encrypted = 0
            if data[5] & 0b10000000:
                encrypted = 1
            authcode = rawdata[-12:]
            expectedauthcode = HMAC.new(
                self.k1, rawdata[4:-12], SHA).digest()[:12]
            if authcode != expectedauthcode:
                return  # BMC failed to assure integrity to us, drop it
            sid = struct.unpack("<I", rawdata[6:10])[0]
            if sid != self.localsid:  # session id mismatch, drop it
                return
            remseqnumber = struct.unpack("<I", rawdata[10:14])[0]
            if (hasattr(self, 'remseqnumber') and
                (remseqnumber < self.remseqnumber) and
                    (self.remseqnumber != 0xffffffff)):
                return
            self.remseqnumber = remseqnumber
            psize = data[14] + (data[15] << 8)
            payload = data[16:16 + psize]
            if encrypted:
                iv = rawdata[16:32]
                decrypter = AES.new(self.aeskey, AES.MODE_CBC, iv)
                decrypted = decrypter.decrypt(
                    struct.pack("%dB" % len(payload[16:]),
                                *payload[16:]))
                payload = struct.unpack("%dB" % len(decrypted), decrypted)
                padsize = payload[-1] + 1
                payload = list(payload[:-padsize])
            if ptype == 0:
                self._parse_ipmi_payload(payload)
            elif ptype == 1:  # There should be no other option
                # note that we assume the SOL payload is good enough to avoid
                # retry SOL logic is sufficiently different, we just
                # defer that call to the sol handler, it can re submit if it
                # is unhappy
                if self.last_payload_type == 1:  # but only if SOL was last tx
                    self.lastpayload = None
                    self.last_payload_type = None
                    Session.waiting_sessions.pop(self, None)
                    if len(self.pendingpayloads) > 0:
                        (nextpayload, nextpayloadtype, retry) = \
                            self.pendingpayloads.popleft()
                        self.send_payload(payload=nextpayload,
                                          payload_type=nextpayloadtype,
                                          retry=retry)
                if self.sol_handler:
                    self.sol_handler(payload)

    def _got_rmcp_response(self, data):
        # see RMCP+ open session response table
        if not (self.sessioncontext and self.sessioncontext != "Established"):
            return -9
            # ignore payload as we are not in a state valid it
        if data[0] != self.rmcptag:
            return -9  # use rmcp tag to track and reject stale responses
        if data[1] != 0:  # response code...
            if data[1] in constants.rmcp_codes:
                errstr = constants.rmcp_codes[data[1]]
            else:
                errstr = "Unrecognized RMCP code %d" % data[1]
            self.onlogon({'error': errstr})
            return -9
        self.allowedpriv = data[2]
        # TODO(jbjohnso): enable lower priv access (e.g. operator/user)
        localsid = struct.unpack("<I", struct.pack("4B", *data[4:8]))[0]
        if self.localsid != localsid:
            return -9
        self.pendingsessionid = struct.unpack(
            "<I", struct.pack("4B", *data[8:12]))[0]
        # TODO(jbjohnso): currently, we take it for granted that the responder
        # accepted our integrity/auth/confidentiality proposal
        self.lastpayload = None
        self._send_rakp1()

    def _send_rakp1(self):
        self.rmcptag += 1
        self.randombytes = os.urandom(16)
        userlen = len(self.userid)
        payload = [self.rmcptag, 0, 0, 0] + \
            list(struct.unpack("4B",
                 struct.pack("<I", self.pendingsessionid))) +\
            list(struct.unpack("16B", self.randombytes)) +\
            [self.privlevel, 0, 0] +\
            [userlen] +\
            list(struct.unpack("%dB" % userlen, self.userid))
        self.sessioncontext = "EXPECTINGRAKP2"
        self.send_payload(
            payload=payload, payload_type=constants.payload_types['rakp1'])

    def _got_rakp2(self, data):
        if not (self.sessioncontext in ('EXPECTINGRAKP2', 'EXPECTINGRAKP4')):
            return -9  # if we are not expecting rakp2, ignore. In a retry
                      # scenario, replying from stale RAKP2 after sending
                      # RAKP3 seems to be best
        if data[0] != self.rmcptag:  # ignore mismatched tags for retry logic
            return -9
        if data[1] != 0:  # if not successful, consider next move
            if data[1] == 2:  # invalid sessionid 99% of the time means a retry
                             # scenario invalidated an in-flight transaction
                return
            if data[1] in constants.rmcp_codes:
                errstr = constants.rmcp_codes[data[1]]
            else:
                errstr = "Unrecognized RMCP code %d" % data[1]
            self.onlogon({'error': errstr + " in RAKP2"})
            return -9
        localsid = struct.unpack("<I", struct.pack("4B", *data[4:8]))[0]
        if localsid != self.localsid:
            return -9  # discard mismatch in the session identifier
        self.remoterandombytes = struct.pack("16B", *data[8:24])
        self.remoteguid = struct.pack("16B", *data[24:40])
        userlen = len(self.userid)
        hmacdata = struct.pack("<II", localsid, self.pendingsessionid) +\
            self.randombytes + self.remoterandombytes + self.remoteguid +\
            struct.pack("2B", self.privlevel, userlen) +\
            self.userid
        expectedhash = HMAC.new(self.password, hmacdata, SHA).digest()
        givenhash = struct.pack("%dB" % len(data[40:]), *data[40:])
        if givenhash != expectedhash:
            self.sessioncontext = "FAILED"
            self.onlogon({'error': "Incorrect password provided"})
            return -9
        # We have now validated that the BMC and client agree on password, time
        # to store the keys
        self.sik = HMAC.new(self.kg,
                            self.randombytes + self.remoterandombytes +
                            struct.pack("2B", self.privlevel, userlen) +
                            self.userid, SHA).digest()
        self.k1 = HMAC.new(self.sik, '\x01' * 20, SHA).digest()
        self.k2 = HMAC.new(self.sik, '\x02' * 20, SHA).digest()
        self.aeskey = self.k2[0:16]
        self.sessioncontext = "EXPECTINGRAKP4"
        self.lastpayload = None
        self._send_rakp3()

    def _send_rakp3(self):  # rakp message 3
        self.rmcptag += 1
        # rmcptag, then status 0, then two reserved 0s
        payload = [self.rmcptag, 0, 0, 0] +\
            list(struct.unpack("4B", struct.pack("<I", self.pendingsessionid)))
        hmacdata = self.remoterandombytes +\
            struct.pack("<I", self.localsid) +\
            struct.pack("2B", self.privlevel, len(self.userid)) +\
            self.userid
        authcode = HMAC.new(self.password, hmacdata, SHA).digest()
        payload += list(struct.unpack("%dB" % len(authcode), authcode))
        self.send_payload(
            payload=payload, payload_type=constants.payload_types['rakp3'])

    def _relog(self):
        self._initsession()
        self.logontries -= 1
        return self._get_channel_auth_cap()

    def _got_rakp4(self, data):
        if self.sessioncontext != "EXPECTINGRAKP4" or data[0] != self.rmcptag:
            return -9
        if data[1] != 0:
            if data[1] == 2 and self.logontries:  # if we retried RAKP3 because
               # RAKP4 got dropped, BMC can consider it done and we must
               # restart
                self._relog()
            if data[1] == 15 and self.logontries:  # ignore 15 value if we are
                    # retrying.  xCAT did but I can't recall why exactly
                    # TODO(jbjohnso) jog my memory to update the comment
                return
            if data[1] in constants.rmcp_codes:
                errstr = constants.rmcp_codes[data[1]]
            else:
                errstr = "Unrecognized RMCP code %d" % data[1]
            self.onlogon({'error': errstr + " reported in RAKP4"})
            return -9
        localsid = struct.unpack("<I", struct.pack("4B", *data[4:8]))[0]
        if localsid != self.localsid:  # ignore if wrong session id indicated
            return -9
        hmacdata = self.randombytes +\
            struct.pack("<I", self.pendingsessionid) +\
            self.remoteguid
        expectedauthcode = HMAC.new(self.sik, hmacdata, SHA).digest()[:12]
        authcode = struct.pack("%dB" % len(data[8:]), *data[8:])
        if authcode != expectedauthcode:
            self.onlogon({'error': "Invalid RAKP4 integrity code (wrong Kg?)"})
            return
        self.sessionid = self.pendingsessionid
        self.integrityalgo = 'sha1'
        self.confalgo = 'aes'
        self.sequencenumber = 1
        self.sessioncontext = 'ESTABLISHED'
        self.lastpayload = None
        self._req_priv_level()

    '''
    Internal function to parse IPMI nugget once extracted from its framing
    '''

    def _parse_ipmi_payload(self, payload):
        # For now, skip the checksums since we are in LAN only,
        # TODO(jbjohnso): if implementing other channels, add checksum checks
        # here
        if (payload[4] != self.seqlun or
                payload[1] >> 2 != self.expectednetfn or
                payload[5] != self.expectedcmd):
            return -1  # payload is not a match for our last packet
        if hasattr(self, 'hasretried') and self.hasretried:
            self.hasretried = 0
            self.tabooseq[
                (self.expectednetfn, self.expectedcmd, self.seqlun)] = 16
             # try to skip it for at most 16 cycles of overflow
        # We want to now remember that we do not have an expected packet
        self.expectednetfn = 0x1ff  # bigger than one byte means it can never
                                    # match the one byte value by mistake
        self.expectedcmd = 0x1ff
        self.seqlun += 4  # prepare seqlun for next transmit
        self.seqlun &= 0xff  # when overflowing, wrap around
        Session.waiting_sessions.pop(self, None)
        self.lastpayload = None  # render retry mechanism utterly incapable of
                                 # doing anything, though it shouldn't matter
        self.last_payload_type = None
        response = {}
        response['netfn'] = payload[1] >> 2
        del payload[0:5]
        # ^^ remove header of rsaddr/netfn/lun/checksum/rq/seq/lun
        del payload[-1]  # remove the trailing checksum
        response['command'] = payload[0]
        response['code'] = payload[1]
        del payload[0:2]
        response['data'] = payload
        self.timeout = initialtimeout + (0.5 * random.random())
        if len(self.pendingpayloads) > 0:
            (nextpayload, nextpayloadtype, retry) = \
                self.pendingpayloads.popleft()
            self.send_payload(payload=nextpayload,
                              payload_type=nextpayloadtype,
                              retry=retry)
        self.incommand = False
        call_with_optional_args(self.ipmicallback,
                                response,
                                self.ipmicallbackargs)

    def _timedout(self):
        if not self.lastpayload:
            return
        self.nowait = True
        self.timeout += 1
        if self.timeout > 5:
            response = {'error': 'timeout'}
            call_with_optional_args(self.ipmicallback,
                                    response,
                                    self.ipmicallbackargs)
            self.incommand = False
            self.nowait = False
            return
        elif self.sessioncontext == 'FAILED':
            self.nowait = False
            return
        if self.sessioncontext == 'OPENSESSION':
            # In this case, we want to craft a new session request to have
            # unambiguous session id regardless of how packet was dropped or
            # delayed in this case, it's safe to just redo the request
            self._open_rmcpplus_request()
        elif (self.sessioncontext == 'EXPECTINGRAKP2' or
              self.sessioncontext == 'EXPECTINGRAKP4'):
            # If we can't be sure which RAKP was dropped or if RAKP3/4 was just
            # delayed, the most reliable thing to do is rewind and start over
            # bmcs do not take kindly to receiving RAKP1 or RAKP3 twice
            self._relog()
        else:  # in IPMI case, the only recourse is to act as if the packet is
              # idempotent.  SOL has more sophisticated retry handling
             # the biggest risks are reset sp which is often fruitless to retry
             # and chassis reset, which sometimes will shoot itself
             # systematically in the head in a shared port case making replies
             # impossible
            self.hasretried = 1  # remember so that we can track taboo
                              # combinations
                              # of sequence number, netfn, and lun due to
                              # ambiguity on the wire
            self.send_payload()
        self.nowait = False

    def _xmit_packet(self, retry=True, delay_xmit=None):
        if not self.nowait:  # if we are retrying, we really need to get the
                            # packet out and get our timeout updated
            Session.wait_for_rsp(timeout=0, callout=False)  # take opportunity
                                                 # to drain the socket queue if
                                                 # applicable
            while Session.pending > Session.maxpending:
                Session.wait_for_rsp()
        if self.sequencenumber:  # seq number of zero will be left alone, it is
                                # special, otherwise increment
            self.sequencenumber += 1
        if retry:
            Session.waiting_sessions[self] = {}
            Session.waiting_sessions[self]['ipmisession'] = self
            Session.waiting_sessions[self]['timeout'] = self.timeout + \
                _monotonic_time()
            Session.pending += 1
        if delay_xmit is not None:
            Session.waiting_sessions[self]['timeout'] = delay_xmit + \
                _monotonic_time()
            return  # skip transmit, let retry timer do it's thing
        if self.sockaddr:
            Session.socket.sendto(self.netpacket, self.sockaddr)
        else:  # he have not yet picked a working sockaddr for this connection,
              # try all the candidates that getaddrinfo provides
            try:
                for res in socket.getaddrinfo(self.bmc,
                                              self.port,
                                              0,
                                              socket.SOCK_DGRAM):
                    sockaddr = res[4]
                    if (res[0] == socket.AF_INET):  # convert the sockaddr
                                                    # to AF_INET6
                        newhost = '::ffff:' + sockaddr[0]
                        sockaddr = (newhost, sockaddr[1], 0, 0)
                    Session.bmc_handlers[sockaddr] = self
                    Session.socket.sendto(self.netpacket, sockaddr)
            except socket.gaierror:
                raise exc.IpmiException(
                    "Unable to transmit to specified address")

    def logout(self, callback=None, callback_args=None):
        if not self.logged:
            if callback is None:
                return {'success': True}
            callback({'success': True})
            return
        if self.cleaningup:
            self.nowait = True
            callback = False
        self.raw_command(command=0x3c,
                         netfn=6,
                         data=struct.unpack("4B",
                                            struct.pack("I", self.sessionid)),
                         retry=False,
                         callback=callback,
                         callback_args=callback_args)
        self.logged = 0
        self.nowait = False
        if not callback:
            return {'success': True}
        callback({'success': True})


if __name__ == "__main__":
    import sys
    ipmis = Session(bmc=sys.argv[1],
                    userid=sys.argv[2],
                    password=os.environ['IPMIPASS'])
    print ipmis.raw_command(command=2, data=[1], netfn=0)
    print get_ipmi_error({'command': 8, 'code': 128, 'netfn': 1})
