# Copyright (C) 2022 Giacomo Furlan (elegos) — original work
# Copyright (C) 2026 loteran — modifications
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import copy
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from time import sleep

from dbus_next.aio.message_bus import MessageBus
from dbus_next.constants import MessageType
from dbus_next.message import Message
from PySide6.QtCore import QObject, Signal, SignalInstance

from arctis_sound_manager.constants import (DBUS_BUS_NAME,
                                            DBUS_CONFIG_INTERFACE_NAME,
                                            DBUS_CONFIG_OBJECT_PATH,
                                            DBUS_SETTINGS_INTERFACE_NAME,
                                            DBUS_SETTINGS_OBJECT_PATH,
                                            DBUS_STATUS_INTERFACE_NAME,
                                            DBUS_STATUS_OBJECT_PATH)


class DbusWrapper(QObject):
    sig_status = Signal(object)
    sig_settings = Signal(object)
    # Emitted with True the first time a request succeeds, and with False when
    # a streak of consecutive failures suggests the daemon is no longer
    # responding. Lets the UI surface a banner instead of just stalling.
    sig_daemon_alive = Signal(bool)

    logger = logging.getLogger('DbusWrapper')
    _executor = ThreadPoolExecutor(max_workers=4)
    _CONNECT_TIMEOUT_SECONDS = 5.0
    _CALL_TIMEOUT_SECONDS = 5.0
    _DEAD_AFTER_FAILURES = 3

    # ── Setting changes: ordered, and not undone by a stale poll ──────────────
    #
    # The GUI polls GetSettings about once a second and every reply redraws the pages. A reply
    # that was read just before a SetSetting landed still carries the *old* value, so it flipped
    # the control the user had just set back for a moment ("it does not listen and turns itself
    # on"). Worse, every change used to go out as its own call on one of four pool threads, so a
    # slider drag (one valueChanged per step) reached the daemon in no particular order and the
    # *last* value to arrive was not necessarily the last one the user chose.
    #
    # So, per setting name: the newest value waits in a one-slot queue and a single sender at a
    # time drains it (calls for one name can never overtake each other, and a drag collapses to
    # the few values the daemon can keep up with); and the value the user chose is held over the
    # poll results until the daemon reports it back, the daemon refuses it, or the hold runs out.
    _SETTING_HOLD_SECONDS = 6.0
    _SETTINGS_SECTIONS = ('general', 'dac', 'device')
    _setting_lock = threading.Lock()
    _setting_held: dict = {}       # name -> (value, expires_at on time.monotonic())
    _setting_queued: dict = {}     # name -> newest value that has not been sent yet
    _setting_senders: set = set()  # names that have a sender running

    # The two polling loops (GetStatus, GetSettings) run for as long as the window is visible and each holds a pool
    # thread. They get a pool of their own: commands (setting changes, option lists) wait in line behind a loop that
    # never ends when they share one, and the pool has four threads.
    _poll_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='asm-poll')

    def __init__(self, parent: QObject|None = None):
        super().__init__(parent)
        self._consecutive_failures = 0
        self._last_alive_state: bool | None = None
        # One number per loop kind, raised on every start and every stop. A loop belongs to the number it was
        # started with and ends when that is no longer the current one — so a window that is hidden and shown again
        # within a second can never leave two loops of the same kind running (the old flag was cleared by the new
        # start before the old loop looked at it).
        self._poll_generation = {'status': 0, 'settings': 0}
        self._poll_lock = threading.Lock()

    def _record_success(self):
        self._consecutive_failures = 0
        if self._last_alive_state is not True:
            self._last_alive_state = True
            self.sig_daemon_alive.emit(True)

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._DEAD_AFTER_FAILURES and self._last_alive_state is not False:
            self._last_alive_state = False
            self.sig_daemon_alive.emit(False)

    def stop(self):
        self.logger.info("Stopping D-Bus wrapper...")
        self._stopping = True
        with self._poll_lock:
            for kind in self._poll_generation:
                self._poll_generation[kind] += 1

    def _begin_poll(self, kind: str):
        """Start a new loop of *kind*: ends every older one and returns the test the new loop asks each round."""
        if hasattr(self, '_stopping'):
            del self._stopping
        with self._poll_lock:
            self._poll_generation[kind] += 1
            mine = self._poll_generation[kind]
        return lambda: self._poll_generation[kind] == mine and not hasattr(self, '_stopping')

    def request_status(self, one_time = False, frequency_seconds: int = 1) -> None:
        if one_time:                                           # one answer; a running loop is not disturbed
            self._executor.submit(self.request_status_thread, frequency_seconds=0, keep_going=lambda: True)
            return
        self._poll_executor.submit(self.request_status_thread, frequency_seconds=frequency_seconds,
                                   keep_going=self._begin_poll('status'))

    def request_settings(self, one_time = False, frequency_seconds: int = 1) -> None:
        if one_time:
            self._executor.submit(self.request_settings_thread, frequency_seconds=0, keep_going=lambda: True)
            return
        self._poll_executor.submit(self.request_settings_thread, frequency_seconds=frequency_seconds,
                                   keep_going=self._begin_poll('settings'))

    @staticmethod
    def request_list_options(list_name: str, qt_signal: SignalInstance):
        DbusWrapper._executor.submit(DbusWrapper.request_list_options_thread, list_name=list_name, qt_signal=qt_signal)

    @staticmethod
    def request_list_options_thread(list_name: str, qt_signal: SignalInstance):
        asyncio.run(DbusWrapper.request_list_options_async(list_name, qt_signal))

    @staticmethod
    async def request_list_options_async(list_name: str, qt_signal: SignalInstance):
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            reply = await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='GetListOptions',
                message_type=MessageType.METHOD_CALL,
                signature='s',
                body=[list_name],
            ))

            if reply is None:
                DbusWrapper.logger.error('Error getting settings: no reply')

            elif reply.message_type == MessageType.ERROR:
                DbusWrapper.logger.error('Error getting settings: %s', reply.body)

            else:
                obj = {'name': list_name, 'list': json.loads(reply.body[0]) or []}
                qt_signal.emit(obj)
        except Exception as e:
            DbusWrapper.logger.error('Error in request_list_options: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def send_eq_command(bands: list[int]) -> None:
        DbusWrapper._executor.submit(DbusWrapper.send_eq_command_thread, bands=bands)

    @staticmethod
    def send_eq_command_thread(bands: list[int]):
        asyncio.run(DbusWrapper.send_eq_command_async(bands))

    @staticmethod
    async def send_eq_command_async(bands: list[int]):
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='SendEqCommand',
                message_type=MessageType.METHOD_CALL,
                signature='s',
                body=[json.dumps(bands)],
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in send_eq_command: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    # One equaliser read at a time. The Equalizer page asks every couple of seconds while it is open, and each
    # ask used to be a job of its own on the shared pool (a new event loop and a new bus connection each): with
    # the daemon slow or gone they piled up faster than they finished, and they held the pool threads that carry
    # setting changes. Now a request that arrives while one is running just joins it.
    _eq_lock = threading.Lock()
    _eq_waiters: list = []
    _eq_running = False
    _EQ_MAX_WAITERS = 16

    @staticmethod
    def get_eq_bands(qt_signal: SignalInstance) -> None:
        with DbusWrapper._eq_lock:
            DbusWrapper._eq_waiters.append(qt_signal)
            del DbusWrapper._eq_waiters[:-DbusWrapper._EQ_MAX_WAITERS]
            if DbusWrapper._eq_running:
                return
            DbusWrapper._eq_running = True
        try:
            DbusWrapper._executor.submit(DbusWrapper.get_eq_bands_thread)
        except Exception:
            with DbusWrapper._eq_lock:
                DbusWrapper._eq_running = False
            raise

    @staticmethod
    def get_eq_bands_thread(qt_signal: SignalInstance | None = None):
        if qt_signal is not None:                       # the old call shape: one reader, answered directly
            asyncio.run(DbusWrapper.get_eq_bands_async(qt_signal))
            return
        try:
            while True:
                with DbusWrapper._eq_lock:
                    waiters, DbusWrapper._eq_waiters = DbusWrapper._eq_waiters, []
                if not waiters:
                    return
                bands = asyncio.run(DbusWrapper._read_eq_bands())
                if bands is not None:
                    for signal in waiters:
                        try:
                            signal.emit(bands)
                        except RuntimeError:            # its page is gone
                            pass
        finally:
            with DbusWrapper._eq_lock:
                DbusWrapper._eq_running = False

    @staticmethod
    async def _read_eq_bands():
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            reply = await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='GetEqBands',
                message_type=MessageType.METHOD_CALL,
            ))
            if reply and reply.message_type == MessageType.METHOD_RETURN:
                return json.loads(reply.body[0])
        except Exception as e:
            DbusWrapper._log_eq_failure(e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()
        return None

    _eq_last_failure = 0.0

    @staticmethod
    def _log_eq_failure(exc: Exception) -> None:
        """At most one line a minute: with the daemon away this fails on every ask."""
        now = time.monotonic()
        if now - DbusWrapper._eq_last_failure >= 60.0:
            DbusWrapper._eq_last_failure = now
            DbusWrapper.logger.error('Error in get_eq_bands: %s', exc)

    @staticmethod
    async def get_eq_bands_async(qt_signal: SignalInstance):
        bands = await DbusWrapper._read_eq_bands()
        if bands is not None:
            qt_signal.emit(bands)

    @staticmethod
    def show_splash() -> None:
        DbusWrapper._executor.submit(DbusWrapper.show_splash_thread)

    @staticmethod
    def show_splash_thread():
        asyncio.run(DbusWrapper.show_splash_async())

    @staticmethod
    async def show_splash_async():
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='ShowSplash',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.debug('ShowSplash: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def change_setting(name: str, value: int|bool|str) -> None:
        with DbusWrapper._setting_lock:
            DbusWrapper._setting_held[name] = (
                copy.deepcopy(value), time.monotonic() + DbusWrapper._SETTING_HOLD_SECONDS)
            DbusWrapper._setting_queued[name] = value
            if name in DbusWrapper._setting_senders:
                return   # the sender already running for this name will pick the newest value up
            DbusWrapper._setting_senders.add(name)
        DbusWrapper._executor.submit(DbusWrapper._send_setting_thread, name)

    @staticmethod
    def _send_setting_thread(name: str) -> None:
        """Send the queued values of one setting, one call at a time, until none is left."""
        try:
            while True:
                with DbusWrapper._setting_lock:
                    if name not in DbusWrapper._setting_queued:
                        DbusWrapper._setting_senders.discard(name)
                        return
                    value = DbusWrapper._setting_queued.pop(name)
                accepted = False
                try:
                    accepted = asyncio.run(DbusWrapper.change_setting_async(name, value))
                except Exception as e:  # noqa: BLE001 - a failed send must not wedge the sender
                    DbusWrapper.logger.error('Error in change_setting %s: %s', name, e)
                with DbusWrapper._setting_lock:
                    held = DbusWrapper._setting_held.get(name)
                    if held is not None and held[0] == value:
                        if accepted:
                            # polls that were already in flight can still carry the old value:
                            # keep holding for a while after the daemon said yes
                            DbusWrapper._setting_held[name] = (
                                held[0], time.monotonic() + DbusWrapper._SETTING_HOLD_SECONDS)
                        elif name not in DbusWrapper._setting_queued:
                            del DbusWrapper._setting_held[name]   # refused: show what the daemon really has
        except BaseException:
            with DbusWrapper._setting_lock:
                DbusWrapper._setting_senders.discard(name)
            raise

    @staticmethod
    def _overlay_held_settings(settings: dict) -> dict:
        """Put the values the user has just chosen over a GetSettings reply (see above)."""
        now = time.monotonic()
        with DbusWrapper._setting_lock:
            for name, (value, expires_at) in list(DbusWrapper._setting_held.items()):
                if expires_at <= now:
                    del DbusWrapper._setting_held[name]
                    continue
                sections = [section for section in (settings.get(key) for key in DbusWrapper._SETTINGS_SECTIONS)
                            if isinstance(section, dict) and name in section]
                if all(section[name] == value for section in sections):
                    # the daemon has caught up; nothing left to hold unless another change is on its way
                    if sections and name not in DbusWrapper._setting_senders:
                        del DbusWrapper._setting_held[name]
                    continue
                for section in sections:
                    section[name] = copy.deepcopy(value)
        return settings

    @staticmethod
    def change_setting_thread(name: str, value: int|bool|str):
        asyncio.run(DbusWrapper.change_setting_async(name, value))

    @staticmethod
    async def change_setting_async(name: str, value: int|bool|str) -> bool:
        """Call SetSetting. True only when the daemon accepted the value."""
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            reply = await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='SetSetting',
                message_type=MessageType.METHOD_CALL,
                signature='ss',
                body=[name, json.dumps(value)],
            ))
            if reply is None:
                DbusWrapper.logger.error('SetSetting %s: no reply', name)
                return False
            if reply.message_type == MessageType.ERROR:
                DbusWrapper.logger.error('SetSetting %s=%r failed: %s', name, value, reply.body)
                return False
            if reply.body and reply.body[0] is False:
                DbusWrapper.logger.warning('SetSetting %s=%r: the daemon refused the value', name, value)
                return False
            DbusWrapper.logger.debug('SetSetting %s=%r accepted', name, value)
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in change_setting: %s', e)
            return False
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def set_weather_settings(enabled: bool, location: str, units: str, callback) -> None:
        """Call SetWeatherSettings D-Bus method and invoke callback(result_dict)."""
        DbusWrapper._executor.submit(
            DbusWrapper._set_weather_thread, enabled=enabled, location=location,
            units=units, callback=callback,
        )

    @staticmethod
    def _set_weather_thread(enabled: bool, location: str, units: str, callback) -> None:
        asyncio.run(DbusWrapper._set_weather_async(enabled, location, units, callback))

    @staticmethod
    async def _set_weather_async(enabled: bool, location: str, units: str, callback) -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            reply = await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_SETTINGS_OBJECT_PATH,
                interface=DBUS_SETTINGS_INTERFACE_NAME,
                member='SetWeatherSettings',
                message_type=MessageType.METHOD_CALL,
                signature='bss',
                body=[enabled, location, units],
            ))
            result = json.loads(reply.body[0]) if reply.body else {"ok": False}
            callback(result)
        except Exception as e:
            DbusWrapper.logger.error('Error in set_weather_settings: %s', e)
            callback({"ok": False, "error": str(e)})
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    def request_status_thread(self, frequency_seconds: int, keep_going=None):
        asyncio.run(self.dbus_request_async(
            self.sig_status,
            frequency_seconds,
            DBUS_BUS_NAME,
            DBUS_STATUS_OBJECT_PATH,
            DBUS_STATUS_INTERFACE_NAME,
            'GetStatus',
            keep_going=keep_going,
        ))

    def request_settings_thread(self, frequency_seconds: int, keep_going=None):
        asyncio.run(self.dbus_request_async(
            self.sig_settings,
            frequency_seconds,
            DBUS_BUS_NAME,
            DBUS_SETTINGS_OBJECT_PATH,
            DBUS_SETTINGS_INTERFACE_NAME,
            'GetSettings',
            transform=self._overlay_held_settings,
            keep_going=keep_going,
        ))

    # A poll that fails again and again (the daemon is not running) is repeated ever less often, up to this long.
    _FAILING_POLL_MAX_SECONDS = 10.0

    def _poll_nap(self, seconds: float, keep_going) -> None:
        """Sleep, but wake up as soon as this loop is told to end."""
        end = time.monotonic() + seconds
        while keep_going():
            left = end - time.monotonic()
            if left <= 0:
                return
            sleep(min(0.1, left))

    async def dbus_request_async(self, sig: SignalInstance, freq: int, destination: str, path: str, interface: str,
                                 member: str, transform=None, keep_going=None):
        if keep_going is None:
            keep_going = lambda: not hasattr(self, '_stopping')   # noqa: E731 — the original single-flag behaviour
        failing = 0
        while keep_going():
            dbus_bus = None
            try:
                # connect() can hang indefinitely when the session bus address
                # points to a stale socket (graphical session crashed), so cap
                # both the connect and the actual call.
                dbus_bus = await asyncio.wait_for(
                    MessageBus().connect(), timeout=self._CONNECT_TIMEOUT_SECONDS,
                )
                reply = await asyncio.wait_for(
                    dbus_bus.call(Message(
                        destination=destination,
                        path=path,
                        interface=interface,
                        member=member,
                        message_type=MessageType.METHOD_CALL
                    )),
                    timeout=self._CALL_TIMEOUT_SECONDS,
                )

                if reply is None:
                    self._log_poll_failure(failing, 'Error getting settings: no reply')
                    failing += 1
                    self._record_failure()
                elif reply.message_type == MessageType.ERROR:
                    self._log_poll_failure(failing, 'Error getting settings: %s' % (reply.body,))
                    failing += 1
                    self._record_failure()
                else:
                    payload = json.loads(reply.body[0]) or {}
                    if transform is not None:
                        payload = transform(payload)
                    failing = 0
                    sig.emit(payload)
                    self._record_success()
            except asyncio.TimeoutError:
                self._log_poll_failure(
                    failing, f'D-Bus {member} timed out after {self._CALL_TIMEOUT_SECONDS}s — daemon unresponsive?'
                )
                failing += 1
                self._record_failure()
            except Exception as e:
                self._log_poll_failure(failing, 'Error in dbus_request: %s' % (e,))
                failing += 1
                self._record_failure()
            finally:
                if dbus_bus is not None:
                    try:
                        dbus_bus.disconnect()
                    except Exception:
                        pass

            if freq == 0:
                return

            # While the daemon does not answer, asking once a second only fills the log and burns a connection
            # attempt each time: ask less and less often, and say so once.
            wait = freq if not failing else min(self._FAILING_POLL_MAX_SECONDS, freq * (2 ** min(failing, 4)))
            self._poll_nap(wait, keep_going)

    def _log_poll_failure(self, failing: int, message: str) -> None:
        """The first failure of a streak is reported; the ones that follow are not repeated."""
        if failing == 0:
            self.logger.error('%s (further failures are not logged until the daemon answers again)', message)
        else:
            self.logger.debug(message)

    @staticmethod
    def reload_configs() -> None:
        """Tell the daemon to re-scan USB and re-configure virtual sinks."""
        DbusWrapper._executor.submit(DbusWrapper._reload_configs_thread)

    @staticmethod
    def _reload_configs_thread() -> None:
        asyncio.run(DbusWrapper._reload_configs_async())

    @staticmethod
    async def _reload_configs_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='ReloadConfigs',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in reload_configs: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    async def _apply_channel_outputs_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='ApplyChannelOutputs',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in apply_channel_outputs: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def apply_channel_outputs() -> None:
        """Ask the daemon to re-link the channels to their saved devices.

        The daemon owns routing: only it has the device_state the enforcement
        passes resolve the headset through, so calling them in this process
        would link nothing at all.
        """
        DbusWrapper._executor.submit(
            lambda: asyncio.run(DbusWrapper._apply_channel_outputs_async())
        )

    @staticmethod
    async def _recreate_loopbacks_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='RecreateLoopbacks',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopbacks: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def recreate_loopbacks() -> None:
        """Tell the daemon to recreate the Arctis_* virtual-sink loopbacks
        (fresh pw-loopback processes) so they relink to the freshly recreated
        Sonar EQ nodes. Fire-and-forget, off the UI thread."""
        DbusWrapper._executor.submit(
            lambda: asyncio.run(DbusWrapper._recreate_loopbacks_async())
        )

    @staticmethod
    def recreate_loopbacks_sync() -> bool:
        """Synchronous variant for worker threads that must sequence
        'restart filter-chain → wait for EQ node → recreate loopbacks → restore
        streams'. Blocks until the D-Bus call completes. Returns False on error.
        MUST be called off the Qt UI thread (e.g. from an _ApplyWorker)."""
        try:
            asyncio.run(DbusWrapper._recreate_loopbacks_async())
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopbacks_sync: %s', e)
            return False

    @staticmethod
    async def _recreate_loopbacks_game_media_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='RecreateLoopbacksGameMedia',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopbacks_game_media: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def recreate_loopbacks_game_media() -> None:
        """Recreate only Game and Media loopbacks (Chat is preserved).
        Fire-and-forget, off the UI thread."""
        DbusWrapper._executor.submit(
            lambda: asyncio.run(DbusWrapper._recreate_loopbacks_game_media_async())
        )

    @staticmethod
    def recreate_loopbacks_game_media_sync() -> bool:
        """Synchronous variant: recreate Game+Media only, Chat stays alive.
        Keeps Arctis_Chat in Discord's device list across filter-chain restarts.
        MUST be called off the Qt UI thread."""
        try:
            asyncio.run(DbusWrapper._recreate_loopbacks_game_media_async())
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopbacks_game_media_sync: %s', e)
            return False

    @staticmethod
    async def _recreate_loopback_single_async(channel: str) -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='RecreateLoopbackSingle',
                message_type=MessageType.METHOD_CALL,
                signature='s',
                body=[channel],
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopback_single: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def recreate_loopback_single_sync(channel: str) -> bool:
        """Synchronous variant: recreate only the given channel's loopback.

        Use this when a single EQ channel's preset was applied, so the sibling
        channel's audio stream is not interrupted. MUST be called off the Qt UI
        thread (e.g. from an _ApplyWorker)."""
        try:
            asyncio.run(DbusWrapper._recreate_loopback_single_async(channel))
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in recreate_loopback_single_sync: %s', e)
            return False

    @staticmethod
    async def _reset_filter_chain_safe_mode_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='ResetFilterChainSafeMode',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in reset_filter_chain_safe_mode: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def reset_filter_chain_safe_mode_sync() -> bool:
        """Clear filter-chain safe mode via the daemon so EQ is re-enabled (#88).

        MUST be called off the Qt UI thread (the daemon restarts the
        filter-chain service, which blocks)."""
        try:
            asyncio.run(DbusWrapper._reset_filter_chain_safe_mode_async())
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in reset_filter_chain_safe_mode_sync: %s', e)
            return False

    @staticmethod
    async def _apply_spatial_audio_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='ApplySpatialAudio',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in apply_spatial_audio: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def apply_spatial_audio() -> None:
        """Tell the daemon to regenerate the HeSuVi chains from the saved
        Immersion/Distance and restart the filter-chain if they changed (#169).

        The GUI can't write these confs itself (no device registered in its
        process). Fire-and-forget, off the UI thread; the daemon self-guards so
        a no-op change (e.g. the Spatial Audio toggle, which leaves
        Immersion/Distance untouched) triggers no restart."""
        DbusWrapper._executor.submit(
            lambda: asyncio.run(DbusWrapper._apply_spatial_audio_async())
        )

    @staticmethod
    async def _regenerate_hesuvi_async() -> None:
        dbus_bus = None
        try:
            dbus_bus = await MessageBus().connect()
            await dbus_bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=DBUS_CONFIG_OBJECT_PATH,
                interface=DBUS_CONFIG_INTERFACE_NAME,
                member='RegenerateHesuvi',
                message_type=MessageType.METHOD_CALL,
            ))
        except Exception as e:
            DbusWrapper.logger.error('Error in regenerate_hesuvi: %s', e)
        finally:
            if dbus_bus is not None:
                dbus_bus.disconnect()

    @staticmethod
    def regenerate_hesuvi_sync() -> bool:
        """Ask the daemon to rewrite the HeSuVi conf(s) from saved state —
        picking up e.g. a new Volume Boost — without restarting anything (#237).

        MUST be called off the Qt UI thread. Blocking but restart-free: the
        caller (an Apply-All worker) restarts the filter-chain itself right
        after writing the EQ confs, and that one restart reloads both."""
        try:
            asyncio.run(DbusWrapper._regenerate_hesuvi_async())
            return True
        except Exception as e:
            DbusWrapper.logger.error('Error in regenerate_hesuvi_sync: %s', e)
            return False
