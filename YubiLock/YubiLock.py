#!/usr/bin/env python
# -*- coding: utf-8 -*-
# YubiLock VERSION 0.6
# LICENSE: GNU General Public License v3.0
# https://stackoverflow.com/questions/285716/trapping-second-keyboard-input-in-ubuntu-linux


import os
import sys
import re
import time

import shlex
import subprocess

import threading
from multiprocessing import Process
from multiprocessing.queues import Queue

import gi.repository
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
gi.require_version('AppIndicator3', '0.1')
from gi.repository import AppIndicator3 as AppIndicator

from ConfigParser import SafeConfigParser
from Xlib import X
from Xlib.display import Display
from Xlib.ext import record
from Xlib.protocol import rq


# change working dir to that of script:
abspath = os.path.abspath(__file__)
d_name = os.path.dirname(abspath)
os.chdir(d_name)

# BASIC SETTINGS -------------------------------------------------------------------------------------------------------
APPINDICATOR_ID = 'yubilock-indicator'
HELP_URL = "https://github.com/bfelder/YubiLock/blob/master/HELP.md"

# icons:
ICON_DIR = './icons/'
OFF_ICON = ICON_DIR + 'off_icon.svg'
ON_ICON = ICON_DIR + 'on_icon.svg'
NOKEY_ICON = ICON_DIR + 'nokey_icon.svg'

# Defining signal for queue communication:
ON_SIGNAL = 'ON'
EXIT_SIGNAL = 'EXIT'
OFF_SIGNAL = 'OFF'
NOKEY_SIGNAL = 'NOKEY'

# fetch configs from settings.ini:
parser = SafeConfigParser()
parser.read('settings.ini')
TIMEOUT = parser.getint('GENERAL', 'TIMEOUT')
KEY_CODES_L = map(int, parser.get('KEYS', 'KEY_CODES').split(','))


# static methods:
def shell(cmd):
    shlex_cmd = shlex.split(cmd)
    stdout = subprocess.Popen(shlex_cmd, stdout=subprocess.PIPE)
    return stdout


class PanelIndicator(object):
    def __init__(self, pi_q):
        self.indicator = AppIndicator.Indicator.new(APPINDICATOR_ID, os.path.abspath(NOKEY_ICON),
                                                    AppIndicator.IndicatorCategory.SYSTEM_SERVICES)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.build_menu)

        self.pi_q = pi_q

    def run_pi(self):
        # suppresses error: Couldn't connect to accessibility bus: Failed to connect to socket:
        stdout = subprocess.Popen(["export", "NO_AT_BRIDGE=1"], shell=True, stdout=subprocess.PIPE)

        # listener loop for icon switch signals
        ui_thread = threading.Thread(target=self.update_icon)
        ui_thread.daemon = True
        ui_thread.start()

        # starting Gtk main:
        Gtk.main()

    @property
    def build_menu(self):
        menu = Gtk.Menu()

        item_quit = Gtk.MenuItem('Help')
        item_quit.connect('activate', self.open_help)

        menu.append(item_quit)

        separator = Gtk.SeparatorMenuItem()
        menu.append(separator)

        item_quit = Gtk.MenuItem('Quit')
        item_quit.connect('activate', self.quit)
        menu.append(item_quit)

        menu.show_all()
        return menu

    def open_help(self, *arg):
        help_cmd = "xdg-open {HELP_URL} || " \
                   "gnome-open {HELP_URL} || " \
                   "kde-open {HELP_URL} || " \
                   "notify-send --expire-time=7000" \
                   " \"Could not open YubiLock web help:\" \"{HELP_URL}\"".format(HELP_URL=HELP_URL)
        subprocess.Popen(help_cmd, shell=True)

    def quit(self, *arg):
        print('Quitting Gtk.')
        Gtk.main_quit()

    def update_icon(self):
        while True:
            if self.pi_q.qsize > 0:
                state = self.pi_q.get()

                if state == ON_SIGNAL:
                    self.indicator.set_icon_full(os.path.abspath(ON_ICON), "")
                elif state == OFF_SIGNAL:
                    self.indicator.set_icon_full(os.path.abspath(OFF_ICON), "")
                elif state == NOKEY_SIGNAL:
                    self.indicator.set_icon_full(os.path.abspath(NOKEY_ICON), "")
            time.sleep(.01)


class KeyEventListener(object):
    def __init__(self, on_q):
        self.disp = Display()
        # query_l = []
        self.kp_l = []  # list to put in currently pressed keys
        self.on_q = on_q

        # Monitor keypress and button press
        self.ctx = self.disp.record_create_context(
            0,
            [record.AllClients],
            [{
                'core_requests': (0, 0),
                'core_replies': (0, 0),
                'ext_requests': (0, 0, 0, 0),
                'ext_replies': (0, 0, 0, 0),
                'delivered_events': (0, 0),
                'device_events': (X.KeyReleaseMask, X.ButtonReleaseMask),
                'errors': (0, 0),
                'client_started': False,
                'client_died': False,
            }])

    def ke_handler(self, reply):
        """ This function is called when a xlib key_event is fired """
        data = reply.data
        global KEY_CODES_L

        while len(data):
            key_event, data = rq.EventField(None).parse_binary_value(data, self.disp.display, None, None)
            if key_event.type == X.KeyPress:
                # KEY PRESSED
                if key_event.detail not in self.kp_l:
                    self.kp_l.append(key_event.detail)
            elif key_event.type == X.KeyRelease:
                # KEY RELEASED
                self.kp_l.remove(key_event.detail)

            if sorted(KEY_CODES_L) == sorted(self.kp_l):
                """ KEY combination PRESSED --> action! """
                print('Key (combination) pressed.')
                self.on_q.put(ON_SIGNAL)

    def start_listener(self):
        self.disp.record_enable_context(self.ctx, self.ke_handler)  # disp.record_enable_context(ctx, handler)
        self.disp.record_free_context(self.ctx)


class AsynchronousFileReader(threading.Thread):
    """
    Helper class to implement asynchronous reading of a file
    in a separate thread. Pushes read lines on a queue to
    be consumed in another thread.
    Credits for this class goes to Stefaan Lippens:
    http://stefaanlippens.net/python-asynchronous-subprocess-pipe-reading
    """

    def __init__(self, fd, queue):
        # assert isinstance(queue, Queue.Queue)  # from import Queue
        assert isinstance(queue, Queue)
        assert callable(fd.readline)
        threading.Thread.__init__(self)
        self._fd = fd
        self._queue = queue

    def run(self):
        """ The body of the tread: read lines and put them on the queue."""
        for line in iter(self._fd.readline, ''):
            self._queue.put(line)

    @property
    def eof(self):
        """ Check whether there is no more content to expect."""
        return not self.is_alive() and self._queue.qsize() > 0


class YubiLock:
    def __init__(self):

        self.id_q = Queue()
        self.on_q = Queue()
        self.pi_q = Queue()

        # init processes
        gi_proc = Process(target=self.get_ids)
        gi_proc.daemon = True

        cs_proc = Process(target=self.change_state)
        cs_proc.daemon = False  # no daemon, or main program will terminate before YubiKeys can be unlocked

        kel = KeyEventListener(self.on_q)
        kel_proc = Process(target=kel.start_listener)
        kel_proc.daemon = True

        pi = PanelIndicator(self.pi_q)

        # starting processes and catching exceptions:
        try:
            gi_proc.start()
            cs_proc.start()
            kel_proc.start()

            pi.run_pi()  # main loop of root process

        except (KeyboardInterrupt, SystemExit):
            print('Caught exit event.')

        finally:
            # code continues here, after gtk.main_quit() has been called or exception
            # send exit signal, will reactivate YubiKey slots
            print('Sending EXIT_SIGNAL')
            self.on_q.put(EXIT_SIGNAL)

    def get_ids(self):
        old_id_l = []
        no_key = True
        while True:

            new_id_l = []
            # get list of xinput device ids and extract those of YubiKeys:
            pat = re.compile(r"(?:Yubikey.*?id=)(\d+)", re.IGNORECASE)
            list_cmd = 'xinput list'
            xinput = shell(list_cmd)
            for line in xinput.stdout:
                match = re.search(pat, line)
                if match:
                    yubi_id = match.groups()
                    new_id_l.extend(yubi_id)

            if not new_id_l and not no_key:
                self.pi_q.put(NOKEY_SIGNAL)
                print('No YubiKey(s) detected.')
                no_key = True
            elif new_id_l and no_key:
                self.pi_q.put(OFF_SIGNAL)
                print('YubiKey(s) detected.')
                no_key = False
                # notify:
                msg_cmd = "notify-send --expire-time=2000 'YubiKey(s) detected.'"
                shell(msg_cmd)

            if new_id_l != old_id_l:
                print('Change in YubiKey ids detected.')
                self.id_q.put(new_id_l)

            old_id_l = new_id_l

            time.sleep(.1)

    def unlock_keys(self, id_l):
        shell('; '.join(["xinput --enable {}".format(cs_id) for cs_id in id_l]))
        print('Unlocking YubiKey(s).')
        # switch to ON icon:
        self.pi_q.put(ON_SIGNAL)

    def lock_keys(self, id_l):
        shell('; '.join(["xinput --disable {}".format(cs_id) for cs_id in id_l]))
        print('Locking YubiKey(s).')
        # switch to OFF icon:
        self.pi_q.put(OFF_SIGNAL)

    def change_state(self):
        cs_is_l = []
        cs_signal = ''
        init_locked = False

        while True:
            # retrieve input from queues
            while self.id_q.qsize() > 0:
                cs_is_l = self.id_q.get()

            while self.on_q.qsize() > 0:
                cs_signal = self.on_q.get()

            # unlock / lock:
            if cs_is_l:
                if cs_signal == EXIT_SIGNAL:
                    print('Exiting gracefully and cleaning up:'),
                    self.unlock_keys(cs_is_l)
                    sys.exit(0)

                elif cs_signal == ON_SIGNAL:
                    self.unlock_keys(cs_is_l)

                    mon_thread = threading.Thread(target=self.yk_monitor, args=(cs_is_l,))
                    mon_thread.start()
                    mon_thread.join()

                    self.lock_keys(cs_is_l)

                elif not init_locked:  # initial disabling
                    self.lock_keys(cs_is_l)
                    init_locked = True

            cs_signal = ''  # reset state to prevent continued unlocking/locking
            time.sleep(.001)

    def yk_monitor(self, mon_l):

        # forming command to run parallel monitoring processes
        monitor = shell(' & '.join(["xinput test {}".format(y_id) for y_id in mon_l]))

        stdout_queue = Queue()
        stdout_reader = AsynchronousFileReader(monitor.stdout, stdout_queue)
        stdout_reader.start()

        triggered = False
        timestamp = time.time()
        while not stdout_reader.eof and time.time() - timestamp < TIMEOUT:
            while stdout_queue.qsize() > 0:
                stdout_queue.get()  # emptying queue
                triggered = True
                time.sleep(.01)
            if triggered:
                print('YubiKey triggered. Now disabling.')
                break

            time.sleep(.001)

        monitor.kill()
        stdout_reader.join()


# FIRING UP YUBILOCK ---------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting YubiLock.")
    yl = YubiLock()