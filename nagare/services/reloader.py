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
import subprocess
from functools import partial

try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

from webob import exc
from watchdog.observers import Observer

from nagare.services import plugin


class DirsObserver(Observer):

    def __init__(self, default_action=lambda dirname, path, event: None):
        super(DirsObserver, self).__init__()

        self._default_action = default_action
        self._actions = []

    def schedule(self, dirname, action=None, recursive=False, **kw):
        dirname = os.path.abspath(dirname)
        if not os.path.isdir(dirname):
            return False

        if dirname not in [callback[0] for callback in self._actions]:
            self._actions.append((dirname, recursive, action, kw))
            self._actions.sort(key=lambda a: len(a[0]), reverse=True)

            super(DirsObserver, self).schedule(self, dirname, recursive)

        return True

    def dispatch(self, event):
        evt_path = event.src_path
        evt_dirname = evt_path if event.is_directory else os.path.dirname(evt_path)
        evt_dirname2 = evt_dirname + os.path.sep

        for dirname, recursive, action, kw in self._actions:
            dirname2 = dirname + os.path.sep
            if (recursive and evt_dirname2.startswith(dirname2)) or (evt_dirname == dirname):
                path = evt_path[len(dirname) + 1:]
                if(action or (lambda event, dirname, path, **kw: True))(event, dirname, path, **kw):
                    self._default_action(event, dirname, path, event)
                break


class FilesObserver(Observer):

    def __init__(self, default_action=lambda path: None):
        super(FilesObserver, self).__init__()

        self._default_action = default_action
        self._actions = {}

    def schedule(self, filename, action=None, **kw):
        filename = os.path.abspath(filename)
        if not os.path.isfile(filename):
            return False

        if filename not in self._actions:
            self._actions[filename] = (os.stat(filename).st_mtime, action, kw)
            super(FilesObserver, self).schedule(self, os.path.dirname(filename))

        return True

    def dispatch(self, event):
        path = event.src_path

        mtime1, action, kw = self._actions.get(path, (None, None, None))
        mtime2 = os.stat(path).st_mtime if not event.event_type == 'deleted' else (mtime1 + 1)

        if mtime1 and (mtime2 > mtime1):
            self._actions[path] = (mtime2, action, kw)
            if (action or (lambda self, path, **kw: True))(event, path, **kw):
                self._default_action(event, path)


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
    WEBSOCKET_URL = 'nagare/services/reloader'

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

        self.dirs_observer = DirsObserver(self.default_dir_action)
        self.files_observer = FilesObserver(self.default_file_action)

        self.websockets = set()
        self.reload = lambda self, path: None
        self.version = 0

    @property
    def activated(self):
        return 'nagare.reload' in os.environ

    def monitor(self, reload_action):
        if self.activated:
            self.start(reload_action)
            return 0

        nb_reload = 0
        exit_code = 3

        while exit_code == 3:
            nb_reload += 1
            # args = [_quote_first_command_arg(sys.executable)] + sys.argv
            args = [sys.executable] + sys.argv

            environ = os.environ.copy()
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
                if proc is not None:
                    proc.terminate()

        return exit_code

    def watch_dir(self, dirname, action=None, recursive=False, **kw):
        if not self.dirs_observer.schedule(dirname, action, recursive, **kw):
            self.logger.warn("Directory `{}` doesn't exist".format(dirname))

    def watch_file(self, filename, action=None, **kw):
        if not self.files_observer.schedule(filename, action, **kw):
            self.logger.warn("File `{}` doesn't exist".format(filename))

    def default_file_action(self, event, path):
        if self.reload is not None:
            print('Reloading: %s modified' % path)
            for service in self.services_to_reload:
                service.handle_reload()

            self.reload(self, path)

    def default_dir_action(self, event, dirname, path):
        self.default_file_action(event, os.path.join(dirname, path) if path else dirname)

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

        self.dirs_observer.start()
        self.files_observer.start()

        self.version = random.randint(10000000, 99999999)

        if self.live and (self.statics is not None):
            self.statics.register_dir('/static/nagare-reloader', self.static)
            self.statics.register(self.WEBSOCKET_URL, self.connect_livereload)

    def handle_request(self, chain, **params):
        renderer = params.get('renderer')
        if renderer is not None:
            if self.live:
                query = {
                    'path': self.WEBSOCKET_URL,
                    'mindelay': str(self.min_connection_delay),
                    'maxdelay': str(self.max_connection_delay),
                    'extver': str(self.version)
                }

                renderer.head.javascript_url('/static/nagare-reloader/livereload.js?' + urlencode(query))

                if self.animation:
                    renderer.head.css('livereload', 'html * { transition: all %dms ease-out }' % self.animation)

        return chain.next(**params)
