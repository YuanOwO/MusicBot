import os
import sys
import logging
import asyncio
import inspect
import pathlib
import re
import shutil
import time
import traceback

import aiohttp

from collections import defaultdict
from colorlog import ColoredFormatter
from datetime import datetime, timedelta
from functools import wraps
from textwrap import dedent
from typing import (
    Literal, NoReturn, Optional, Tuple, Union,
    Any, DefaultDict, Dict, List, Set,
    get_args
)
from yt_dlp.utils import DownloadError
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from nextcord import (
    AppInfo, VoiceClient, Message, Guild, Interaction, Member,
    ApplicationError,
    SlashOption, StageChannel, TextChannel, VoiceState, VoiceChannel,
    Object, Embed, Intents, Activity, ActivityType,
    Permissions,
    ApplicationInvokeError,
    Forbidden, HTTPException, LoginFailure, NotFound,
    abc, utils as dc_utils,
    slash_command
)
from nextcord.ext import (
    application_checks as app_checks,
    commands
)

from . import exceptions

from .config import Config
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH
from .constructs import SkipState
from .downloader import Downloader
from .entry import StreamPlaylistEntry, PlaylistEntry
from .exceptions import (
    ExtractionError, HelpfulError, InvalidDataError,
    CommandError, PermissionsError,
    RestartSignal, Signal, TerminateSignal
)
from .opus_loader import load_opus_lib
from .player import MusicPlayer
from .playlist import Playlist
from .utils import (
    _func_,
    _get_variable,
    fixg,
    format_song_duration,
    ftimedelta,
)


GuildVoiceChannel = Union[VoiceChannel, StageChannel]

load_opus_lib()

log = logging.getLogger(__name__)

intents = Intents.all()


class MusicBot(commands.Bot):
    def __init__(self, config_file: Optional[str] = None) -> None:
        try:
            sys.stdout.write('\x1b[38;5;51mMusicBot\x1b[0m')
        except:
            pass

        print()

        self.players: Dict[int, MusicPlayer] = {}
        self.exit_signal: Signal = None
        self.init_ok: bool = False
        self.cached_app_info: AppInfo = None
        self.last_status: Activity = None

        self.config = Config(config_file)

        self._setup_logging()

        self.aiolocks: DefaultDict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )
        self.downloader = Downloader(AUDIO_CACHE_PATH)

        log.info('Starting MusicBot')

        # TODO: Do these properly
        gpd_defaults = {
            'auto': False,
            'availability': False
        }
        # guild_paused_data
        self.guild_paused_data: DefaultDict[Guild, Dict[str, Any]] = (
            defaultdict(gpd_defaults.copy)
        )

        super().__init__(intents=intents)

        for _, cog in inspect.getmembers(sys.modules[__name__]):
            if (
                inspect.isclass(cog)
                and issubclass(cog, BasicCog)
                and cog != BasicCog
            ):
                log.debug('Add a cod {}'.format(cog))
                self.add_cog(cog(self))

        self.http.user_agent = 'MusicBot'
        self.aiosession = aiohttp.ClientSession(
            loop=self.loop, headers={'User-Agent': self.http.user_agent}
        )

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(
        self, *, guild: Optional[Guild] = None, voice=False
    ) -> Union[Member, None]:
        return dc_utils.find(
            lambda mem: (
                mem.id == self.config.owner_id
                and (mem.voice if (voice) else True)
            ),
            guild.members if (guild) else self.get_all_members()
        )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH) -> bool:
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self) -> None:
        if (len(logging.getLogger(__package__).handlers) > 1):
            log.debug('Skipping logger setup, already set up')
            return

        sh = logging.StreamHandler(stream=sys.stdout)
        sf = ColoredFormatter(
            fmt=(
                '{log_color}[{asctime}] [{levelname}] [{module}] '
                '{message}'
            ),
            datefmt='%H:%M:%S',
            style='{',
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'white',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red',
                'EVERYTHING': 'white',
                'NOISY': 'white',
                'FFMPEG': 'bold_purple',
                'VOICEDEBUG': 'purple',
            }
        )
        sh.setFormatter(sf)
        sh.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(sh)

        log.debug(
            'Set logging level to {}'
            .format(self.config.debug_level_str)
        )

        dlog = logging.getLogger('nextcord')
        dlog.setLevel(logging.DEBUG)
        dh = logging.FileHandler(
            filename='logs/discord.log', encoding='utf-8', mode='w'
        )
        dh.setFormatter(
            logging.Formatter(
                fmt='[{asctime}] [{levelname}] {name}: {message}',
                datefmt='%m-%d %H:%M:%S',
                style='{'
            )
        )
        dlog.addHandler(dh)

    @staticmethod
    def _check_if_empty(
        vchannel: VoiceChannel,
        *, excluding_me=True, excluding_deaf=False
    ) -> bool:
        def check(member: Member):
            if (excluding_me and member == vchannel.guild.me):
                return False

            if (
                excluding_deaf
                and any([member.voice.deaf, member.voice.self_deaf])
            ):
                return False

            if (member.bot):
                return False

            return True

        return not sum(1 for mem in vchannel.members if check(mem))

    async def _cache_app_info(self, *, update=False) -> AppInfo:
        if (not self.cached_app_info and not update and self.user.bot):
            log.debug('Caching app info')
            self.cached_app_info = await self.application_info()

        return self.cached_app_info

    @ensure_appinfo
    async def generate_invite_link(
        self, *, permissions=Permissions(70380544), guild=None
    ) -> str:
        return dc_utils.oauth_url(
            self.cached_app_info.id,
            permissions=permissions,
            guild=guild
        )

    async def get_voice_client(
        self, channel: VoiceChannel
    ) -> VoiceClient:
        if (isinstance(channel, Object)):
            channel = self.get_channel(channel.id)

        if (not isinstance(channel, VoiceChannel)):
            raise AttributeError(
                'Channel passed must be a voice channel'
            )

        if (channel.guild.voice_client):
            return channel.guild.voice_client
        else:
            client = await channel.connect(timeout=60, reconnect=True)
            await channel.guild.change_voice_state(
                channel=channel, self_mute=False, self_deaf=True
            )
            return client

    async def disconnect_voice_client(self, guild: Guild) -> None:
        vc = self.voice_client_in(guild)
        if (not vc):
            return

        if (guild.id in self.players):
            self.players.pop(guild.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self) -> None:
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.guild)

    def get_player_in(self, guild: Guild) -> MusicPlayer:
        return self.players.get(guild.id)

    async def get_player(
        self, channel: VoiceChannel, create=False, *, deserialize=False
    ) -> MusicPlayer:
        guild = channel.guild

        async with self.aiolocks[_func_() + ':' + str(guild.id)]:
            if (deserialize):
                vc = await self.get_voice_client(channel)
                player = await self.deserialize_queue(guild, vc)

                if (player):
                    log.debug(
                        'Created player via deserialization for guild '
                        '{} with {} entries'.format(
                            guild.id, len(player.playlist)
                        ),
                    )
                    # Since deserializing only happens when the bot
                    # starts, I should never need to reconnect
                    return self._init_player(player, guild=guild)

            if (guild.id not in self.players):
                if (not create):
                    raise CommandError(
                        'The bot is not in a voice channel. Use `/join`'
                        'to join your voice channel.'
                    )

                vc = await self.get_voice_client(channel)

                playlist = Playlist(
                    self.aiosession, self.config, self.downloader,
                    self.loop
                )
                player = MusicPlayer(
                    self.config, self.loop, playlist, vc
                )
                self._init_player(player, guild=guild)

        return self.players[guild.id]

    def _init_player(
        self, player: MusicPlayer, *, guild: Optional[Guild] = None
    ) -> MusicPlayer:
        player = (
            player.on('play', self.on_player_play)
            .on('resume', self.on_player_resume)
            .on('pause', self.on_player_pause)
            .on('stop', self.on_player_stop)
            .on('finished-playing', self.on_player_finished_playing)
            .on('entry-added', self.on_player_entry_added)
            .on('error', self.on_player_error)
        )

        player.skip_state = SkipState()

        if (guild):
            self.players[guild.id] = player

        return player

    async def on_player_play(
        self, player: MusicPlayer, entry: PlaylistEntry
    ) -> None:
        log.debug('Running on_player_play')
        await self.update_now_playing_status()
        player.skip_state.reset()

    async def on_player_resume(
        self, player: MusicPlayer, entry: PlaylistEntry, **_
    ) -> None:
        log.debug('Running on_player_resume')
        await self.update_now_playing_status()

    async def on_player_pause(
        self, player: MusicPlayer, entry: PlaylistEntry, **_
    ) -> None:
        log.debug('Running on_player_pause')
        await self.update_now_playing_status()
        # await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_stop(self, player: MusicPlayer, **_) -> None:
        log.debug('Running on_player_stop')
        await self.update_now_playing_status()

    async def on_player_finished_playing(
        self, player: MusicPlayer, **_
    ) -> None:
        log.debug('Running on_player_finished_playing')

        if (not player.playlist.entries and not player.current_entry):
            # Don't serialize for autoplaylist events
            await self.serialize_queue(
                player.voice_client.channel.guild
            )

        if (not player.is_stopped and not player.is_dead):
            player.play(_continue=True)

    async def on_player_entry_added(
        self, player: MusicPlayer, playlist: Playlist,
        entry: PlaylistEntry, **_
    ) -> None:
        log.debug('Running on_player_entry_added')
        if (entry.meta.get('author') and entry.meta.get('channel')):
            await self.serialize_queue(
                player.voice_client.channel.guild
            )

    async def on_player_error(
        self, player: MusicPlayer, entry: PlaylistEntry, ex, **_
    ) -> None:
        if (channel := entry.meta.get('channel', None)):
            channel = self.get_channel(channel)
            await channel.send(
                '```\nError while playing:\n{}\n```'.format(ex)
            )
        else:
            log.exception('Player error', exc_info=ex)

    async def update_now_playing_status(self) -> None:
        activity = None

        if (not self.config.status_message):
            if (self.user.bot):
                active_players = sum(
                    1
                    for player in self.players.values()
                    if player.is_playing
                )
                if (active_players >= 1):
                    activity = Activity(
                        name='music on {} guilds'.format(
                            active_players
                        ),
                        type=ActivityType.listening
                    )

            if (not activity):
                activity = Activity(
                    name='I don\'t know what can I do now :(',
                    type=ActivityType.custom
                )
        else:
            activity = Activity(
                name=self.config.status_message.strip()[:128],
                type=ActivityType.playing
            )

        async with self.aiolocks[_func_()]:
            if (activity != self.last_status):
                await self.change_presence(activity=activity)
                self.last_status = activity

    async def serialize_queue(self, guild: Guild, *, dir=None) -> None:
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(guild)
        if (not player):
            return

        if (dir is None):
            dir = 'data/{}/queue.json'.format(guild.id)

        async with (
            self.aiolocks['queue_serialization' + ':' + str(guild.id)]
        ):
            log.debug('Serializing queue for {}'.format(guild.id))

            with open(dir, 'w', encoding='utf8') as file:
                file.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None) -> None:
        coros = [
            self.serialize_queue(guild, dir=dir)
            for guild in self.guilds
        ]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(
        self, guild: Guild, voice_client: VoiceClient,
        playlist: Optional[Playlist] = None,
        *, dir: Optional[str] = None
    ) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer. If no
        queue is saved, returns None.
        """

        if (playlist is None):
            playlist = Playlist(
                self.aiosession, self.config, self.downloader, self.loop
            )

        if (dir is None):
            dir = 'data/{}/queue.json'.format(guild.id)

        async with (
            self.aiolocks['queue_serialization' + ':' + str(guild.id)]
        ):
            if (not os.path.isfile(dir)):
                return None

            log.debug('Deserializing queue for {}'.format(guild.id))

            with open(dir, 'r', encoding='utf8') as file:
                data = file.read()

        return MusicPlayer.from_json(
            data, self.config, self.loop, playlist, voice_client
        )

    async def write_current_song(
        self, guild: Guild, entry: PlaylistEntry, *, dir: str = None
    ) -> None:
        """
        Writes the current song to file
        """
        player = self.get_player_in(guild)
        if (not player):
            return

        if (dir is None):
            dir = 'data/{}/current.txt'.format(guild.id)

        async with self.aiolocks['current_song' + ':' + str(guild.id)]:
            log.debug('Writing current song for {}'.format(guild.id))

            with open(dir, 'w', encoding='utf8') as file:
                file.write(entry.title)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self) -> None:
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # config/permissions async validate?
        await self._scheck_configs()

    async def _scheck_ensure_env(self) -> None:
        log.debug('Ensuring data folders exist')
        for guild in self.guilds:
            pathlib.Path('data/{}/'.format(guild.id)).mkdir(
                exist_ok=True
            )

        with (
            open('data/server_names.txt', 'w', encoding='utf8') as file
        ):
            for guild in sorted(
                self.guilds, key=lambda guild: int(guild.id)
            ):
                file.write('{:<22} {}\n'.format(guild.id, guild.name))

        if (
            not self.config.save_videos
            and os.path.isdir(AUDIO_CACHE_PATH)
        ):
            if (self._delete_old_audiocache()):
                log.debug('Deleted old audio cache')
            else:
                log.debug(
                    'Could not delete old audio cache, moving on.'
                )

    async def _scheck_server_permissions(self) -> None:
        log.debug('Checking server permissions')
        pass  # TODO

    async def _scheck_configs(self) -> None:
        log.debug('Validating config')
        await self.config.async_validate(
            self.user, self.cached_app_info
        )

    ####################################################################

    async def restart(self) -> None:
        self.exit_signal = RestartSignal()
        await self.close()

    def restart_threadsafe(self) -> None:
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self) -> None:
        try:
            self.loop.run_until_complete(self.logout())
            self.loop.run_until_complete(self.aiosession.close())
        except:
            pass

        pending = asyncio.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass

    # noinspection PyMethodOverriding
    def run(self) -> None:
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except LoginFailure:
            # Add if token, else
            raise HelpfulError(
                'Bot cannot login, bad credentials.',
                'Fix your token in the options file. Remember that '
                'each field should be on their own line.',
            )
            # ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error('Error in cleanup', exc_info=True)

            if (self.exit_signal):
                # pylint: disable=E0702
                raise self.exit_signal

    async def logout(self) -> None:
        await self.disconnect_all_voice_clients()
        return await super().close()

    async def on_error(self, event: str, *args, **kwargs) -> None:
        log.debug('ON ERROR')
        ex_type, ex, stack = sys.exc_info()

        if (ex_type == HelpfulError):
            log.error('Exception in {}:\n{}'.format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif (issubclass(ex_type, Signal)):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error('Exception in {}'.format(event), exc_info=True)

    # @becort

    async def on_application_command_error(
        self, inter: Interaction, exc: Exception
    ) -> None:
        log.error('ON APP ERROR')
        
        log.debug(
            'inter response is done: {}'.format(
                inter.response.is_done()
            )
        )

        log.debug(exc)
        log.debug(type(exc))
        log.debug(getattr(exc, 'original', 'No such attr'))
        if (isinstance(exc, ApplicationInvokeError)):
            exc = exc.original
        log.debug(exc.__dict__)

        embed = self._gen_embed('error')

        if (isinstance(exc, CommandError)):
            embed.title = '指令發生錯誤'
            embed.description = exc.message
        elif (isinstance(exc, PermissionsError)):
            embed.title = '您沒有權限執行此指令'
            embed.description = exc.message
        elif (isinstance(exc, Signal)):
            self.exit_signal = type(exc)
            await self.logout()
            return
        else:
            embed.title = '發生未知錯誤: ' + exc.__class__.__name__
            embed.description = str(exc)

            log.error('Exception happened', exc_info=True)

        if (inter.response.is_done()):
            await inter.followup.send(embed=embed)
        else:
            await inter.send(embed=embed)

    async def on_resumed(self) -> None:
        log.info('\nReconnected to discord.\n')

    async def on_ready(self) -> None:
        dlog = logging.getLogger('nextcord')
        for handler in dlog.handlers:
            if (getattr(handler, 'terminator', None) == ''):
                dlog.removeHandler(handler)
                print()

        log.debug('Connection established, ready to go.')

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if (self.init_ok):
            log.debug(
                'Received additional READY event, may have failed to '
                'resume'
            )
            return

        await self._on_ready_sanity_checks()

        self.init_ok = True

        ################################

        log.info(
            'Connected: {}/{}#{}'.format(
                self.user.id, self.user.name, self.user.discriminator
            )
        )

        owner = self._get_owner(voice=True) or self._get_owner()
        if (self.guilds):
            if (owner):
                log.info(
                    'Owner:     {}/{}#{}\n'.format(
                        owner.id, owner.name, owner.discriminator
                    )
                )
            else:
                log.warning(
                    'Owner could not be found on any guild (owner id: '
                    '{})\n'.format(self.config.owner_id)
                )

            log.info('Guild List:')
            unavailable_guilds = 0
            for guild in self.guilds:
                guild_str = (
                    guild.name + ' (unavailable)'
                    if (guild.unavailable)
                    else guild.name
                )
                log.info(' - ' + guild_str)

                if (owner and self.config.leave_non_owners):
                    if (guild.unavailable):
                        unavailable_guilds += 1
                    else:
                        check = guild.get_member(owner.id)
                        if (check == None):
                            await guild.leave()
                            log.info(
                                'Left {} due to bot owner not '
                                'found'.format(guild.name)
                            )
            if (unavailable_guilds):
                log.info(
                    'Not proceeding with checks in {} guilds due to '
                    'unavailability'.format(unavailable_guilds)
                )

        else:
            log.warning('Owner unknown, bot is not on any guilds.')
            if (self.user.bot):
                log.warning(
                    'To make the bot join a guild, paste this link in '
                    'your browser. \n'
                    'Note: You should be logged into your main account '
                    'and have manage \n'
                    'server permissions on the guild you want the bot '
                    'to join.\n'
                    '  ' + await self.generate_invite_link()
                )

        print(flush=True)

        if (self.config.show_config_at_start):
            def log_bool_options(
                name: str, val: bool, *, options=['Disabled', 'Enabled']
            ) -> None:
                log.info('  {}: {}'.format(name, options[val]))

            print(flush=True)

            log.info('Options:')
            # log.info('  Command prefix: /')
            log.info(
                '  Default volume: {}%'.format(
                    self.config.default_volume * 100
                )
            )
            log.info(
                '  Skip threshold: {} votes or {}%'.format(
                    self.config.skips_required,
                    fixg(self.config.skip_ratio_required * 100)
                )
            )
            log_bool_options(
                'Now Playing @mentions',
                self.config.now_playing_mentions
            )
            log_bool_options('Auto-Summon', self.config.auto_summon)
            log_bool_options('Auto-Pause', self.config.auto_pause)
            log_bool_options(
                'Delete Messages', self.config.delete_messages
            )
            if (self.config.delete_messages):
                log_bool_options(
                    'Delete Invoking', self.config.delete_invoking
                )
            log_bool_options('Debug Mode', self.config.debug_mode)
            log_bool_options(
                'Downloaded songs will be',
                self.config.save_videos,
                options=['deleted', 'saved']
            )
            if (self.config.status_message):
                log.info(
                    '  Status message: ' + self.config.status_message
                )
            log_bool_options(
                'Write current songs to file',
                self.config.write_current_song
            )
            log_bool_options(
                'Author insta-skip', self.config.allow_author_skip
            )
            log_bool_options('Embeds', self.config.embeds)
            log_bool_options('Legacy skip', self.config.legacy_skip)
            log_bool_options(
                'Leave non owners', self.config.leave_non_owners
            )

        print(flush=True)

        await self.update_now_playing_status()

        # we do this after the config stuff because it's a lot easier
        # to notice here
        if (self.config.missing_keys):
            log.warning(
                'Your config file is missing some options. If you have '
                'recently updated, check the example_options.ini file '
                'to see if there are new options available to you. The '
                'options missing are: {}'.format(
                    self.config.missing_keys
                )
            )
            print(flush=True)
        # t-t-th-th-that's all folks!

    def _gen_embed(self, status_type='info') -> Embed:
        """
        Provides a basic template for embeds
        """
        embed = Embed()

        match (status_type.lower()):
            case 'info':
                embed.colour = 0x7289da
            case 'warn' | 'warning':
                embed.colour = 0xffc107
            case 'error' | 'exception' | 'critical':
                embed.colour = 0xdc3545
            case _:
                embed.colour = 0xeaeaea

        embed.timestamp = datetime.now(tz=ZoneInfo(key='Asia/Taipei'))
        embed.set_footer(
            text=self.config.footer_text,
            icon_url='https://i.imgur.com/gFHBoZA.png'
        )
        embed.set_author(
            name=self.user.name,
            url='https://github.com/Just-Some-Bots/MusicBot',
            icon_url=self.user.avatar.url,
        )
        return embed

    async def on_voice_state_update(
        self, member: Member, before: VoiceState, after: VoiceState
    ) -> None:
        if (not self.init_ok):
            # Ignore stuff before ready
            return

        if (
            (channel := before.channel)
            and isinstance(channel, VoiceChannel)
        ):
            pass
        elif (
            (channel := after.channel)
            and isinstance(channel, VoiceChannel)
        ):
            pass
        else:
            return

        if (member == self.user and not after.channel):
            # if bot was disconnected from channel
            await self.disconnect_voice_client(before.channel.guild)
            return

        if (not self.config.auto_pause):
            return

        auto_paused = self.guild_paused_data[channel.guild]['auto']

        try:
            player = await self.get_player(channel)
        except CommandError:
            return

        def is_active(member: Member) -> bool:
            if (not member.voice):
                return False

            return not any(
                [member.voice.deaf, member.voice.self_deaf, member.bot]
            )

        going_paused = None
        reason = None

        # if the user is not inactive
        if (member != self.user and is_active(member)):
            if (
                channel != before.channel and channel == after.channel
            ):
                # if the person joined
                if (auto_paused and player.is_paused):
                    going_paused = False
                    reason = ''
            elif (
                channel == before.channel and channel != after.channel
            ):
                if (not any(is_active(m) for m in channel.members)):
                    # channel is empty
                    if (not auto_paused and player.is_playing):
                        going_paused = True
                        reason = '(empty channel)'
            elif (
                channel == before.channel and channel == after.channel
            ):
                # if the person undeafen
                if (auto_paused and player.is_paused):
                    going_paused = False
                    reason = '(member undeafen)'
        else:
            if (any(is_active(m) for m in channel.members)):
                # channel is not empty
                if (auto_paused and player.is_paused):
                    going_paused = False
                    reason = ''

            else:
                if (not auto_paused and player.is_playing):
                    going_paused = True
                    reason = '(empty channel or member deafend)'

        if (going_paused != None):
            log.info(
                '{} in {}/{} {}'.format(
                    'Pausing' if (going_paused) else 'Unpausing',
                    channel.guild.name,
                    channel.name,
                    reason
                ).strip()
            )
            self.guild_paused_data[channel.guild]['auto'] = going_paused
            if (going_paused):
                player.pause()
            else:
                player.resume()

    async def on_guild_update(
        self, before: Guild, after: Guild
    ) -> None:
        if (before.region != after.region):
            log.warning(
                'Guild "{}" changed regions: {} -> {}'.format(
                    after.name, before.region, after.region
                )
            )

    async def on_guild_join(self, guild: Guild) -> None:
        log.info('Bot has been added to guild: {}'.format(guild.name))
        owner = self._get_owner(voice=True) or self._get_owner()
        if (self.config.leave_non_owners):
            check = guild.get_member(owner.id)
            if (check == None):
                await guild.leave()
                log.info(
                    'Left {} due to bot owner not found.'.format(
                        guild.name
                    )
                )
                await owner.send(
                    '我離開了伺服器 `{}`，因為你不在裡面。'.format(
                        guild.name
                    )
                )
                return

        log.debug('Creating data folder for guild {}'.format(guild.id))
        pathlib.Path('data/{}/'.format(guild.id)).mkdir(exist_ok=True)

    async def on_guild_remove(self, guild: Guild) -> None:
        log.info(
            'Bot has been removed from guild: {}'.format(guild.name)
        )
        log.debug('Updated guild list:')
        for g in self.guilds:
            log.debug(' - ' + g.name)

        if (guild.id in self.players):
            self.players.pop(guild.id).kill()

    async def on_guild_available(self, guild: Guild) -> None:
        if (not self.init_ok):
            # Ignore pre-ready events
            return

        log.debug('Guild "{}" has become available.'.format(guild.name))

        player = self.get_player_in(guild)

        if (player and player.is_paused):
            av_paused = self.guild_paused_data[guild]['availability']

            if (av_paused):
                log.debug(
                    'Resuming player in "{}" due to availability.'
                    .format(guild.name)
                )
                self.guild_paused_data[guild]['availability'] = False
                player.resume()

    async def on_guild_unavailable(self, guild: Guild) -> None:
        log.debug(
            'Guild "{}" has become unavailable.'.format(guild.name)
        )

        player = self.get_player_in(guild)

        if (player and player.is_playing):
            log.debug(
                'Pausing player in "{}" due to unavailability.'.format(
                    guild.name
                )
            )
            self.guild_paused_data[guild]['availability'] = True
            player.pause()

    def voice_client_in(self, guild: Guild) -> Union[VoiceClient, None]:
        for vc in self.voice_clients:
            if vc.guild == guild:
                return vc
        return None


class BasicCog(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot


class Info(BasicCog):
    @slash_command(description='砰！')
    async def ping(self, inter: Interaction) -> None:
        """
        用法:
            /ping
        回答"砰！"以及我的網路延遲。
        """

        await inter.send(
            '砰！順帶一提，我的延遲是 {} 毫秒'.format(
                fixg(self.bot.latency * 1000)
            ),
            ephemeral=True
        )

    @slash_command(description='獲得幫助')
    async def help(
        self,
        inter: Interaction,
        command: Optional[str] = SlashOption(
            description='要查詢的指令',
            required=False,
            default=None
        )
    ) -> None:
        """
        用法:
            /help [command]
        *Constructing...*
        """

        await inter.send('*Constructing...*', ephemeral=True)

        # """
        # 用法:
        #     /help [command]
        # Prints a help message.
        # If a command is specified, it prints a help message for that command.
        # Otherwise, it lists the available commands.
        # """
        # commands = []
        # isAll = False

        # if (command):
        #     if (command.lower() == 'all'):
        #         isAll = True

        #     else:
        #         cmd = getattr(self, 'cmd_' + command, None)
        #         if (cmd and not hasattr(cmd, 'dev_cmd')):
        #             await inter.send(
        #                 '```\n{}```'.format(dedent(cmd.__doc__)),
        #                 ephemeral=True
        #             )
        #             return
        #         else:
        #             raise CommandError(
        #                 self.i18n.get('cmd-help-invalid'),
        #                 expire_in=10,
        #             )

        # elif (inter.user.id == self.config.owner_id):
        #     isAll = True

        # for attr in dir(self):
        #     # This will always return at least cmd_help, since they
        #     # needed perms to run this command
        #     if (attr.startswith('cmd_')):
        #         cmd_name = attr.replace('cmd_', '').lower()
        #         if (
        #             isAll
        #             or not hasattr(getattr(self, attr), 'dev_cmd')
        #         ):
        #             commands.append('/{}' + cmd_name)

        # desc = (
        #     f'```\n{", ".join(commands)}\n```\n'
        #     + self.i18n.get('cmd-help-response')
        # )
        # if (not isAll):
        #     desc += self.i18n.get('cmd-help-all')

        # await inter.send(desc, ephemeral=True)

    @slash_command(description='告訴你使用者的 ID')
    async def id(
        self,
        inter: Interaction,
        user: Optional[Member] = SlashOption(
            description='將被查詢 ID 的人，預設是你自己',
            required=False,
            default=None
        )
    ) -> None:
        """
        用法:
            /id [@user]
        告訴你該使用者的 ID，若沒有指定，則是你自己。
        """

        if (not user):
            msg = '你的 ID 是 `{}`'.format(inter.user.id)
        else:
            msg = '**{}**的 ID 是 `{}`'.format(user.name, user.id)
        await inter.send(msg, ephemeral=True)


class Owner(BasicCog):
    @slash_command(description='變更機器人名稱')
    @app_checks.is_owner()
    async def setname(
        self,
        inter: Interaction,
        name: str = SlashOption(
            description='新的名稱',
            required=True
        )
    ) -> None:
        """
        用法:
            /setname <name>
        變更機器人的名稱。
        Note: This operation is limited by discord to twice per hour.
        """

        try:
            await self.bot.user.edit(username=name)

        except HTTPException:
            raise CommandError(
                '變更失敗。你是否多次變更名稱？\n每小時只變更名稱兩次。'
            )

        except Exception as e:
            raise CommandError(e)

        await inter.send(
            '成功將機器人暱稱設為 `{}`'.format(name), ephemeral=True
        )

    @slash_command(description='變更機器人頭像')
    @app_checks.is_owner()
    async def setavatar(
        self,
        inter: Interaction,
        url: str = SlashOption(
            description='新頭像檔案的 URL',
            required=True
        )
    ) -> None:
        """
        用法:
            /setavatar <url>
        變更機器人頭像。
        """
        if url:
            thing = url.strip('<>')

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with (
                self.bot.aiosession.get(thing, timeout=timeout) as res
            ):
                await self.bot.user.edit(avatar=await res.read())

        except Exception as e:
            raise CommandError(
                '無法變更頭像: {}'.format(e)
            )

        await inter.send('變更成功!', ephemeral=True)

    @slash_command(description='重啟機器人')
    @app_checks.is_owner()
    async def restart(self, inter: Interaction) -> None:
        """
        用法:
            /restart
        重新啟動機器人。
        Will not properly load new dependencies or file updates unless 
        fully shutdown and restarted.
        """
        await inter.send(
            '\N{WAVING HAND SIGN} Restarting. If you have updated your '
            'bot or its dependencies, you need to restart the bot '
            'properly, rather than using this command.'
        )

        player = self.bot.get_player_in(inter.guild)
        if (player and player.is_paused):
            player.resume()

        await self.bot.disconnect_all_voice_clients()
        raise RestartSignal()

    @slash_command(description='關閉機器人')
    async def shutdown(self, inter: Interaction) -> None:
        """
        用法:
            /shutdown
        關閉機器人。
        Disconnects from voice channels and closes the bot process.
        """
        await inter.send('\N{WAVING HAND SIGN}')

        player = self.bot.get_player_in(inter.guild)
        if (player and player.is_paused):
            player.resume()

        await self.bot.disconnect_all_voice_clients()
        raise TerminateSignal()

    @slash_command()
    @app_checks.is_owner()
    async def breakpoint(self, inter: Interaction) -> None:
        log.critical('Activating debug breakpoint')
        await inter.send('紀錄成功', ephemeral=True)

    @slash_command()
    async def test(self, inter: Interaction) -> NoReturn:
        log.debug('test')
        raise Exception('owowo')


class Admin(BasicCog):
    @slash_command(description='刪除訊息')
    @app_checks.has_permissions(manage_messages=True)
    async def purge(
        self,
        inter: Interaction,
        limit: int = SlashOption(
            description='要刪除的訊息數',
            required=False,
            min_value=1,
            default=1
        )
    ) -> None:
        """
        用法:
            /purge [limit]
        刪除 `limit` 筆訊息
        """

        ch = inter.channel

        if (not ch.permissions_for(inter.guild.me).manage_messages):
            raise CommandError('刪除失敗，沒有管理訊息權限。')

        await ch.purge(limit=limit)
        await inter.send('成功刪除 `{}` 筆訊息'.format(limit))

    @slash_command(description='變更機器人暱稱')
    @app_checks.has_guild_permissions(administrator=True)
    async def setnick(
        self,
        inter: Interaction,
        nickname: str = SlashOption(
            description='新的暱稱',
            required=True
        )
    ) -> None:
        """
        用法:
            /setnick <nickname>
        變更機器人的暱稱。
        """
        me = inter.guild.me
        if (not inter.channel.permissions_for(me).change_nickname):
            raise CommandError('無法變更暱稱，沒有權限。')

        try:
            await me.edit(nick=nickname)
        except Exception as e:
            raise CommandError(e)

        await inter.send(
            '成功將機器人暱稱設為 `{}`'.format(nickname), ephemeral=True
        )


class Music(BasicCog):
    def is_in_voice_channel(inter: Interaction) -> Literal[True]:
        if (
            inter.user.voice
            and isinstance(inter.user.voice.channel, VoiceChannel)
        ):
            return True

        raise CommandError('你不在語音頻道！')

    def has_manage_perission_in_voice_channel(
        inter: Interaction
    ) -> Literal[True]:
        user = inter.user
        voice_ch = user.voice.channel

        if (voice_ch.permissions_for(user).manage_channels):
            return True

        raise PermissionsError('您沒有權限執行該指令')

    async def _join_channel(self, voice_ch: VoiceChannel) -> None:
        guild = voice_ch.guild

        vc = self.bot.voice_client_in(guild)
        ch_perms = voice_ch.permissions_for(guild.me)

        if (not ch_perms.connect):
            log.warning(
                'Cannot join channel "{0}", no permission to '
                'connect.'.format(voice_ch.name)
            )
            raise CommandError(
                '無法加入頻道 `{0}`，沒有連接權限。'.format(
                    voice_ch.name
                )
            )

        elif (not ch_perms.speak):
            log.warning(
                'Cannot join channel "{0}", no permission to '
                'speak.'.format(voice_ch.name)
            )
            raise CommandError(
                '無法加入頻道 `{0}`，沒有語音權限。'.format(
                    voice_ch.name
                )
            )

        if (vc):
            await vc.move_to(voice_ch)

        else:
            player = await self.bot.get_player(
                voice_ch,
                create=True,
                deserialize=self.bot.config.persistent_queue
            )

            if (player.is_stopped):
                player.play()

        log.info('Joining {}/{}'.format(guild.name, voice_ch.name))

    async def _play_playlist_async(
        self,
        inter: Interaction,
        player: MusicPlayer,
        playlist_url: str,
        extractor_type
    ):
        """
        Secret handler to use the async wizardry to make playlist
        queuing non-'blocking'
        """

        info = await self.bot.downloader.extract_info(
            self.bot.loop, playlist_url, download=False, process=False
        )

        if (not info):
            raise CommandError('該播放清單無法播放。')

        num_songs = sum(1 for _ in info['entries'])
        t_start = time.time()

        msg = await inter.followup.send(
            '處理歌曲 {0} ...'.format(num_songs),
            wait=True
        )  # TODO: From playlist_title

        entries_added = 0
        if (extractor_type == 'youtube:playlist'):
            try:
                entries_added = (
                    await player.playlist
                    .async_process_youtube_playlist(
                        playlist_url,
                        channel=inter.channel.id,
                        author=inter.user.id
                    )
                )
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error('Error processing playlist', exc_info=True)
                raise CommandError(
                    '處理播放列表 {0} 的排隊時發生錯誤。'
                    .format(playlist_url)
                )

        elif (
            extractor_type.lower() in [
                'soundcloud:set', 'bandcamp:album'
            ]
        ):
            try:
                entries_added = (
                    await player.playlist.async_process_sc_bc_playlist(
                        playlist_url,
                        channel=inter.channel.id,
                        author=inter.user.id
                    )
                )
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error('Error processing playlist', exc_info=True)
                raise CommandError(
                    '處理播放清單 `{0}` 時發生錯誤。'.format(playlist_url)
                )

        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        max_song_length = self.bot.config.max_song_length
        for e in entries_added.copy():
            if (e.duration > max_song_length):
                try:
                    player.playlist.entries.remove(e)
                    entries_added.remove(e)
                    drop_count += 1
                except:
                    pass

        if (drop_count):
            log.debug('Dropped {} songs'.format(drop_count))

        songs_added = len(entries_added)
        t_end = time.time()
        t_time = t_end - t_start
        t_avg = t_time / num_songs
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function
        #       and return that too

        # This is technically inaccurate since bad songs are ignored but
        # still take up time
        log.info(
            'Processed {}/{} songs in {} seconds at {:.2f}s/song, '
            '{:+.2g}/song from expected ({}s)'.format(
                songs_processed,
                num_songs,
                fixg(t_time),
                t_avg if num_songs else 0,
                (
                    t_avg - wait_per_song
                    if (num_songs - wait_per_song)
                    else 0
                ),
                fixg(wait_per_song * num_songs),
            )
        )

        if (not songs_added):
            base_text = (
                '沒有添加歌曲，所有歌曲都超過了最長時間 (`{}` 秒)'
                .format(max_song_length)
            )
            if (skipped):
                base_text += '\n此外，當前的歌曲因為太長而被跳過。'

            raise CommandError(base_text, expire_in=30)
        
        await msg.edit(
            '加入播放清單的歌曲 {} 將在 {} 秒後播放'
            .format(songs_added, fixg(t_time, 1))
        )

    async def _play(
        self, inter: Interaction, song_url: str, head=False,
    ) -> None:
        await inter.response.defer()
        
        player = self.bot.get_player_in(inter.guild)

        if (not player):
            voice_ch = inter.user.voice.channel
            await self._join_channel(voice_ch)
            player = self.bot.get_player_in(inter.guild)

        song_url = song_url.strip('<>')

        max_songs = self.bot.config.max_playlist_songs
        if (len(player.playlist) >= max_songs):
            raise CommandError(
                '播放清單已經滿了！請稍後再點播 (播放清單最多只能有 {0} 首)'
                .format(max_songs),
            )

        # Make sure forward slashes work properly in search queries
        linksRegex = (
            '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        )
        matchUrl = re.match(linksRegex, song_url)
        song_url = (
            song_url.replace('/', '%2F')
            if (matchUrl is None)
            else song_url
        )

        # Rewrite YouTube playlist URLs if the wrong URL type is given
        playlistRegex = r'watch\?v=.+&(list=[^&]+)'
        matches = re.search(playlistRegex, song_url)
        groups = matches.groups() if (matches is not None) else []
        song_url = (
            'https://www.youtube.com/playlist?' + groups[0]
            if (len(groups) > 0)
            else song_url
        )

        async def get_info(song_url: str) -> Tuple[
            Dict[str, Any],
            Optional[Dict[str, Any]],
            Optional[Exception]
        ]:
            info = await self.bot.downloader.extract_info(
                player.loop, song_url, download=False, process=False
            )
            # If there is an exception arise when processing we go on
            # and let extract_info down the line report it because info
            # might be a playlist and thing that's broke it might be
            # individual entry
            try:
                info_proc = await self.bot.downloader.extract_info(
                    player.loop, song_url, download=False
                )
                info_proc_err = None
            except Exception as err:
                info_proc, info_proc_err = None, err

            return (info, info_proc, info_proc_err)

        # This lock prevent spamming play command to add entries that
        # exceeds time limit/ maximum song limit
        async with (
            self.bot.aiolocks[_func_() + ':' + str(inter.user.id)]
        ):
            # Try to determine entry type, if _type is playlist then
            # there should be entries
            while (True):
                try:
                    info, info_proc, info_proc_err = (
                        await get_info(song_url)
                    )
                    log.debug(info)

                    if (
                        info_proc
                        and info
                        and (
                            info_proc.get('_type', None) == 'playlist'
                        )
                        and 'entries' not in info
                        and (
                            not info.get('url', '')
                            .startswith('ytsearch')
                        )
                    ):
                        use_url = (
                            info_proc.get('webpage_url', None)
                            or info_proc.get('url', None)
                        )
                        if (use_url == song_url):
                            log.warning(
                                'Determined incorrect entry type, but '
                                'suggested url is the same. Help.'
                            )
                            break
                            # If we break here it will break things down
                            # the line and give 'This is a playlist'
                            # exception as a result

                        log.debug(
                            'Assumed url "{}" was a single entry, was '
                            'actually a playlist'.format(song_url)
                        )
                        log.debug('Using "{}" instead'.format(use_url))
                        song_url = use_url
                    else:
                        break

                except Exception as err:
                    if ('unknown url type' in str(err)):
                        song_url = song_url.replace(':', '')
                        # it's probably not actually an extractor
                        info, info_proc, info_proc_err = (
                            await get_info(song_url)
                        )
                    else:
                        raise CommandError(err)

            if (not info):
                raise CommandError(
                    '無法播放此音樂。嘗試使用 `/stream` 指令。'
                )

            # abstract the search handling away from the user
            # our ytdl options allow us to use search strings as input
            # urls
            if (info.get('url', '').startswith('ytsearch')):
                # print(
                #     '[Command:play] Searching for "{}"'
                #     .format(song_url)
                # )
                if (info_proc):
                    info = info_proc
                else:
                    log.error(
                        '{}: {}'.format(
                            type(info_proc_err.__class__), info_proc_err
                        )
                    )
                    raise CommandError(
                        '從字串中提取訊息錯誤，youtube-dl 沒有回傳資料。'
                        '如果這種情況持續發生，請聯繫機器人開發人員。'
                        '```\n{}\n```'.format(info_proc_err)
                    )

                song_url = (
                    info_proc.get('webpage_url', None)
                    or info_proc.get('url', None)
                )

                if ('entries' in info):
                    # if entry is playlist then only get the first one
                    song_url = info['entries'][0]['webpage_url']
                    info = info['entries'][0]

            max_song_length = self.bot.config.max_song_length

            # If it's playlist
            if ('entries' in info):
                num_songs = sum(1 for _ in info['entries'])

                if (
                    info['extractor'].lower() in [
                        'youtube:playlist',
                        'soundcloud:set',
                        'bandcamp:album',
                    ]
                ):
                    try:
                        await self._play_playlist_async(
                            inter, player, song_url, info['extractor']
                        )
                        return
                    except CommandError:
                        raise
                    except Exception as err:
                        log.error(
                            'Error queuing playlist', exc_info=True
                        )
                        raise CommandError(
                            '在新增至播放清單時發生錯誤：\n`{}`'.format(err)
                        )

                t_start = time.time()

                # My test was 1.2 seconds per song, but we maybe should
                # fudge it a bit, unless we can monitor it and edit the
                # message with the estimated time, but that's some
                # ADVANCED SHIT
                # I don't think we can hook into it anyways, so this
                # will have to do.
                # It would probably be a thread to check a few playlists
                # and get the speed from that Different playlists might
                # download at different speeds though
                wait_per_song = 1.2
                eta = (
                    '，預估剩餘時間 `{}` 秒'.format(
                        fixg(num_songs * wait_per_song)
                    )
                    if (num_songs >= 10)
                    else '.'
                )

                # We don't have a pretty way of doing this yet. We need
                # either a loop that sends these every 10 seconds or a
                # nice context manager.
                # await self.bot.send_typing(channel)

                # TODO: I can create an event emitter object instead,
                #       add event functions, and every play list might
                #       be asyncified Also have a 'verify_entry' hook
                #       with the entry as an arg and returns the entry
                #       if its ok

                entry_list, pos = await player.playlist.import_from(
                    song_url,
                    channel=inter.channel.id,
                    author=inter.user.id
                )

                t_end = time.time()
                t_time = t_end - t_start
                list_len = len(entry_list)
                t_avg = t_time / list_len
                drop_count = 0

                for e in entry_list.copy():
                    if (e.duration > max_song_length):
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where
                        # this would ever break Unless the first
                        # entry starts being played, which would
                        # make this a race condition
                if (drop_count):
                    print('Dropped {} songs'.format(drop_count))

                log.info(
                    'Processed {} songs in {} seconds at {:.2f}s/song, '
                    '{:+.2g}/song from expected ({} sec)'.format(
                        list_len,
                        fixg(t_time),
                        t_avg if (list_len) else 0,
                        (
                            t_avg - wait_per_song
                            if (list_len - wait_per_song)
                            else 0
                        ),
                        fixg(wait_per_song * num_songs),
                    )
                )

                if (not list_len - drop_count):
                    raise exceptions.CommandError(
                        '沒有添加歌曲，所有歌曲都超過了最長時間 (`{} sec`)'
                        .format(max_song_length)
                    )

                b_text = str(list_len - drop_count)

            # If it's an entry
            else:
                # youtube:playlist extractor but it's actually an entry
                if (
                    info.get('extractor', '')
                    .startswith('youtube:playlist')
                ):
                    try:
                        info = await self.bot.downloader.extract_info(
                            player.playlist.loop,
                            'https://www.youtube.com/watch?v={}'
                            .format(info.get('url', '')),
                            download=False,
                            process=False,
                        )
                    except Exception as e:
                        raise exceptions.CommandError(e)

                if (info.get('duration', 0) > max_song_length):
                    raise exceptions.PermissionsError(
                        '歌曲長度超出限制 (`{} sec`)'
                        .format(max_song_length)
                    )

                entry, pos = await player.playlist.add_entry(
                    song_url, head=head,
                    channel=inter.channel.id,
                    author=inter.user.id
                )

                b_text = entry.title

            reply_text = '**{}** 加入播放清單。位在：{{}}'.format(b_text)

            if (pos == 1 and player.is_stopped):
                reply_text = reply_text.format('下一首！')

            else:
                reply_text = reply_text.format(pos)
                try:
                    reply_text += (
                        ' - 預估等待播放時間：' + ftimedelta(
                            await player.playlist.estimate_time_until(
                                pos, player
                            )
                        )
                    )
                except exceptions.InvalidDataError:
                    reply_text += ' - 無法預估等待播放的時間'
                except:
                    traceback.print_exc()

        await inter.followup.send(reply_text)

    ####################################################################

    # async def _play(self, inter: Interaction, song: str, head: bool = False):
    #     channel = inter.channel
    #     user = inter.user
    #     permissions = self.permissions.for_user(inter.user)

    #     player = self.get_player_in(inter.guild)
    #     if not player and permissions.summon_no_voice:
    #         vc = user.voice.channel if user.voice else None
    #         response = await self.cmd_summon(
    #             channel, inter.guild, user, vc
    #         )  # @TheerapakG: As far as I know voice_channel param is unused

    #         if self.config.embeds:
    #             embed = self._gen_embed()
    #             embed.title = 'summon'
    #             embed.description = response.content
    #             await channel.send(
    #                 embed=embed,
    #                 delete_after=response.delete_after if self.config.delete_messages else 0
    #             )
    #         else:
    #             content = response.content
    #             await channel.send(
    #                 content,
    #                 delete_after=response.delete_after if self.config.delete_messages else 0
    #             )
    #         player = self.get_player_in(inter.guild)

    #     if not player:
    #         raise CommandError(
    #             'The bot is not in a voice channel. '
    #             'Use /summon to summon it to your voice channel.'
    #         )

    #     song = song.strip('<>')

    #     await self.send_typing(channel)

    #     # YouTube URL format
    #     # scheme
    #     # - https://
    #     # - http://
    #     # - (null)

    #     # domain
    #     # - www.youtube.com
    #     # - m.youtube.com
    #     # - youtube.com
    #     # - www.youtube-nocookie.com
    #     # - youtube-nocookie.com

    #     # path
    #     # - /watch?v={vid}
    #     # - /embed/{vid}
    #     # - /live/{vid}
    #     # - /v/{vid}
    #     # - /watch/{vid}

    #     # shortURL
    #     # https://youtu.be/{VideoID}

    #     # song_type = 'text'
    #     # # URL testing
    #     # if re.match(
    #     #     r'^(?:https?://)?:(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&/=]*)$',
    #     #     song
    #     # ):
    #     #     if not song.startswith('http'):
    #     #         song = 'https://' + song
    #     #     url = urlparse(song)

    #     #     if url.netloc == 'youtu.be':
    #     #         if vid := re.match(r'/([a-zA-Z0-9+\-_]{11})', url.path):
    #     #             vid = vid.group(1)
    #     #     else:
    #     #         # This isn't a YouTube URL
    #     #         if not re.match(r'((www|m)\.)?youtube(?:-nocookie)?\.com', url.netloc):
    #     #             raise CommandError('Unknown URL')

    #     #         vid = lid = None
    #     #         # Video
    #     #         if vid := re.match(r'/(?:embed|live|v|watch)/([a-zA-Z0-9+\-_]{11})/?', url.path):
    #     #             vid = vid.group(1)
    #     #         elif url.path == '/watch' and (query := url.query):
    #     #             for q in query.split('&'):
    #     #                 if q.startswith('v=') and re.match(r'[a-zA-Z0-9+\-_]{11}', q[2:]):
    #     #                     vid = q[2:]

    #     #         # Playlist
    #     #         if not vid and url.path == '/playlist' and (query := url.query):
    #     #             for q in query.split('&'):
    #     #                 if q.startswith('list=') and re.match(r'[a-zA-Z0-9+\-_]{34}', q[5:]):
    #     #                     lid = q[5:]

    #     #         # This is a invalid YouTube URL
    #     #         if vid:
    #     #             song_type = 'yt_video'
    #     #             song = 'https://www.youtube.com/watch?v=' + vid
    #     #         elif lid:
    #     #             song_type = 'yt_playlist'
    #     #             song = 'https://www.youtube.com/playlist?list=' + lid
    #     #         else:
    #     #             raise CommandError('Unknown URL')

    #     # Make sure forward slashes work properly in search queries
    #     linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
    #     pattern = re.compile(linksRegex)
    #     matchUrl = pattern.match(song)
    #     song = song.replace(
    #         '/', '%2F') if matchUrl is None else song

    #     # Rewrite YouTube playlist URLs if the wrong URL type is given
    #     playlistRegex = r'watch\?v=.+&(list=[^&]+)'
    #     matches = re.search(playlistRegex, song)
    #     groups = matches.groups() if matches is not None else []
    #     song = (
    #         'https://www.youtube.com/playlist?' + groups[0]
    #         if len(groups) > 0
    #         else song
    #     )

    #     async def get_info(song: song):
    #         info = await self.downloader.extract_info(
    #             player.playlist.loop, song, download=False, process=False
    #         )
    #         # If there is an exception arise when processing we go on and let extract_info down the line report it
    #         # because info might be a playlist and thing that's broke it might be individual entry
    #         try:
    #             info_process = await self.downloader.extract_info(
    #                 player.playlist.loop, song, download=False
    #             )
    #             info_process_err = None
    #         except Exception as e:
    #             info_process = None
    #             info_process_err = e

    #         return (info, info_process, info_process_err)

    #     # This lock prevent spamming play command to add entries that exceeds time limit/ maximum song limit
    #     async with self.aiolocks[_func_() + ':' + str(user.id)]:
    #         if (
    #             permissions.max_songs
    #             and player.playlist.count_for_user(user) >= permissions.max_songs
    #         ):
    #             raise PermissionsError(
    #                 self.i18n.get(
    #                     'cmd-play-limit',
    #                     'You have reached your enqueued song limit ({0})',
    #                 ).format(permissions.max_songs),
    #                 expire_in=30,
    #             )

    #         if player.karaoke_mode and not permissions.bypass_karaoke_mode:
    #             raise PermissionsError(
    #                 self.i18n.get(
    #                     'karaoke-enabled',
    #                     'Karaoke mode is enabled, please try again when its disabled!',
    #                 ),
    #                 expire_in=30,
    #             )

    #         # Try to determine entry type, if _type is playlist then there should be entries
    #         while True:
    #             try:
    #                 info, info_process, info_process_err = await get_info(song)
    #                 log.debug(info)

    #                 if (
    #                     info_process
    #                     and info
    #                     and info_process.get('_type', None) == 'playlist'
    #                     and 'entries' not in info
    #                     and not info.get('url', '').startswith('ytsearch')
    #                 ):
    #                     use_url = info_process.get(
    #                         'webpage_url', None
    #                     ) or info_process.get('url', None)
    #                     if use_url == song:
    #                         log.warning(
    #                             'Determined incorrect entry type, but suggested url is the same. Help.'
    #                         )
    #                         break  # If we break here it will break things down the line and give 'This is a playlist' exception as a result

    #                     log.debug(
    #                         'Assumed url "%s" was a single entry, was actually a playlist'
    #                         % song
    #                     )
    #                     log.debug('Using "%s" instead' % use_url)
    #                     song = use_url
    #                 else:
    #                     break

    #             except Exception as e:
    #                 if 'unknown url type' in str(e):
    #                     song = song.replace(
    #                         ':', ''
    #                     )  # it's probably not actually an extractor
    #                     info, info_process, info_process_err = await get_info(song)
    #                 else:
    #                     raise CommandError(e, expire_in=30)

    #         if not info:
    #             raise CommandError(
    #                 self.i18n.get(
    #                     'cmd-play-noinfo',
    #                     'That video cannot be played. Try using the {0}stream command.',
    #                 ).format(self.config.command_prefix),
    #                 expire_in=30,
    #             )

    #         if (
    #             info.get('extractor', '') not in permissions.extractors
    #             and permissions.extractors
    #         ):
    #             raise PermissionsError(
    #                 self.i18n.get(
    #                     'cmd-play-badextractor',
    #                     'You do not have permission to play media from this service.',
    #                 ),
    #                 expire_in=30,
    #             )

    #         # abstract the search handling away from the user
    #         # our ytdl options allow us to use search strings as input urls
    #         if info.get('url', '').startswith('ytsearch'):
    #             # print('[Command:play] Searching for "%s"' % song)
    #             if info_process:
    #                 info = info_process
    #             else:
    #                 await self.safe_send_message(
    #                     channel, '```\n%s\n```' % info_process_err, expire_in=120
    #                 )
    #                 raise CommandError(
    #                     self.i18n.get(
    #                         'cmd-play-nodata',
    #                         'Error extracting info from search string, youtubedl returned no data. '
    #                         'You may need to restart the bot if this continues to happen.',
    #                     ),
    #                     expire_in=30,
    #                 )

    #             song = info_process.get('webpage_url', None) or info_process.get(
    #                 'url', None
    #             )

    #             if 'entries' in info:
    #                 # if entry is playlist then only get the first one
    #                 song = info['entries'][0]['webpage_url']
    #                 info = info['entries'][0]

    #         # If it's playlist
    #         if 'entries' in info:
    #             await self._do_playlist_checks(
    #                 permissions, player, user, info['entries']
    #             )

    #             num_songs = sum(1 for _ in info['entries'])

    #             if info['extractor'].lower() in [
    #                 'youtube:playlist',
    #                 'soundcloud:set',
    #                 'bandcamp:album',
    #             ]:
    #                 try:
    #                     return await self._cmd_play_playlist_async(
    #                         player,
    #                         channel,
    #                         user,
    #                         permissions,
    #                         song,
    #                         info['extractor'],
    #                     )
    #                 except CommandError:
    #                     raise
    #                 except Exception as e:
    #                     log.error('Error queuing playlist', exc_info=True)
    #                     raise CommandError(
    #                         self.i18n.get(
    #                             'cmd-play-playlist-error',
    #                             'Error queuing playlist:\n`{0}`',
    #                         ).format(e),
    #                         expire_in=30,
    #                     )

    #             t0 = time.time()

    #             # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
    #             # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
    #             # I don't think we can hook into it anyways, so this will have to do.
    #             # It would probably be a thread to check a few playlists and get the speed from that
    #             # Different playlists might download at different speeds though
    #             wait_per_song = 1.2

    #             procmesg = await self.safe_send_message(
    #                 channel,
    #                 self.i18n.get(
    #                     'cmd-play-playlist-gathering-1',
    #                     'Gathering playlist information for {0} songs{1}',
    #                 ).format(
    #                     num_songs,
    #                     self.i18n.get(
    #                         'cmd-play-playlist-gathering-2', ', ETA: {0} seconds'
    #                     ).format(fixg(num_songs * wait_per_song))
    #                     if num_songs >= 10
    #                     else '.',
    #                 ),
    #             )

    #             # We don't have a pretty way of doing this yet. We need either a loop
    #             # that sends these every 10 seconds or a nice context manager.
    #             await self.send_typing(channel)

    #             # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
    #             #       Also have a 'verify_entry' hook with the entry as an arg and returns the entry if its ok

    #             entry_list, position = await player.playlist.import_from(
    #                 song, channel=channel, author=user, head=False
    #             )

    #             tnow = time.time()
    #             ttime = tnow - t0
    #             listlen = len(entry_list)
    #             drop_count = 0

    #             if permissions.max_song_length:
    #                 for e in entry_list.copy():
    #                     if e.duration > permissions.max_song_length:
    #                         player.playlist.entries.remove(e)
    #                         entry_list.remove(e)
    #                         drop_count += 1
    #                         # Im pretty sure there's no situation where this would ever break
    #                         # Unless the first entry starts being played, which would make this a race condition
    #                 if drop_count:
    #                     print('Dropped %s songs' % drop_count)

    #             log.info(
    #                 'Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)'.format(
    #                     listlen,
    #                     fixg(ttime),
    #                     ttime / listlen if listlen else 0,
    #                     ttime / listlen - wait_per_song
    #                     if listlen - wait_per_song
    #                     else 0,
    #                     fixg(wait_per_song * num_songs),
    #                 )
    #             )

    #             await self.safe_delete_message(procmesg)

    #             if not listlen - drop_count:
    #                 raise CommandError(
    #                     self.i18n.get(
    #                         'cmd-play-playlist-maxduration',
    #                         'No songs were added, all songs were over max duration (%ss)',
    #                     )
    #                     % permissions.max_song_length,
    #                     expire_in=30,
    #                 )

    #             reply_text = self.i18n.get(
    #                 'cmd-play-playlist-reply',
    #                 'Enqueued **%s** songs to be played. Position in queue: %s',
    #             )
    #             btext = str(listlen - drop_count)

    #         # If it's an entry
    #         else:
    #             # youtube:playlist extractor but it's actually an entry
    #             if info.get('extractor', '').startswith('youtube:playlist'):
    #                 try:
    #                     info = await self.downloader.extract_info(
    #                         player.playlist.loop,
    #                         'https://www.youtube.com/watch?v=%s' % info.get(
    #                             'url', ''),
    #                         download=False,
    #                         process=False,
    #                     )
    #                 except Exception as e:
    #                     raise CommandError(e, expire_in=30)

    #             if (
    #                 permissions.max_song_length
    #                 and info.get('duration', 0) > permissions.max_song_length
    #             ):
    #                 raise PermissionsError(
    #                     self.i18n.get(
    #                         'cmd-play-song-limit',
    #                         'Song duration exceeds limit ({0} > {1})',
    #                     ).format(info['duration'], permissions.max_song_length),
    #                     expire_in=30,
    #                 )

    #             entry, position = await player.playlist.add_entry(
    #                 song, channel=channel, author=user, head=head
    #             )

    #             reply_text = self.i18n.get(
    #                 'cmd-play-song-reply',
    #                 'Enqueued `%s` to be played. Position in queue: %s',
    #             )
    #             btext = entry.title

    #         if position == 1 and player.is_stopped:
    #             position = self.i18n.get('cmd-play-next', 'Up next!')
    #             reply_text %= (btext, position)

    #         else:
    #             reply_text %= (btext, position)
    #             try:
    #                 time_until = await player.playlist.estimate_time_until(
    #                     position, player
    #                 )
    #                 reply_text += self.i18n.get(
    #                     'cmd-play-eta', ' - estimated time until playing: %s'
    #                 ) % ftimedelta(time_until)
    #             except InvalidDataError:
    #                 reply_text += self.i18n.get(
    #                     'cmd-play-eta-error', ' - cannot estimate time until playing'
    #                 )
    #             except:
    #                 traceback.print_exc()

    #     return Response(reply_text, delete_after=30)

    ####################################################################

    @slash_command(name='play', description='新增歌曲至播放清單')
    async def play(
        self,
        inter: Interaction,
        song_url: str = SlashOption(
            description='YouTube 連結',
            required=True
        )
    ) -> None:
        """
        用法:
            /play <song_link>
            /play text to search for
        由 YouTube 影片連結來新增歌曲至播放清單。
        """
        await self._play(inter, song_url)

    @slash_command(description='插入歌曲至播放清單，並且在下一首播放')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def playnext(
        self,
        inter: Interaction,
        song_url: str = SlashOption(
            description='YouTube 連結',
            required=True
        )
    ) -> None:
        """
        用法:
            /playnext <song_link>
            /playnext text to search for
        插入歌曲至播放清單，並且在下一首播放。
        """

        await self._play(inter, song_url, head=True)

    @slash_command(description='新增 stream 音樂至播放清單')
    async def stream(
        self,
        inter: Interaction,
        song_url: str = SlashOption(
            description='Streaming media URL',
            required=True
        )
    ) -> None:
        """
        用法:
            /stream <song_link>
        Enqueue a media stream.
        This could mean an actual stream like Twitch or shoutcast, or 
        simply streaming media without predownloading it.
        Note: FFmpeg is notoriously bad at handling streams, especially 
        on poor connections. You have been warned.
        """

        await inter.response.defer()

        player = self.bot.get_player_in(inter.guild)

        if (not player):
            await self._join_channel(inter.user.voice.channel)
            player = self.get_player_in(inter.guild)

        song_url = song_url.strip('<>')

        max_songs = self.bot.config.max_playlist_songs
        if (len(player.playlist) >= max_songs):
            raise PermissionsError(
                '播放清單已經滿了！請稍後再點播 (播放清單最多只能點 {0} 首)'
                .format(max_songs),
            )

        entry, _ = await player.playlist.add_stream_entry(
            song_url, channel=inter.channel.id, author=inter.user.id
        )

        await inter.followup.send(
            '成功加入歌曲 `{}`！目前播放清單中有 `{}` 首歌'
            .format(entry.title, len(player.playlist))
        )

    @slash_command(description='顯示現在播放的歌曲')
    @app_checks.check(is_in_voice_channel)
    async def nowplaying(self, inter: Interaction) -> None:
        """
        用法:
            /nowplaying
        顯示現在播放的歌曲。你必須在語音頻道中。
        """

        player = await self.bot.get_player(inter.user.voice.channel)

        if (cur_entry := player.current_entry):
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(
                timedelta(seconds=player.progress)
            )
            song_total = (
                ftimedelta(timedelta(seconds=cur_entry.duration))
                if (cur_entry.duration != None)
                else '(no duration data)'
            )

            is_streaming = isinstance(cur_entry, StreamPlaylistEntry)

            prog_str = (
                '`[{progress}]`'
                if (is_streaming)
                else '`[{progress}/{total}]`'
            ).format(progress=song_progress, total=song_total)
            prog_bar = []

            # percentage shows how much of the current song has already
            # been played
            percentage = 0.0
            if (cur_entry.duration and cur_entry.duration > 0):
                percentage = player.progress / cur_entry.duration

            # create the actual bar
            prog_bar_length = 30
            prog_bar_unit = 1 / prog_bar_length
            for i in range(prog_bar_length):
                if (percentage < prog_bar_unit * i):
                    prog_bar.append('□')
                else:
                    prog_bar.append('■')
            prog_bar = ''.join(prog_bar)

            action = '串流中' if (is_streaming) else '播放中'

            msg = ['現在播放 {action}：']

            if (author := cur_entry.meta.get('author', None)):
                author = inter.guild.get_member(author)
                msg.append('由 **{}** 點播的'.format(author.name))

            msg.append(
                ' **{title}**\n進度： {progress_bar} {progress}\n'
                ':point_right: <{url}>'
            )
            msg = ''.join(msg).format(
                action=action,
                title=cur_entry.title,
                progress_bar=prog_bar,
                progress=prog_str,
                url=cur_entry.url,
            )
        else:
            msg = '沒有歌可以放了！ 用 `/play` 新增歌曲。'

        await inter.send(msg)

    @slash_command(description='加入使用者的語音頻道')
    @app_checks.check(is_in_voice_channel)
    async def join(self, inter: Interaction) -> None:
        """
        用法:
            /join
        加入使用者的語音頻道。你必須在語音頻道中。
        """

        voice_ch = inter.user.voice.channel

        await self._join_channel(voice_ch)

        await inter.send('成功連接至頻道 `{0}`'.format(voice_ch.name))

    # @join.error
    # async def join_error(
    #     self,
    #     inter: Interaction,
    #     app_err: ApplicationError
    # ) -> None:
    #     log.critical('Join error handler!')
    #     exc_type, exc, _ = sys.exc_info()

    #     log.debug(app_err.__dict__)

    #     embed = self.bot._gen_embed('error')

    #     if (exc_type == CommandError):
    #         embed.title = '指令發生錯誤'
    #         embed.description = exc.message
    #     elif (exc_type == PermissionsError):
    #         embed.title = '您沒有權限執行此指令'
    #         embed.description = exc.message
    #     else:
    #         embed.title = '發生未知錯誤'
    #         embed.description = str(exc)

    #         inter.message
    #         log.error('Exception happened', exc_info=True)

    #     await inter.send(embed=embed)

    @slash_command(description='離開語音頻道')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def disconnect(self, inter: Interaction) -> None:
        """
        用法:
            /disconnect
        使機器人離開語音頻道
        """

        await self.bot.disconnect_voice_client(inter.guild)
        await inter.send(
            '成功離開頻道 `{}`'.format(inter.user.voice.channel.name)
        )

    @slash_command(description='暫停播放')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def pause(self, inter: Interaction) -> None:
        """
        用法:
            /pause
        暫停播放。
        """

        player = await self.bot.get_player(inter.user.voice.channel)

        if (player.is_playing):
            player.pause()
            await inter.send('成功暫停播放')

        else:
            raise CommandError('播放器已經停止。')

    @slash_command(description='繼續播放')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def resume(self, inter: Interaction) -> None:
        """
        用法:
            /resume
        繼續播放。
        """

        player = await self.bot.get_player(inter.user.voice.channel)

        if (player.is_paused):
            player.resume()
            await inter.send('成功繼續播放')
        elif (player.is_stopped and player.playlist):
            player.play()
            await inter.send('成功開始播放')
        else:
            raise CommandError('播放器已經在播放。')

    @slash_command(description='隨機排列播放清單')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def shuffle(self, inter: Interaction) -> None:
        """
        用法:
            /shuffle
        隨機排列播放清單。
        """

        player = await self.bot.get_player(inter.user.voice.channel)
        player.playlist.shuffle()
        await inter.send('重新排列播放清單成功')

    @slash_command(description='清除播放清單')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def clear(self, inter: Interaction) -> None:
        """
        用法:
            /clear
        清除播放清單。
        """

        player = await self.bot.get_player(inter.user.voice.channel)

        player.playlist.clear()
        await inter.send('成功清除播放清單')

    @slash_command(description='清除播放清單的一首歌曲')
    @app_checks.check(is_in_voice_channel)
    async def remove(
        self,
        inter: Interaction,
        index: Optional[int] = SlashOption(
            description='該歌曲在播放清單中的位置，預設為最後一首',
            required=False,
            min_value=1,
            default=None
        )
    ) -> None:
        """
        用法:
            /remove [index]
        清除播放清單的第 `index` 首歌曲。若沒有指定，則是最後一首
        """

        voice_ch = inter.user.voice.channel
        user_perms = voice_ch.permissions_for(inter.user)
        player = await self.bot.get_player(voice_ch)
        playlist = player.playlist

        if (not playlist.entries):
            raise CommandError('沒有歌曲可以刪除了！')

        if (not index):
            index = len(playlist.entries)
        index -= 1

        if (index > len(playlist.entries)):
            raise CommandError(
                '無效的號碼。使用 `/queue` 查詢播放清單位置'
            )

        author = playlist.get_entry_at_index(index).meta.get(
            'author', None
        )

        if (
            not user_perms.manage_channels
            and inter.user.id != author
        ):
            raise PermissionsError(
                '您沒有從播放清單中刪除該歌曲的權限'
            )

        entry = playlist.delete_entry_at_index(index)

        if (author):
            author = inter.guild.get_member(author)
            msg = (
                '成功刪除了由 `{1}` 添加的 `{0}`'
                .format(entry.title, author.name)
                .strip()
            )
        else:
            msg = '成功刪除了歌曲 `{}`'.format(entry.title).strip()

        await inter.send(msg)

    @slash_command(description='跳過正在播放的歌曲')
    @app_checks.check(is_in_voice_channel)
    async def skip(self, inter: Interaction) -> None:
        """
        用法:
            /skip
        跳過正在播放的歌曲。
        """

        voice_ch = inter.user.voice.channel
        user_perms = voice_ch.permissions_for(inter.user)
        player = await self.bot.get_player(voice_ch)
        cur_entry = player.current_entry
        playlist = player.playlist

        if (player.is_stopped):
            raise CommandError('無法跳過！播放器沒在播放！')

        if (not cur_entry):
            if (next := playlist.peek()):
                if (next._is_downloading):
                    await inter.send(
                        '下一首 (`{}`) 正在下載中，請稍等。'.format(
                            next.title
                        )
                    )
                    return

                elif (next.is_downloaded):
                    print(
                        'The next song will be played shortly. Please '
                        'wait.'
                    )
                else:
                    print(
                        'Something odd is happening. You might want to '
                        'restart the bot if it doesn\'t start working.'
                    )
            else:
                print(
                    'Something strange is happening. You might want to '
                    'restart the bot if it doesn\'t start working.'
                )

        author = cur_entry.meta.get('author', None)

        if (not user_perms.manage_channels and inter.user.id != author):
            raise PermissionsError('您沒有跳過這首歌曲的權限')

        player.skip()  # TODO: check autopause stuff here
        await inter.send('跳過 `{}`'.format(cur_entry.title))

    @slash_command(description='設置播放音樂的音量')
    @app_checks.check(is_in_voice_channel)
    @app_checks.check(has_manage_perission_in_voice_channel)
    async def volume(
        self,
        inter: Interaction,
        value: Optional[int] = SlashOption(
            description='新的音量，需介於 0 ~ 100 之間',
            required=False,
            min_value=0,
            max_value=100,
            default=None
        )
    ) -> None:
        """
        用法:
            /volume [value]
        設置播放音樂的音量。
        `value` 需介於 0 ~ 100 之間，若沒填入數值則顯示目前的音量。
        """

        player = await self.bot.get_player(inter.user.voice.channel)

        if (not value):
            await inter.send(
                '現在音量： `{}%`'.format(player.volume * 100),
                ephemeral=True
            )

        old_val = int(player.volume * 100)

        player.volume = value / 100.0

        await inter.send(
            '成功將音量從 **{}**% 更改到 **{}**%'.format(old_val, value)
        )

    @slash_command(description='查詢播放清單')
    @app_checks.check(is_in_voice_channel)
    async def queue(
        self,
        inter: Interaction,
        page: int = SlashOption(
            description='頁數',
            required=False,
            min_value=1,
            default=1
        )
    ) -> None:
        """
        用法:
            /queue [page]
        查詢播放清單。預設為第一頁。
        """

        player = await self.bot.get_player(inter.user.voice.channel)
        lines = []

        limit = self.bot.config.queue_length
        start = (page - 1) * limit + 1
        end = start + limit

        for i, entry in enumerate(player.playlist, 1):
            if (i < start):
                continue
            if (i > end):
                break

            title = (
                entry.title
                if (len(entry.title) <= 200)
                else entry.title[:197] + '...'
            )
            next_line = '#{} `{}`'.format(i, title)
            if (author := entry.meta.get('author', None)):
                author = inter.guild.get_member(author)
                next_line += '由 `{}` 點播'.format(author.name)

            lines.append(next_line)

        if (not lines):
            lines.append('沒有歌可以放了！ 用 /play 新增歌曲。')

        msg = '\n'.join(lines)
        await inter.send(msg)
