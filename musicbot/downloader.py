import os.path
import logging

import yt_dlp.utils

from asyncio import (
    AbstractEventLoop, ensure_future, iscoroutine, iscoroutinefunction
)
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, Optional

from yt_dlp import YoutubeDL

log = logging.getLogger(__name__)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'usenetrc': True,
}

# Fuck your useless bugreports message that gets two link embeds and
# confuses users
yt_dlp.utils.bug_reports_message = lambda: ''

"""
    Alright, here's the problem. To catch youtube-dl errors for their
    useful information, I have to catch the exceptions with
    `ignoreerrors` off. To not break when ytdl hits a dumb video (rental
    videos, etc), I have to have `ignoreerrors` on. I can change these
    whenever, but with async that's bad. So I need multiple ytdl
    objects.
"""


class Downloader:
    def __init__(self, download_folder: Optional[str] = None) -> None:
        self.thread_pool = ThreadPoolExecutor(max_workers=2)
        self.download_folder = download_folder

        if (download_folder):
            ytdl_format_options['outtmpl'] = os.path.join(
                download_folder, ytdl_format_options['outtmpl']
            )

        self.unsafe_ytdl = YoutubeDL(ytdl_format_options)
        self.safe_ytdl = YoutubeDL(
            {**ytdl_format_options, 'ignoreerrors': True}
        )

    @property
    def ytdl(self) -> YoutubeDL:
        return self.safe_ytdl

    async def extract_info(
        self, loop: AbstractEventLoop, *args,
        on_error: Optional[Callable] = None, retry_on_error=False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Runs ytdl.extract_info within the threadpool. Returns a future
        that will fire when it's done. If `on_error` is passed and an
        exception is raised, the exception will be caught and passed to
        on_error as an argument.
        """
        if (callable(on_error)):
            try:
                return await loop.run_in_executor(
                    self.thread_pool,
                    partial(
                        self.unsafe_ytdl.extract_info, *args, **kwargs
                    ),
                )

            except Exception as e:
                # (
                #     yt_dlp.utils.ExtractorError,
                #     yt_dlp.utils.DownloadError
                # )
                # I hope I don't have to deal with
                # ContentTooShortError's
                if (iscoroutinefunction(on_error)):
                    ensure_future(on_error(e), loop=loop)

                elif (iscoroutine(on_error)):
                    ensure_future(on_error, loop=loop)

                else:
                    loop.call_soon_threadsafe(on_error, e)

                if (retry_on_error):
                    return await self.safe_extract_info(
                        loop, *args, **kwargs
                    )
        else:
            return await loop.run_in_executor(
                self.thread_pool,
                partial(
                    self.unsafe_ytdl.extract_info, *args, **kwargs
                ),
            )

    async def safe_extract_info(
        self, loop: AbstractEventLoop, *args, **kwargs
    ) -> Dict[str, Any]:
        return await loop.run_in_executor(
            self.thread_pool,
            partial(
                self.safe_ytdl.extract_info, *args, **kwargs
            ),
        )
