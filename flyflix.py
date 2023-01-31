#!/bin/env python

import socket
import math
import time
import logging
import random
import inspect
import warnings


from datetime import datetime, timedelta
from pathlib import Path
from logging import FileHandler

import eventlet

from flask import Flask, render_template, request, abort, url_for
from flask.logging import default_handler
from flask_socketio import SocketIO

from jinja2 import TemplateNotFound

from engineio.payload import Payload

from Experiment import SpatialTemporal, Duration, OpenLoopCondition, SweepCondition, ClosedLoopCondition, Trial, CsvFormatter

app = Flask(__name__)

start = False
SWEEPCOUNTERREACHED = False
RUN_FICTRAC = False


Payload.max_decode_packets = 500

# Using eventlet breaks UDP reading thread unless patched. 
# See http://eventlet.net/doc/basic_usage.html?highlight=monkey_patch#patching-functions for more.
# Alternatively disable eventlet and use development libraries via 
# `socketio = SocketIO(app, async_mode='threading')`

eventlet.monkey_patch()
# socketio = SocketIO(app)
# FIXME: find out if CORS is needed
socketio = SocketIO(app, cors_allowed_origins='*')

# socketio = SocketIO(app, async_mode='threading')

@app.before_first_request
def before_first_request():
    """
    Server initiator: check for paths  and initialize logger.
    """
    app.config.update(
        FICTRAC_HOST = '127.0.0.1',
        FICTRAC_PORT = 1717
    )
    data_path = Path("data")
    if data_path.exists():
        if not data_path.is_dir():
            errmsg = "'data' exists as a file, but we need to create a directory with that name to log data"
            app.logger.error(errmsg)
            raise Exception("'data' exists as a file, but we need to create a directory with that name to log data")
    else:
        data_path.mkdir()
    csv_handler = FileHandler("data/repeater_{}.csv".format(time.strftime("%Y%m%d_%H%M%S")))
    csv_handler.setFormatter(CsvFormatter())
    app.logger.removeHandler(default_handler)
    app.logger.setLevel(logging.INFO)
    app.logger.addHandler(csv_handler)
    app.logger.info(["client_id", "client_timestamp", "request_timestamp", "key", "value"])


def savedata(sid, shared, key, value=0):
    """
    Store data on disk. It is intended to be key-value pairs, together with a shared knowledge
    item. Data storage is done through the logging.FileHandler. 

    :param str shared: intended for shared knowledge between client and server
    :param str key: Key from the key-value pair
    :param str value: Value from the key-value pair.
    """
    app.logger.info([sid, shared, key, value])


def logdata(sid, client_timestamp, request_timestamp, key, value):
    """
    Store data on disk. In addition to a key-value pair, the interface allows to store a client
    timestamp and an additional timestamp, for example from the initial server request. In 
    practice, all these values are just logged to disk and stored no matter what they are.

    :param str client_timestamp: timestamp received from the client
    :param str request_timestamp: server timestamp that initiated the client action
    :param str key: key of the key-value pair
    :param str value: value of the key-value pair
    """
    app.logger.info([sid, client_timestamp, request_timestamp, key, value])
    

@socketio.on("connect")
def connect():
    """
    Confirm SocketIO connection by printing "Client connected"
    """
    print("Client connected", request.sid)


@socketio.on("disconnect")
def disconnect():
    """
    Verify SocketIO disconnect
    """
    print("Client disconnected", request.sid)


@socketio.on('start-experiment')
def finally_start(number):
    """
    When the server receives a `start-experiment` message via SocketIO, the global variable start
    is set to true

    :param number: TODO find out what it does
    """
    # FIXME: bad practice. Will break at some point
    print("Started")
    global start
    start = True
    socketio.emit('experiment-started');


@socketio.on('slog')
def server_log(json):
    """
    Save `key` and `value` from the dictionary received inside the SocketIO message `slog` to disk.

    :param json: dictionary received from the client via SocketIO
    """
    shared_key = time.time()
    savedata(request.sid, shared_key, json['key'], json['value'])

@socketio.on('csync')
def server_client_sync(client_timestamp, request_timestamp, key):
    """
    Save parameters to disk together with a current timestamp. This can be used for precisely 
    logging the round trip times.

    :param client_timestamp: timestamp from the client
    :param request_timestamp: timestamp that the client initially received from the server and 
        which started the process
    :param key: key that should be logged. 
    """
    logdata(request.sid, client_timestamp, request_timestamp, key, time.time_ns())


@socketio.on('dl')
def data_logger(client_timestamp, request_timestamp, key, value):
    """
    data logger routine for data sent from the client.

    :param client_timestamp: timestamp from the client
    :param request_timestamp: timestamp that the client initially received from the server and 
        which started the process
    :param key: key from key-value pair
    :param value: value from key-value pair
    """
    logdata(request.sid, client_timestamp, request_timestamp, key, value)


@socketio.on('display')
def display_event(json):
    savedata(request.sid, json['cnt'], "display-offset", json['counter'])


def log_fictrac_timestamp():
    shared_key = time.time_ns()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.1)
        data = ""
        prevts = 0
        prevfrm = 0
        try:
            sock.bind(( '127.0.0.1', 1717))
            new_data = sock.recv(1)
            data = new_data.decode('UTF-8')
            socketio.emit("meta", (shared_key, "fictrac-connect-ok", 1))
        except: # If Fictrac doesn't exist # FIXME: catch specific exception
            socketio.emit("meta", (shared_key, "fictrac-connect-fail", 0))
            warnings.warn("Fictrac is not running on 127.0.0.1:1717")
            return

        while RUN_FICTRAC:
            new_data = sock.recv(1024)
            if not new_data:
                break
            data += new_data.decode('UTF-8')
            endline = data.find("\n")
            line = data[:endline]
            data = data[endline+1:]
            toks = line.split(", ")
            if (len(toks) < 24) | (toks[0] != "FT"):
                continue # This is not the expected fictrac data package
            cnt = int(toks[1])
            #if cnt-prevfrm > 100:
            socketio.emit("meta", (shared_key, "fictrac-frame", cnt))
            #    prevfrm = cnt

def optomotor():
    print(time.strftime("%H:%M:%S", time.localtime()))
    block = []
    gains = [0.9, 1, 1.1]
    counter = 0
    gaincount = 0
    log_metadata()

    ## rotation 
    for alpha in [30]: #specifying the bar_deg
        for speed in [5]: #specifying the temporal frequency (in Hz)
            for direction in [-1, 1]: #specifying the direction of movement
                for clrs in [(0, 50), (0, 255)]: #specifying the contrast/brightness
                    for fg_color in [0x00ff00, 0xffffff]: #specifying the colors of the foreground
                        bright = clrs[1]
                        contrast = round((clrs[1]-clrs[0])/(clrs[1]+clrs[0]), 1)
                        if fg_color == 0x00ff00:
                            fg_color = clrs[1] << 8
                        elif fg_color == 0xffffff:
                            fg_color = clrs[1] + (clrs[1] << 8) + (clrs[1] << 16)
                        bg_color = clrs[0] << 8
                        rotation_speed = alpha*2*speed*direction
                        t = Trial(
                            counter, 
                            bar_deg=alpha, 
                            space_deg = (60-alpha),
                            rotate_deg_hz=rotation_speed,
                            openloop_duration = Duration(20000),
                            pretrial_duration=Duration(250), posttrial_duration=Duration(250),
                            fg_color=fg_color, bg_color=bg_color,
                            comment=f"Rotation alpha {alpha} speed {speed} direction {direction} brightness {bright} contrast {contrast}")
                        block.append(t)
                        counter += 1
                        
                        
    ## Static
    
    for alpha in [30]:
        for speed in [0]:
            for direction in [-1, 1]:
                for clrs in [(0, 50)]:
                    bright = clrs[1]
                    contrast = round((clrs[1]-clrs[0])/(clrs[1]+clrs[0]), 1)
                    fg_color = clrs[1] << 8
                    bg_color = clrs[0] << 8
                    rotation_speed = alpha*2*speed*direction
                    t = Trial(
                        counter, 
                        bar_deg=alpha, 
                        space_deg = (60-alpha),
                        rotate_deg_hz=rotation_speed,
                        openloop_duration = Duration(20000),
                        pretrial_duration=Duration(250), posttrial_duration=Duration(250),
                        fg_color=fg_color, bg_color=bg_color,
                        comment=f"Rotation alpha {alpha} speed {speed} direction {direction} brightness {bright} contrast {contrast}")
                    block.append(t)
                    counter += 1
                        
    # Oscillation
    #for alpha in [15]:
    #    for freq in [0.333]:
    #        for direction in [-1, 1]:
    #           for clrs in [(190, 64)]:
    #               right = clrs[1]
    #               contrast = round((clrs[1]-clrs[0])/(clrs[1]+clrs[0]), 1)
    #               fg_color = clrs[1] << 8
    #               bg_color = clrs[0] << 8
    #               t = Trial(
    #                   counter, 
    #                   bar_deg=alpha,
    #                   osc_freq=freq, osc_width=90*direction,
    #                   pretrial_duration=Duration(250), posttrial_duration=Duration(250),
    #                   fg_color=fg_color, bg_color=bg_color,
    #                   #bar_height=0.04,
    #                   comment=f"Oscillation with frequency {freq} direction {direction} brightness {bright} contrast {contrast}")
    #               block.append(t)
    #               counter += 1

    # Small object
    #for alpha in [10]:
    #    for speed in [2, 4]:
    #        for direction in [-1, 1]:
    #            for clrs in [(190, 64)]:
    #                bright = clrs[1]
    #                contrast = round((clrs[1]-clrs[0])/(clrs[1]+clrs[0]), 1)
    #                fg_color = clrs[1] << 8
    #                bg_color = clrs[0] << 8
    #                rotation_speed = alpha*2*speed*direction
    #                t = Trial(
    #                    counter, 
    #                    bar_deg=alpha, space_deg=180-alpha,
    #                    rotate_deg_hz=rotation_speed,
    #                    pretrial_duration=Duration(250), posttrial_duration=Duration(250),
    #                    fg_color=fg_color, bg_color=bg_color,
    #                    bar_height=0.03,
    #                    comment=f"Object alpha {alpha} speed {speed} direction {direction} brightness {bright} contrast {contrast}")
    #                block.append(t)
    #                counter += 1

    

    while not start:
        time.sleep(0.1)
    global RUN_FICTRAC
    RUN_FICTRAC = True
    _ = socketio.start_background_task(target = log_fictrac_timestamp)
    

    repetitions = 3
    counter = 0
    
    opening_screen = Trial(
        #counter, 
        bar_deg=0, space_deg=360, 
        openloop_duration=Duration(10000), 
        rotate_deg_hz=0, comment="opening black screen")
    opening_screen.trigger(socketio)
    #opening_black_screen = Duration(15000)
    #opening_black_screen.trigger_delay(socketio)
    for i in range(repetitions):
        socketio.emit("meta", (time.time_ns(), "block-repetition", i))
        block = random.sample(block, k=len(block))
        for current_trial in block:
            counter += 1
            print(f"Condition {counter} of {len(block*repetitions)}")
            current_trial.set_id(counter)
            current_trial.trigger(socketio)

    RUN_FICTRAC = False
    print(time.strftime("%H:%M:%S", time.localtime()))


@app.route('/optomotor/')
def local_optomotor():
    _ = socketio.start_background_task(target=optomotor)
    return render_template('cshlfly22.html')


def log_metadata():
    """
    The content of the `metadata` dictionary gets logged.
    
    This is a rudimentary way to save information related to the experiment to a file. Edit the 
    content of the dictionary for each experiment.
    """
    metadata = {
        "fly-strain": "CTRL",
        "fly-batch": "2022-07-06",
        "day-night-since": "2022-07-06",

        "birth-start": "2022-07-06 00:00:00",
        "birth-end": "2022-07-08 16:30:00",

        "starvation-start": "2022-07-10 21:00:00",

        "tether-start": "2022-07-11 10:00:00",
        "fly": 1,
        "tether-end"  : "2022-07-11 11:00:00",
        "sex": "f",
        
        "day-start": "07:00:00",
        "day-end": "19:00:00",
        

        "ball": "1",
        "air": "wall",
        "glue": "KOA",
        
        "temperature": 30,
        "distance": 35,
        "protocol": 1,
        "screen-brightness": 67,
        "display": "fire7",
        "color": "#00FF00"
    }


    shared_key = time.time_ns()
    for key, value in metadata.items():
        logdata(1, 0, shared_key, key, value)


@app.route("/")
def sitemap():
    """ List all routes and associated functions that FlyFlix currently supports. """
    links = []
    for rule in app.url_map.iter_rules():
        #breakpoint()
        if len(rule.defaults or '') >= len(rule.arguments or ''):
            url = url_for(rule.endpoint, **(rule.defaults or {}))
            desc = inspect.getdoc(eval(rule.endpoint))
            links.append((url, rule.endpoint, desc))
    return render_template("sitemap.html", links=links)


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port = 17000)
