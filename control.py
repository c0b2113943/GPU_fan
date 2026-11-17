#!/usr/bin/env python3
"""Temperature-based NVIDIA fan control using the installed NVML library."""
import argparse
import ctypes as C
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import threading
import time


class NVError(RuntimeError):
    pass


class NVML:
    def __init__(self):
        self.lib = C.CDLL('libnvidia-ml.so.1')
        self.call('nvmlInit_v2', [])

    def call(self, name, types, *args):
        try:
            fn = getattr(self.lib, name)
        except AttributeError:
            raise NVError(name + ': driver does not provide this API') from None
        fn.argtypes, fn.restype = types, C.c_uint
        result = fn(*args)
        if result:
            error = self.lib.nvmlErrorString
            error.argtypes, error.restype = [C.c_uint], C.c_char_p
            raise NVError(f'{name}: {error(result).decode()} (code {result})')

    def uint(self, name, handle, *args):
        value = C.c_uint()
        self.call(name, [C.c_void_p] + [C.c_uint]*len(args) + [C.POINTER(C.c_uint)],
                  handle, *args, C.byref(value))
        return value.value

    def text(self, name, handle):
        buf = C.create_string_buffer(256)
        self.call(name, [C.c_void_p, C.c_char_p, C.c_uint], handle, buf, len(buf))
        return buf.value.decode()

    def device(self, uuid):
        handle = C.c_void_p()
        self.call('nvmlDeviceGetHandleByUUID', [C.c_char_p, C.POINTER(C.c_void_p)],
                  uuid.encode(), C.byref(handle))
        return handle

    def devices(self):
        count = C.c_uint()
        self.call('nvmlDeviceGetCount_v2', [C.POINTER(C.c_uint)], C.byref(count))
        for index in range(count.value):
            handle = C.c_void_p()
            self.call('nvmlDeviceGetHandleByIndex_v2', [C.c_uint, C.POINTER(C.c_void_p)], index, C.byref(handle))
            yield index, handle

    def bounds(self, handle):
        low, high = C.c_uint(), C.c_uint()
        self.call('nvmlDeviceGetMinMaxFanSpeed', [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_uint)],
                  handle, C.byref(low), C.byref(high))
        return low.value, high.value

    def temperature(self, handle):
        value = self.uint('nvmlDeviceGetTemperature', handle, 0)
        if not 0 <= value <= 125:
            raise NVError('Invalid GPU temperature')
        return value

    def fan_count(self, handle):
        count = self.uint('nvmlDeviceGetNumFans', handle)
        if not 1 <= count <= 16:
            raise NVError('No supported fans found')
        return count

    def set_speed(self, handle, fan, speed):
        self.call('nvmlDeviceSetFanSpeed_v2', [C.c_void_p, C.c_uint, C.c_uint], handle, fan, speed)

    def restore(self, handle, count):
        errors = []
        for fan in range(count):
            try:
                self.call('nvmlDeviceSetDefaultFanSpeed_v2', [C.c_void_p, C.c_uint], handle, fan)
            except NVError as exc:
                errors.append(str(exc))
        if errors:
            raise NVError('Auto restore failed: ' + '; '.join(errors))
        logging.info('Restored default automatic fan control')

    def close(self):
        self.call('nvmlShutdown', [])


def load_config(path):
    config = json.loads(Path(path).read_text())
    points = config['points']
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('At least two temperature/speed points are required')
    previous_temp, previous_speed = -1, -1
    for point in points:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError('Each point must be [temperature, percentage]')
        temp, speed = point
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in point):
            raise ValueError('Curve values must be finite numbers')
        if not previous_temp < temp <= 125 or not previous_speed <= speed <= 100 or temp < 0 or speed < 0:
            raise ValueError('Temperatures must increase; speeds must not decrease (0–100%)')
        previous_temp, previous_speed = temp, speed
    for key, low, high in [('interval_seconds', 1, 10), ('decrease_delay_seconds', 0, 120)]:
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid ' + key)
    return config


def target_speed(points, temperature):
    if temperature <= points[0][0]:
        return math.ceil(points[0][1])
    for (t0, s0), (t1, s1) in zip(points, points[1:]):
        if temperature <= t1:
            return math.ceil(s0 + (temperature-t0)/(t1-t0)*(s1-s0))
    return math.ceil(points[-1][1])


class Curve:
    def __init__(self, points, delay):
        self.points, self.delay = points, delay
        self.current, self.lower_since = None, None

    def update(self, temperature, now):
        target = target_speed(self.points, temperature)
        if self.current is None or target >= self.current:
            self.current, self.lower_since = target, None
        elif self.lower_since is None:
            self.lower_since = now
        elif now-self.lower_since >= self.delay:
            self.current, self.lower_since = target, None
        return self.current


def run_curve(nv, handle, count, config, stop, dry_run=False, duration=0):
    curve = Curve(config['points'], config['decrease_delay_seconds'])
    start, previous = time.monotonic(), None
    manual = False
    try:
        while not stop.is_set():
            now = time.monotonic()
            if duration and now-start >= duration:
                break
            temperature = nv.temperature(handle)
            speed = curve.update(temperature, now)
            if speed != previous:
                logging.info('%sGPU %d°C -> fan %d%%', 'DRY RUN: ' if dry_run else '', temperature, speed)
                if not dry_run:
                    # Include a partially applied multi-fan change in cleanup.
                    manual = True
                    for fan in range(count):
                        nv.set_speed(handle, fan, speed)
                previous = speed
            stop.wait(config['interval_seconds'])
    finally:
        if manual:
            nv.restore(handle, count)


def describe(nv):
    for index, handle in nv.devices():
        print(f"GPU {index}: {nv.text('nvmlDeviceGetName', handle)}")
        print('  UUID:', nv.text('nvmlDeviceGetUUID', handle))
        print('  Temperature:', nv.temperature(handle), '°C')
        try:
            count = nv.fan_count(handle)
            print('  Fans:', count, '/ allowed range:', nv.bounds(handle))
            for fan in range(count):
                try:
                    print(f"  Fan {fan}: {nv.uint('nvmlDeviceGetFanSpeed_v2', handle, fan)}%")
                except NVError as exc:
                    print(' ', exc)
        except NVError as exc:
            print('  Fan capability:', exc)
        print('  Read-only check; write permission/support is not yet verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'run', 'auto'])
    parser.add_argument('--uuid', help='Exact GPU UUID from status (required for run/auto)')
    parser.add_argument('--config', default=str(Path(__file__).with_name('curve.json')))
    parser.add_argument('--dry-run', action='store_true', help='read temperatures, log targets; never change fans')
    parser.add_argument('--duration', type=float, default=0, help='stop after N seconds; 0 means until Ctrl+C')
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 0:
        parser.error('--duration must be finite and >= 0')
    if args.dry_run and args.action != 'run':
        parser.error('--dry-run is supported only with run')
    if args.action != 'status' and (not args.uuid or not re.fullmatch(r'GPU-[A-Za-z0-9-]+', args.uuid)):
        parser.error('--uuid GPU-... is required (copy it from status)')
    if args.action != 'status' and not args.dry_run and os.geteuid() != 0:
        parser.error('Use sudo for run/auto, or use run --dry-run')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    nv, lock_fd = None, None
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: stop.set())
    try:
        nv = NVML()
        if args.action == 'status':
            describe(nv)
            return 0
        handle = nv.device(args.uuid)
        logging.info('Target: %s / %s', nv.text('nvmlDeviceGetName', handle), args.uuid)
        count = nv.fan_count(handle)
        if not args.dry_run:
            lock_fd = os.open('/run/lock/gpu-fan-' + args.uuid + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NVError('Controller already running. Stop it with Ctrl+C first.') from None
        if args.action == 'auto':
            nv.restore(handle, count)
            return 0
        config = load_config(args.config)
        low, high = nv.bounds(handle)
        if config['points'][0][1] < low or config['points'][-1][1] > high:
            raise NVError(f'Curve outside supported fan range {low}–{high}%. No settings changed.')
        run_curve(nv, handle, count, config, stop, args.dry_run, args.duration)
        return 0
    except (NVError, OSError, ValueError, KeyError, TypeError) as exc:
        logging.error('%s', exc)
        return 1
    finally:
        if nv is not None:
            try:
                nv.close()
            except NVError as exc:
                logging.error('%s', exc)
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
