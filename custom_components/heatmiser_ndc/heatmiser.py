"""
    Heatmiser library to access multiple Heatmiser thermostats (PRT-N) via an RS485 interface
    library designed to be used by Home Assistant and other apps. 
    Try to keep all Home Assistant stuff out of here
"""
# NDC Dec 2020
# See changelog


import serial
import logging
import asyncio
import serial_asyncio
import time

# use a semaphore to stop concurrent access to serial line.
# Hass seems to call Update on multiple stats in sequence. However writes (to change setpoint or frost setting)
# may occur in middle of a read, causing line errors (eg CRC)
import threading
sema = threading.Semaphore(1)

# COMM SETTINGS
COM_PORT = 6  # 1 less than com port, USB is 6=com7, ether is 9=10
COM_BAUD = 4800
COM_SIZE = serial.EIGHTBITS
COM_PARITY = serial.PARITY_NONE
COM_STOP = serial.STOPBITS_ONE
COM_TIMEOUT = 0.8 # seconds

# Serial line retries
MAX_RETRIES = 5


_LOGGER = logging.getLogger(__name__)

class HM_RS485:
    """ The Heatmiser RS485 interface with multiple thermostats """

    def __init__(self, ipaddress, port):
        _LOGGER.info(f'Initialising interface {ipaddress} : {port}')
        self.thermostats = {}
        self._serport = serial.serial_for_url("socket://" + ipaddress + ":" + port)
        # close port just in case its been left open from before
        serport_response = self._serport.close()
        _LOGGER.debug(f'SerialPortResponse: {serport_response}')
        self._serport.baudrate = COM_BAUD
        self._serport.bytesize = COM_SIZE
        self._serport.parity = COM_PARITY
        self._serport.stopbits = COM_STOP
        self._serport.timeout = COM_TIMEOUT
        self._serport.open()
        _LOGGER.debug("Serial port opened OK")


    def registerThermostat(self, thermostat):
        """Registers a thermostat with the serial interface"""
            
        if thermostat.address in self.thermostats.keys():
            _LOGGER.error(f'Stat already present {thermostat.address}')
        else:
            self.thermostats[thermostat.address] = thermostat
            _LOGGER.debug(f'Thermostat: {thermostat.address} registered')
        return self._serport


class CRC16:
    """CRC function (aka CCITT) """
    LookupHi = [
        0x00, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70,
        0x81, 0x91, 0xa1, 0xb1, 0xc1, 0xd1, 0xe1, 0xf1
    ]
    LookupLo = [
        0x00, 0x21, 0x42, 0x63, 0x84, 0xa5, 0xc6, 0xe7,
        0x08, 0x29, 0x4a, 0x6b, 0x8c, 0xad, 0xce, 0xef
    ]

    def __init__(self):
        self.hi = 0xff
        self.lo = 0xff

    def _extract_bits(self, val):
        thisval = self.hi >> 4
        thisval = thisval ^ val
        self.hi = (self.hi << 4) | (self.lo >> 4)
        self.hi = self.hi & 0xff    # force char
        self.lo = self.lo << 4
        self.lo = self.lo & 0xff      # force char
        # Do the table lookups and XOR the result into the CRC tables
        self.hi = self.hi ^ self.LookupHi[thisval]
        self.hi = self.hi & 0xff    # force char
        self.lo = self.lo ^ self.LookupLo[thisval]
        self.lo = self.lo & 0xff      # force char

    def _update(self, val):
        self._extract_bits(val >> 4)     # High nibble first
        self._extract_bits(val & 0x0f)   # Low nibble

    def run(self, message):
        for value in message:
            self._update(value)
        return [self.lo, self.hi]


class HeatmiserStat:
    """ Represents a heatmiser thermostat 
    Provides methods to:
       read_dcb -  read all fields from the stat into dcb, in raw state
       get_...... -extracts individual fields from dcb eg target temp, frost temp, status, heating,  temp format etc
       set_...    - writes a single field to the stat eg target temp, frost protect temp, floor max temp etc
       After calls of set methods, HA itself then calls update to update internal dcb
    """
    
    def __init__(self, address, name, rs485):
        # address is stat no.
        #name is passed in solely to make error reporting more meaningful
        _LOGGER.debug(f'HeatmiserStat init stat {address} {name}')
        self.address = address
        self.name = name
        #Allocate space and initialise dcb to 0. Necessary to avoid crash, if first read from stat fails 
        self.dcb = [0] * 160

        # Initialise statistics - [read, write]
        self.rwcount = [0,0]   #read/write count
        self.crccount = [0,0]  #crc errors - soft
        self.ndrcount = [0,0]  #No Data Read NDR errors - soft
        self.othcount = [0,0]  #Other errors - soft
        self.hardcount = [0,0] #if retries fail - hard

        self.conn = rs485.registerThermostat(self)  # register stat to ser i/f
        _LOGGER.debug(f'Init done. Conn = {self.conn}')

    def _lohibytes(self, value):
        # splits value into 2 bytes, returns lo, hi bytes
        return value & 0xff, (value >> 8) & 0xff

    def _verify(self, stat, function, datal):
        # verifies reply from stat by checking CRC and header fields
        # if any fields invalid, increments error counter and raises ValueError exception 
        # function : 0=read or 1=write
        # check most frequent things first ie NDR or CRC errors

        _LOGGER.debug(f'Verifying {stat} function {function}')
        length = len(datal)
        if length < 3:
            self.ndrcount [function] +=1
            raise ValueError(f'No data read {length}')
        
        checksum = datal[length - 2:]
        rxmsg = datal[:length - 2]
        crc = CRC16()   # Initialises the CRC
        if crc.run(rxmsg) != checksum:
            self.crccount [function] +=1
            raise ValueError(f'Bad CRC, length {length}')
        
        dest = datal[0]
        if (dest != 129 and dest != 160):
            self.othcount [function] +=1
            raise ValueError(f'Bad dest addr {dest}')
        
        source = datal[3]
        if source != stat:
            self.othcount [function] +=1
            raise ValueError(f'Bad src addr {source}')

        func = datal[4]
        if func != 1 and func != 0:
            self.othcount [function] +=1
            raise ValueError(f'Bad Func {func}')
        if func != function:
            self.othcount [function] +=1
            raise ValueError(f'Wrong Func Code {func} vs {function}')
        
        frame_len = datal[2] * 256 + datal[1]
        if function == 1 and frame_len != 7:
            self.othcount [function] +=1
            raise ValueError("Write length <> 7")
        if length != frame_len:
            self.othcount [function] +=1
            raise ValueError(f'reply length {length}<> header {frame_len}')
        
        # otherwise message appears OK

    def _send_msg(self, message):
        # Sends message to serial line, after adding CRC
        # This is the only method to write to the serial line

        _LOGGER.debug(f'Send msg - length: {len(message)}')
        
        crc = CRC16()
        string = bytes(message + crc.run(message))  # add CRC
        try:
            self.conn.write(string)
        except serial.SerialTimeoutException:
            # never seen this
            _LOGGER.error("Serial timeout on write")

    def _read_reply(self):
        # Read reply from serial line
        # max read length = 75 in 5/2 mode, 159 in 7day mode ?
        # TBD check these
        # This is the only method to read the serial line

        reply = list(self.conn.read(159))
        _LOGGER.debug(f'Reply read, length {len(reply)} Data = {reply}')
        return reply

    def _write_stat(self, stat, index, value):
        # writes a single value to the stat
        # index gives position in dcb
        # TBD - simplify len(payload) should be 1,  & lohibytes of 1
        # TBD master addr = 129 or 160, what does touchpad use

        _LOGGER.debug(f'write stat- no, index, value = {stat} {index} {value}')
        self.rwcount [1] +=1  # inc write count
        payload = [value]  # makes a list of 1 item
        startlo, starthi = self._lohibytes(index)
        lengthlo, lengthhi = self._lohibytes(len(payload))
        
        #form message to write value to stat
        msg = [stat, 10+len(payload), 129, 1,
               startlo, starthi, lengthlo, lengthhi]
        
        sema.acquire()
        for _retries in range(MAX_RETRIES):
            try:
                _LOGGER.debug(f'write stat- Retries {_retries}')
                self._send_msg(msg+payload)
                data = self._read_reply ()
                self._verify(stat, 1, data)
                _LOGGER.debug(f'write stat reply = {data}')
            except ValueError as err:
                _LOGGER.debug(f'write stat exception {err} for {self.name}')
                # sleep a bit and retry
                # should be OK for Hass, we run in sync worker thread
                time.sleep(0.5)
                continue
            else:
                break  #  no exception, skips else block below
        else:
            # Exhausted retries
            self.hardcount [1] +=1
            _LOGGER.error(f'Error - Write stat - too many retries - {self.name}')     
        #
        sema.release()

    def read_dcb(self):
        # Reads all data from stat 
        #   sends standard read message to serial i/f
        #   reads reply and verifies
        #   sets up dcb so later calls can extract values
        # verify raises various excpetions if errors are found
        
        stat = self.address
        # form standard read all command
        # TBD master addr = 129 or 160 ??
        msg = [stat, 10, 129, 0, 0, 0, 0xff, 0xff]
        self.rwcount [0] +=1  # increment read count
        sema.acquire()   # stop Hass multithreading calls to the serial line

        for _retries in range(MAX_RETRIES):
            try:
                _LOGGER.debug(f'read dcb- stat {stat} Retries {_retries}')
                self._send_msg(msg)  # send to serial line
                data = self._read_reply ()  #get reply
                self._verify(stat, 0, data)  #check reply is ok
                self.dcb = data[9:len(data)-2]  # strip off header & crc
            except ValueError as err:
                _LOGGER.debug(f'read dcb exception {err} for {self.name}')
                 # sleep a bit and retry
                # should be OK for Hass, we run in sync worker thread
                time.sleep(0.5)
                continue
            else:
                break  #  no exception, skips else block below
        else:
            # Exhausted retries
            self.hardcount [0] +=1
            _LOGGER.error(f'Error - Read dcb - too many retries - {self.name}')
        
        sema.release()


# Now methods to get thermostat attributes by extracting values from dcb
# No point in logging as the calling method in Climate.py logs the values

    def get_frost_temp(self):
        return self.dcb[17]

    def get_target_temp(self):
        return self.dcb[18]

    def get_thermostat_id(self):
        return self.address

    def get_temperature_format(self):
        return self.dcb[5]

    def get_sensor_selection(self):
        return self.dcb[13]

    def get_program_mode(self):
        return self.dcb[16]

    def get_current_temp(self):
        # Climate entity only has 1 current temperature variable
        # but the stat has a floor and remote or builtin air sensor
        # this method returns the air sensor (builtin or remote) if present, otherwise floor sensor

        senselect = self.dcb[13]
        if senselect in [0, 3]:    # Built In sensor
            index = 32
        elif senselect in [1, 4]:  # remote  air sensor
            index = 28
        else:
            index = 30    # assume floor sensor

        value = (self.dcb[index] * 256 +
                 self.dcb[index + 1])/10
        return value

    def get_run_mode(self):
        return self.dcb[23]  # 1 = frost protect, o = normal (heating)

    def get_heat_state(self):
        return self.dcb[35]  # 1 = heating, o = not

    # extra state attributes

    def get_vendor_id(self):
        return self.dcb [2]

    def get_version (self) :
        return self.dcb [3] & 0x7f

    def get_floor_limit_state (self) :
        return self.dcb [3] & 0x80

    def get_model (self) :
        return self.dcb [4]

    def get_sw_diff (self) :
        return self.dcb [6]

    def get_cal_offset (self) :
        return self.dcb[8] * 256 + self.dcb[9]

    def get_output_delay (self) :
        return self.dcb[10]

    def get_address (self) :
        return self.dcb[11]

    def get_updown_limit (self) :
        return self.dcb[12]

    def get_sensor_select (self) :
        return self.dcb[13]

    def get_opt_start (self) :
        return self.dcb[14]

    def get_rate_of_change (self) :
        return self.dcb[15]

    def get_floor_limit (self) :
        return self.dcb[19]

    def get_floor_limit_enable (self) :
        return self.dcb[20]

    def get_key_lock (self) :
        return self.dcb[22]

    def get_hol_hours (self) :
        return self.dcb[24] * 256 + self.dcb[25]

    def get_temp_hold (self) :
        return self.dcb[26] * 256 + self.dcb[27]

    def get_remote_air_temp (self) :
        return (self.dcb[28] * 256 + self.dcb[29])/10

    def get_floor_temp (self) :
        return (self.dcb[30] * 256 + self.dcb[31])/10

    def get_built_in_temp (self) :
        return (self.dcb[32] * 256 + self.dcb[33])/10

    def get_error_code (self) :
        return self.dcb[34]

    def get_day_and_time (self) :
        _day_of_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f'{_day_of_week[self.dcb[36]-1]} {self.dcb[37]:0>2d}:{self.dcb[38]:0>2d}:{self.dcb[39]:0>2d}' 

    def _comfort_string (self, idx) :
        # returns comfort setting string with 4 entries in the form 
        # hh:mm tt, hh:mm tt, hh:mm tt, hh:mm tt,
        _string = (
            f'{self.dcb[idx]:0>2d}:{self.dcb[idx+1]:0>2d} {self.dcb[idx+2]}; '
            f'{self.dcb[idx+3]:0>2d}:{self.dcb[idx+4]:0>2d} {self.dcb[idx+5]}; '
            f'{self.dcb[idx+6]:0>2d}:{self.dcb[idx+7]:0>2d} {self.dcb[idx+8]}; '
            f'{self.dcb[idx+9]:0>2d}:{self.dcb[idx+10]:0>2d} {self.dcb[idx+11]};'
        )
        return _string

    def get_weekday_settings (self) :
        return self._comfort_string (40)

    def get_weekend_settings (self) :
        return self._comfort_string (52)
        
    def get_day_settings (self, dayno) :
        if self.dcb[16] == 0 :   # 5/2 mode
            return '00:00 00; 00:00 00; 00:00 00; 00:00 00;'
        else :
            return self._comfort_string (dayno*12 + 52)

    def get_read_statistics (self) :
        # return read stats - over time large number of reads, so show error %
        # returns string "Err% CRC NDR Oth Hrd" for read transactions
        _total = self.crccount[0] + self.ndrcount[0] + self.othcount[0]
        _count = self.rwcount[0]
        _err_rate = 0 if _count == 0 else _total/_count
        return f'{_err_rate:.3%} {self.crccount[0]} {self.ndrcount[0]} {self.othcount[0]} {self.hardcount[0]}'
        
    def get_write_statistics (self) :
        # far fewer writes, so return overall count
        # returns string "Count CRC NDR Oth Hrd" for write transactions
        return f'{self.rwcount[1]} {self.crccount[1]} {self.ndrcount[1]} {self.othcount[1]} {self.hardcount[1]}'

    # Now the set methods
    # in future there will be more of these to set time, comfort levels, modes etc
    
    def set_target_temp(self, temp):
        if 35 >= temp >= 5:
            self._write_stat(self.address, 18, temp)

    def set_frost_temp(self, temp):
        if 7 <= temp <= 17:
            self._write_stat(self.address, 17, temp)

    def set_run_mode(self, state):
        if state == 0 or state == 1: # 0 =normal, 1 =frost protect
            self._write_stat(self.address, 23, state)

    
