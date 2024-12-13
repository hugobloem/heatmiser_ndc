"""
  Home Assistant component to access PRT Heatmiser themostats using the V3 protocol
  via the heatmiser library
"""

# Dec 2020 NDC version
# In this code, we will not access the dcb directly (as other versions have done)
# We let the library decode and access the dcb fields

import logging
from typing import List

from . import heatmiser
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
    """Set up the heatmiser platform"""
    _LOGGER.info("Setting up platform - Code version 1.2.1")
    statobject = heatmiser.HeatmiserStat

    host = config[CONF_HOST]
    port = str(config.get(CONF_PORT))
    statlist = config[CONF_THERMOSTATS]
    uh1_hub = heatmiser.HM_UH1(host, port)

    # Add all entities - False in call means update is not called before adding
    # because this slows down startup which generates warning message
    # However, entities are added with zero initial values
    # These are soon updated after setup completes
    
    add_entities([HMV3Stat(statobject, stat, uh1_hub)
                  for stat in statlist], False, )

    _LOGGER.info("Platform setup complete")


class HMV3Stat(ClimateEntity):
    """Representation of a HeatmiserV3 thermostat."""

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, therm, device, uh1):
        """Initialize the thermostat."""

        self.therm = therm(device[CONF_ID], "prt", uh1)
        self._name = device[CONF_NAME]
        _LOGGER.info(f'Initialised thermostat {self._name}')
        _LOGGER.debug(f'Init uh1 = {uh1}')

        
    @property
    def extra_state_attributes(self) -> dict:
        _result = {
            "vendor id": self.therm.get_vendor_id(),
            "version": self.therm.get_version(),
            "floor limit state": self.therm.get_floor_limit_state(),
            "model": self.therm.get_model(),
            "sw diff" : self.therm.get_sw_diff(),
            "cal offset" : self.therm.get_cal_offset(),
            "output delay" : self.therm.get_output_delay(),
            "address" : self.therm.get_address(),
            "up/down limit" : self.therm.get_updown_limit(),
            "sensor select" : self.therm.get_sensor_select(),
            "opt start" : self.therm.get_opt_start(),
            "rate of change" : self.therm.get_rate_of_change(),
            "program mode" : self.therm.get_program_mode(),
            "floor limit" : self.therm.get_floor_limit(),
            "floor limit enable" : self.therm.get_floor_limit_enable(),
            "key lock" : self.therm.get_key_lock(),
            "hol hours" : self.therm.get_hol_hours(),
            "temp hold" : self.therm.get_temp_hold(),
            "remote air temp" : self.therm.get_remote_air_temp(),
            "floor temp" : self.therm.get_floor_temp(),
            "built in temp" : self.therm.get_built_in_temp(),
            "error code" : self.therm.get_error_code(),
            "heat state" : self.therm.get_heat_state(),
            "time" : self.therm.get_day_and_time(),
            "weekday" : self.therm.get_weekday_settings(),
            "weekend" : self.therm.get_weekend_settings(),
            "mon" : self.therm.get_day_settings(1),
            "tue" : self.therm.get_day_settings(2),
            "wed" : self.therm.get_day_settings(3),
            "thu" : self.therm.get_day_settings(4),
            "fri" : self.therm.get_day_settings(5),
            "sat" : self.therm.get_day_settings(6),
            "sun" : self.therm.get_day_settings(7),
        } 
        _LOGGER.debug(f'extra state attributes returning {_result}')
        return _result

    @property
    def name(self):
        _LOGGER.debug(f'name returning {self._name}')
        return self._name

    @property
    def unique_id(self):
        _stat = self.therm.get_thermostat_id()
        _id = f"Heatmiser Prt {_stat}"
        _LOGGER.debug(f'unique_id returning {_id}')
        return _id
        
    @property
    def temperature_unit(self):

        _temp_format = self.therm.get_temperature_format()
        value = UnitOfTemperature.CELSIUS if (_temp_format == 0) else UnitOfTemperature.FAHRENHEIT
        _LOGGER.debug(f'temperature unit returning {value}')
        return value

    @property
    def hvac_mode(self) -> str:
        # Returns Hvac mode - Off / Auto / Heat
        # stat has frost protect on/off and heat state on/off
        # we map frost protect to hvac mode off
        _run_mode=self.therm.get_run_mode()
        _heat_state =self.therm.get_heat_state()
        if _run_mode == 1:   #frost protect
            value = HVACMode.OFF
        elif _heat_state == 0:  # not heating
            value = HVACMode.AUTO
        else:
            value = HVACMode.HEAT
        _LOGGER.debug(f'hvac mode returning {value}')
        return value

    def set_hvac_mode(self, hvac_mode):
        # If Off , set stat to frost protect mode
        # If Heat or Auto, set stat to normal
        _LOGGER.debug(f'set hvac mode to {hvac_mode}')
        if hvac_mode == HVACMode.OFF:
            self.therm.set_run_mode(1)
        else:
            self.therm.set_run_mode(0)

    def turn_off(self):
        """Turn off the stat"""
        _LOGGER.debug(f'turn off called')
        self.set_hvac_mode(HVACMode.OFF)

    def turn_on(self):
        """Turn on the zone"""
        _LOGGER.debug(f'turn on called')
        self.set_hvac_mode(HVACMode.AUTO)

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        _LOGGER.debug(f'target temp step returning {PRECISION_WHOLE}')
        return PRECISION_WHOLE

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        _LOGGER.debug(f'min temp returning 5')
        return 5

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        _LOGGER.debug(f'max temp returning 35')
        return 35

    @property
    def hvac_modes(self) -> List[str]:
        """Return the list of available hvac operation modes"""
        # Need to be a subset of HVAC_MODES.
        
        result = self._attr_hvac_modes
        _LOGGER.debug(f'hvac modes returning {result}')
        return result

    @property
    def current_temperature(self):
        """Return the current temperature depending on sensor select"""
        temp = self.therm.get_current_temp()
        _LOGGER.debug(f'Current temperature returned {temp}')
        return (temp)

    @property
    def target_temperature(self):
        temp = self.therm.get_target_temp()
        _LOGGER.debug(f'Target temp returned {temp}')
        return temp

    @property
    def min_humidity(self):
        """Return the minimum humidity."""
        _LOGGER.debug(f'min humidity returning 7')
        return 7

    @property
    def max_humidity(self):
        """Return the maximum humidity."""
        _LOGGER.debug(f'max humidity returning 17')
        return 17

    @property
    def current_humidity(self):
        """Return the current humidity."""
        # same as target humidity
        temp = self.therm.get_frost_temp()
        _LOGGER.debug(f'Current humidity returned {temp}')
        return temp

    @property
    def target_humidity(self):
        """Return the target humidity """
        temp = self.therm.get_frost_temp()
        _LOGGER.debug(f'Target humidity returned {temp}')
        return temp

    def set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        _LOGGER.debug(f'Set target temp: {temperature}')

        try:
            self._target_temperature = int(temperature)
            self.therm.set_target_temp(self._target_temperature)
        except ValueError as err:
            _LOGGER.error(
                f'Error - Set Temperature exception {err} for {self._name}')

    def set_humidity(self, humidity):
        """Set new target humidity."""
        _hum = int(humidity) 
        _LOGGER.debug(f'set humidity to {_hum}')
        self.therm.set_frost_temp(_hum)
        
    def update(self):
        """Get the latest data."""
        _LOGGER.debug(f'Update started for {self._name}')

        try:
            self.therm.read_dcb()
        except ValueError as err:
            _LOGGER.error(f'Error - Update exception {err} for {self._name}')
        _LOGGER.debug(f'Update done')
