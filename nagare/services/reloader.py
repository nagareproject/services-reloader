# --
# Copyright (c) 2008-2018 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import os
try:
    import cPickle as pickle
except ImportError:
    import pickle

from nagare.services import plugin
from hupper import reloader, watchdog


class Monitor(watchdog.WatchdogFileMonitor):
    def __init__(self, callback, logger, **kw):
        super(Monitor, self).__init__(lambda path: self.on_change(callback, path), logger, **kw)
        self.actions = {}

    def add_path(self, path):
        if not isinstance(path, str) or not path.startswith(os.sep):
            path, action, kw = pickle.loads(path)
            if action:
                self.actions[path] = (action, kw)

        if not path.endswith('.pyc') and ('lib/python' not in path):
            super(Monitor, self).add_path(path)

    def on_change(self, on_change, path):
        action, kw = self.actions.get(path, (lambda path: True, {}))

        if action(path, **kw):
            on_change(path)


class Reloader(plugin.Plugin, reloader.Reloader):
    """Reload on source changes
    """
    LOAD_PRIORITY = 0
    CONFIG_SPEC = {
        'interval': 'integer(default=1)',
        'files': 'force_list(default="")',
        '_config_filename': 'string(default=$config_filename)',
        '_user_config_filename': 'string(default=$user_config_filename)'
    }

    def __init__(self, name, dist, interval, files, _config_filename, _user_config_filename):
        """Initialization
        """
        plugin.Plugin.__init__(self, name, dist)
        self.plugin_category = 'nagare.services'

        reloader.Reloader.__init__(
            self,
            logger=None,
            worker_path='nagare.admin.admin.run',
            reload_interval=interval,
            monitor_factory=Monitor
        )

        self.files = filter(None, files + [_config_filename, _user_config_filename])

    @staticmethod
    def watch_files(files, action=None, **kw):
        if reloader.is_active():
            for file in files:
                file = os.path.abspath(file)
                reloader.get_reloader().pipe.send(('watch', (pickle.dumps((file, action, kw), 0),)))

    def start(self):
        if not reloader.is_active():
            self.run()
        else:
            self.watch_files(self.files)
