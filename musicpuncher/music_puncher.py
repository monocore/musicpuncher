from time import sleep, time
from typing import List

import pigpio

from .music import NoteSequence

MIN_SPS = 1000  # steps per second
MAX_SPS = 3000  # steps per second
ACCELERATION = 1000  # SPS per second
from .keyboard import Keyboard


class MusicPuncher(object):
    def __init__(self, config, keyboard: Keyboard, notesequence: NoteSequence, address: str = 'localhost',
                 port: int = 8888):
        self.pi = pigpio.pi(address, port)
        if not self.pi.connected:
            raise RuntimeError(
                f"PI Not connected, make sure the pi is available on {address} and is running pigpiod on port {port}")

        self.keyboard = keyboard

        self.feed_stepper = PiGPIOStepperMotor(self.pi, config['feed-stepper'])
        self.tone_stepper = PiGPIOStepperMotor(self.pi, config['tone-stepper'])
        self.zero_button = None if config['zero-button'] == 'absent' else Button(self.pi, config['zero-button'])
        self.puncher = Puncher(self.pi, config['puncher'])
        self.cutter = Cutter(self.pi, config['cutter'])

        self.idle_position = config['idle-position']
        self.row0 = config['row0']
        self.tone_steps = config['tone-steps']
        self.feed_steps = config['feed-steps']
        self.cutter_position = config['cutter-position']
        self.end_feed = config['end-feed']
        self.position = None
        self.notesequence = notesequence

        print(f"PiGPIO max pulses: {self.pi.wave_get_max_pulses()}")
        print(f"PiGPIO max cbs:    {self.pi.wave_get_max_cbs()}")

    def run(self):
        self.reset()

        position = self.position
        steps = []  # list of tuple with (delay, note) steps for each hole
        for delayNotes in self.notesequence:
            if delayNotes.notes == []:
                raise (RuntimeError("Empty note set is not supported, consolidate consecutive delays first"))
            positions = sorted(
                [self.row0 + (self.keyboard.get_index(note) * self.tone_steps) for note in delayNotes.notes])
            if abs(position - positions[-1]) < abs(position - positions[0]):
                positions.reverse()  # Start from nearest end
            delay = round(delayNotes.delay * self.feed_steps)
            for targetPosition in positions:
                tuple = (delay, targetPosition - position)
                if tuple != (0, 0):
                    steps.append(tuple)
                position = targetPosition
                delay = 0

        self.do_run(steps)
        print(f"Head position: {self.position}")

    def do_run(self, steps):
        total_time_steps = 0
        for step in steps:
            total_time_steps += step[0]

        cutter_step = total_time_steps + self.cutter_position
        did_cut = False

        total_time_steps = 0
        for idx, step in enumerate(steps):
            if idx == 0:
                self.__prepare_waveform(step[0], step[1])
            self.__create_and_send_wave()  # run wave prepared for previous step
            if idx < len(steps) - 1:
                self.__prepare_waveform(steps[idx + 1][0], steps[idx + 1][1])
            self.__wait_for_wave()
            self.position += step[1]
            self.puncher.punch()
            total_time_steps += step[0]

            if not did_cut and total_time_steps >= cutter_step:
                self.cutter.cut()
                did_cut = True

        if not did_cut:
            if self.cutter_position > 0:
                self.__move(self.cutter_position, self.idle_position-self.position)
            self.cutter.cut()

        if self.end_feed > 0:
            self.__move(self.end_feed, self.idle_position-self.position)

        self.__move(0, self.idle_position-self.position) # will do nothing if already in position

    def __move(self, feedsteps, tonesteps):
        if feedsteps == 0 and tonesteps == 0:
            return
        self.__prepare_waveform(feedsteps, tonesteps)
        self.__create_and_send_wave()
        self.__wait_for_wave()
        self.position += tonesteps

    def reset(self):
        print('* reset *')
        if self.zero_button:
            self.tone_stepper.move_until(-1, lambda: self.zero_button.is_on())
            self.position = 0
            self.__move(0, self.idle_position)
        else:
            print(f"Assuming that the puncher is manually aligned at step {self.idle_position}")
            self.position = self.idle_position

    def __prepare_waveform(self, timesteps, notesteps):
        self.pi.wave_clear()
        wave1 = self.feed_stepper.create_move_waveform(timesteps)
        wave2 = self.tone_stepper.create_move_waveform(notesteps)
        self.prepared_wave_length = self.__synchronize(wave1, wave2) / 1000000

        self.__add_wave(wave1)
        self.__add_wave(wave2)

    def __create_and_send_wave(self):
        id = self.pi.wave_create()
        if id < 0:
            raise RuntimeError(f"pigpio error on wave_create: {id}")
        cbs = self.pi.wave_send_once(id)
        self.expected_wave_end_time = time() + self.prepared_wave_length

    def __wait_for_wave(self):
        now = time()
        expected_end = self.expected_wave_end_time + 0.001
        if expected_end > now:
            sleep(expected_end - now)
        while self.pi.wave_tx_busy():  # wait for waveform to be sent
            sleep(0.1)

    def __add_wave(self, pulses: List[pigpio.pulse]):
        if len(pulses) > 0:
            self.pi.wave_add_generic(pulses)

    def __synchronize(self, wave1: List[pigpio.pulse], wave2: List[pigpio.pulse]) -> int:
        """Scales the wave to the longest to have the same length, and returns the length in microseconds"""
        def wavelength(pulses):
            l = 0
            for pulse in pulses:
                l += pulse.delay
            return l

        if wave1 == []:
            return wavelength(wave2)
        if wave2 == []:
            return wavelength(wave1)


        def scale(pulses, factor):
            for pulse in pulses:
                pulse.delay = round(pulse.delay * factor)

        l1 = wavelength(wave1)
        l2 = wavelength(wave2)

        if l1 > l2:
            scale(wave2, l1 / l2)
            return l1
        scale(wave1, l2 / l1)
        return l2


def calculate_acceleration_profile(min_sps, max_sps, acceleration):
    "Returns a list of delays in microseconds"
    profile = []

    sps = min_sps
    while sps < max_sps:
        profile.append(round(1000000 / sps))
        sps += acceleration * (1 / sps)
    return profile


class PiGPIOStepperMotor(object):

    def __init__(self, pi: pigpio.pi, config):
        self.pi = pi
        self.dir_pin = config['dir-pin']
        self.step_pin = config['step-pin']
        self.acceleration_profile = calculate_acceleration_profile(config['min-sps'], config['max-sps'],
                                                                   config['acceleration'])
        self.min_delay = 1 / config['max-sps']
        self.max_delay = 1 / config['min-sps']

        pi.set_mode(self.dir_pin, pigpio.OUTPUT)
        pi.set_mode(self.step_pin, pigpio.OUTPUT)

    def __set_dir(self, dir: int):
        self.pi.write(self.dir_pin, 0 if dir < 0 else 1)

    def __step(self, delay):
        self.pi.write(self.step_pin, 1)
        sleep(delay / 2)
        self.pi.write(self.step_pin, 0)
        sleep(delay / 2)

    def move_until(self, dir: int, condition):
        """Slowly moves the motor in the given direction (-1,+1) until the condition becomes true"""
        self.__set_dir(dir)
        while not condition():
            self.__step(self.max_delay)

    def create_move_waveform(self, steps: int) -> List[pigpio.pulse]:
        self.__set_dir(-1 if steps < 0 else 1)

        pulses = []

        profile = self.acceleration_profile
        proflen = len(profile)
        min_delay_us = round(self.min_delay * 1000000)
        count = abs(steps)
        steps_to_stop = 0
        rem = count
        for i in range(0, count):
            rem -= 1
            if rem <= steps_to_stop:
                delay_us = profile[rem]
            elif i < proflen:
                delay_us = profile[i]
                steps_to_stop = i
            else:
                delay_us = min_delay_us
            pulses.append(pigpio.pulse(1 << self.step_pin, 0, delay_us >> 1))
            pulses.append(pigpio.pulse(0, 1 << self.step_pin, delay_us >> 1))

        return pulses


class Button:
    def __init__(self, pi: pigpio.pi, config):
        self.pi = pi
        self.pin = config['pin']
        self.pi.set_mode(self.pin, pigpio.INPUT)
        self.pi.set_pull_up_down(self.pin, pigpio.PUD_DOWN)

    def is_on(self):
        return self.pi.read(self.pin) == 1

    def is_off(self):
        return self.pi.read(self.pin) == 0


class Puncher:
    def __init__(self, pi: pigpio.pi, config):
        self.pi = pi
        self.pin = config['pin']
        self.on_length = config['on-length']
        self.off_length = config['off-length']
        self.pi.set_mode(self.pin, pigpio.INPUT)
        self.pi.set_pull_up_down(self.pin, pigpio.PUD_DOWN)

    def punch(self):
        print(f"punch")
        self.pi.write(self.pin, 1)
        sleep(self.on_length)
        self.pi.write(self.pin, 0)
        sleep(self.off_length)


class Cutter:
    def __init__(self, pi: pigpio.pi, config):
        self.pi = pi
        self.pin = config['pin']
        self.on_length = config['on-length']
        self.off_length = config['off-length']
        self.pi.set_mode(self.pin, pigpio.INPUT)
        self.pi.set_pull_up_down(self.pin, pigpio.PUD_DOWN)

    def cut(self):
        print(f"cut")
        self.pi.write(self.pin, 1)
        sleep(self.on_length)
        self.pi.write(self.pin, 0)
        sleep(self.off_length)