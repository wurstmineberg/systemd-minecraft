#!/usr/bin/env python3

"""Systemd init script for one or more vanilla Minecraft servers.

Usage:
  minecraft [options] start | stop | restart | status | backup [<world>...]
  minecraft [options] update [(<world> | --all | --main) [snapshot <snapshot-id> | <version>]]
  minecraft [options] command (<world> | --all | --main) <command>...
  minecraft -h | --help
  minecraft --version

Options:
  -h, --help         Print this message and exit.
  --all              Apply the action to all configured worlds.
  --config=<config>  Path to the config file [default: /opt/wurstmineberg/config/systemd-minecraft.json].
  --main             Apply the action to the main world. This is the default.
  --version          Print version info and exit.
"""

import sys

sys.path.append('/opt/py')

import contextlib
from datetime import date
from datetime import datetime
from docopt import docopt
from datetime import time as dtime
import errno
import gzip
import json
import loops
import more_itertools
import os
import os.path
import pathlib
import pwd
import re
import requests
import socket
import subprocess
import time
from datetime import timedelta
from datetime import timezone
import urllib.parse

def parse_version_string():
    path = pathlib.Path(__file__).resolve().parent # go up one level, from repo/minecraft.py to repo, where README.md is located
    version = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=str(path)).decode('utf-8').strip('\n')
    if version == 'master':
        with (path / 'README.md').open() as readme:
            for line in readme.read().splitlines():
                if line.startswith('This is version '):
                    return line.split(' ')[3]
    return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(path)).decode('utf-8').strip('\n')

__version__ = str(parse_version_string())

DEFAULT_CONFIG = {
    'javaOptions': {
        'cpuCount': 1,
        'jarOptions': ['nogui'],
        'maxHeap': 4096,
        'minHeap': 2048
    },
    'mainWorld': 'wurstmineberg',
    'paths': {
        'assets': '/var/www/wurstmineberg.de/assets/serverstatus',
        'backup': '/opt/wurstmineberg/backup',
        'backupWeb': '/var/www/wurstmineberg.de/assets/latestbackup.tar.gz',
        'clientVersions': '/opt/wurstmineberg/home/.minecraft/versions',
        'commandLog': '/opt/wurstmineberg/log/commands.log',
        'home': '/opt/wurstmineberg',
        'httpDocs': '/var/www/wurstmineberg.de',
        'jar': '/opt/wurstmineberg/jar',
        'log': '/opt/wurstmineberg/log',
        'logConfig': 'log4j2.xml',
        'people': '/opt/wurstmineberg/config/people.json',
        'service': 'minecraft_server.jar',
        'sockets': '/var/local/wurstmineberg/minecraft_commands',
        'worlds': '/opt/wurstmineberg/world'
    },
    'serviceName': 'minecraft_server.jar',
    'startTimeout': 60,
    'whitelist': {
        'additional': [],
        'enabled': True,
        'ignorePeople': False
    },
    'worlds': {
        'wurstmineberg': {
            'enabled': True
        }
    }
}

CONFIG_FILE = pathlib.Path('/opt/wurstmineberg/config/systemd-minecraft.json')

if __name__ == '__main__':
    arguments = docopt(__doc__, version='Minecraft init script ' + __version__)
    CONFIG_FILE = pathlib.Path(arguments['--config'])

CONFIG = DEFAULT_CONFIG.copy()
with contextlib.suppress(FileNotFoundError):
    with CONFIG_FILE.open() as config_file:
        CONFIG.update(json.load(config_file))
for key in CONFIG['paths']:
    CONFIG['paths'][key] = pathlib.Path(CONFIG['paths'][key])

class World:
    def __init__(self, name=None):
        if name is None:
            name = CONFIG['mainWorld']
        if name in CONFIG['worlds']:
            self.name = name
        else:
            raise ValueError('no such world')

    def __repr__(self):
        return 'minecraft.World({!r})'.format(self.name)

    def __str__(self):
        return self.name

    def backup(self, announce=False, reply=print, path=None):
        """Back up the Minecraft world.

        Optional arguments:
        announce -- Whether to announce in-game that saves are being disabled/reenabled.
        reply -- This function is called with human-readable progress updates. Defaults to the built-in print function.
        path -- Where the backup will be saved. The file extension .tar.gz will be appended automatically. Defaults to a file with the world name and a timestamp in the backups directory.
        """
        self.save_off(announce=announce, reply=reply)
        if path is None:
            now = datetime.utcnow().strftime('%Y-%m-%d_%Hh%M')
            path = str(CONFIG['paths']['backup'] / '{}_{}'.format(self.name, now))
        backup_file = pathlib.Path(path + '.tar')
        reply('Backing up minecraft world...')
        subprocess.call(['tar', '-C', str(self.path), '-cf', backup_file, self.name]) # tar the world directory (e.g. /opt/wurstmineberg/world/wurstmineberg/wurstmineberg)
        subprocess.call(['rsync', '-av', '--delete', str(self.path / self.name) + '/', str(CONFIG['paths']['backup'] / self.name / 'latest')])
        self.save_on(announce=announce, reply=reply)
        reply('Compressing backup...')
        subprocess.call(['gzip', '-f', str(backup_file)])
        backup_file = pathlib.Path(str(backup_file) + '.gz')
        if self.is_main():
            reply('Symlinking to httpdocs...')
            if CONFIG['paths']['backupweb'].is_symlink():
                CONFIG['paths']['backupweb'].unlink()
            CONFIG['paths']['backupweb'].symlink_to(backup_file)
        reply('Done.')

    def command(self, cmd, args=[], block=False):
        """Send a command to the server.

        Required arguments:
        cmd -- The command name.

        Optional arguments:
        args -- A list of arguments passed to the command.
        block -- If True and the server is not running, tries to wait until the server is running to send the command. Defaults to False.

        Raises:
        MinecraftServerNotRunningError -- If the world is not running and block is set to False.
        socket.error -- If the world is running but the command socket is disconnected.
        """
        def file_len(file): #FROM http://stackoverflow.com/questions/845058/how-to-get-line-count-cheaply-in-python
            for i, l in enumerate(file):
                pass
            return i + 1

        if (not block) and not self.status():
            raise MinecraftServerNotRunningError('')
        try:
            with (self.path / 'logs' / 'latest.log').open() as logfile:
                pre_log_len = file_len(logfile)
        except (IOError, OSError):
            pre_log_len = 0
        except:
            pre_log_len = None
        cmd += (' ' + ' '.join(str(arg) for arg in args)) if len(args) else ''
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.socket_path))
            s.sendall(cmd.encode('utf-8') + b'\n')
        if pre_log_len is None:
            return None
        time.sleep(0.2) # assumes that the command will run and print to the log file in less than .2 seconds
        return _command_output('tail', ['-n', '+' + str(pre_log_len + 1), str(self.path / 'logs' / 'latest.log')])

    @property
    def config(self):
        ret = {
            'enabled': CONFIG['worlds'][self.name].get('enabled', False),
            'javaOptions': CONFIG['javaOptions'].copy(),
            'whitelist': CONFIG['whitelist'].copy()
        }
        ret['javaOptions'].update(CONFIG['worlds'][self.name].get('javaOptions', {}))
        ret['whitelist'].update(CONFIG['worlds'][self.name].get('whitelist', {}))
        return ret

    def is_main(self):
        return self.name == CONFIG['mainWorld']

    def iter_update(self, version=None, snapshot=False, reply=print, log_path=None, override=False):
        """Download a different version of Minecraft and restart the world if it is running. Returns a generator where each iteration performs one step of the update process.

        Optional arguments:
        version -- If given, a version with this name will be downloaded. By default, the newest available version is downloaded.
        snapshot -- If version is given, this specifies whether the version is a development version. If no version is given, this specifies whether the newest stable version or the newest development version should be downloaded. Defaults to False.
        reply -- This function is called several times with a string argument representing update progress. Defaults to the built-in print function.
        log_path -- This is passed to the stop and start functions if the server is stopped before the update.
        override -- If this is True and the server jar for the target version already exists, it will be deleted and redownloaded. Defaults to False.
        """
        versions_json = requests.get('https://s3.amazonaws.com/Minecraft.Download/versions/versions.json').json()
        if version is None: # try to dynamically get the latest version number from assets
            version = versions_json['latest']['snapshot' if snapshot else 'release']
        elif snapshot:
            version = datetime.utcnow().strftime('%yw%V') + version
        for version_dict in versions_json['versions']:
            if version_dict.get('id') == version:
                snapshot = version_dict.get('type') == 'snapshot'
                break
        else:
            reply('Minecraft version not found in assets, will try downloading anyway')
            version_dict = None
        version_text = 'Minecraft {} {}'.format('snapshot' if snapshot else 'version', version)
        yield {
            'version': version,
            'is_snapshot': snapshot,
            'version_text': version_text
        }
        jar_path = CONFIG['paths']['jar'] / 'minecraft_server.{}.jar'.format(version)
        if override and jar_path.exists():
            jar_path.unlink()
        if not jar_path.exists():
            _download('https://s3.amazonaws.com/Minecraft.Download/versions/{0}/minecraft_server.{0}.jar'.format(version), local_filename=str(jar_path))
        if 'clientVersions' in CONFIG['paths']:
            with contextlib.suppress(FileExistsError):
                (CONFIG['paths'] / version).mkdir(parents=True)
            _download('https://s3.amazonaws.com/Minecraft.Download/versions/{0}/{0}.jar'.format(version), local_filename=str(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version)))
        yield 'Download finished. Stopping server...'
        self.say('Server will be upgrading to ' + version_text + ' and therefore restart')
        time.sleep(5)
        was_running = self.status()
        self.stop(reply=reply, log_path=log_path)
        yield 'Server stopped. Installing new server...'
        if self.service_path.exists():
            self.service_path.unlink()
        self.service_path.symlink_to(CONFIG['paths']['jar'] / 'minecraft_server.{}.jar'.format(version))
        client_jar_path = CONFIG['paths']['home'] / 'home' / 'client.jar'
        if self.is_main():
            if client_jar_path.exists():
                client_jar_path.unlink()
            client_jar_path.symlink_to(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version))
            try:
                subprocess.check_call(['mapcrafter_textures.py', str(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version)), '/usr/local/share/mapcrafter/textures'])
            except Exception as e:
                reply('Error while updating mapcrafter textures: {}'.format(e))
        if was_running:
            self.start(reply=reply, start_message='Server updated. Restarting...', log_path=log_path)

    @property
    def path(self):
        return CONFIG['paths']['worlds'] / self.name

    def restart(self, *args, **kwargs):
        reply = kwargs.get('reply', print)
        if not self.stop(*args, **kwargs):
            return False
        kwargs['start_message'] = kwargs.get('start_message', 'Server stopped. Restarting...')
        return self.start(*args, **kwargs)

    def save_off(self, announce=True, reply=print):
        """Turn off automatic world saves, then force-save once.

        Optional arguments:
        announce -- Whether to announce in-game that saves are being disabled.
        reply -- This function is called with human-readable progress updates. Defaults to the built-in print function.
        """
        if self.status():
            reply('Minecraft is running... suspending saves')
            if announce:
                self.say('Server backup starting. Server going readonly...')
            self.command('save-off')
            self.command('save-all')
            time.sleep(10)
            os.sync()
        else:
            reply('Minecraft is not running. Not suspending saves.')

    def save_on(self, announce=True, reply=print):
        """Enable automatic world saves.

        Optional arguments:
        announce -- Whether to announce in-game that saves are being enabled.
        reply -- This function is called with human-readable progress updates. Defaults to the built-in print function.
        """
        if self.status():
            reply('Minecraft is running... re-enabling saves')
            self.command('save-on')
            if announce:
                self.say('Server backup ended. Server going readwrite...')
        else:
            reply('Minecraft is not running. Not resuming saves.')

    def say(self, message, prefix=True):
        """Broadcast a message in the world's in-game chat. This is a simple wrapper around the /say and /tellraw commands.

        Required arguments:
        message -- The message to display in chat.

        Optional arguments:
        prefix -- If False, uses /tellraw instead of /say to send a message without the [server] prefix. Defaults to True.
        """
        if prefix:
            self.command('say', [message])
        else:
            self.tellraw(message)

    @property
    def service_path(self):
        return self.path / CONFIG['paths']['service']

    @property
    def socket_path(self):
        return CONFIG['paths']['sockets'] / self.name

    def start(self, *args, **kwargs):
        def feed_commands(java_popen):
            loop_var = True
            with socket.socket(socket.AF_UNIX) as s:
                if self.socket_path.exists():
                    self.socket_path.unlink()
                s.bind(str(self.socket_path))
                while loop_var and self.socket_path.exists():
                    str_buffer = ''
                    s.listen(1)
                    c, _ = s.accept()
                    while loop_var:
                        data = c.recv(1024)
                        if not data:
                            break
                        lines = (str_buffer + data.decode('utf-8')).split('\n')
                        for line in lines[:-1]:
                            if line == 'stop':
                                loop_var = False
                                break
                            java_popen.stdin.write(line.encode('utf-8') + b'\n')
                        str_buffer = lines[-1]
                    c.close()
                    if java_popen.poll() is not None:
                        return
            java_popen.communicate(input=b'stop\n')
            if self.socket_path.exists():
                self.socket_path.unlink()

        invocation = ['java', '-Xmx' + str(self.config['javaOptions']['maxHeap']) + 'M', '-Xms' + str(self.config['javaOptions']['minHeap']) + 'M', '-XX:+UseConcMarkSweepGC', '-XX:+CMSIncrementalMode', '-XX:+CMSIncrementalPacing', '-XX:ParallelGCThreads=' + str(self.config['javaOptions']['cpuCount']), '-XX:+AggressiveOpts', '-Dlog4j.configurationFile=' + str(CONFIG['paths']['logConfig']), '-jar', str(CONFIG['paths']['service'])] + self.config['javaOptions']['jarOptions']
        reply = kwargs.get('reply', print)
        if self.status():
            reply('Server is already running!')
            return False
        reply(kwargs.get('start_message', 'starting Minecraft server...'))
        if not self.socket_path.parent.exists():
            # make sure the command sockets directory exists
            self.socket_path.parent.mkdir(parents=True)
        java_popen = subprocess.Popen(invocation, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(self.path)) # start the java process
        for line in loops.timeout_total(java_popen.stdout, timedelta(seconds=CONFIG['startTimeout'])): # wait until the timeout has been exceeded...
            if re.match(regexes.full_timestamp + ' [Server thread/INFO]: Done \\([0-9]+.[0-9]+s\\)!', line.decode('utf-8')): # ...or the server has finished starting
                break
        _fork(feed_commands, java_popen) # feed commands from the socket to java
        _fork(more_itertools.consume, java_popen.stdout) # consume java stdout to prevent deadlocking
        if kwargs.get('log_path'):
            with (kwargs['log_path'].open('a') if hasattr(kwargs['log_path'], 'open') else open(kwargs['log_path'], 'a')) as logins_log:
                ver = self.version()
                print(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + (' @restart' if ver is None else ' @start ' + ver), file=logins_log) # logs in UTC
        return self.status()

    def status(self):
        return self.socket_path.exists()

    def stop(self, *args, **kwargs):
        reply = kwargs.get('reply', print)
        if self.status():
            reply('SERVER SHUTTING DOWN IN 10 SECONDS. Saving map...')
            notice = kwargs.get('notice', 'SERVER SHUTTING DOWN IN 10 SECONDS. Saving map...')
            if notice is not None:
                self.say(str(notice))
            self.command('save-all')
            time.sleep(10)
            self.command('stop')
            time.sleep(7)
            for _ in range(12):
                if self.status():
                    time.sleep(5)
                    continue
                else:
                    break
            else:
                reply('The server could not be stopped! D:')
                if self.socket_path.exists():
                    self.socket_path.unlink()
                return False #TODO change this or improve status check
            if kwargs.get('log_path'):
                with (kwargs['log_path'].open('a') if hasattr(kwargs['log_path'], 'open') else open(kwargs['log_path'], 'a')) as logins_log:
                    print(datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' @stop', file=logins_log) # logs in UTC
        else:
            reply('Minecraft server was not running.')
        if self.socket_path.exists():
            self.socket_path.unlink()
        return not self.status()

    def tellraw(self, message_dict, player='@a'):
        if isinstance(message_dict, str):
            message_dict = {'text': message_dict}
        elif isinstance(message_dict, list):
            message_dict = {'text': '', 'extra': message_dict}
        self.command('tellraw', [player, json.dumps(message_dict)])

    def update(self, version=None, snapshot=False, reply=print, log_path=None, override=False):
        """Download a different version of Minecraft and restart the server if it is running.

        Optional arguments:
        version -- If given, a version with this name will be downloaded. By default, the newest available version is downloaded.
        snapshot -- If version is given, this specifies whether the version is a development version. If no version is given, this specifies whether the newest stable version or the newest development version should be downloaded. Defaults to False.
        reply -- This function is called several times with a string argument representing update progress. Defaults to the built-in print function.
        log_path -- This is passed to the stop function if the server is stopped before the update.
        override -- If this is True and the server jar for the target version already exists, it will be deleted and redownloaded. Defaults to False.
        """
        update_iterator = self.iter_update(version=version, snapshot=snapshot, reply=reply, log_path=log_path, override=override)
        version_dict = next(update_iterator)
        reply('Downloading ' + version_dict['version_text'])
        for message in update_iterator:
            reply(message)
        return version_dict['version'], version_dict['is_snapshot'], version_dict['version_text']

    def update_whitelist(self, people_file=None):
        # get wanted whitelist from people file
        if people_file is None:
            people_file = CONFIG['paths']['people']
        whitelist = []
        additional = self.config['whitelist']['additional']
        if not self.config['whitelist']['ignorePeople']:
            with people_file.open() as people_fobj:
                people = json.load(people_fobj)['people']
                for person in people:
                    if not ('minecraft' in person or 'minecraftUUID' in person):
                        continue
                    if person.get('status', 'later') not in ['founding', 'later', 'postfreeze']:
                        continue
                    if person.get('minecraftUUID'):
                        uuid = person['minecraftUUID'] if isinstance(person['minecraftUUID'], str) else format(person['minecraftUUID'], 'x')
                        if 'minecraft' in person:
                            name = person['minecraft']
                        else:
                            name = requests.get('https://api.mojang.com/user/profiles/{}/names'.format(uuid)).json()[-1]['name']
                    else:
                        response_json = requests.get('https://api.mojang.com/users/profiles/minecraft/{}'.format(person['minecraft'])).json()
                        uuid = response_json['id']
                        name = response_json['name']
                    if '-' not in uuid:
                        uuid = uuid[:8] + '-' + uuid[8:12] + '-' + uuid[12:16] + '-' + uuid[16:20] + '-' + uuid[20:]
                    whitelist.append({
                        'name': name,
                        'uuid': uuid
                    })
        # write whitelist
        whitelist_path = self.path / 'whitelist.json'
        with whitelist_path.open('a'):
            os.utime(str(whitelist_path), None) # touch the file
        with whitelist_path.open('w') as whitelist_json:
            json.dump(whitelist, whitelist_json, sort_keys=True, indent=4, separators=(',', ': '))
        # apply changes to whitelist files
        self.command('whitelist', ['reload'])
        # add people with unknown UUIDs to new whitelist using the command
        for name in additional:
            self.command('whitelist', ['add', name])
        # update people file
        try:
            import lazyjson
        except ImportError:
            return
        try:
            with whitelist_path.open() as whitelist_json:
                whitelist = json.load(whitelist_json)
        except ValueError:
            return
        people = lazyjson.File(CONFIG['paths']['people'])
        for whitelist_entry in whitelist:
            for person in people['people']:
                if person.get('minecraftUUID') == whitelist_entry['uuid']:
                    if 'minecraft' in person and person.get('minecraft') != whitelist_entry['name'] and person.get('minecraft') not in person.get('minecraft_previous', []):
                        if 'minecraft_previous' in person:
                            person['minecraft_previous'].append(person['minecraft'])
                        else:
                            person['minecraft_previous'] = [person['minecraft']]
                    person['minecraft'] = whitelist_entry['name']
                elif person.get('minecraft') == whitelist_entry['name'] and 'minecraftUUID' not in person:
                    person['minecraftUUID'] = whitelist_entry['uuid']

    def version(self):
        """Returns the version of Minecraft the world is currently configured to run.
        """
        return self.service_path.resolve().stem[len('minecraft_server.'):]

class MinecraftServerNotRunningError(Exception):
    pass

class regexes:
    full_timestamp = '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'
    player = '[A-Za-z0-9_]{1,16}'
    prefix = '\\[(.+?)\\]:?'
    timestamp = '\\[[0-9]{2}:[0-9]{2}:[0-9]{2}\\]'

    @staticmethod
    def strptime(base_date, timestamp, tzinfo=timezone.utc):
        # return aware datetime object from log timestamp
        if isinstance(base_date, str):
            offset = tzinfo.utcoffset(datetime.now())
            if offset < timedelta():
                prefix = '-'
                offset *= -1
            else:
                prefix = '+'
            timezone_string = prefix + str(offset // timedelta(hours=1)).rjust(2, '0') + str(offset // timedelta(minutes=1) % 60).rjust(2, '0')
            return datetime.strptime(base_date + timestamp + timezone_string, '%Y-%m-%d[%H:%M:%S]%z')
        hour = int(timestamp[1:3])
        minute = int(timestamp[4:6])
        second = int(timestamp[7:9])
        return datetime.combine(base_date, dtime(hour=hour, minute=minute, second=second, tzinfo=tzinfo))

def _command_output(cmd, args=[]):
    p = subprocess.Popen([cmd] + args, stdout=subprocess.PIPE)
    out, _ = p.communicate()
    return out.decode('utf-8')

def _download(url, local_filename=None): #FROM http://stackoverflow.com/a/16696317/667338
    if local_filename is None:
        local_filename = url.split('#')[0].split('?')[0].split('/')[-1]
        if local_filename == '':
            raise ValueError('no local filename specified')
    r = requests.get(url, stream=True)
    with open(local_filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk: # filter out keep-alive new chunks
                f.write(chunk, flush=True)

def _fork(func, *args, **kwargs):
    #FROM http://stackoverflow.com/a/6011298/667338
    # do the UNIX double-fork magic, see Stevens' "Advanced Programming in the UNIX Environment" for details (ISBN 0201563177)
    try:
        pid = os.fork()
        if pid > 0:
            # parent process, return and keep running
            return
    except OSError as e:
        print('fork #1 failed: %d (%s)' % (e.errno, e.strerror), file=sys.stderr)
        sys.exit(1)
    os.setsid()
    # do second fork
    try:
        pid = os.fork()
        if pid > 0:
            # exit from second parent
            sys.exit(0)
    except OSError as e:
        print('fork #2 failed: %d (%s)' % (e.errno, e.strerror), file=sys.stderr)
        sys.exit(1)
    with open(os.path.devnull) as devnull:
        sys.stdin = devnull
        sys.stdout = devnull
        func(*args, **kwargs) # do stuff
        os._exit(os.EX_OK) # all done

def worlds():
    """Iterates over all configured worlds."""
    for world_name in CONFIG['worlds'].keys():
        yield World(world_name)

if __name__ == '__main__':
    try:
        wurstmineberg_user = pwd.getpwnam('wurstmineberg')
    except:
        sys.exit('[!!!!] User ‘wurstmineberg’ does not exist!')
    if os.geteuid() != wurstmineberg_user.pw_uid:
        sys.exit('[!!!!] Only the user ‘wurstmineberg’ may use this program!')
    if arguments['--all']:
        selected_worlds = worlds()
    elif arguments['<world>']:
        selected_worlds = arguments['<world>']
    else:
        selected_worlds = [World()]
    if arguments['start']:
        for world in selected_worlds:
            if world.config['enabled']:
                if not world.start():
                    print('[FAIL] Error! Could not start the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is now running.')
    elif arguments['stop']:
        for world in selected_worlds:
            if not world.stop():
                print('[FAIL] Error! Could not stop the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is stopped.')
    elif arguments['restart']:
        for world in selected_worlds:
            if not world.restart():
                print('[FAIL] Error! Could not restart the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is now running.')
    elif arguments['update']:
        for world in selected_worlds:
            if arguments['snapshot']:
                world.update(arguments['<snapshot-id>'], snapshot=True)
            elif arguments['VERSION']:
                world.update(arguments['<snapshot-id>'])
            else:
                world.update(snapshot=True)
    elif arguments['backup']:
        for world in selected_worlds:
            world.backup()
    elif arguments['status']:
        statuses = {world: world.status() for world in selected_worlds}
        for world, world_status in statuses.items():
            print('[info] The {} world (Minecraft {}) is{} running'.format(world, world.version(), '' if world_status else ' not'))
        if not any(statuses.values()):
            sys.exit(1)
    elif arguments['command']:
        selected_worlds = list(selected_worlds)
        for world in selected_worlds:
            if len(selected_worlds) > 1:
                print('[info] running command on {} world'.format(world))
            cmdlog = world.command(arguments['<command>'][0], arguments['<command>'][1:])
            for line in cmdlog.splitlines():
                print(str(line))
