"""
  Home Assistant component to access PRT Heatmiser themostats
  via the RS485 library
"""

import logging
from typing import List
from datetime import datetime, timezone, timedelta

from . import rs485
import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode
) 

from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_ID,
    CONF_NAME,
    CONF_PORT,
    UnitOfTemperature,
    PRECISION_WHOLE,
)

import homeassistant.helpers.config_validation as cv
_LOGGER = logging.getLogger(__name__)

DOMAIN = "heatmiser_ndc"
CONF_THERMOSTATS = "tstats"
VERSION = "1.7.1"

TSTAT_SCHEMA = vol.Schema(
    {vol.Required(CONF_ID): vol.Range(1, 32),
     vol.Required(CONF_NAME): cv.string, }
)

TSTATS_SCHEMA = vol.All(cv.ensure_list, [TSTAT_SCHEMA])

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT): cv.port,
        vol.Required(CONF_THERMOSTATS, default=[]): TSTATS_SCHEMA,
    }
)

COMPONENT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT): cv.port,
        vol.Required(CONF_THERMOSTATS): TSTATS_SCHEMA,
    }
)
CONFIG_SCHEMA = vol.Schema({DOMAIN: COMPONENT_SCHEMA}, extra=vol.ALLOW_EXTRA)

def setup_platform(hass, config, add_entities, discovery_info=None):

    _LOGGER.info(f'Setting up platform: Domian {DOMAIN} Version {VERSION}')

    host = config[CONF_HOST]
    port = str(config.get(CONF_PORT))
    statlist = config[CONF_THERMOSTATS]

    #Setup the RS485 serial interface
    serial = rs485.HM_RS485(host, port)

    # Add all entities - False in call means update is not called before adding
    # because this slows down startup which generates warning message
    # However, entities are added with zero initial values
    # These are soon updated after setup completes
    
    add_entities([HMV3Stat(stat,serial) for stat in statlist], False, )
    _LOGGER.info("Platform setup complete")


class HMV3Stat(ClimateEntity):
    """Representation of a Heatmiser V3 PRT thermostat."""

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TARGET_HUMIDITY
        | ClimateEntityFeature.PRESET_MODE
    )
   
    def __init__(self, device, rs485_line):
        
        self._statno = device[CONF_ID]
        self._name = device[CONF_NAME]
        self.rs485 = rs485_line
        #Allocate space and initialise dcb to 0. Necessary to avoid crash, if first read from stat fails 
        self.dcb = [0] * 160

        # Maintain statistics for each stat. [0] = read, [1] = write
        self.rw_count =    [0,0]  #read/write count
        self.soft_errors = [0,0]  #CRC, NDR or other errors
        self.hard_errors = [0,0]  #if retries fail

        _LOGGER.info(f'Initialised stat {self._statno} = {self._name}')  

    # local methods to help assemble the extra attributes

    def _get_day_and_time (self) :
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
        
    def _get_day_settings (self, dayno) :
        if self.dcb[16] == 0 :   # 5/2 mode
            return '00:00 00; 00:00 00; 00:00 00; 00:00 00;'
        else :
            return self._comfort_string (dayno*12 + 52)

    def _get_read_statistics (self) :
        # return read stats - over time large number of reads, so show error %
        # returns string "Err% hard" for read transactions
       
        _count = self.rw_count [0]
        _err_rate = 0 if _count == 0 else self.soft_errors[0]/_count
        return f'{_err_rate:.3%}  {self.hard_errors[0]}'
        
    def _get_write_statistics (self) :
        # far fewer writes, so return "Count soft hard"
        return f'{self.rw_count[1]} {self.soft_errors[1]} {self.hard_errors[1]}'

    @property
    def extra_state_attributes(self) -> dict:
        _result = {
            "vendor id"          : self.dcb [2],
            "version"            : self.dcb [3] & 0x7f,
            "floor limit state"  : self.dcb [3] & 0x80,
            "model"              : self.dcb [4],
            "temp format"        : self.dcb [5],
            "sw diff"            : self.dcb [6],
            "cal offset"         : self.dcb[8] * 256 + self.dcb[9],
            "output delay"       : self.dcb[10],
            "address"            : self.dcb[11],
            "up/down limit"      : self.dcb[12],
            "sensor select"      : self.dcb[13],
            "opt start"          : self.dcb[14],
            "rate of change"     : self.dcb[15],
            "program mode"       : self.dcb[16],
            "floor limit"        : self.dcb[19],
            "floor limit enable" : self.dcb[20],
            "key lock"           : self.dcb[22],
            "hol hours"          : self.dcb[24] * 256 + self.dcb[25],
            "temp hold"          : self.dcb[26] * 256 + self.dcb[27],
            "remote air temp"    : (self.dcb[28] * 256 + self.dcb[29])/10,
            "floor temp"         : (self.dcb[30] * 256 + self.dcb[31])/10,
            "built in temp"      : (self.dcb[32] * 256 + self.dcb[33])/10,
            "error code"         : self.dcb[34],
            "heat state"         : self.dcb[35],
            "time"               : self._get_day_and_time(),
            "weekday"            : self._comfort_string (40),
            "weekend"            : self._comfort_string (52),
            "mon"                : self._get_day_settings(1),
            "tue"                : self._get_day_settings(2),
            "wed"                : self._get_day_settings(3),
            "thu"                : self._get_day_settings(4),
            "fri"                : self._get_day_settings(5),
            "sat"                : self._get_day_settings(6),
            "sun"                : self._get_day_settings(7),
            "read stats"         : self._get_read_statistics(),
            "write stats"        : self._get_write_statistics(),
        } 
        _LOGGER.debug(f'extra state attributes returning {_result}')
        return _result

    @property
    def name(self):
        _LOGGER.debug(f'name returning {self._name}')
        return self._name

    @property
    def unique_id(self):
        _id = f"Heatmiser Prt {self._statno}"
        _LOGGER.debug(f'unique_id returning {_id}')
        return _id
        
    @property
    def temperature_unit(self):
        value = UnitOfTemperature.CELSIUS if (self.dcb[5] == 0) else UnitOfTemperature.FAHRENHEIT
        _LOGGER.debug(f'temperature unit returning {value}')
        return value

    @property
    def hvac_mode(self) -> str:
        # Returns Hvac mode - Off / Auto / Heat
        # stat has frost protect on/off and heat state on/off
        # we map frost protect to hvac mode off
        
        if self.dcb[23] == 1:   #run mode = frost protect
            value = HVACMode.OFF
        elif self.dcb[35] == 0:  # heat state not heating
            value = HVACMode.AUTO
        else:
            value = HVACMode.HEAT
        _LOGGER.debug(f'hvac mode returning {value}')
        return value

    @property
    def target_temperature_step(self):
        _LOGGER.debug(f'target temp step returning {PRECISION_WHOLE}')
        return PRECISION_WHOLE

    # TBD - max , min temps should be different if stat is in F not C
    @property
    def min_temp(self):
        _LOGGER.debug(f'min temp returning 5')
        return 5

    @property
    def max_temp(self):
        _LOGGER.debug(f'max temp returning 35')
        return 35

    @property
    def min_humidity(self):
        _LOGGER.debug(f'min humidity returning 7')
        return 7

    @property
    def max_humidity(self):
        _LOGGER.debug(f'max humidity returning 17')
        return 17

    @property
    def hvac_modes(self) -> List[str]:
        result = self._attr_hvac_modes
        _LOGGER.debug(f'hvac modes returning {result}')
        return result

    @property
    def current_temperature(self):
    # Heatmiser stat has a floor and remote or builtin air sensor
    # Return the air sensor (builtin or remote) if present, otherwise floor sensor

        senselect = self.dcb[13]
        if senselect in [0, 3]:    # Built In sensor
            idx = 32
        elif senselect in [1, 4]:  # remote  air sensor
            idx = 28
        else:
            idx = 30    # assume floor sensor

        value = (self.dcb[idx] * 256 + self.dcb[idx + 1])/10
        
        _LOGGER.debug(f'Current temperature returned {value}')
        return (value)

    @property
    def target_temperature(self):
        temp = self.dcb[18]
        _LOGGER.debug(f'Target temp returned {temp}')
        return temp

    @property
    def current_humidity(self):
        # same as target humidity
        temp = self.dcb[17]
        _LOGGER.debug(f'Current humidity returned {temp}')
        return temp

    @property
    def target_humidity(self):
        temp = self.dcb[17]
        _LOGGER.debug(f'Target humidity returned {temp}')
        return temp
    
    @property
    def preset_modes(self):
        return ["Set time","Set UTC","Set time+offset"]

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        return "Set time"

    ######################################################
    # Now methods to write to the stat

    def _write_to_stat (self, index, payload):
        #local method to call write stat and update statistics
        _status, _errors = self.rs485.write_stat(self._statno, index, payload)
        if _status != 0:
            self.hard_errors [1] += 1
        self.rw_count [1] +=1 
        self.soft_errors [1] = self.soft_errors [1] + _errors 

    def set_preset_mode(self, preset_mode):
        _LOGGER.info(f'set preset mode {preset_mode}')
        _dt = 0
        if preset_mode == "Set time":
            _dt = datetime.now()  # regular local time
        elif preset_mode == "Set UTC":
            _dt = datetime.now(timezone.utc)  #UTC time aka GMT
        elif preset_mode == "Set time+offset":
            # special offset time to match clock on elec meter 
            _dt = datetime.now(timezone.utc) + timedelta(minutes=34, seconds =39)

        if _dt != 0:
            #_payload = [_dt.weekday(), _dt.hour, _dt.minute, _dt.second]
            _payload = [23]  # test - set hour to 23
            _LOGGER.info (f'writing time {_payload}')
            #self._write_to_stat (36, _payload)
            self._write_to_stat (37, _payload)

        

    
    def set_hvac_mode(self, hvac_mode):
        # If Off , set stat to frost protect mode
        # If Heat or Auto, set stat to normal
        _LOGGER.debug(f'set hvac mode to {hvac_mode}')
        _run_mode = 1 if hvac_mode == HVACMode.OFF else 0
        self._write_to_stat (23, [_run_mode])
       
    def turn_off(self):
        _LOGGER.debug(f'turn off called')
        self.set_hvac_mode(HVACMode.OFF)
        
    def turn_on(self):
        _LOGGER.debug(f'turn on called')
        self.set_hvac_mode(HVACMode.AUTO)

    def set_temperature(self, **kwargs):
        temp = kwargs.get(ATTR_TEMPERATURE)
        _LOGGER.debug(f'Set target temp: {temp}')
        temp = int(temp)
        if 35 >= temp >= 5:
            self._write_to_stat(18, [temp])

    def set_humidity(self, humidity):
        _hum = int(humidity) 
        _LOGGER.debug(f'set humidity to {_hum}')
        if 7 <= _hum <= 17:
            self._write_to_stat(17, [_hum])
        
    # Now method to refresh the whole dcb from the stat

    def update(self):
        _LOGGER.debug(f'Update started for {self._name}')
        
        _status,data,_errors = self.rs485.read_stat(self._statno)
        if _status == 0:
            self.dcb = data
        else:
            self.hard_errors[0] += 1
        
        self.rw_count[0] +=1 
        self.soft_errors[0] = self.soft_errors[0] + _errors 
        _LOGGER.debug(f'Update done for {self._name}')
