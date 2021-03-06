#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
import copy
import re
from datetime import datetime
from threading import Timer
import reverse_geocode
from timezonefinder import TimezoneFinder
from pytz import timezone
from tzlocal import get_localzone
from cleep.core import CleepModule
from cleep.exception import CommandError, InvalidParameter, MissingParameter
from cleep.libs.configs.hostname import Hostname
from cleep.libs.internals.sun import Sun
from cleep.libs.internals.console import Console
from cleep.libs.internals.task import Task

__all__ = ['Parameters']

class Parameters(CleepModule):
    """
    Parameters application.

    Allow to configure Cleep parameters:

        * system time(current time, sunset, sunrise) according to position
        * system locale

    Useful doc:

        * debian timezone: https://wiki.debian.org/TimeZoneChanges
        * python datetime handling: https://hackernoon.com/avoid-a-bad-date-and-have-a-good-time-423792186f30
    """
    MODULE_AUTHOR = 'Cleep'
    MODULE_VERSION = '2.0.1'
    MODULE_CATEGORY = 'APPLICATION'
    MODULE_PRICE = 0
    MODULE_DEPS = []
    MODULE_DESCRIPTION = 'Configure generic parameters of your device'
    MODULE_LONGDESCRIPTION = 'Application that helps you to configure generic parameters of your device'
    MODULE_TAGS = ['configuration', 'date', 'time', 'locale', 'lang']
    MODULE_COUNTRY = None
    MODULE_URLINFO = 'https://github.com/tangb/cleepmod-parameters'
    MODULE_URLHELP = None
    MODULE_URLBUGS = 'https://github.com/tangb/cleepmod-parameters/issues'
    MODULE_URLSITE = None

    MODULE_CONFIG_FILE = 'parameters.conf'
    # default position to raspberry pi foundation
    DEFAULT_CONFIG = {
        'position': {
            'latitude': 52.2040,
            'longitude': 0.1208
        },
        'country': {
            'country': 'United Kingdom',
            'alpha2': 'GB'
        },
        'timezone': 'Europe/London',
        'timestamp': 0
    }

    SYSTEM_ZONEINFO_DIR = '/usr/share/zoneinfo/'
    SYSTEM_LOCALTIME = '/etc/localtime'
    SYSTEM_TIMEZONE = '/etc/timezone'
    NTP_SYNC_INTERVAL = 60

    def __init__(self, bootstrap, debug_enabled):
        """
        Constructor

        Args:
            bootstrap (dict): bootstrap objects
            debug_enabled (bool): flag to set debug level to logger
        """
        # init
        CleepModule.__init__(self, bootstrap, debug_enabled)

        # members
        self.hostname = Hostname(self.cleep_filesystem)
        self.sun = Sun()
        self.sunset = None
        self.sunrise = None
        self.suns = {
            'sunset': 0,
            'sunset_iso': '',
            'sunrise': 0,
            'sunrise_iso': ''
        }
        self.timezonefinder = TimezoneFinder()
        self.timezone_name = None
        self.timezone = None
        self.time_task = None
        self.sync_time_task = None
        self.__clock_uuid = None
        # code from https://stackoverflow.com/a/106223
        self.__hostname_pattern = r'^(([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*([A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9\-]*[A-Za-z0-9])$'

        # events
        self.time_now_event = self._get_event('parameters.time.now')
        self.time_sunrise_event = self._get_event('parameters.time.sunrise')
        self.time_sunset_event = self._get_event('parameters.time.sunset')
        self.hostname_update_event = self._get_event('parameters.hostname.update')
        self.country_update_event = self._get_event('parameters.country.update')

    def _configure(self):
        """
        Configure module
        """
        # add clock device if not already added
        if self._get_device_count() < 1:
            self.logger.debug('Add default devices')
            clock = {
                'type': 'clock',
                'name': 'Clock'
            }
            self._add_device(clock)

        # prepare country
        country = self._get_config_field('country')
        if not country:
            self.set_country()

        # prepare timezone
        timezone_name = self._get_config_field('timezone')
        if timezone_name:
            self.timezone = timezone(timezone_name)
        else:
            self.logger.info('No timezone defined, use default one. It will be updated when user sets its position.')
            self.timezone = get_localzone().zone

        # compute sun times
        self.set_sun()

        # store device uuids for events
        devices = self.get_module_devices()
        for uuid in devices:
            if devices[uuid]['type'] == 'clock':
                self.__clock_uuid = uuid

    def _on_start(self):
        """
        Module starts
        """
        # restore last saved timestamp if system time seems very old (NTP error)
        saved_timestamp = self._get_config_field('timestamp')
        if (int(time.time()) - saved_timestamp) < 0:
            # it seems NTP sync failed, launch timer to regularly try to sync device time
            self.logger.info(
                'Device time seems to be invalid (%s), launch synchronization time task',
                datetime.now().strftime("%Y-%m-%d %H:%M")
            )
            self.sync_time_task = Task(Parameters.NTP_SYNC_INTERVAL, self._sync_time_task, self.logger)
            self.sync_time_task.start()

        # launch time task (synced to current seconds)
        seconds = 60 - (int(time.time()) % 60)
        self.time_task = Task(60.0, self._time_task, self.logger)
        timer = Timer(0 if seconds == 60 else seconds, self.time_task.start)
        timer.start()

    def _on_stop(self):
        """
        Module stops
        """
        if self.time_task:
            self.time_task.stop()

    def get_module_config(self):
        """
        Get full module configuration

        Returns:
            dict: module configuration
        """
        config = {}

        config['hostname'] = self.get_hostname()
        config['position'] = self.get_position()
        config['sun'] = self.get_sun()
        config['country'] = self.get_country()
        config['timezone'] = self.get_timezone()

        return config

    def get_module_devices(self):
        """
        Return clock as parameters device

        Returns:
            dict: module devices
        """
        devices = super(Parameters, self).get_module_devices()

        for uuid in devices:
            if devices[uuid]['type'] == 'clock':
                data = self.__format_time()
                data.update({
                    'sunrise': self.suns['sunrise'],
                    'sunset': self.suns['sunset']
                })
                devices[uuid].update(data)

        return devices

    def __format_time(self, now=None):
        """
        Return time with different splitted infos

        Args:
            now (int): timestamp to use. If None current timestamp if used

        Returns:
            dict: time data::

                {
                    timestamp (int): current timestamp
                    iso (string): current datetime in iso 8601 format
                    year (int)
                    month (int)
                    day (int)
                    hour (int)
                    minute (int)
                    weekday (int): 0=monday, 1=tuesday... 6=sunday
                    weekday_literal (string): english literal weekday value (monday, tuesday, ...)
                }

        """
        # current time
        if not now:
            now = int(time.time())
        current_dt = datetime.fromtimestamp(now)
        current_dt = self.timezone.localize(current_dt)
        weekday = current_dt.weekday()
        if weekday == 0:
            weekday_literal = 'monday'
        elif weekday == 1:
            weekday_literal = 'tuesday'
        elif weekday == 2:
            weekday_literal = 'wednesday'
        elif weekday == 3:
            weekday_literal = 'thursday'
        elif weekday == 4:
            weekday_literal = 'friday'
        elif weekday == 5:
            weekday_literal = 'saturday'
        elif weekday == 6:
            weekday_literal = 'sunday'

        return {
            'timestamp': now,
            'iso': current_dt.isoformat(),
            'year': current_dt.year,
            'month': current_dt.month,
            'day': current_dt.day,
            'hour': current_dt.hour,
            'minute': current_dt.minute,
            'weekday': weekday,
            'weekday_literal': weekday_literal
        }

    def _sync_time_task(self):
        """
        Sync time task. It is used to try to sync device time using NTP server.

        Note:
            This task is launched only if device time is insane.
        """
        if self.sync_time():
            self.logger.info('Time synchronized with NTP server (%s)' % datetime.now().strftime("%Y-%m-%d %H:%M"))
            self.sync_time_task.stop()
            self.sync_time_task = None

    def _time_task(self):
        """
        Time task used to refresh time
        """
        now_formatted = self.__format_time()

        # send now event
        now_event_params = copy.deepcopy(now_formatted)
        now_event_params.update({
            'sunrise': self.suns['sunrise'],
            'sunset': self.suns['sunset']
        })
        self.time_now_event.send(params=now_event_params, device_id=self.__clock_uuid)

        # send sunrise event
        if self.sunrise:
            if now_formatted['hour'] == self.sunrise.hour and now_formatted['minute'] == self.sunrise.minute:
                self.time_sunrise_event.send(device_id=self.__clock_uuid)

        # send sunset event
        if self.sunset:
            if now_formatted['hour'] == self.sunset.hour and now_formatted['minute'] == self.sunset.minute:
                self.time_sunset_event.send(device_id=self.__clock_uuid)

        # update sun times after midnight
        if now_formatted['hour'] == 0 and now_formatted['minute'] == 5:
            self.set_sun()

        # save last timestamp in config to restore it after a reboot and NTP sync failed (no internet)
        if not self.sync_time_task:
            self._set_config_field('timestamp', now_formatted['timestamp'])

    def set_hostname(self, hostname):
        """
        Set raspi hostname

        Args:
            hostname (string): hostname

        Returns:
            bool: True if hostname saved successfully, False otherwise

        Raises:
            InvalidParameter: if hostname has invalid format
        """
        # check hostname
        if re.match(self.__hostname_pattern, hostname) is None:
            raise InvalidParameter('Hostname is not valid')

        # update hostname
        res = self.hostname.set_hostname(hostname)

        # send event to update hostname on all devices
        if res:
            self.hostname_update_event.send(params={'hostname': hostname})

        return res

    def get_hostname(self):
        """
        Return raspi hostname

        Returns:
            string: raspberry pi hostname
        """
        return self.hostname.get_hostname()

    def set_position(self, latitude, longitude):
        """
        Set device position

        Args:
            latitude (float): latitude
            longitude (float): longitude

        Raises:
            CommandError: if error occured during position saving
        """
        if latitude is None:
            raise MissingParameter('Parameter "latitude" is missing')
        if not isinstance(latitude, float):
            raise InvalidParameter('Parameter "latitude" is invalid')
        if longitude is None:
            raise MissingParameter('Parameter "longitude" is missing')
        if not isinstance(longitude, float):
            raise InvalidParameter('Parameter "longitude" is invalid')

        # save new position
        position = {
            'latitude': latitude,
            'longitude': longitude
        }

        if not self._set_config_field('position', position):
            raise CommandError('Unable to save position')

        # reset python time to take into account last modifications before
        # computing new times
        time.tzset()

        # and update related stuff
        self.set_timezone()
        self.set_country()
        self.set_sun()

        # send now event
        self._time_task()

    def get_position(self):
        """
        Return device position

        Returns:
            dict: position coordinates::

                {
                    latitude (float),
                    longitude (float)
                }

        """
        return self._get_config_field('position')

    def get_sun(self):
        """
        Compute sun times

        Returns:
            dict: sunset/sunrise timestamps::

                {
                    sunrise (int),
                    sunset (int)
                }

        """
        return self.suns

    def set_sun(self):
        """"
        Compute sun times (sunrise and sunset) according to configured position
        """
        # get position
        position = self._get_config_field('position')

        # compute sun times
        self.sunset = None
        self.sunrise = None
        if position['latitude'] != 0 and position['longitude'] != 0:
            self.sun.set_position(position['latitude'], position['longitude'])
            self.sunset = self.sun.sunset()
            self.sunrise = self.sun.sunrise()
            self.logger.debug('Found sunrise:%s sunset:%s' % (self.sunrise, self.sunset))

            # save times
            self.suns['sunrise'] = int(self.sunrise.strftime('%s'))
            self.suns['sunrise_iso'] = self.sunrise.isoformat()
            self.suns['sunset'] = int(self.sunset.strftime('%s'))
            self.suns['sunset_iso'] = self.sunset.isoformat()

    def set_country(self):
        """
        Compute country (and associated alpha) from current internal position

        Warning:
            This function can take some time to find country info on slow device like raspi 1st generation (~15secs)
        """
        # get position
        position = self._get_config_field('position')
        if not position['latitude'] and not position['longitude']:
            self.logger.debug('Unable to set country from unspecified position (%s)' % position)
            return

        # get country from position
        country = {
            'country': None,
            'alpha2': None
        }
        try:
            # search country
            coordinates = ((position['latitude'], position['longitude']), )
            # need a tuple
            geo = reverse_geocode.search(coordinates)
            self.logger.debug('Found country infos from position %s: %s' % (position, geo))
            if geo and len(geo) > 0 and 'country_code' in geo[0] and 'country' in geo[0]:
                country['alpha2'] = geo[0]['country_code']
                country['country'] = geo[0]['country']

            # save new country
            if not self._set_config_field('country', country):
                raise CommandError('Unable to save country')

            # send event
            self.country_update_event.send(params=country)

        except CommandError:
            raise

        except Exception:
            self.logger.exception('Unable to find country for position %s:' % position)

    def get_country(self):
        """
        Get country from position

        Returns:
            dict: return country infos::

            {
                country (string): country label
                alpha2 (string): country code
            }

        """
        return self._get_config_field('country')

    def set_timezone(self):
        """
        Set timezone according to coordinates

        Returns:
            bool: True if function succeed, False otherwise

        Raises:
            CommandError: if unable to save timezone
        """
        # get position
        position = self._get_config_field('position')
        if not position['latitude'] and not position['longitude']:
            self.logger.warning('Unable to set timezone from unspecified position (%s)' % position)
            return False

        # compute timezone
        current_timezone = None
        try:
            # try to find timezone at position
            current_timezone = self.timezonefinder.timezone_at(lat=position['latitude'], lng=position['longitude'])
            if current_timezone is None:
                # extend search to closest position
                # TODO increase delta_degree to extend research, careful it use more CPU !
                current_timezone = self.timezonefinder.closest_timezone_at(
                    lat=position['latitude'],
                    lng=position['longitude']
                )
        except ValueError:
            # the coordinates were out of bounds
            self.logger.exception('Coordinates out of bounds')
        except Exception:
            self.logger.exception('Error occured searching timezone at position')
        if not current_timezone:
            self.logger.warning('Unable to set device timezone because it was not found')
            return False

        # save timezone value
        self.logger.debug('Save new timezone: %s' % current_timezone)
        if not self._set_config_field('timezone', current_timezone):
            raise CommandError('Unable to save timezone')

        # configure system timezone
        zoneinfo = os.path.join(self.SYSTEM_ZONEINFO_DIR, current_timezone)
        self.logger.debug('Checking zoneinfo file: %s' % zoneinfo)
        if not os.path.exists(zoneinfo):
            raise CommandError('No system file found for "%s" timezone' % current_timezone)
        self.logger.debug('zoneinfo file "%s" exists' % zoneinfo)
        self.cleep_filesystem.rm(self.SYSTEM_LOCALTIME)

        self.logger.debug('Writing timezone "%s" in "%s"' % (current_timezone, self.SYSTEM_TIMEZONE))
        if not self.cleep_filesystem.write_data(self.SYSTEM_TIMEZONE, '%s' % current_timezone):
            self.logger.error('Unable to write timezone data on "%s". System timezone is not configured!' % self.SYSTEM_TIMEZONE)
            return False

        # launch timezone update in background
        self.logger.debug('Updating system timezone')
        command = Console()
        res = command.command('/usr/sbin/dpkg-reconfigure -f noninteractive tzdata', timeout=15.0)
        self.logger.debug('Timezone update command result: %s' % res)
        if res['returncode'] != 0:
            self.logger.error('Error reconfiguring system timezone: %s' % res['stderr'])
            return False

        # TODO configure all wpa_supplicant.conf country code

        return True

    def get_timezone(self):
        """
        Return timezone

        Returns:
            string: current timezone name
        """
        return self._get_config_field('timezone')

    def sync_time(self):
        """
        Synchronize device time using NTP server

        Note:
            This command may lasts some seconds

        Returns:
            bool: True if NTP sync succeed, False otherwise
        """
        console = Console()
        resp = console.command('/usr/sbin/ntpdate-debian', timeout=60.0)

        return resp['returncode'] == 0

