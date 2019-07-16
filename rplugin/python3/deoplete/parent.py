# ============================================================================
# FILE: parent.py
# AUTHOR: Shougo Matsushita <Shougo.Matsu at gmail.com>
# License: MIT license
# ============================================================================

import time
import os
import msgpack
import subprocess
import sys
import typing
from functools import partial
from pathlib import Path
from queue import Queue

from deoplete import logger
from deoplete.process import Process
from deoplete.util import error_tb, error, Nvim

UserContext = typing.Dict[str, typing.Any]


class _Parent(logger.LoggingMixin):
    def __init__(self, vim: Nvim) -> None:
        self.name = 'parent'

        self._vim = vim
        self._loaded_filters = set()

        self._start_process()

    def enable_logging(self) -> None:
        self._put('enable_logging', [])
        self.is_debug_enabled = True

    def add_source(self, path: str) -> None:
        self._put('add_source', [path])

    def add_filter(self, path: str) -> None:
        if path in self._loaded_filters:
            return
        self._loaded_filters.add(path)

        self._put('add_filter', [path])

    def set_source_attributes(self, context: UserContext) -> None:
        self._put('set_source_attributes', [context])

    def set_custom(self, custom: typing.Any) -> None:
        self._put('set_custom', [custom])

    def on_event(self, context: UserContext) -> None:
        self._put('on_event', [context])


class SyncParent(_Parent):
    def _start_process(self) -> None:
        from deoplete.child import Child
        self._child = Child(self._vim)

    def merge_results(self,
                      context: UserContext) -> typing.Tuple[typing.Any]:
        results = self._child._merge_results(context, queue_id=None)
        return (results['is_async'], results['is_async'],
                results['merged_results']) if results else (False, [])

    def _put(self, name: str, args: typing.List[typing.Any]) -> None:
        self._child.main(name, args, queue_id=None)


class AsyncParent(_Parent):
    def _get_python_executable(self) -> str:
        """Get Python executable.

        This handles Python being embedded in Vim on Windows or OSX.

        Taken from jedia.api.environment._try_get_same_env.
        """
        exe = sys.executable
        if not os.path.basename(exe).lower().startswith('python'):
            if os.name == 'nt':
                checks = (r'Scripts\python.exe', 'python.exe')
            else:
                checks = (
                    'bin/python%s.%s' % (sys.version_info[0], sys.version[1]),
                    'bin/python%s' % (sys.version_info[0]),
                    'bin/python',
                )
            for check in checks:
                guess = os.path.join(sys.exec_prefix, check)
                if os.path.isfile(guess):
                    return guess
            return str(self._vim.vars.get('python3_host_prog', 'python3'))
        return exe

    def _start_process(self) -> None:
        self._stdin = None
        self._queue_id = ''
        self._queue_in = Queue()
        self._queue_out = Queue()
        self._queue_err = Queue()
        self._packer = msgpack.Packer(
            use_bin_type=True,
            encoding='utf-8',
            unicode_errors='surrogateescape')
        self._unpacker = msgpack.Unpacker(
            encoding='utf-8',
            unicode_errors='surrogateescape')
        self._prev_pos = []

        info = None
        if os.name == 'nt':
            info = subprocess.STARTUPINFO()  # type: ignore
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore

        main = str(Path(__file__).parent.parent.parent.parent.joinpath(
            'autoload', 'deoplete', '_main.py'))

        self._hnd = self._vim.loop.create_task(
            self._vim.loop.subprocess_exec(
                partial(Process, self),
                self._get_python_executable(),
                main,
                self._vim.vars['deoplete#_serveraddr'],
                startupinfo=info))

    def _print_error(self, message: str) -> None:
        error(self._vim, message)

    def _connect_stdin(self, stdin: int) -> msgpack.Unpacker:
        self._stdin = stdin
        return self._unpacker

    def merge_results(self,
                      context: UserContext) -> typing.Tuple[typing.Any]:
        if (context['event'] == 'Async' and
                context['position'] == self._prev_pos and self._queue_id):
            # Use previous id
            queue_id = self._queue_id
        else:
            queue_id = self._put('merge_results', [context])
            if not queue_id:
                return (False, False, [])

        get = self._get(queue_id)
        if not get:
            # Skip the next merge_results
            self._queue_id = queue_id
            self._prev_pos = context['position']
            return (True, False, [])
        self._queue_id = ''
        results = get[0]
        return (results['is_async'], results['is_async'],
                results['merged_results']) if results else (False, [])

    def _put(self, name: str,
             args: typing.List[typing.Any]) -> typing.Optional[int]:
        if not self._hnd:
            return None

        queue_id = str(time.time())
        msg = self._packer.pack({
            'name': name, 'args': args, 'queue_id': queue_id
        })
        self._queue_in.put(msg)

        if self._stdin:
            try:
                while not self._queue_in.empty():
                    self._stdin.write(self._queue_in.get_nowait())
            except BrokenPipeError:
                error_tb(self._vim, 'Crash in child process')
                error(self._vim, 'stderr=' + str(self._proc.read_error()))
                self._hnd = None
        return queue_id

    def _get(self, queue_id: int) -> typing.List[typing.Any]:
        if not self._hnd:
            return []

        while not self._queue_err.empty():
            self._print_error(self._queue_err.get_nowait())

        outs = []
        while not self._queue_out.empty():
            outs.append(self._queue_out.get_nowait())
        try:
            return [x for x in outs if x['queue_id'] == queue_id]
        except TypeError:
            error_tb(self._vim,
                     '"stdout" seems contaminated by sources. '
                     '"stdout" is used for RPC; Please pipe or discard')
            return []
