# --
# Copyright (c) 2008-2019 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import os
import sys
import json
import random
import signal
import platform
import subprocess
from functools import partial

from webob import exc
from watchdog.observers import Observer

from nagare.services import plugin


class Dispatch(object):
    def __init__(self, dispatch):
        self.dispatch = dispatch


class Reloader(plugin.Plugin):
    """Reload on source changes
    """

    CONFIG_SPEC = dict(
        plugin.Plugin.CONFIG_SPEC,
        live='boolean(default=True)',
        min_connection_delay='integer(default=500)',
        max_connection_delay='integer(default=500)',
        animation='integer(default=150)'
    )

    def __init__(
        self,
        name, dist,
        live, min_connection_delay, max_connection_delay, animation,
        services_service, statics_service=None
    ):
        """Initialization
        """
        plugin.Plugin.__init__(self, name, dist)

        self.static = os.path.join(dist.location, 'nagare', 'static')

        self.live = live
        self.min_connection_delay = min_connection_delay
        self.max_connection_delay = max_connection_delay
        self.animation = animation
        self.services_to_reload = services_service.reload_handlers
        self.statics = statics_service

        self.dir_observer = Observer()
        self.dir_dispatcher = Dispatch(self.dir_dispatch)
        self.dir_actions = []
        self.file_observer = Observer()
        self.file_actions = {}
        self.file_dispatcher = Dispatch(self.file_dispatch)

        self.websockets = set()
        self.reload = lambda self, path: None
        self.version = 0

    @property
    def activated(self):
        return 'nagare.reloaded' in os.environ

    def monitor(self, reload_action):
        if self.activated:
            self.start(reload_action)
            return 0

        exit_code = 3

        nb_reload = 0

        while exit_code == 3:
            nb_reload += 1
            # args = [_quote_first_command_arg(sys.executable)] + sys.argv
            args = [sys.executable] + sys.argv

            environ = os.environ.copy()
            environ['nagare.reloaded'] = '1'
            environ['nagare.reload'] = str(nb_reload)

            proc = None
            try:
                # _turn_sigterm_into_systemexit()
                proc = subprocess.Popen(args, env=environ)
                exit_code = proc.wait()
                proc = None
            except KeyboardInterrupt:
                exit_code = 1
            finally:
                if (proc is not None) and hasattr(os, 'kill') and (platform.system() != 'Windows'):
                    os.kill(proc.pid, signal.SIGTERM)

        return exit_code

    def watch_dir(self, dirname, action=None, recursive=False, **kw):
        abs_dirname = os.path.abspath(dirname)
        if os.path.isdir(abs_dirname):
            self.dir_actions.append((dirname + os.path.sep, action, kw))
            self.dir_actions.sort(key=lambda a: len(a[0]), reverse=True)
            self.dir_observer.schedule(self.dir_dispatcher, abs_dirname, recursive)
        else:
            self.logger.warn("Directory `{}` doesn't exist".format(abs_dirname))

    def watch_file(self, filename, action=None, **kw):
        filename = os.path.abspath(filename)
        if os.path.isfile(filename):
            dirname = os.path.dirname(filename)

            self.file_actions[filename] = (action, kw)
            self.file_observer.schedule(self.file_dispatcher, dirname,)
        else:
            self.logger.warn("File `{}` doesn't exist".format(filename))

    def default_action(self, _, path):
        if self.reload is not None:
            print('Reloading: %s modified' % path)
            for service in self.services_to_reload:
                service.handle_reload()

            self.reload(self, path)

    def dir_dispatch(self, event):
        path = event.src_path
        for dirname, action, kw in self.dir_actions:
            if path.startswith(dirname) and (action or self.default_action)(self, path[:len(dirname)], path[len(dirname):], **kw):
                self.default_action(self, path)
                break

    def file_dispatch(self, event):
        path = event.src_path
        action = self.file_actions.get(path)
        if (action is not None) and (action[0] or self.default_action)(self, path, **action[1]):
            self.default_action(self, path)

    def connect_livereload(self, request, websocket, **params):
        if request.path_info:
            raise exc.HTTPNotFound()

        if websocket is None:
            raise exc.HTTPBadRequest()

        websocket.received_message = partial(self.receive_livereload, websocket)
        websocket.closed = partial(self.close_livereload, websocket)

        self.websockets.add(websocket)

    def receive_livereload(self, websocket, message):
        command = json.loads(message.data)

        if command['command'] == 'hello':
            response = {
                'command': 'hello',
                'protocols': ['http://livereload.com/protocols/official-7'],
                'serverName': 'nagare-livereload',
            }
            websocket.send(json.dumps(response))

            if command['extver'] != self.version:
                self.reload_document()

        if command['command'] == 'info':
            pass

    def broadcast_livereload(self, command):
        message = json.dumps(command)
        for websocket in self.websockets:
            websocket.send(message)

    def close_livereload(self, websocket, code=None, reason=None):
        del websocket.received_message
        del websocket.closed
        self.websockets.remove(websocket)

    def alert(self, message):
        self.broadcast_livereload({'command': 'alert', 'message': message})

    def reload_asset(self, path):
        self.broadcast_livereload({'command': 'reload', 'path': path})

    def reload_document(self):
        self.reload_asset('')

    def start(self, reload_action):
        self.reload = reload_action

        self.dir_observer.start()
        self.file_observer.start()

        self.version = random.randint(10000000, 99999999)

        if self.live and (self.statics is not None):
            self.statics.register_dir('/static/nagare-reloader', self.static)
            self.statics.register('/livereload', self.connect_livereload)

    def handle_request(self, chain, renderer=None, **params):
        if self.live and (renderer is not None):
            query = (
                'mindelay=%d' % self.min_connection_delay,
                'maxdelay=%d' % self.max_connection_delay,
                'extver=%d' % self.version
            )

            if self.animation:
                renderer.head.css('livereload', 'html * { transition: all %dms ease-out }' % self.animation)
            renderer.head.javascript_url('/static/nagare-reloader/livereload.js?' + '&'.join(query))

        return chain.next(renderer=renderer, **params)
