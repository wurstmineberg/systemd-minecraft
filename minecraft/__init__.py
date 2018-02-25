#!/usr/bin/env python3

"""Systemd init script for one or more vanilla Minecraft servers.

Usage:
  minecraft [options] (start | stop | kill | restart | status | backup) [<world>...]
  minecraft [options] (update | revert) [<world> [snapshot <snapshot-id> | <version>]]
  minecraft [options] saves (on | off) [<world>...]
  minecraft [options] update-all [snapshot <snapshot-id> | <version>]
  minecraft [options] command <world> [--] <command>...
  minecraft -h | --help
  minecraft --version

Options:
  -h, --help         Print this message and exit.
  --all              Apply the action to all configured worlds.
  --config=<config>  Path to the config file [default: /opt/wurstmineberg/config/systemd-minecraft.json].
  --enabled          Apply the action to all enabled worlds. This option is intended to be used only by the service file, to automatically start all enabled worlds on boot.
  --main             Apply the action to the main world. This is the default.
  --no-backup        Don't back up the world(s) before updating/reverting.
  --version          Print version info and exit.
"""

import sys

sys.path.append('/opt/py')

import contextlib
import datetime
import docopt
import errno
import gzip
import json
import loops
import mcrcon
import more_itertools
import os
import signal
import os.path
import pathlib
import pwd
import re
import requests
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse


from minecraft.version import __version__

from wmb import get_config, from_assets
CONFIG = get_config("systemd-minecraft", base = from_assets(__file__))

if __name__ == '__main__':
    arguments = docopt.docopt(__doc__, version='Minecraft init script {}'.format(__version__))

for key in CONFIG['paths']:
    if isinstance(CONFIG['paths'][key], str):
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

    def backup(self, announce=False, reply=print, path=None, *, copy_to_latest=None):
        """Back up the Minecraft world.

        Optional arguments:
        announce -- Whether to announce in-game that saves are being disabled/reenabled. Defaults to False.
        reply -- This function is called with human-readable progress updates. Defaults to the built-in print function.
        path -- Where the backup will be saved. The file extension .tar.gz will be appended automatically. Defaults to a file with the world name and a timestamp in the backups directory.

        Keyword-only arguments:
        copy_to_latest -- Whether to create or update the copy of the world directory at backups/latest. Defaults to True for the main world and to False for all other worlds.

        Returns:
        A pathlib.Path representing the gzipped backup tarball.
        """
        if copy_to_latest is None:
            copy_to_latest = self.is_main
        self.save_off(announce=announce, reply=reply)
        if path is None:
            path = str(self.backup_path / '{}_{:%Y-%m-%d_%Hh%M}'.format(self.name, datetime.datetime.utcnow()))
        else:
            path = str(path)
        backup_file = pathlib.Path(path + '.tar')
        reply('Backing up minecraft world...')
        if not backup_file.parent.exists():
            # make sure the backup directory exists
            backup_file.parent.mkdir(parents=True)
        subprocess.call(['tar', '-C', str(self.path), '-cf', str(backup_file), self.world_path.name]) # tar the world directory (e.g. /opt/wurstmineberg/world/wurstmineberg/world or /opt/wurstmineberg/world/wurstmineberg/wurstmineberg)
        if copy_to_latest:
            # make a copy of the world directory for the main world to be used by map rendering
            subprocess.call(['rsync', '-av', '--delete', str(self.world_path) + '/', str(self.backup_path / 'latest')])
        self.save_on(announce=announce, reply=reply)
        reply('Compressing backup...')
        subprocess.call(['gzip', '-f', str(backup_file)])
        backup_file = pathlib.Path(str(backup_file) + '.gz')
        if self.is_main and CONFIG['paths']['backupWeb'] is not None:
            reply('Symlinking to httpdocs...')
            if CONFIG['paths']['backupWeb'].is_symlink():
                CONFIG['paths']['backupWeb'].unlink()
            CONFIG['paths']['backupWeb'].symlink_to(backup_file)
        reply('Done.')
        return backup_file

    @property
    def backup_path(self):
        return CONFIG['paths']['backup'] / self.name

    def command(self, cmd, args=[], block=False):
        """Send a command to the server.

        Required arguments:
        cmd -- The command name.

        Optional arguments:
        args -- A list of arguments passed to the command.
        block -- If True and the server is not running, tries to wait until the server is running to send the command. Defaults to False.

        Raises:
        MinecraftServerNotRunningError -- If the world is not running and block is set to False.
        socket.error -- If the world is running but the RCON connection failed.
        """
        while not self.status():
            if block:
                time.sleep(1)
            else:
                raise MinecraftServerNotRunningError('')

        cmd += (' ' + ' '.join(str(arg) for arg in args)) if len(args) else ''

        rcon = mcrcon.MCRcon()
        rcon.connect('localhost', self.config['rconPort'], self.config['rconPassword'])
        return rcon.command(cmd)

    def cleanup(self, reply=print):
        if self.pidfile_path.exists():
            reply("Removing PID file...")
            self.pidfile_path.unlink()
        if self.socket_path.exists():
            reply("Removing socket file...")
            self.socket_path.unlink()

    @property
    def config(self):
        ret = {
            'customServer': CONFIG['worlds'][self.name].get('customServer', False),
            'enabled': CONFIG['worlds'][self.name].get('enabled', False),
            'javaOptions': CONFIG['javaOptions'].copy(),
            'rconPassword': CONFIG['worlds'][self.name].get('rconPassword'),
            'rconPort': CONFIG['worlds'][self.name].get('rconPort', 25575),
            'whitelist': CONFIG['whitelist'].copy()
        }
        ret['javaOptions'].update(CONFIG['worlds'][self.name].get('javaOptions', {}))
        ret['whitelist'].update(CONFIG['worlds'][self.name].get('whitelist', {}))
        return ret

    @property
    def is_main(self):
        return self.name == CONFIG['mainWorld']

    def iter_update(self, version=None, snapshot=False, *, reply=print, log_path=None, make_backup=True, override=None):
        """Download a different version of Minecraft and restart the world if it is running. Returns a generator where each iteration performs one step of the update process.

        Optional arguments:
        version -- If given, a version with this name will be downloaded. By default, the newest available version is downloaded.
        snapshot -- If version is given, this specifies whether the version is a development version. If no version is given, this specifies whether the newest stable version or the newest development version should be downloaded. Defaults to False.

        Keyword-only arguments:
        log_path -- This is passed to the stop and start functions if the server is stopped before the update.
        make_backup -- Whether to back up the world before updating. Defaults to True.
        override -- If this is true and the server jar for the target version already exists, it will be deleted and redownloaded. Defaults to True if the target version is the current version, False otherwise.
        reply -- This function is called several times with a string argument representing update progress. Defaults to the built-in print function.
        """
        # get version
        versions_json = requests.get('https://launchermeta.mojang.com/mc/game/version_manifest.json').json()
        if version is None: # try to dynamically get the latest version number from assets
            version = versions_json['latest']['snapshot' if snapshot else 'release']
        elif snapshot:
            version = datetime.datetime.utcnow().strftime('%yw%V') + version
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
        old_version = self.version()
        if override is None:
            override = version == old_version
        if version_dict is not None and 'url' in version_dict:
            version_json = requests.get(version_dict['url']).json()
        else:
            version_json = None
        # back up world in background
        if make_backup:
            backup_path = self.backup_path / 'pre-update' / '{}_{:%Y-%m-%d_%Hh%M}_{}_{}'.format(self.name, datetime.datetime.utcnow(), old_version, version)
            backup_thread = threading.Thread(target=self.backup, kwargs={'reply': reply, 'path': backup_path})
            backup_thread.start()
        # get server jar
        jar_path = CONFIG['paths']['jar'] / 'minecraft_server.{}.jar'.format(version)
        if override and jar_path.exists():
            jar_path.unlink()
        if not jar_path.exists():
            _download('https://s3.amazonaws.com/Minecraft.Download/versions/{0}/minecraft_server.{0}.jar'.format(version), local_filename=str(jar_path))
        # get client jar
        if 'clientVersions' in CONFIG['paths']:
            with contextlib.suppress(FileExistsError):
                (CONFIG['paths']['clientVersions'] / version).mkdir(parents=True)
            _download('https://s3.amazonaws.com/Minecraft.Download/versions/{0}/{0}.jar'.format(version) if version_json is None else version_json['downloads']['client']['url'], local_filename=str(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version)))
        # wait for backup to finish
        if make_backup:
            yield 'Download finished. Waiting for backup to finish...'
            backup_thread.join()
            yield 'Backup finished. Stopping server...'
        else:
            yield 'Download finished. Stopping server...'
        # stop server
        was_running = self.status()
        if was_running:
            self.say('Server will be upgrading to ' + version_text + ' and therefore restart')
            time.sleep(5)
            self.stop(reply=reply, log_path=log_path)
        yield 'Server stopped. Installing new server...'
        # install new server
        if self.service_path.exists():
            self.service_path.unlink()
        self.service_path.symlink_to(CONFIG['paths']['jar'] / 'minecraft_server.{}.jar'.format(version))
        client_jar_path = CONFIG['paths']['home'] / 'home' / 'client.jar'
        # update Mapcrafter textures
        if self.is_main:
            if client_jar_path.exists():
                client_jar_path.unlink()
            client_jar_path.symlink_to(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version))
            if CONFIG['updateMapcrafterTextures']:
                try:
                    subprocess.check_call(['mapcrafter_textures.py', str(CONFIG['paths']['clientVersions'] / version / '{}.jar'.format(version)), '/usr/local/share/mapcrafter/textures'])
                except Exception as e:
                    reply('Error while updating mapcrafter textures: {}'.format(e))
        # restart server
        if was_running:
            self.start(reply=reply, start_message='Server updated. Restarting...', log_path=log_path)

    def kill(self, reply=print):
        """Kills a non responding minecraft server using the PID saved in the PID file."""
        with self.pidfile_path.open("r") as pidfile:
            pid = int(pidfile.read())
        reply("World '" + self.name + "': Sending SIGTERM to PID " + str(pid) + " and waiting 60 seconds for shutdown...")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                live = self.pidrunning(pid)
                if not live:
                    reply("Terminated world '" + self.name + "'")
                    break
                time.sleep(1)
            else:
                reply("Could not terminate with SIGQUIT. Sending SIGKILL to PID " + str(pid) + "...")
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            reply("Process does not exist. Cleaning up...")
        finally:
            self.cleanup(reply)
        return not self.status()

    @property
    def path(self):
        return CONFIG['paths']['worlds'] / self.name

    @property
    def pid(self):
        try:
            with self.pidfile_path.open("r") as pidfile:
                return int(pidfile.read())
        except FileNotFoundError:
            return None

    def pidrunning(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but you can't send signals
            return True

    def pidstatus(self, reply=print):
        if self.pidfile_path.exists() and self.pid is not None:
            if self.pidrunning(self.pid):
                return True
        elif self.pidfile_path.exists():
            reply("PID file exists but process is terminated. Cleaning up...")
            self.cleanup(reply)
        return False

    @property
    def pidfile_path(self):
        return CONFIG['paths']['pidfiles'] / (self.name + ".pid")

    def restart(self, *args, **kwargs):
        reply = kwargs.get('reply', print)
        if not self.stop(*args, **kwargs):
            return False
        kwargs['start_message'] = kwargs.get('start_message', 'Server stopped. Restarting...')
        return self.start(*args, **kwargs)

    def revert(self, path_or_version=None, snapshot=False, *, log_path=None, make_backup=True, override=False, reply=print):
        """Revert to a different version of Minecraft and restore a pre-update backup.

        Optional arguments:
        path_or_version -- If given, a pathlib.Path pointing at the backup file to be restored, or the Minecraft version to which to restore. By default, the newest available pre-update backup is restored.
        snapshot -- If true, single-letter Minecraft versions will be expanded to include the current year and week number. Defaults to False.

        Keyword-only arguments:
        log_path -- This is passed to the stop function if the server is stopped before the revert.
        make_backup -- Whether to back up the world before reverting. Defaults to True.
        override -- If this is True and the server jar for the target version already exists, it will be deleted and redownloaded. Defaults to False.
        reply -- This function is called several times with a string argument representing revert progress. Defaults to the built-in print function.
        """
        # determine version and backup path
        if path_or_version is None:
            path = sorted((self.backup_path / 'pre-update').iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)[0] # latest pre-update backup
            version = path.name.split('_')[3]
        elif isinstance(path_or_version, pathlib.Path):
            path = path_or_version
            version = path.name.split('_')[3]
        else:
            version = path_or_version
            if snapshot and len(version) == 1:
                version = datetime.datetime.utcnow().strftime('%yw%V') + version
            path = next(path for path in sorted((self.backup_path / 'pre-update').iterdir(), key=lambda path: path.stat().st_mtime, reverse=True) if path.name.split('_')[3] == version)
        # start iter_update
        update_iterator = self.iter_update(version, log_path=log_path, make_backup=False, override=override, reply=reply)
        version_dict = next(update_iterator)
        reply('Downloading ' + version_dict['version_text'])
        # make a backup to backup/<world>/reverted
        if make_backup:
            old_version = self.version()
            backup_path = self.backup_path / 'reverted' / '{}_{:%Y-%m-%d_%Hh%M}_{}_{}'.format(self.name, datetime.datetime.utcnow(), old_version, version)
            self.backup(reply=reply, path=backup_path, copy_to_latest=False)
        # stop the server
        was_running = self.status()
        if was_running:
            self.say('Server will be reverting to ' + version_dict["version_text"] + ' and therefore restart')
            time.sleep(5)
            self.stop(reply=reply, log_path=log_path)
        reply('Server stopped. Restoring backup...')
        # revert Minecraft version
        for message in update_iterator:
            reply(message)
        # restore backup
        world_path = self.world_path
        if world_path.exists():
            shutil.rmtree(str(world_path))
        subprocess.call(['tar', '-C', str(self.path), '-xzf', str(path), world_path.name]) # untar tar the world backup
        # restart server
        if was_running:
            self.start(reply=reply, start_message='Server reverted. Restarting...', log_path=log_path)
        return version_dict['version'], version_dict['is_snapshot'], version_dict['version_text']

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
            """This function will run a loop to feed commands sent through the socket to minecraft"""
            mypid = os.getpid()
            loop_var = True
            with socket.socket(socket.AF_UNIX) as s:
                # Set 1 minute timeout so that the process actually exits (this is not crucial but we don't want to spam the system)
                s.settimeout(60)
                if self.socket_path.exists():
                    self.socket_path.unlink()
                s.bind(str(self.socket_path))

                while loop_var and self.socket_path.exists():
                    if not self.pidrunning(java_popen.pid):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                            s.close()
                        except:
                            pass
                        return

                    str_buffer = ''
                    try:
                        s.listen(1)
                        c, _ = s.accept()
                        while loop_var:
                            data = c.recv(4096)
                            if not data:
                                break
                            lines = (str_buffer + data.decode('utf-8')).split('\n')
                            for line in lines[:-1]:
                                if line == 'stop':
                                    loop_var = False
                                    break
                                java_popen.stdin.write(line.encode('utf-8') + b'\n')
                                java_popen.stdin.flush()
                            str_buffer = lines[-1]
                        try:
                            c.shutdown(socket.SHUT_RDWR)
                            c.close()
                        except:
                            pass
                    except (socket.timeout, socket.error):
                        continue
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            except:
                pass
            java_popen.communicate(input=b'stop\n')
            if self.socket_path.exists():
                self.socket_path.unlink()

        invocation = [
            'java',
            '-Xmx' + str(self.config['javaOptions']['maxHeap']) + 'M',
            '-Xms' + str(self.config['javaOptions']['minHeap']) + 'M',
            '-XX:+UseConcMarkSweepGC',
            '-XX:ParallelGCThreads=' + str(self.config['javaOptions']['cpuCount']),
            '-XX:+AggressiveOpts',
            '-Dlog4j.configurationFile=' + str(CONFIG['paths']['logConfig']),
            '-jar',
            str(CONFIG['paths']['service'])
        ] + self.config['javaOptions']['jarOptions']

        reply = kwargs.get('reply', print)
        if self.status():
            reply('Server is already running!')
            return False
        reply(kwargs.get('start_message', 'Starting Minecraft server...'))

        if not self.socket_path.parent.exists():
            # make sure the command sockets directory exists
            self.socket_path.parent.mkdir(parents=True)
        if not self.pidfile_path.parent.exists():
            # make sure the pidfile directory exists
            self.pidfile_path.parent.mkdir(parents=True)

        java_popen = subprocess.Popen(invocation, stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(self.path)) # start the java process
        with self.pidfile_path.open("w+") as pidfile:
            pidfile.write(str(java_popen.pid))
        for line in loops.timeout_total(java_popen.stdout, datetime.timedelta(seconds=CONFIG['startTimeout'])): # wait until the timeout has been exceeded...
            if re.match('[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} \\[Server thread/INFO\\]: Done \\([0-9]+.[0-9]+s\\)!', line.decode('utf-8')): # ...or the server has finished starting
                break
        _fork(feed_commands, java_popen) # feed commands from the socket to java
        _fork(more_itertools.consume, java_popen.stdout) # consume java stdout to prevent deadlocking
        if kwargs.get('log_path'):
            with (kwargs['log_path'].open('a') if hasattr(kwargs['log_path'], 'open') else open(kwargs['log_path'], 'a')) as logins_log:
                ver = self.version()
                print(datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + (' @restart' if ver is None else ' @start ' + ver), file=logins_log) # logs in UTC

        # Wait for the socket listener to spin up
        for _ in range(20):
            if not self.status():
                time.sleep(0.5)
            else:
                break
        return self.status()

    def status(self, reply=print):
        return self.pidstatus(reply=reply) and self.socket_path.exists()

    def stop(self, *args, **kwargs):
        reply = kwargs.get('reply', print)
        if self.status():
            try:
                reply('SERVER SHUTTING DOWN IN 10 SECONDS. Saving map...')
                notice = kwargs.get('notice', 'SERVER SHUTTING DOWN IN 10 SECONDS. Saving map...')
                if self.config['rconPassword'] is None:
                    reply('Cannot communicate with the world, missing RCON password! Killing...')
                    return self.kill()
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
                    reply('The server could not be stopped! Killing...')
                    return self.kill()
                if kwargs.get('log_path'):
                    with (kwargs['log_path'].open('a') if hasattr(kwargs['log_path'], 'open') else open(kwargs['log_path'], 'a')) as logins_log:
                        print(datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S') + ' @stop', file=logins_log) # logs in UTC
            except ConnectionRefusedError:
                reply("Can't communicate with the socket. We need to kill the server...")
                return self.kill()
        else:
            reply('Minecraft server was not running.')
        self.cleanup(reply=reply)
        return not self.status()

    def tellraw(self, message_dict, player='@a'):
        if isinstance(message_dict, str):
            message_dict = {'text': message_dict}
        elif isinstance(message_dict, list):
            message_dict = {'text': '', 'extra': message_dict}
        try:
            import api.util2
        except ImportError:
            pass # no support for Player objects
        else:
            if isinstance(player, api.util2.Player):
                player = player.data['minecraft']['nicks'][-1]
        self.command('tellraw', [player, json.dumps(message_dict)])

    def update(self, version=None, snapshot=False, *, log_path=None, make_backup=True, override=False, reply=print):
        """Download a different version of Minecraft and restart the server if it is running.

        Optional arguments:
        version -- If given, a version with this name will be downloaded. By default, the newest available version is downloaded.
        snapshot -- If version is given, this specifies whether the version is a development version. If no version is given, this specifies whether the newest stable version or the newest development version should be downloaded. Defaults to False.

        Keyword-only arguments:
        log_path -- This is passed to the stop function if the server is stopped before the update.
        make_backup -- Whether to back up the world before updating. Defaults to True.
        override -- If this is True and the server jar for the target version already exists, it will be deleted and redownloaded. Defaults to False.
        reply -- This function is called several times with a string argument representing update progress. Defaults to the built-in print function.

        Returns:
        The new version, a boolean indicating whether or not the new version is a snapshot (or pre-release), and the full name of the new version.

        Raises:
        NotImplementedError -- For worlds with custom servers.
        """
        if self.config['customServer']:
            raise NotImplementedError('Update is not implemented for worlds with custom servers')
        update_iterator = self.iter_update(version=version, snapshot=snapshot, log_path=log_path, make_backup=make_backup, override=override, reply=reply)
        version_dict = next(update_iterator)
        reply('Downloading ' + version_dict['version_text'])
        for message in update_iterator:
            reply(message)
        return version_dict['version'], version_dict['is_snapshot'], version_dict['version_text']

    def update_whitelist(self, people_file=None):
        # get wanted whitelist from people file
        if people_file is None:
            people = people.get_people_db().obj_dump(version=3)
        else:
            with open(str(people_file)) as people_fobj:
                people = json.load(people_fobj)['people']
        whitelist = []
        additional = self.config['whitelist']['additional']
        if not self.config['whitelist']['ignorePeople']:
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
        """Returns the version of Minecraft the world is currently configured to run. For worlds with custom servers, returns None instead.
        """
        if self.config['customServer']:
            return None
        return self.service_path.resolve().stem[len('minecraft_server.'):]

    @property
    def world_path(self):
        """Returns the world save directory"""
        result = self.path / 'world'
        if not result.exists():
            return self.path / self.name
        return result

class MinecraftServerNotRunningError(Exception):
    pass

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
                f.write(chunk)
        f.flush()

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
        expect_user = CONFIG["runUser"]
        wurstmineberg_user = pwd.getpwnam(expect_user)
    except:
        sys.exit('[!!!!] User ‘{}’ does not exist!'.format(expect_user))
    if os.geteuid() != wurstmineberg_user.pw_uid:
        sys.exit('[!!!!] Only the user ‘{}’ may use this program!'.format(expect_user))
    if arguments['--all'] or arguments['update-all']:
        selected_worlds = worlds()
    elif arguments['--enabled']:
        selected_worlds = filter(lambda world: world.config['enabled'], worlds())
    elif arguments['<world>']:
        selected_worlds = (World(world_name) for world_name in arguments['<world>'])
    else:
        selected_worlds = [World()]
    if arguments['kill']:
        for world in selected_worlds:
            if world.pidstatus():
                world.kill()
            else:
                sys.exit('[WARN] Could not kill the "{}" world, PID file does not exist.'.format(world))
    elif arguments['start']:
        for world in selected_worlds:
            if not world.start():
                sys.exit('[FAIL] Error! Could not start the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is now running.')
    elif arguments['stop']:
        for world in selected_worlds:
            if not world.stop():
                sys.exit('[FAIL] Error! Could not stop the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is stopped.')
    elif arguments['restart']:
        for world in selected_worlds:
            if not world.restart():
                sys.exit('[FAIL] Error! Could not restart the {} world.'.format(world))
        else:
            print('[ ok ] Minecraft is now running.')
    elif arguments['update'] or arguments['update-all']:
        for world in selected_worlds:
            if arguments['snapshot']:
                world.update(arguments['<snapshot-id>'], snapshot=True, make_backup=not arguments['--no-backup'])
            elif arguments['<version>']:
                world.update(arguments['<version>'], make_backup=not arguments['--no-backup'])
            else:
                world.update(snapshot=True)
    elif arguments['revert']:
        for world in selected_worlds:
            if arguments['snapshot']:
                world.revert(arguments['<snapshot-id>'], snapshot=True, make_backup=not arguments['--no-backup'])
            elif arguments['<version>']:
                world.revert(arguments['<version>'], make_backup=not arguments['--no-backup'])
            else:
                world.revert()
    elif arguments['backup']:
        for world in selected_worlds:
            world.backup()
    elif arguments['status']:
        exit1 = False
        for world in selected_worlds:
            mcversion = "" if world.version() == "" else "(Minecraft {}) ".format(world.version())
            if world.status():
                print('[info] The "{}" world {}is running with PID {}.'.format(world, mcversion, world.pid))
            else:
                exit1 = True
                if world.pidstatus():
                    print('[info] The "{}" world is running but the socket file does not exist. Please kill the world and restart.'.format(world))
                else:
                    print('[info] The "{}" world {}is not running.'.format(world, mcversion))
        if exit1:
            sys.exit(1)
    elif arguments['command']:
        selected_worlds = list(selected_worlds)
        for world in selected_worlds:
            if len(selected_worlds) > 1:
                print('[info] running command on {} world'.format(world))
            cmdlog = world.command(arguments['<command>'][0], arguments['<command>'][1:])
            for line in cmdlog.splitlines():
                print(str(line))
    elif arguments['saves']:
        for world in selected_worlds:
            if arguments['on']:
                world.save_on()
            elif arguments['off']:
                world.save_off()
            else:
                raise NotImplementedError('Subcommand not implemented')
    else:
        raise NotImplementedError('Subcommand not implemented')
