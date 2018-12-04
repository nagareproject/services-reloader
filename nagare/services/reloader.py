# --
# Copyright (c) 2008-2018 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import os
import sys
import json
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
    def __init__(self, name, dist, statics_service=None):
        """Initialization
        """
        plugin.Plugin.__init__(self, name, dist)
        self.statics = statics_service

        self.dir_observer = Observer()
        self.dir_dispatcher = Dispatch(self.dir_dispatch)
        self.dir_actions = []
        self.file_observer = Observer()
        self.file_actions = {}
        self.file_dispatcher = Dispatch(self.file_dispatch)

        self.websockets = set()
        self.first_request = True
        self.reload = None

    def monitor(self, reload_action):
        if 'nagare_reloaded' in os.environ:
            self.start(reload_action)
            return 0

        exit_code = 3

        while exit_code == 3:
            # args = [_quote_first_command_arg(sys.executable)] + sys.argv
            args = [sys.executable] + sys.argv
            environ = os.environ.copy()
            environ['nagare_reloaded'] = 'True'

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
        self.dir_actions.append((dirname + os.path.sep, action, kw))
        self.dir_actions.sort(key=lambda a: len(a[0]), reverse=True)
        self.dir_observer.schedule(self.dir_dispatcher, os.path.abspath(dirname), recursive)

    def watch_file(self, filename, action=None, **kw):
        filename = os.path.abspath(filename)
        dirname = os.path.dirname(filename)

        self.file_actions[filename] = (action, kw)
        self.file_observer.schedule(self.file_dispatcher, dirname,)

    def default_action(self, _, path):
        if self.reload is not None:
            print('Reloading: %s modified' % path)
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

        if (command['command'] == 'info') and self.first_request:
            self.reload_document()

    def broadcast_livereload(self, command):
        '''
        reload:
          path
          liveCSS: (ref = message.liveCSS) != null ? ref : true,
          liveImg: (ref1 = message.liveImg) != null ? ref1 : true,
          reloadMissingCSS: (ref2 = message.reloadMissingCSS) != null ? ref2 : true,
          originalPath: message.originalPath || '',
          overrideURL: message.overrideURL || '',
          serverURL: "http://" + this.options.host + ":" + this.options.port

        alert:
          message
        '''
        message = json.dumps(command)
        for websocket in self.websockets:
            websocket.send(message)

    def close_livereload(self, websocket, code=None, reason=None):
        del websocket.received_message
        del websocket.closed
        self.websockets.remove(websocket)

    def reload_asset(self, path):
        self.broadcast_livereload({'command': 'reload', 'path': path})

    def reload_document(self):
        self.reload_asset('')

    def start(self, reload_action):
        self.reload = reload_action

        self.dir_observer.start()
        self.file_observer.start()

        if self.statics is not None:
            self.statics.register('/livereload', self.connect_livereload)

    def handle_request(self, chain, renderer=None, **params):
        self.first_request = False

        if renderer is not None:
            renderer.head.javascript_url('/static/nagare/livereload.js')

        return chain.next(**params)
