#!/usr/bin/env python

# --
# Copyright (c) 2014-2026 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import sys
import gzip


def build_assets():
    from webassets import filter, script
    from webassets_browserify import Browserify

    filter.register_filter(Browserify)

    status = script.main(['-c', 'conf/assets.yaml', 'build', '--no-cache']) or 0
    if status == 0:
        with (
            open('src/nagare/static/livereload.js', 'rb') as f,
            gzip.open('src/nagare/static/livereload.js.gz', 'wb') as g,
        ):
            g.write(f.read())

        return status


if __name__ == '__main__':
    sys.exit(build_assets())
