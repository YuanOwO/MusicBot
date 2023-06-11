from shutil import get_terminal_size
from textwrap import wrap
from typing import Optional

# Base class for exceptions


class MusicbotException(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)  # ???
        self._message = message

    @property
    def message(self) -> str:
        return self._message

    @property
    def message_no_format(self) -> str:
        return self._message


# Something went wrong during the processing of a command
class CommandError(MusicbotException):
    pass


# Something went wrong during the processing of a song/ytdl stuff
class ExtractionError(MusicbotException):
    pass


# Something is wrong about data
class InvalidDataError(MusicbotException):
    pass


# The no processing entry type failed and an entry was a playlist/vice
# versa
# TODO: Add typing options instead of is_playlist
class WrongEntryTypeError(ExtractionError):
    def __init__(
        self, message: str, is_playlist: bool, use_url: Optional[str]
    ) -> None:
        super().__init__(message)
        self.is_playlist = is_playlist
        self.use_url = use_url


# FFmpeg complained about something
class FFmpegError(MusicbotException):
    pass


# FFmpeg complained about something but we don't care
class FFmpegWarning(MusicbotException):
    pass


# The user doesn't have permission to use a command
class PermissionsError(CommandError):
    @property
    def message(self):
        return (
            'You don\'t have permission to use that command.'
            '\nReason: ' + self._message
        )


# Error with pretty formatting for hand-holding users through various
# errors
class HelpfulError(MusicbotException):
    def __init__(
        self, issue: str, solution: str,
        *, preface='An error has occured:', footnote=''
    ) -> None:
        self.issue = issue
        self.solution = solution
        self.preface = preface
        self.footnote = footnote
        self._message_fmt = (
            '\n{preface}\n{problem}\n\n{solution}\n\n{footnote}'
        )

    @property
    def message(self) -> str:
        return self._message_fmt.format(
            preface=self.preface,
            problem=self._pretty_wrap(self.issue, '  Problem:'),
            solution=self._pretty_wrap(self.solution, '  Solution:'),
            footnote=self.footnote
        )

    @property
    def message_no_format(self) -> str:
        return self._message_fmt.format(
            preface=self.preface,
            problem=self._pretty_wrap(
                self.issue, '  Problem:', width=None
            ),
            solution=self._pretty_wrap(
                self.solution, '  Solution:', width=None),
            footnote=self.footnote
        )

    @staticmethod
    def _pretty_wrap(text: str, pretext: str, *, width=-1) -> str:
        if (width is None):
            return '\n'.join((pretext.strip(), text))
        elif (width == -1):
            pretext = pretext.rstrip() + '\n'
            width = get_terminal_size().columns

        lines = wrap(text, width=width - 5)
        lines = (
            ('    ' + line).rstrip().ljust(width - 1).rstrip() + '\n'
            for line in lines
        )

        return pretext + ''.join(lines).rstrip()


class HelpfulWarning(HelpfulError):
    pass


# Base class for control signals
class Signal(Exception):
    pass


# signal to restart the bot
class RestartSignal(Signal):
    pass


# signal to end the bot 'gracefully'
class TerminateSignal(Signal):
    pass
