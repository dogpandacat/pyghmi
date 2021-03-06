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
#
# This represents the low layer message framing portion of IPMI

import fcntl
import os
import struct

from pyghmi.ipmi.private import constants
from pyghmi.ipmi.private import session


class Console(object):
    """IPMI SOL class.

    This object represents an SOL channel, multiplexing SOL data with
    commands issued by ipmi.command.

    :param bmc: hostname or ip address of BMC
    :param userid: username to use to connect
    :param password: password to connect to the BMC
    :param iohandler: Either a function to call with bytes, a filehandle to
                      use for input and output, or a tuple of (input, output)
                      handles
    :param kg: optional parameter for BMCs configured to require it
    """

    #TODO(jbjohnso): still need an exit and a data callin function
    def __init__(self, bmc, userid, password,
                 iohandler, port=623,
                 force=False, kg=None):
        if type(iohandler) == tuple:  # two file handles
            self.console_in = iohandler[0]
            self.console_out = iohandler[1]
        elif type(iohandler) == file:  # one full duplex file handle
            self.console_out = iohandler
            self.console_in = iohandler
        elif hasattr(iohandler, '__call__'):
            self.console_out = None
            self.console_in = None
            self.out_handler = iohandler
        if self.console_in is not None:
            fcntl.fcntl(self.console_in.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
        self.remseq = 0
        self.myseq = 0
        self.lastsize = 0
        self.sendbreak = 0
        self.ackedcount = 0
        self.ackedseq = 0
        self.retriedpayload = 0
        self.pendingoutput = ""
        self.awaitingack = False
        self.force_session = force
        self.ipmi_session = session.Session(bmc=bmc,
                                            userid=userid,
                                            password=password,
                                            port=port,
                                            kg=kg,
                                            onlogon=self._got_session)

    def _got_session(self, response):
        """Private function to navigate SOL payload activation
        """
        if 'error' in response:
            self._print_data(response['error'])
            return
        #Send activate sol payload directive
        #netfn= 6 (application)
        #command = 0x48 (activate payload)
        #data = (1, sol payload type
        #        1, first instance
        #        0b11000000, -encrypt, authenticate,
        #                      disable serial/modem alerts, CTS fine
        #        0, 0, 0 reserved
        self.ipmi_session.raw_command(netfn=0x6, command=0x48,
                                      data=(1, 1, 192, 0, 0, 0),
                                      callback=self._payload_activated)

    def _payload_activated(self, response):
        """Check status of activate payload request
        """
        if 'error' in response:
            self._print_data(response['error'])
        #given that these are specific to the command,
        #it's probably best if one can grep the error
        #here instead of in constants
        sol_activate_codes = {
            0x81: 'SOL is disabled',
            0x82: 'Maximum SOL session count reached',
            0x83: 'Cannot activate payload with encryption',
            0x84: 'Cannot activate payload without encryption',
        }
        if response['code']:
            if response['code'] in constants.ipmi_completion_codes:
                self._print_data(
                    constants.ipmi_completion_codes[response['code']])
                return
            elif response['code'] == 0x80:
                if self.force_session and not self.retriedpayload:
                    self.retriedpayload = 1
                    self.ipmi_session.raw_command(netfn=0x6, command=0x49,
                                                  data=(1, 1, 0, 0, 0, 0),
                                                  callback=self._got_session)
                    return
                else:
                    self._print_data('SOL Session active for another client\n')
                    return
            elif response['code'] in sol_activate_codes:
                self._print_data(sol_activate_codes[response['code']]+'\n')
                return
            else:
                self._print_data(
                    'SOL encountered Unrecognized error code %d\n' %
                    response['code'])
                return
        #data[0:3] is reserved except for the test mode, which we don't use
        data = response['data']
        self.maxoutcount = (data[5] << 8) + data[4]
           #BMC tells us this is the maximum allowed size
        #data[6:7] is the promise of how small packets are going to be, but we
        #don't have any reason to worry about it
        if (data[8] + (data[9] << 8)) != 623:
            #TODO(jbjohnso): support atypical SOL port number
            raise NotImplementedError("Non-standard SOL Port Number")
        #ignore data[10:11] for now, the vlan detail, shouldn't matter to this
        #code anyway...
        self.ipmi_session.sol_handler = self._got_sol_payload
        if self.console_in is not None:
            self.ipmi_session.register_handle_callback(self.console_in,
                                                       self._got_cons_input)

    def _got_cons_input(self, handle):
        """Callback for handle events detected by ipmi session
        """
        self.pendingoutput += handle.read()
        if not self.awaitingack:
            self._sendpendingoutput()

    def send_data(self, data):
        self.pendingoutput += data
        if not self.awaitingack:
            self._sendpendingoutput()

    @classmethod
    def wait_for_rsp(cls, timeout):
        """Delay for no longer than timeout for next response.

        This acts like a sleep that exits on activity.

        :param timeout: Maximum number of seconds before returning
        """
        return session.Session.wait_for_rsp(timeout=timeout)

    def _sendpendingoutput(self):
        self.myseq += 1
        self.myseq &= 0xf
        if self.myseq == 0:
            self.myseq = 1
        payload = struct.pack("BBBB",
                              self.myseq,
                              self.ackedseq,
                              self.ackedseq,
                              self.sendbreak)
        payload += self.pendingoutput
        self.lasttextsize = len(self.pendingoutput)
        self.pendingoutput = ""
        self.awaitingack = True
        payload = struct.unpack("%dB" % len(payload), payload)
        self.lastpayload = payload
        self.ipmi_session.send_payload(payload, payload_type=1)

    def _print_data(self, data):
        """Convey received data back to caller in the format of their choice.

        Caller may elect to provide this class filehandle(s) or else give a
        callback function that this class will use to convey data back to
        caller.
        """
        if self.console_out is not None:
            self.console_out.write(data)
            self.console_out.flush()
        elif self.out_handler:  # callback style..
            self.out_handler(data)

    def _got_sol_payload(self, payload):
        """SOL payload callback
        """
        #TODO(jbjohnso) test cases to throw some likely scenarios at functions
        #for example, retry with new data, retry with no new data
        #retry with unexpected sequence number
        newseq = payload[0] & 0b1111
        ackseq = payload[1] & 0b1111
        ackcount = payload[2]
        nacked = payload[3] & 0b1000000
        poweredoff = payload[3] & 0b100000
        deactivated = payload[3] & 0b10000
        #for now, ignore overrun.  I assume partial NACK for this reason or for
        #no reason would be treated the same, new payload with partial data
        remdata = ""
        remdatalen = 0
        if newseq != 0:  # this packet at least has some data to send to us..
            if len(payload) > 4:
                remdatalen = len(payload[4:])  # store remote len before dupe
                    #retry logic, we must ack *this* many even if it is
                    #a retry packet with new partial data
                remdata = struct.pack("%dB" % remdatalen, *payload[4:])
            if newseq == self.remseq:  # it is a retry, but could have new data
                if remdatalen > self.lastsize:
                    remdata = remdata[4 + self.lastsize:]
                else:  # no new data...
                    remdata = ""
            else:  # TODO(jbjohnso) what if remote sequence number is wrong??
                self.remseq = newseq
            self.lastsize = remdatalen
            if remdata:  # Do not subject callers to empty data
                self._print_data(remdata)
            ackpayload = (0, self.remseq, remdatalen, 0)
            #Why not put pending data into the ack? because it's rare
            #and might be hard to decide what to do in the context of
            #retry situation
            self.ipmi_session.send_payload(ackpayload,
                                           payload_type=1, retry=False)
        if self.myseq != 0 and ackseq == self.myseq:  # the bmc has something
                                                      # to say about last xmit
            self.awaitingack = False
            if nacked > 0:  # the BMC was in some way unhappy
                if poweredoff:
                    self._print_data("Remote system is powered down\n")
                if deactivated:
                    self._print_data("Remote IPMI console disconnected\n")
                else:  # retry all or part of packet, but in a new form
                    # also add pending output for efficiency and ease
                    newtext = self.lastpayload[4 + ackcount:]
                    newtext = struct.pack("B"*len(newtext), *newtext)
                    self.pendingoutput = newtext + self.pendingoutput
                    self._sendpendingoutput()
        elif self.awaitingack:  # session marked us as happy, but we are not
                #this does mean that we will occasionally retry a packet
                #sooner than retry suggests, but that's no big deal
            self.ipmi_session.send_payload(payload=self.lastpayload,
                                           payload_type=1)

    def main_loop(self):
        """Process all events until no more sessions exist.

        If a caller is a simple little utility, provide a function to
        eternally run the event loop.  More complicated usage would be expected
        to provide their own event loop behavior, though this could be used
        within the greenthread implementation of caller's choice if desired.
        """
        #wait_for_rsp promises to return a false value when no sessions are
        #alive anymore
        #TODO(jbjohnso): wait_for_rsp is not returning a true value for our own
        #session
        while (1):
            session.Session.wait_for_rsp(timeout=600)
