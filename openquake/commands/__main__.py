#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2022 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import logging
import warnings
import operator
from scipy import sparse

from openquake.baselib import sap, general
from openquake.calculators import export
from openquake.server.db.actions import DISPLAY_NAME
from openquake import commands

# check for Python version
PY_VER = sys.version_info[:2]
if PY_VER < (3, 6):
    sys.exit('Python 3.6+ is required, you are using %s', sys.executable)
elif PY_VER == (3, 6):
    print('Python 3.6 (%s) is not supported; the engine may not work correctly'
          % sys.executable)


# sanity check, all display name keys must be exportable
dic = general.groupby(export.export, operator.itemgetter(0))
for key in DISPLAY_NAME:
    assert key in dic, key


# global settings, like logging and warnings
def oq():
    args = set(sys.argv[1:])
    if 'engine' not in args and 'dbserver' not in args:
        # oq engine and oq dbserver define their own log levels
        level = logging.DEBUG if 'debug' in args else logging.INFO
        logging.basicConfig(level=level)

    warnings.simplefilter(  # make sure we do not make efficiency errors
        "error", category=sparse.SparseEfficiencyWarning)
    sap.run(commands, prog='oq')


if __name__ == '__main__':
    oq()
