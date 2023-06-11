import os
import sys
import logging

from configparser import ConfigParser
from typing import Optional, List, Set

from nextcord import AppInfo, ClientUser

from .exceptions import HelpfulError


log = logging.getLogger(__name__)

all_keys = {
    'Token', 'OwnerID', 'DefaultVolume', 'SkipsRequired', 'SkipRatio',
    'SaveVideos', 'AutoPause', 'DeleteMessages', 'DeleteInvoking',
    'PersistentQueue', 'DebugLevel', 'StatusMessage',
    'WriteCurrentSong', 'AllowAuthorSkip',
    'UseExperimentalEqualization', 'QueueLength', 'MaxPlaylistSongs',
    'ShowConfigOnLaunch', 'LegacySkip', 'LeaveServersWithoutOwner',
    'SearchList', 'DefaultSearchResults', 'CustomEmbedFooter',
    'i18nFile'
}


class ConfigDefaults:
    owner_id = None

    token = None
    dev_ids: Set[int] = set()
    bot_exception_ids: Set[int] = set()

    command_prefix = '!'
    bound_channels: Set[int] = set()
    unbound_servers = False
    autojoin_channels: Set[int] = set()
    dm_nowplaying = False
    no_nowplaying_auto = False
    nowplaying_channels: Set[int] = set()
    delete_nowplaying = True

    default_volume = 0.15
    skips_required = 4
    skip_ratio_required = 0.5
    save_videos = True
    now_playing_mentions = False
    auto_summon = True
    auto_playlist = True
    auto_playlist_random = True
    auto_pause = True
    delete_messages = True
    delete_invoking = False
    persistent_queue = True
    debug_level = 'INFO'
    status_message = None
    write_current_song = False
    allow_author_skip = True
    use_experimental_equalization = False
    embeds = True
    queue_length = 10
    max_playlist_songs = 300
    max_song_length = 600
    remove_ap = True
    show_config_at_start = False
    legacy_skip = False
    leave_non_owners = False
    usealias = True
    searchlist = False
    defaultsearchresults = 3
    footer_text = 'MusicBot'

    options_file = 'config/options.ini'
    blacklist_file = 'config/blacklist.txt'
    i18n_file = 'config/i18n/zh_TW.json'


class Config:
    # noinspection PyUnresolvedReferences
    def __init__(self, config_file: Optional[str] = None) -> None:
        config_file = config_file or ConfigDefaults.options_file
        self.config_file = config_file
        self.find_config()

        config = ConfigParser(interpolation=None)
        config.read(config_file, encoding='utf-8')

        if (
            confsections := {
                'Credentials', 'Permissions', 'MusicBot', 'Files'
            }.difference(config.sections())
        ):
            raise HelpfulError(
                'One or more required config sections are missing.',
                'Fix your config. Each [Section] should be on its own '
                'line with nothing else on it. The following sections '
                'are missing: {}'.format(
                    ', '.join(['[' + s + ']' for s in confsections])
                ),
                preface='An error has occured parsing the config:\n'
            )

        self._confpreface = 'An error has occured reading the config:\n'
        self._confpreface2 = (
            'An error has occured validating the config:\n'
        )

        ##############################
        #        Credentials         #
        ##############################

        self._login_token = config.get(
            'Credentials', 'Token', fallback=ConfigDefaults.token
        )

        if (not self._login_token):
            raise HelpfulError(
                'No bot token was specified in the config.',
                'You are required to use a Discord bot account.',
                preface=self._confpreface
            )
        else:
            self.auth = (self._login_token,)

        ##############################
        #        Permissions         #
        ##############################

        owner_id = config.get(
            'Permissions', 'OwnerID', fallback=ConfigDefaults.owner_id
        )

        if (owner_id):
            owner_id = owner_id.lower()

            if (owner_id.isdigit()):
                if (int(owner_id) < 10000):
                    raise HelpfulError(
                        'An invalid OwnerID was set: {}'.format(
                            owner_id
                        ),
                        'Correct your OwnerID. The ID should be just a '
                        'number, approximately 18 characters long, or '
                        '\'auto\'. If you don\'t know what your ID is, '
                        'read the instructions in the options.',
                        preface=self._confpreface
                    )
                owner_id = int(owner_id)
            elif (owner_id == 'auto'):
                owner_id = 'auto'
                # defer to async check
            else:
                owner_id = None

        if (not owner_id):
            raise HelpfulError(
                'No OwnerID was set.',
                'Please set the OwnerID option in {}'.format(
                    self.config_file
                ),
                preface=self._confpreface
            )
        self.owner_id = owner_id

        dev_ids = config.get(
            'Permissions', 'DevIDs', fallback=None
        )

        if (dev_ids):
            try:
                self.dev_ids = set(
                    int(x)
                    for x in
                    dev_ids.replace(',', ' ').split()
                )
            except:
                log.warning(
                    'BotExceptionIDs data is invalid, will ignore all '
                    'bots'
                )
                self.dev_ids = ConfigDefaults.dev_ids
        else:
            self.dev_ids = ConfigDefaults.dev_ids

        bot_exception_ids = config.get(
            'Permissions', 'BotExceptionIDs', fallback=None
        )

        if (bot_exception_ids):
            try:
                self.bot_exception_ids = set(
                    int(x)
                    for x in
                    bot_exception_ids.replace(',', ' ').split()
                )
            except:
                log.warning(
                    'BotExceptionIDs data is invalid, will ignore all '
                    'bots'
                )
                self.bot_exception_ids: Set[int] = set()
        else:
            self.bot_exception_ids = ConfigDefaults.bot_exception_ids

        ##############################
        #            Chat            #
        ##############################

        self.command_prefix = config.get(
            'Chat', 'CommandPrefix',
            fallback=ConfigDefaults.command_prefix
        )

        bound_channels = config.get(
            'Chat', 'BindToChannels',
            fallback=ConfigDefaults.bound_channels
        )

        if (bound_channels):
            try:
                self.bound_channels = set(
                    int(x)
                    for x in
                    self.bound_channels.replace(',', ' ').split()
                    if x
                )
            except:
                log.warning(
                    'BindToChannels data is invalid, will not bind to '
                    'any channels'
                )
                self.bound_channels: Set[int] = set()
        else:
            self.bound_channels = ConfigDefaults.bound_channels

        self.unbound_servers = config.getboolean(
            'Chat', 'AllowUnboundServers',
            fallback=ConfigDefaults.unbound_servers
        )

        autojoin_channels = config.get(
            'Chat', 'AutojoinChannels', fallback=None
        )

        if (autojoin_channels):
            try:
                self.autojoin_channels = set(
                    int(x)
                    for x in
                    autojoin_channels.replace(',', ' ').split()
                    if x
                )
            except:
                log.warning(
                    'AutojoinChannels data is invalid, will not '
                    'autojoin any channels'
                )
                self.autojoin_channels = set()
        else:
            self.autojoin_channels = ConfigDefaults.autojoin_channels

        self.dm_nowplaying = config.getboolean(
            'Chat', 'DMNowPlaying',
            fallback=ConfigDefaults.dm_nowplaying
        )

        self.no_nowplaying_auto = config.getboolean(
            'Chat', 'DisableNowPlayingAutomatic',
            fallback=ConfigDefaults.no_nowplaying_auto
        )

        nowplaying_channels = config.get(
            'Chat', 'NowPlayingChannels', fallback=None
        )

        if (nowplaying_channels):
            try:
                self.nowplaying_channels = set(
                    int(x)
                    for x in
                    nowplaying_channels.replace(',', ' ').split()
                    if x
                )
            except:
                log.warning(
                    'NowPlayingChannels data is invalid, will use the '
                    'default behavior for all servers'
                )
                self.nowplaying_channels: Set[int] = set()
        else:
            self.nowplaying_channels = \
                ConfigDefaults.nowplaying_channels

        self.delete_nowplaying = config.getboolean(
            'Chat', 'DeleteNowPlaying',
            fallback=ConfigDefaults.delete_nowplaying
        )

        ##############################
        #          MusicBot          #
        ##############################

        self.default_volume = config.getfloat(
            'MusicBot', 'DefaultVolume',
            fallback=ConfigDefaults.default_volume
        )

        self.skips_required = config.getint(
            'MusicBot', 'SkipsRequired',
            fallback=ConfigDefaults.skips_required
        )

        self.skip_ratio_required = config.getfloat(
            'MusicBot', 'SkipRatio',
            fallback=ConfigDefaults.skip_ratio_required
        )

        self.save_videos = config.getboolean(
            'MusicBot', 'SaveVideos',
            fallback=ConfigDefaults.save_videos
        )

        self.now_playing_mentions = config.getboolean(
            'MusicBot', 'NowPlayingMentions',
            fallback=ConfigDefaults.now_playing_mentions
        )

        self.auto_summon = config.getboolean(
            'MusicBot', 'AutoSummon',
            fallback=ConfigDefaults.auto_summon
        )

        self.auto_playlist = config.getboolean(
            'MusicBot', 'UseAutoPlaylist',
            fallback=ConfigDefaults.auto_playlist
        )

        self.auto_playlist_random = config.getboolean(
            'MusicBot', 'AutoPlaylistRandom',
            fallback=ConfigDefaults.auto_playlist_random
        )

        self.auto_pause = config.getboolean(
            'MusicBot', 'AutoPause', fallback=ConfigDefaults.auto_pause
        )

        self.delete_messages = config.getboolean(
            'MusicBot', 'DeleteMessages',
            fallback=ConfigDefaults.delete_messages
        )

        self.delete_invoking = config.getboolean(
            'MusicBot', 'DeleteInvoking',
            fallback=ConfigDefaults.delete_invoking
        )
        self.delete_invoking = (
            self.delete_invoking and self.delete_messages
        )

        self.persistent_queue = config.getboolean(
            'MusicBot', 'PersistentQueue',
            fallback=ConfigDefaults.persistent_queue
        )

        self.status_message = config.get(
            'MusicBot', 'StatusMessage',
            fallback=ConfigDefaults.status_message
        )

        self.write_current_song = config.getboolean(
            'MusicBot', 'WriteCurrentSong',
            fallback=ConfigDefaults.write_current_song
        )

        self.allow_author_skip = config.getboolean(
            'MusicBot', 'AllowAuthorSkip',
            fallback=ConfigDefaults.allow_author_skip
        )

        self.use_experimental_equalization = config.getboolean(
            'MusicBot', 'UseExperimentalEqualization',
            fallback=ConfigDefaults.use_experimental_equalization
        )

        self.embeds = config.getboolean(
            'MusicBot', 'UseEmbeds', fallback=ConfigDefaults.embeds
        )

        self.queue_length = config.getint(
            'MusicBot', 'QueueLength',
            fallback=ConfigDefaults.queue_length
        )

        self.max_playlist_songs = config.getint(
            'MusicBot', 'MaxPlaylistSongs',
            fallback=ConfigDefaults.max_playlist_songs
        )
        
        self.max_song_length = config.getint(
            'MusicBot', 'MaxSongLength',
            fallback=ConfigDefaults.max_song_length
        )

        self.remove_ap = config.getboolean(
            'MusicBot', 'RemoveFromAPOnError',
            fallback=ConfigDefaults.remove_ap
        )

        self.show_config_at_start = config.getboolean(
            'MusicBot', 'ShowConfigOnLaunch',
            fallback=ConfigDefaults.show_config_at_start
        )

        self.legacy_skip = config.getboolean(
            'MusicBot', 'LegacySkip',
            fallback=ConfigDefaults.legacy_skip
        )

        self.leave_non_owners = config.getboolean(
            'MusicBot', 'LeaveServersWithoutOwner',
            fallback=ConfigDefaults.leave_non_owners
        )

        self.usealias = config.getboolean(
            'MusicBot', 'UseAlias',
            fallback=ConfigDefaults.usealias
        )

        self.footer_text = config.get(
            'MusicBot', 'CustomEmbedFooter',
            fallback=ConfigDefaults.footer_text
        )

        if (not self.footer_text):
            self.footer_text = ConfigDefaults.footer_text

        self.searchlist = config.getboolean(
            'MusicBot', 'SearchList',
            fallback=ConfigDefaults.searchlist
        )

        self.defaultsearchresults = config.getint(
            'MusicBot', 'DefaultSearchResults',
            fallback=ConfigDefaults.defaultsearchresults
        )

        self.debug_level = config.get(
            'MusicBot', 'DebugLevel',
            fallback=ConfigDefaults.debug_level
        )

        self.debug_level_str = self.debug_level
        self.debug_mode = False

        if (hasattr(logging, self.debug_level.upper())):
            self.debug_level = getattr(
                logging, self.debug_level.upper()
            )
        else:
            log.warning(
                'Invalid DebugLevel option \'{}\' given, falling back '
                'to INFO'.format(self.debug_level_str)
            )
            self.debug_level = logging.INFO
            self.debug_level_str = 'INFO'

        self.debug_mode = self.debug_level <= logging.DEBUG

        ##############################
        #           Files            #
        ##############################

        self.blacklist_file = config.get(
            'Files', 'BlacklistFile',
            fallback=ConfigDefaults.blacklist_file
        )

        self.i18n_file = config.get(
            'Files', 'i18nFile', fallback=ConfigDefaults.i18n_file
        )

        if (
            self.i18n_file != ConfigDefaults.i18n_file
            and not os.path.isfile(self.i18n_file)
        ):
            log.warning(
                'i18n file does not exist. '
                'Trying to fallback to {0}.'.format(
                    ConfigDefaults.i18n_file
                )
            )
            self.i18n_file = ConfigDefaults.i18n_file

        if (not os.path.isfile(self.i18n_file)):
            raise HelpfulError(
                'Your i18n file was not found, '
                'and we could not fallback.',
                'As a result, the bot cannot launch. Have you moved '
                'some files? Try resetting your local repo.',
                preface=self._confpreface
            )

        log.info('Using i18n: {0}'.format(self.i18n_file))

        ##############################

        # self.create_empty_file_ifnoexist('config/blacklist.txt')
        # self.create_empty_file_ifnoexist('config/whitelist.txt')

        self.missing_keys: Set[str] = set()
        self.check_missing(config)

    def get_all_keys(self, conf: ConfigParser) -> List[str]:
        """
        Returns all config keys as a list
        """
        keys: List[str] = []
        for _, sect in conf.items():
            keys += [key for key in sect.keys()]
        return keys

    def check_missing(self, conf: ConfigParser) -> None:
        usr_keys = set(self.get_all_keys(conf))
        log.info(usr_keys)
        if usr_keys != all_keys:
            self.missing_keys = all_keys - usr_keys
            # to raise this as an issue in `bot.py` later

    def create_empty_file_ifnoexist(self, path: str) -> None:
        if (not os.path.isfile(path)):
            open(path, 'a').close()
            log.warning('Creating {}'.format(path))

    # TODO: Add save function for future editing of options with commands
    #       Maybe add warnings about fields missing from the config file

    async def async_validate(
        self, bot: ClientUser, cached_app_info: AppInfo
    ) -> None:
        log.debug('Validating options...')

        if (self.owner_id == 'auto'):
            if (not bot.bot):
                raise HelpfulError(
                    'Invalid parameter "auto" for OwnerID option.',
                    'Only bot accounts can use the "auto" option. '
                    'Please set the OwnerID in the config.',
                    preface=self._confpreface2
                )

            self.owner_id = cached_app_info.owner.id
            log.debug('Acquired owner id via API')

        if (self.owner_id == bot.id):
            raise HelpfulError(
                'Your OwnerID is incorrect or you\'ve used the wrong '
                'credentials.',
                'The bot\'s user ID and the id for OwnerID is '
                'identical. This is wrong. The bot needs a bot account '
                'to function, meaning you cannot use your own account '
                'to run the bot on. The OwnerID is the id of the '
                'owner, not the bot. Figure out which one is which and '
                'use the correct information.',
                preface=self._confpreface2
            )

    def find_config(self) -> None:
        config = ConfigParser(interpolation=None)

        if (not os.path.isfile(self.config_file)):
            raise HelpfulError(
                'Your config files are missing. `options.ini` wasn\'t '
                'found.',
                'Don\'t removing important files!'
            )

        if (not config.read(self.config_file, encoding='utf-8')):
            c = ConfigParser()
            try:
                # load the config again and check to see if the user
                # edited that one
                c.read(self.config_file, encoding='utf-8')

                if (not int(
                    c.get('Permissions', 'OwnerID', fallback=0)
                )):
                    # jake pls no flame
                    print(flush=True)
                    log.critical(
                        'Please configure config/options.ini and '
                        're-run the bot.'
                    )
                    sys.exit(1)

            except ValueError:
                # Config id value was changed but its not valid
                raise HelpfulError(
                    'Invalid value \'{}\' for OwnerID, config cannot '
                    'be loaded. '.format(
                        c.get('Permissions', 'OwnerID', fallback=None)
                    ),
                    'The OwnerID option requires a user ID or "auto".'
                )

    def write_default_config(self, location) -> None:
        pass


# These two are going to be wrappers for the id lists, with
# add/remove/load/save functions and id/object conversion so types
# aren't an issue


class Blacklist:
    pass


class Whitelist:
    pass
