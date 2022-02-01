"""
    Heatmiser library to access Heatmiser thermostats (PRT-N) via an RS485 interface
    Multiple stats may be connected to each UH1 wiring hub
    library designed to be used by Home Assistant and other apps
"""
# NDC Dec 2020
# from previous great work by Neil Trimboy 2011, and others
# Oct 2021 Sort out HVAC modes
# Nov 2021 supprort humidity as proxy for frost protect


import serial
import logging
import asyncio
import serial_asyncio
import time

# Heatmiser read / write codes
FUNC_READ = 0
FUNC_WRITE = 1

# COMM SETTINGS
COM_PORT = 6  # 1 less than com port, USB is 6=com7, ether is 9=10
COM_BAUD = 4800
COM_SIZE = serial.EIGHTBITS
COM_PARITY = serial.PARITY_NONE
COM_STOP = serial.STOPBITS_ONE
COM_TIMEOUT = 0.8 # seconds

_LOGGER = logging.getLogger(__name__)

class HM_UH1:
    """ The Heatmiser UH1 interface that holds the serial
    connection, and can have multiple thermostats
    """

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
        """Registers a thermostat with the UH1"""
        try:
            # check we have a heatmiser stat object
            type(thermostat) == HeatmiserStat
            if thermostat.address in self.thermostats.keys():
                raise ValueError(f'Stat already present {thermostat.address}')
            else:
                self.thermostats[thermostat.address] = thermostat
                _LOGGER.debug(f'Thermostat: {thermostat.address} registered')
        except ValueError:
            pass
        except Exception as err:
            _LOGGER.error(f'Not a Heatmiser Thermostat Object {err}')
        return self._serport


class CRC16:
    """CRC function (aka CCITT) mechanism used by the Heatmiser V3 protocol."""
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
       read all fields from the stat in raw state
       get individual fields eg target temp, frost temp, status, heating,  temp format etc
       set writable stat fields eg target temp, frost protect temp, floor max temp etc
    """
    
    def __init__(self, address, model, uh1):
        _LOGGER.debug(f'HeatmiserStat init addr {address}')
        self.address = address
        #Allocate space and initialise dcb to 0. Necessary to avoid crash, if first read from stat fails 
        self.dcb = [0] * 160

        self.conn = uh1.registerThermostat(self)  # register stat to ser i/f
        _LOGGER.debug(f'Init done. Conn = {self.conn}')

    def _lohibytes(self, value):
        # splits value into 2 bytes, returns lo, hi bytes
        return value & 0xff, (value >> 8) & 0xff

    def _verify(self, stat, exp_func, datal):
        # verifies reply from stat by checking CRC and header fields
        # raises ValueError exception if any fields invalid

        _LOGGER.debug(f'Verifying {stat}')
        length = len(datal)
        if length < 3:
            raise ValueError("No data read")
        checksum = datal[length - 2:]
        rxmsg = datal[:length - 2]
        crc = CRC16()   # Initialises the CRC
        if crc.run(rxmsg) != checksum:
            raise ValueError(f'Bad CRC, length {length}')
        dest = datal[0]
        if (dest != 129 and dest != 160):
            raise ValueError("dest is ILLEGAL")

        source = datal[3]
        if (source < 1 or source > 32 or source != stat):
            raise ValueError(f'source is bad {source}')

        func = datal[4]
        frame_len = datal[2] * 256 + datal[1]
        if func != FUNC_WRITE and func != FUNC_READ:
            raise ValueError("Func Code is UNKNWON")
        if func != exp_func:
            raise ValueError("Func Code is UNEXPECTED")
        if func == FUNC_WRITE and frame_len != 7:
            raise ValueError("Unexpected Write length")
        if length != frame_len:
            raise ValueError("response length MISMATCHES header")

        # message appears OK

    def _send_msg(self, message):
        # Adds CRC, then sends message to serial interface, & reads reply
        # max read length = 75 in 5/2 mode, 159 in 7day mode ? TBD check these
        # This is the only interface to the serial connection.

        _LOGGER.debug(f'Send msg - length: {len(message)}')
        crc = CRC16()
        string = bytes(message + crc.run(message))  # add CRC
        try:
            self.conn.write(string)
        except serial.SerialTimeoutException:
            _LOGGER.error("Serial timeout on write")

        datal = list(self.conn.read(159))
        _LOGGER.debug(f'Reply read, length {len(datal)}')
        _LOGGER.debug(f'Data= {datal}')
        return datal

    def _write_stat(self, stat, address, value):
        # writes a single value to the stat at address in dcb

        _LOGGER.debug(f'write stat no, addr, value = {stat}  {address}  {value}')
        payload = [value]  # makes a list of 1 item
        startlo, starthi = self._lohibytes(address)
        lengthlo, lengthhi = self._lohibytes(len(payload))
        msg = [stat, 10+len(payload), 129, 1,
               startlo, starthi, lengthlo, lengthhi]
        datal = self._send_msg(msg+payload)
        self._verify(stat, 1, datal)
        _LOGGER.debug(f'write stat reply = {datal}')

        # try a little sleep to ensure stat returns proper value on next update
        # this should be OK for rest of Hass, as this runs in sync worker thread
        time.sleep(1.0)

        return datal

    # Methods to get or set thermostat attributes
    # read_dcb used to read all data from stat
    # get methods extract fields from internal stored values
    # set methods write the single field to the stat
    # After calls of set methods, HA itself calls update to update internal dcb

    def read_dcb(self):
        # Reads all data from stat 
        #   sends standard read message to serial i/f
        #   reads reply and verifies
        #   returns data as list after stripping out frame header and checksum
        
        stat = self.address
        _LOGGER.debug(f'read dcb for : {stat}')

        # form standard read command to read all fields from stat
        msg = [stat, 10, 129, 0, 0, 0, 0xff, 0xff]
        datal = self._send_msg(msg)
        self._verify(stat, 0, datal)
        self.dcb = datal[9:len(datal)-2]  # strip off header & crc
        return self.dcb

    def get_frost_temp(self):
        value = self.dcb[17]
        _LOGGER.debug(f'get frost temp {value}')
        return value

    def get_target_temp(self):
        value = self.dcb[18]
        _LOGGER.debug(f'get target temp {value}')
        return value

    def get_thermostat_id(self):
        value = self.dcb[11]
        _LOGGER.debug(f'get thermostat id {value}')
        return value

    def get_temperature_format(self):
        value = self.dcb[5]
        _LOGGER.debug(f'get temp format {value}')
        return value

    def get_sensor_selection(self):
        sensor = self.dcb[13]
        _LOGGER.debug(f'get sensor select ={sensor}')
        return sensor

    def get_program_mode(self):
        mode = self.dcb[16]
        _LOGGER.debug(f'get prog mode {mode}')
        return mode

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

        _LOGGER.debug(f'get current temp {value}')
        return value

    def get_run_mode(self):
        value = self.dcb[23]  # 1 = frost protect, o = normal (heating)
        _LOGGER.debug(f'get run mode {value}')
        return value

    def get_heat_state(self):
        value = self.dcb[35]  # 1 = heating, o = not
        _LOGGER.debug(f'get heat state {value}')
        return value

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

    # Now the set methods
    
    def set_target_temp(self, temp):
        _LOGGER.debug(f'set target temp {temp}')
        if 35 < temp < 5:
            raise ValueError(f'Attempt to set bad target temp {temp}')
        self._write_stat(self.address, 18, temp)
        return True

    def set_frost_temp(self, temp):
        _LOGGER.debug(f'set frost temp {temp}')
        if 17 < temp < 7:
            raise ValueError(f'Attempt to set bad frost temp {temp}')
        self._write_stat(self.address, 17, temp)
        return True

    def set_run_mode(self, state):
        #sets run mode to 0 =normal, or 1 = frost protect
        _LOGGER.debug(f'set run mode {state}')
        if (state != 0 and state != 1):
            raise ValueError(f'Attempt to set bad run mode {state}')
        self._write_stat(self.address, 23, state)
        return True

    
