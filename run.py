#!/usr/bin/env python
# -*- coding: UTF-8 -*-

from __future__ import print_function

import os
import sys
import logging
import time

from shutil import disk_usage
from tempfile import TemporaryFile
from traceback import print_exc
from typing import NoReturn

try:
    from importlib.util import find_spec
    from pathlib import Path
except ImportError:
    pass


# Setup initial loggers

tmpfile = TemporaryFile('w+', encoding='utf8')
log = logging.getLogger('launcher')
log.setLevel(logging.DEBUG)

fmt = logging.Formatter(
    fmt='[{asctime}] [{levelname}] [{name}] {message}',
    datefmt='%H:%M:%S',
    style='{',
)

sh = logging.StreamHandler(stream=sys.stdout)
sh.setFormatter(fmt)
sh.setLevel(logging.INFO)
log.addHandler(sh)

tfh = logging.StreamHandler(stream=tmpfile)
tfh.setFormatter(fmt)
tfh.setLevel(logging.DEBUG)
log.addHandler(tfh)


def finalize_logging() -> None:
    filename = os.path.join('logs', 'lastest.log')
    log.debug('filename {}'.format(filename))
    log_existed = False
    if (os.path.isfile(filename)):
        today = time.localtime()[:3]
        last = time.localtime(os.path.getmtime(filename))
        if (last[:3] == today):
            log_existed = True
        else:
            new_name = os.path.join(
                'logs', time.strftime('%Y%m%d.log', last)
            )
            os.rename(filename, new_name)

    with open(filename, 'a', encoding='utf8') as file:
        tmpfile.seek(0)
        if (log_existed):
            file.write('\n\n')
            file.write(' BOT RE-START RUNNING '.center(72, '#'))
            file.write('\n\n\n')
        file.write(tmpfile.read())
        tmpfile.close()

        file.write('\n')
        file.write(' PRE-RUN SANITY CHECKS PASSED '.center(72, '#'))
        file.write('\n\n')

    global tfh
    log.removeHandler(tfh)
    del tfh

    fh = logging.FileHandler(filename, mode='a')
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)

    sh.setLevel(logging.INFO)

    dlog = logging.getLogger('nextcord')
    dlh = logging.StreamHandler(stream=sys.stdout)
    dlh.terminator = ''
    try:
        dlh.setFormatter(logging.Formatter('.'))
    except ValueError:
        dlh.setFormatter(logging.Formatter('.', validate=False))
        # pylint: disable=unexpected-keyword-arg
    dlog.addHandler(dlh)


########################################################################


def bugger_off(msg='Press enter to continue . . .', code=1) -> NoReturn:
    input(msg)
    sys.exit(code)


# TODO: all of this
def sanity_checks(optional=True) -> None:
    log.info('Starting sanity checks')

    ########## Required ##########

    # Make sure we're on Python 3.10+
    req_ensure_py310()

    # Fix windows encoding fuckery
    req_ensure_encoding()

    # Make sure we're in a writeable env
    req_ensure_env()

    # Make our folders if needed
    req_ensure_folders()

    # For rewrite only
    req_check_deps()

    log.info('Required checks passed.')

    ########## Optional ##########
    if (not optional):
        return

    # Check disk usage
    opt_check_disk_space()

    log.info('Optional checks passed.')


def req_ensure_py310() -> None:
    log.info('Checking for Python 3.10+')

    if (sys.version_info < (3, 10)):
        log.critical(
            'Python 3.10 or higher is required. But this version is {}'
            .format(sys.version.split()[0])
        )
        bugger_off()


def req_ensure_encoding() -> None:
    log.info('Checking console encoding')

    if (
        sys.platform.startswith('win')
        or sys.stdout.encoding.replace('-', '').lower() != 'utf8'
    ):
        log.info('Setting console encoding to UTF-8')

        from io import TextIOWrapper

        sys.stdout = TextIOWrapper(
            sys.stdout.detach(), encoding='utf8', line_buffering=True
        )
        # only slightly evil
        sys.__stdout__ = sh.stream = sys.stdout

        if (os.environ.get('PYCHARM_HOSTED', None) not in (None, '0')):
            log.info('Enabling colors in pycharm pseudoconsole')
            sys.stdout.isatty = lambda: True


def req_ensure_env() -> None:
    log.info('Ensuring we\'re in the right environment')

    try:
        assert os.path.isdir('config'), 'folder `config` not found'
        assert os.path.isdir('musicbot'), 'folder `musicbot` not found'
        assert os.path.isfile('musicbot/__init__.py'), (
            'musicbot folder is not a Python module'
        )
        assert find_spec('musicbot'), (
            'musicbot module is not importable'
        )
    except AssertionError as e:
        log.critical('Failed environment check, {}'.format(e))
        bugger_off()

    try:
        os.mkdir('musicbot-test-folder')
    except Exception:
        log.critical(
            'Current working directory does not seem to be writable'
        )
        log.critical('Please move the bot to a folder that is writable')
        bugger_off()
    finally:
        os.rmdir('musicbot-test-folder')

    if (sys.platform.startswith('win')):
        log.info('Adding local `bins/` folder to path')
        os.environ['PATH'] += ';' + os.path.abspath('bin')
        sys.path.append(os.path.abspath('bin'))  # might as well


def req_ensure_folders() -> None:
    Path('logs').mkdir(exist_ok=True)
    Path('data').mkdir(exist_ok=True)


def req_check_deps() -> None:
    try:
        import nextcord

        if (nextcord.version_info.major < 1):
            log.critical(
                'This version of MusicBot requires a newer version of '
                'pycord. Your version is {0}.'
                .format(nextcord.__version__)
            )
            bugger_off()

    except ImportError:
        # if we can't import nextcord, an error will be thrown later
        # down the line anyway
        pass


def opt_check_disk_space(warnlimit_mb=200) -> None:
    if (disk_usage('.').free < warnlimit_mb * 1024 * 2):
        log.warning(
            'Less than {0}MB of free space remains on this device'
            .format(warnlimit_mb)
        )


########################################################################


def pyexec(pycom, *args, pycom2=None) -> NoReturn:
    pycom2 = pycom2 or pycom
    os.execlp(pycom, pycom2, *args)


def main() -> None:
    # TODO: *actual* argparsing

    if ('--no-checks' not in sys.argv):
        sanity_checks()

    finalize_logging()

    import asyncio

    if (sys.platform == 'win32'):
        loop = asyncio.ProactorEventLoop()  # needed for subprocesses
        asyncio.set_event_loop(loop)

    tryagain = True

    loops = 0
    max_wait_time = 60

    while (tryagain):
        # Maybe I need to try to import stuff first, then actually
        # import stuff
        # It'd save me a lot of pain with all that awful exception
        # type checking

        bot = None
        try:
            from musicbot import MusicBot

            bot = MusicBot()

            sh.terminator = ''
            sh.terminator = '\n'

            bot.run()

        except SyntaxError:
            log.exception(
                'Syntax error (this is a bug, not your fault)'
            )
            break

        except ImportError:
            log.exception('ImportError, exiting.')
            break

        except Exception as e:
            match (e.__class__.__name__):
                case 'HelpfulError':
                    log.info(e.message)
                case 'TerminateSignal':
                    log.warning('Bot has been stopped.')
                    break
                case 'RestartSignal':
                    log.warning('Bot restart.')
                case _:
                    log.exception('Error starting bot')

        finally:
            if (not bot or not bot.init_ok):
                if (any(sys.exc_info())):
                    # How to log this without redundant messages...
                    print_exc()
                break

            asyncio.set_event_loop(asyncio.new_event_loop())
            loops += 1

        sleeptime = min(loops * 2, max_wait_time)
        if (sleeptime):
            log.info('Restarting in {} seconds...'.format(loops * 2))
            time.sleep(sleeptime)

        del MusicBot

    print()
    log.info('All done.')


if (__name__ == '__main__'):
    main()
