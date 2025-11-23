""" Library to access Heatmiser thermostats via an RS485 interface
    Primarily intended for Home Assistant use, but can also be used by other apps.
    Changelog in repo
"""
# TBD check master addr = 129 or 160, does it matter?, what does touchpad use?

import serial
import logging
import time
import threading

# use a semaphore to stop concurrent access to serial line.
# Hass seems to call Update on multiple stats in sequence. However writes (to change setpoint or frost setting)
# may occur in middle of a read, causing line errors (eg CRC)

sema = threading.Semaphore(1)

# Serial line attempts
MAX_TRIES = 5

_LOGGER = logging.getLogger(__name__)

class CRC16:
    """CRC function (aka CCITT) used by Heatmiser stats"""
    LookupHi = [ 0x00, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70,
                 0x81, 0x91, 0xa1, 0xb1, 0xc1, 0xd1, 0xe1, 0xf1 ]
    LookupLo = [ 0x00, 0x21, 0x42, 0x63, 0x84, 0xa5, 0xc6, 0xe7,
                 0x08, 0x29, 0x4a, 0x6b, 0x8c, 0xad, 0xce, 0xef ]

    def __init__(self):
        self.hi = 0xff
        self.lo = 0xff

    def _extract_bits(self, val):
        t = (self.hi >> 4) ^ val
        self.hi = ((self.hi << 4) | (self.lo >> 4)) & 0xff
        self.lo = (self.lo << 4) & 0xff 
        # Do the table lookups and XOR the result into the CRC tables
        self.hi = (self.hi ^ self.LookupHi[t]) & 0xff
        self.lo = (self.lo ^ self.LookupLo[t]) & 0xff

    def _update(self, val):
        self._extract_bits(val >> 4)     # High nibble first
        self._extract_bits(val & 0x0f)   # Low nibble

    def run(self, message):
        for value in message:
            self._update(value)
        return [self.lo, self.hi]


class HM_RS485:
    """ The RS485 interface supports 1-32 heatmiser thermostats
        Provides methods to:
        read_stat  - read all fields from a stat
        write_stat - writes a list of values to the stat
    """
    def __init__(self, ipaddress: None, port: None, serialid: None):
        if serialid is None and ipaddress and port:
            _LOGGER.info(f'Initialising RS485 {ipaddress} : {port}')
            self.serport = serial.serial_for_url("socket://" + ipaddress + ":" + port)
        elif serialid and ipaddress is None and port is None:
            _LOGGER.info(f'Initialising RS485 on {serialid}')
            self.serport = serial.Serial()
            self.serport.port = serialid
        else:
            raise ValueError(f"Provide one of ipaddress and port or serialid, not both:\n ip: {ipaddress}, port: {port}: serialid: {serialid}") 
        self.serport.baudrate = 4800
        self.serport.bytesize = serial.EIGHTBITS
        self.serport.parity = serial.PARITY_NONE
        self.serport.stopbits = serial.STOPBITS_ONE
        self.serport.timeout = 0.8
        
        self.serport.close()  # just in case it was left open
        self.serport.open()
        _LOGGER.debug("Serial port opened OK")

        # Maintain statistics for the line
        self.total      = 0 #total reads & writes
        self.crc_count  = 0 #crc errors - soft
        self.ndr_count  = 0 #No Data Read NDR errors - soft
        self.oth_count  = 0 #Other errors - soft
        self.hard_count = 0 #if retries fail - hard

    def _lohibytes(self, value):
        # splits value into 2 bytes, returns lo, hi bytes
        return value & 0xff, (value >> 8) & 0xff

    def _verify(self, stat, datal):
        # verifies reply from stat by checking CRC and header fields, raises exception if error
        # nly called from _send_read_check, but easier to read seperated like this

        length = len(datal)
        if length < 3:
            self.ndr_count +=1
            raise ValueError(f'No data read {length}')
        
        #calculate and check the checksum
        rxmsg = datal[:length - 2]
        crc = CRC16()
        if crc.run(rxmsg) != datal[length - 2:]:
            self.crc_count +=1
            raise ValueError(f'Bad CRC {length}')
        
        dest = datal[0]
        source = datal[3]
        if (dest != 129 and dest != 160) or source != stat:
            self.oth_count +=1
            raise ValueError(f'Bad source/dest addr {source} {dest}')
        
        func = datal[4]  # should be 0 read or 1 write
        if func != 1 and func != 0:
            self.oth_count +=1
            raise ValueError(f'Bad Func {func}')
        
        frame_len = datal[2] * 256 + datal[1]
        if func == 1 and frame_len != 7 or length != frame_len:
            self.oth_count +=1
            raise ValueError("Length Error")

        # reply OK

    def _send_read_check (self, stat, msg):
        # sends message to stat, reads reply and checks it's ok
        # common ode for both read and write

        _status = 0  # assume success
        _reply = [0]
        self.total +=1
        for _tries in range(MAX_TRIES):  # ie 0 to max_tries-1
            try:
                _LOGGER.debug(f'sending to {stat}- len: {len(msg)} msg ={msg} tries ={_tries}')
                
                #Add crc and send to serial line
                crc = CRC16()
                string = bytes(msg + crc.run(msg))
                self.serport.write(string)
               
                # now read reply and check its ok
                data = list(self.serport.read(159)) 
                _LOGGER.debug(f'Reply: length {len(data)} Data = {data}')
                self._verify(stat, data)  # will raise exception if error
                _LOGGER.debug(f'Reply OK')
                _reply = data[9:len(data)-2] # strip off header & crc

            except ValueError as err:
                _LOGGER.info(f'Exception - stat {stat} = {err}')
                time.sleep(0.1) # sleep, then retry
                continue

            except serial.SerialException as err:
                #probably a broken pipe error - line disconnected, powered off etc
                _LOGGER.error(f'Serial exception:  stat= {stat} err= {err}')
                self .serport.close()  # just in case it was left open
                self.serport.open()
                _LOGGER.debug("Serial port re-opened")
                continue

            else:      # no exception
                _errors = _tries
                break  # skips else block below
        else:    
            # Exhausted max_tries, so hard error
            self.hard_count +=1
            _errors =_tries + 1
            _status = -1
            _LOGGER.error(f'Hard error: too may tries on stat {stat}')     
        
        # Every few thousand calls, log line stats    
        if self.total % 10000==0:
            _LOGGER.info(f'Line stats - [total, crc, ndr, oth , hard] {self.total} {self.crc_count} {self.ndr_count} {self.oth_count} {self.hard_count}') 

        return _status, _reply, _errors


    def write_stat(self, stat, index, payload):
        # writes the payload (a list of values) to the stat. index gives position in dcb
        # returns status, error count
        
        sema.acquire() # stop Hass multithreading calls to the serial line
        _LOGGER.info(f'write_stat- no, index, payload = {stat} {index} {payload}')
        
         #form command to write value to stat
        startlo, starthi = self._lohibytes(index)
        lengthlo, lengthhi = self._lohibytes(len(payload))
        _command = [stat, 10+len(payload), 129, 1,
                   startlo, starthi, lengthlo, lengthhi] + payload
        _status, _reply, _errors = self._send_read_check (stat, _command )
        sema.release()
        return _status, _errors


    def read_stat(self,stat): 
        # reads the whole dcb from the stat, returns status, raw dcb, error count
       
        sema.acquire()  
        _LOGGER.debug(f'read_stat - {stat}')
        
        # use standard read all command
        _command = [stat, 10, 129, 0, 0, 0, 0xff, 0xff]
        _status, _reply, _errors = self._send_read_check ( stat, _command )
        sema.release()
        return _status, _reply, _errors
