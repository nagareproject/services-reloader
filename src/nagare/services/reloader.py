# --
# Copyright (c) 2008-2024 Net-ng.
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
import string
import subprocess
from functools import partial
from collections import defaultdict

try:
    from watchdog_gevent import Observer

    gevent = True
except ImportError:
    if sys.platform.startswith('darwin'):
        # On MacOS `KqueueObserver` instead of `FSEventObserver` to avoid
        # "The process has forked and you cannot use this CoreFoundation functionality safely" error
        from watchdog.observers.kqueue import KqueueObserver as Observer
    else:
        from watchdog.observers import Observer

    gevent = False

from webob import exc, multidict

from nagare import packaging
from nagare.services import plugin


class ObserverBase(Observer):
    def __init__(self, default_action, services_service):
        super(ObserverBase, self).__init__()

        self._default_action = default_action
        self._services = services_service

    @staticmethod
    def reload_document(reloader_service):
        reloader_service.reload_document()

    def execute_callback(self, action, event, *args, **kw):
        to_restart = self._services(action or (lambda *args, **kw: True), event, *args, **kw)
        if to_restart is not None:
            if to_restart:
                self._services(self._default_action, event, *args, not action)
            else:
                self._services(self.reload_document)


class _DirsObserver(ObserverBase):
    def __init__(self, default_action=lambda dirname, path, event: None, services_service=None):
        services_service(super(_DirsObserver, self).__init__, default_action)
        self._actions = []

    def schedule(self, dirname, action=None, recursive=False, **kw):
        dirname = os.path.abspath(dirname)
        if not os.path.isdir(dirname):
            return False

        if dirname not in [callback[0] for callback in self._actions]:
            self._actions.append((dirname, recursive, action, kw))
            self._actions.sort(key=lambda a: len(a[0]), reverse=True)

            super(_DirsObserver, self).schedule(self, dirname, recursive=recursive)

        return True

    def dispatch(self, event):
        evt_path = event.src_path
        evt_dirname = evt_path if event.is_directory else os.path.dirname(evt_path)
        evt_dirname2 = evt_dirname + os.path.sep

        for dirname, recursive, action, kw in self._actions:
            dirname2 = dirname + os.path.sep
            if (recursive and evt_dirname2.startswith(dirname2)) or (evt_dirname == dirname):
                path = evt_path[len(dirname) + 1 :]

                self.execute_callback(action, event, dirname, path, **kw)
                break


class DirsObserver(object):
    def __init__(self, default_action=lambda dirname, path, event: None):
        self.default_action = default_action
        self.watched_dirs = []
        self.dirs_observer = None

    def schedule(self, dirname, action=None, recursive=False, **kw):
        if self.dirs_observer is None:
            self.watched_dirs.append((dirname, action, recursive, kw))
        else:
            self.dirs_observer.schedule(dirname, action, recursive, **kw)

    def start(self, services_service):
        self.dirs_observer = services_service(_DirsObserver, self.default_action)

        for dirname, action, recursive, kw in self.watched_dirs:
            self.dirs_observer.schedule(dirname, action, recursive, **kw)

        self.watched_dirs = []
        self.dirs_observer.start()


class _FilesObserver(ObserverBase):
    def __init__(self, default_action=lambda path: None, files_mtime_check=False, services_service=None):
        services_service(super(_FilesObserver, self).__init__, default_action)

        self._files_mtime_check = files_mtime_check
        self._dirs = defaultdict(dict)

    def schedule(self, filename, action=None, **kw):
        global gevent

        filename = os.path.abspath(filename)
        if not os.path.isfile(filename):
            return False

        dirname = os.path.dirname(filename)
        basename = os.path.basename(filename)
        self._dirs[dirname][basename] = [os.stat(filename).st_mtime, action, kw]

        super(_FilesObserver, self).schedule(self, filename if gevent else dirname)

        return True

    def dispatch(self, event):
        if event.is_directory:
            return

        filename = getattr(event, 'dest_path', None) or event.src_path
        dirname = os.path.dirname(filename)
        basename = os.path.basename(filename)

        file_infos = self._dirs[dirname].get(basename)
        if not file_infos:
            return

        mtime1, action, kw = file_infos
        mtime2 = os.stat(filename).st_mtime if os.path.isfile(filename) else mtime1 + 1
        if not self._files_mtime_check or (mtime2 > mtime1):
            if event.event_type != 'deleted':
                file_infos[0] = mtime2

            self.execute_callback(action, event, filename, **kw)


class FilesObserver(object):
    def __init__(self, default_action=lambda path: None, files_mtime_check=False):
        self.default_action = default_action
        self.files_mtime_check = files_mtime_check
        self.watched_files = []
        self.files_observer = None

    def schedule(self, filename, action=None, **kw):
        if self.files_observer is None:
            self.watched_files.append((filename, action, kw))
        else:
            self.files_observer.schedule(filename, action, **kw)

    def start(self, services_service):
        self.files_observer = services_service(_FilesObserver, self.default_action, self.files_mtime_check)

        for filename, action, kw in self.watched_files:
            self.files_observer.schedule(filename, action, **kw)

        self.watched_files = []
        self.files_observer.start()


class Reloader(plugin.Plugin):
    """Reload on source changes."""

    LOAD_PRIORITY = 24
    CONFIG_SPEC = dict(
        plugin.Plugin.CONFIG_SPEC,
        files_mtime_check='boolean(default=False)',
        live='boolean(default=True)',
        min_connection_delay='integer(default=500)',
        max_connection_delay='integer(default=500)',
        animation='integer(default=150)',
    )
    WEBSOCKET_URL = '/nagare/reloader/'

    def __init__(
        self,
        name,
        dist,
        files_mtime_check,
        live,
        min_connection_delay,
        max_connection_delay,
        animation,
        services_service,
        statics_service=None,
        **config,
    ):
        """Initialization."""
        plugin.Plugin.__init__(
            self,
            name,
            dist,
            files_mtime_check=files_mtime_check,
            live=live,
            animation=animation,
            min_connection_delay=min_connection_delay,
            max_connection_delay=max_connection_delay,
            **config,
        )

        editable_project_location = packaging.Distribution(dist).editable_project_location
        location = os.path.join(editable_project_location, 'src') if editable_project_location else dist.location
        self.static = os.path.join(location, 'nagare', 'static')

        self.live = live
        self.animation = animation

        self.dirs_observer = DirsObserver(self.default_dir_action)
        self.files_observer = FilesObserver(self.default_file_action, files_mtime_check)

        self.websockets = set()
        self.reload = lambda self, path: None
        self.version = 0

        if self.live:
            self.query = {'mindelay': str(min_connection_delay), 'maxdelay': str(max_connection_delay)}

        self.head = b''

    @property
    def reload_script(self):
        query = '&'.join(k + '=' + v for k, v in self.query.items()) + '&extver=' + str(self.version)

        return self.head % query.encode('ascii')

    @property
    def activated(self):
        return 'nagare.reload' in os.environ

    def monitor(self, reload_action, services_service):
        if self.activated:
            services_service(self.start, reload_action)
            return 0

        nb_reload = 0
        exit_code = 3

        while exit_code == 3:
            nb_reload += 1
            nagare = sys.argv[0]
            if os.path.exists(nagare + '.exe'):
                nagare += '.exe'

            args = [sys.executable, nagare] + sys.argv[1:]

            environ = os.environ.copy()
            environ['nagare.reload'] = str(nb_reload)

            proc = None
            try:
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
        self.dirs_observer.schedule(dirname, action, recursive, **kw)

    def watch_file(self, filename, action=None, **kw):
        self.files_observer.schedule(filename, action, **kw)

    def default_file_action(self, event, path, only_on_modified=False, services_service=None):
        if (self.reload is not None) and (
            not only_on_modified or (event.event_type in ('created', 'modified', 'moved'))
        ):
            self.logger.info('Reloading: %s modified' % path)
            services_service.handle_reload()
            self.reload(self, path)

    def default_dir_action(self, event, dirname, path, only_on_modified=False, services_service=None):
        services_service(self.default_file_action, event, os.path.join(dirname, path) if path else dirname)

    def connect_livereload(self, request, websocket, **params):
        if request.path_info.rstrip('/'):
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

    def start(self, reload_action, services_service, statics_service=None):
        self.reload = reload_action

        services_service(self.dirs_observer.start)
        services_service(self.files_observer.start)

        self.version = random.randint(10000000, 99999999)  # noqa: S311

    @staticmethod
    def insert_reload_script(body, reload_script):
        before, tag, after = body.partition(b'</head>')
        if not tag:
            before, tag, after = body.partition(b'</body>')

        if tag:
            body = b''.join([before, reload_script, tag, after])

        return body

    def generate_response(self, start_response, status, headers, body):
        headers = multidict.MultiDict(headers)
        content_type = headers.get('Content-Type')
        if content_type and content_type.startswith('text/html'):
            body = self.insert_reload_script(body, self.reload_script)
            headers['Content-Length'] = str(len(body))

        start_response(status, list(headers.iteritems()))(body)

    def handle_http_exception(self, http_exception, **params):
        if http_exception.status_code // 100 in (4, 5):
            reload_script = self.reload_script

            if http_exception.has_body:
                http_exception.body = self.insert_reload_script(http_exception.body, reload_script)
            else:
                template = http_exception.body_template_obj.template
                http_exception.body_template_obj = string.Template(template + reload_script.decode('ascii'))

        return http_exception

    def handle_start(self, app, exceptions_service, statics_service=None):
        if self.live and (statics_service is not None) and hasattr(app, 'static_url') and hasattr(app, 'service_url'):
            static_url = app.static_url + '/nagare/reloader'
            self.head = b'<script type="text/javascript" src="%s/livereload.js?%%s"></script>' % static_url.encode(
                'ascii'
            )
            if self.animation:
                self.head += b'<style type="text/css">html * { transition: all %dms ease-out }</style>' % self.animation
            statics_service.register_dir(static_url, self.static, gzip=True)

            websocket_url = app.service_url + self.WEBSOCKET_URL
            self.query['path'] = websocket_url.lstrip('/')
            statics_service.register_ws(websocket_url, self.connect_livereload)

            exceptions_service.add_exception_handler(self.handle_http_exception)

    def handle_request(self, chain, start_response=None, **params):
        if start_response is None:
            return chain.next(**params)

        if self.live and not params['request'].is_xhr:
            start_response = partial(partial, self.generate_response, start_response)

        return chain.next(start_response=start_response, **params)
