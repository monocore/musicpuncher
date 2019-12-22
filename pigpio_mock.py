from random import randint
from time import time

import pigpio

INPUT = 0
OUTPUT = 1

pulse = pigpio.pulse


class pi():
    def __init__(self,
                 host='localhost',
                 port=8888,
                 show_errors=True):
        self.connected = True
        self.waves = []
        self.wave_end_time = time()

    def set_mode(self, gpio, mode):
        pass

    def write(self, gpio, level):
        pass

    def wave_tx_busy(self):
        return time() < self.wave_end_time

    def wave_clear(self):
        self.waves = []

    def wave_add_generic(self, pulses):
        self.waves.append(pulses)

    def wave_create(self):
        return randint(0, 100)

    def wave_send_once(self, wave_id):
        totaltime_us = 0
        for pulses in self.waves:
            wavetime_us = 0
            for pulse in pulses:
                wavetime_us += pulse.delay
            print(f"Wavelength: {len(pulses)} ({round(totaltime_us / 1000)} ms)")
            if wavetime_us > totaltime_us:
                totaltime_us = wavetime_us
        self.wave_end_time = time() + totaltime_us / 1000000
        return 0
