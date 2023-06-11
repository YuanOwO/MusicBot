from __future__ import annotations

import os.path
import logging

from asyncio import AbstractEventLoop
from collections import deque
from datetime import timedelta
from itertools import islice
from random import shuffle
from typing import (
    Iterator, Optional, Tuple, Union,
    Any, Deque, Dict, List
)
from urllib.error import URLError

from aiohttp import ClientSession
from nextcord import Member, User
# For the time being, youtube_dl is often slow and inconsistent
# With this in mind, lets stick to the fork until it gets a dev
from yt_dlp.utils import (
    DownloadError,
    UnsupportedError
)

from .config import Config
from .constructs import Serializable
from .downloader import Downloader
from .entry import URLPlaylistEntry, StreamPlaylistEntry, PlaylistEntry
from .exceptions import (
    ExtractionError,
    InvalidDataError,
    WrongEntryTypeError
)
from .lib.event_emitter import EventEmitter
from .utils import get_header


log = logging.getLogger(__name__)


class Playlist(EventEmitter, Serializable):
    """
    A playlist is manages the list of songs that will be played.
    """

    def __init__(
        self, aiosession: ClientSession, config: Config,
        downloader: Downloader, loop: AbstractEventLoop
    ) -> None:
        super().__init__()
        self.aiosession = aiosession
        self.config = config
        self.downloader = downloader
        self.loop = loop
        self.entries: Deque[PlaylistEntry] = deque()

    def __iter__(self) -> Iterator[PlaylistEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def shuffle(self) -> None:
        shuffle(self.entries)

    def clear(self) -> None:
        self.entries.clear()

    def get_entry_at_index(self, index: int) -> PlaylistEntry:
        self.entries.rotate(-index)
        entry = self.entries[0]
        self.entries.rotate(index)
        return entry

    def delete_entry_at_index(self, index: int) -> PlaylistEntry:
        self.entries.rotate(-index)
        entry = self.entries.popleft()
        self.entries.rotate(index)
        return entry

    async def add_entry(
        self, song_url: str, *, head: bool, **meta: int
    ) -> Tuple[PlaylistEntry, int]:
        """
        Validates and adds a song_url to be played. This does not start
        the download of the song.

        Returns the entry & the position it is in the queue.

        :param song_url: The song url to add to the playlist.
        :param meta: Any additional metadata to add to the playlist
        entry.
        """

        try:
            info = await self.downloader.extract_info(
                self.loop, song_url, download=False
            )
        except Exception as e:
            raise ExtractionError(
                'Could not extract information from {}\n\n{}'.format(
                    song_url, e
                )
            )

        if (not info):
            raise ExtractionError(
                'Could not extract information from {}'.format(
                    song_url
                )
            )

        # TODO: Sort out what happens next when this happens
        if (info.get('_type', None) == 'playlist'):
            raise WrongEntryTypeError(
                'This is a playlist.',
                True,
                info.get('webpage_url', None) or info.get('url', None),
            )

        if (info.get('is_live', False)):
            return await self.add_stream_entry(
                song_url, info=info, **meta
            )

        # TODO: Extract this to its own function
        if (info['extractor'] in ['generic', 'Dropbox']):
            log.debug('Detected a generic extractor, or Dropbox')
            try:
                headers = await get_header(self.aiosession, info['url'])
                content_type = headers.get('CONTENT-TYPE')
                log.debug('Got content type {}'.format(content_type))
            except Exception as e:
                log.warning(
                    'Failed to get content type for url {} ({})'.format(
                        song_url, e
                    )
                )
                content_type = None

            if (content_type):
                if (
                    content_type.startswith(('application/', 'image/'))
                ):
                    if (
                        not any(
                            x in content_type
                            for x in ('/ogg', '/octet-stream')
                        )
                    ):
                        # How does a server say `application/ogg` what
                        # the actual fuck
                        raise ExtractionError(
                            'Invalid content type "{}" for url '
                            '{}'.format(content_type, song_url)
                        )

                elif (
                    content_type.startswith('text/html')
                    and info['extractor'] == 'generic'
                ):
                    log.warning(
                        'Got text/html for content-type, '
                        'this might be a stream.'
                    )
                    # TODO: Check for shoutcast/icecast
                    return await self.add_stream_entry(
                        song_url, info=info, **meta
                    )

                elif (
                    not content_type.startswith(('audio/', 'video/'))
                ):
                    log.warning(
                        'Questionable content-type "{}" '
                        'for url {}'.format(
                            content_type, song_url
                        )
                    )

        entry = URLPlaylistEntry(
            self.aiosession, self.config, self.downloader, self.loop,
            song_url,
            info.get('title', 'Untitled'),
            info.get('duration', None) or None,
            self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry, head=head)
        return entry, (1 if head else len(self.entries))

    async def add_stream_entry(
        self,
        song_url: str,
        info: Optional[Dict[str, Any]] = None,
        **meta: int
    ) -> Tuple[StreamPlaylistEntry, int]:
        if (info) is None:
            info = {'title': song_url, 'extractor': None}

            try:
                info = await self.downloader.extract_info(
                    self.loop, song_url, download=False
                )

            except DownloadError as e:
                if (
                    e.exc_info[0] == UnsupportedError
                ):
                    # ytdl doesn't like it but its probably a stream
                    log.debug('Assuming content is a direct stream')

                elif e.exc_info[0] == URLError:
                    if (os.path.exists(os.path.abspath(song_url))):
                        raise ExtractionError(
                            'This is not a stream, this is a file path.'
                        )
                    else:
                        # it might be a file path that just doesn't
                        # exist
                        raise ExtractionError(
                            'Invalid input: {0.exc_info[0]}: '
                            '{0.exc_info[1].reason}'.format(e)
                        )

                else:
                    # traceback.print_exc()
                    raise ExtractionError(
                        'Unknown error: {}'.format(e)
                    )

            except Exception as e:
                log.error(
                    'Could not extract information from {} ({}), '
                    'falling back to direct'.format(song_url, e),
                    exc_info=True,
                )

        if (
            info.get('is_live') is None
            and info.get('extractor', None) != 'generic'
        ):
            # wew hacky
            raise ExtractionError('This is not a stream.')

        dest_url = song_url
        if (info.get('extractor')):
            dest_url = info.get('url')

        if (
            info.get('extractor', None) == 'twitch:stream'
        ):
            # may need to add other twitch types
            title = info.get('description')
        else:
            title = info.get('title', 'Untitled')

        # TODO: A bit more validation, '~stream some_url' should not
        #       just say :ok_hand:

        entry = StreamPlaylistEntry(
            self.aiosession, self.config, self.downloader, self.loop,
            song_url, title, destination=dest_url, **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def import_from(
        self, playlist_url: str, head=False, **meta: int
    ) -> Tuple[List[URLPlaylistEntry], int]:
        """
        Imports the songs from `playlist_url` and queues them to be
        played.

        Returns a list of `entries` that have been enqueued.

        :param playlist_url: The playlist url to be cut into individual
        urls and added to the playlist
        :param meta: Any additional metadata to add to the playlist
        entry
        """
        position = 1 if head else len(self.entries) + 1
        entry_list: List[URLPlaylistEntry] = []

        try:
            info = await self.downloader.safe_extract_info(
                self.loop, playlist_url, download=False
            )
        except Exception as e:
            raise ExtractionError(
                'Could not extract information from {}\n\n{}'.format(
                    playlist_url, e
                )
            )

        if (not info):
            raise ExtractionError(
                'Could not extract information from {}'.format(
                    playlist_url
                )
            )

        # Once again, the generic extractor fucks things up.
        if (info.get('extractor', None) == 'generic'):
            url_field = 'url'
        else:
            url_field = 'webpage_url'

        baditems = 0
        entries = list(info['entries'])
        if (head):
            entries.reverse()
        for item in info['entries']:
            if (item):
                try:
                    entry = URLPlaylistEntry(
                        self.aiosession,
                        self.config,
                        self.downloader,
                        self.loop,
                        item[url_field],
                        item.get('title', 'Untitled'),
                        item.get('duration', 0) or 0,
                        self.downloader.ytdl.prepare_filename(item),
                        **meta
                    )

                    self._add_entry(entry, head=head)
                    entry_list.append(entry)
                except Exception as e:
                    baditems += 1
                    log.warning('Could not add item', exc_info=e)
                    log.debug('Item: {}'.format(item), exc_info=True)
            else:
                baditems += 1

        if (baditems):
            log.info('Skipped {} bad entries'.format(baditems))

        if (head):
            entry_list.reverse()
        return entry_list, position

    async def async_process_youtube_playlist(
        self, playlist_url: str, *, head: bool, **meta: int
    ) -> List[PlaylistEntry]:
        """
        Processes youtube playlists links from `playlist_url` in a
        questionable, async fashion.

        :param playlist_url: The playlist url to be cut into individual
        urls and added to the playlist
        :param meta: Any additional metadata to add to the playlist
        entry
        """

        try:
            info = await self.downloader.safe_extract_info(
                self.loop, playlist_url, download=False, process=False
            )
        except Exception as e:
            raise ExtractionError(
                'Could not extract information from {}\n\n{}'.format(
                    playlist_url, e
                )
            )

        if (not info):
            raise ExtractionError(
                'Could not extract information from {}'.format(
                    playlist_url
                )
            )

        gooditems: List[PlaylistEntry] = []
        baditems = 0

        entries = list(info['entries'])
        if (head):
            entries.reverse()
        for entry_data in info['entries']:
            if (entry_data):
                baseurl = info['webpage_url'].split('playlist?list=')[0]
                song_url = baseurl + 'watch?v=' + entry_data['id']

                try:
                    entry, elen = await self.add_entry(
                        song_url, head=head, **meta
                    )
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error(
                        'Error adding entry {}'.format(
                            entry_data['id']
                        ),
                        exc_info=e
                    )
            else:
                baditems += 1

        if (baditems):
            log.info('Skipped {} bad entries'.format(baditems))

        if (head):
            gooditems.reverse()
        return gooditems

    async def async_process_sc_bc_playlist(
        self, playlist_url: str, *, head=False, **meta: int
    ) -> List[PlaylistEntry]:
        """
        Processes soundcloud set and bancdamp album links from 
        `playlist_url` in a questionable, async fashion.

        :param playlist_url: The playlist url to be cut into individual
        urls and added to the playlist
        :param meta: Any additional metadata to add to the playlist
        entry
        """

        try:
            info = await self.downloader.safe_extract_info(
                self.loop, playlist_url, download=False, process=False
            )
        except Exception as e:
            raise ExtractionError(
                'Could not extract information from {}\n\n{}'.format(
                    playlist_url, e
                )
            )

        if (not info):
            raise ExtractionError(
                'Could not extract information from {}'.format(
                    playlist_url
                )
            )

        gooditems: List[PlaylistEntry] = []
        baditems = 0

        entries = list(info['entries'])
        if (head):
            entries.reverse()
        for entry_data in info['entries']:
            if (entry_data):
                song_url = entry_data['url']

                try:
                    entry, e_len = await self.add_entry(
                        song_url, head=head, **meta
                    )
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error(
                        'Error adding entry {}'.format(
                            entry_data['id']
                        ),
                        exc_info=e
                    )
            else:
                baditems += 1

        if (baditems):
            log.info('Skipped {} bad entries'.format(baditems))

        if (head):
            gooditems.reverse()

        return gooditems

    def _add_entry(self, entry: PlaylistEntry, *, head=False) -> None:
        if (head):
            self.entries.appendleft(entry)
        else:
            self.entries.append(entry)

        self.emit('entry-added', playlist=self, entry=entry)

        if (self.peek() is entry):
            entry.get_ready_future()

    def remove_entry(self, index: int) -> None:
        del self.entries[index]

    async def get_next_entry(
        self, predownload_next=True
    ) -> Union[PlaylistEntry, None]:
        """
        A coroutine which will return the next song or None if no songs
        left to play.

        Additionally, if predownload_next is set to True, it will
        attempt to download the next song to be played - so that it's
        ready by the time we get to it.
        """
        if (not self.entries):
            return None

        entry = self.entries.popleft()

        if (predownload_next):
            next_entry = self.peek()
            if (next_entry):
                next_entry.get_ready_future()

        return await entry.get_ready_future()

    def peek(self) -> PlaylistEntry:
        """
        Returns the next entry that should be scheduled to be played.
        """
        if (self.entries):
            return self.entries[0]

    async def estimate_time_until(self, pos: int, player) -> timedelta:
        """
        (very) Roughly estimates the time till the queue will 'position'
        """

        pos -= 1
        if (
            any(
                e.duration == None
                for e in islice(self.entries, pos)
            )
        ):
            raise InvalidDataError('no duration data')
        else:
            estimated_time = sum(
                e.duration
                for e in islice(self.entries, pos)
            )

        # When the player plays a song, it eats the first playlist item,
        # so we just have to add the time back
        if (not player.is_stopped and player.current_entry):
            if (player.current_entry.duration == None):
                # duration can be 0
                raise InvalidDataError(
                    'no duration data in current entry'
                )
            else:
                estimated_time += (
                    player.current_entry.duration - player.progress
                )

        return timedelta(seconds=estimated_time)

    def count_for_user(self, user: Union[Member, User]) -> int:
        return sum(
            1
            for e in self.entries
            if (e.meta.get('author', None) == user.id)
        )

    def __json__(self) -> Dict[str, Any]:
        return self._enclose_json({'entries': list(self.entries)})

    @classmethod
    def _deserialize(
        cls,
        data,
        aiosession: ClientSession = None,
        config: Config = None,
        downloader: Downloader = None,
        loop: AbstractEventLoop = None
    ) -> Playlist:
        assert aiosession is not None, cls._bad('aiosession')
        assert config is not None, cls._bad('config')
        assert downloader is not None, cls._bad('downloader')
        assert loop is not None, cls._bad('loop')

        # log.debug('Deserializing playlist')
        playlist = cls(aiosession, config, downloader, loop)

        for entry in data['entries']:
            playlist.entries.append(entry)

        # TODO: create a function to init downloading (since we don't do
        #       it here)?
        return playlist
