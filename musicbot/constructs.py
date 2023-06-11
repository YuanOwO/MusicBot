from __future__ import annotations

import logging
import inspect
import json

from pydoc import locate as locateObj
from typing import Any, Dict, NoReturn, Set

from nextcord import Message

from .utils import _get_variable


log = logging.getLogger(__name__)


class BetterLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.relativeCreated /= 1000


class SkipState:
    __slots__ = ('skippers', 'skip_msgs')

    def __init__(self) -> None:
        self.skippers: Set[int] = set()
        self.skip_msgs: Set[Message] = set()

    @property
    def skip_count(self) -> int:
        return len(self.skippers)

    def reset(self) -> None:
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper: int, msg: Message) -> int:
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Serializer(json.JSONEncoder):
    def default(self, o: object) -> Any:
        if (hasattr(o, '__json__')):
            return o.__json__()

        return super().default(o)

    @classmethod
    def deserialize(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if (all(x in data for x in Serializable._class_signature)):
            # log.debug('Deserialization requested for {}'.format(data))
            factory = locateObj(
                data['__module__'] + '.' + data['__class__']
            )
            # log.debug('Found object {}'.format(factory))
            if (factory and issubclass(factory, Serializable)):
                # log.debug('Deserializing {} object'.format(factory))
                return factory._deserialize(
                    data['data'], **cls._get_vars(factory._deserialize)
                )

        return data

    @classmethod
    def _get_vars(cls, func) -> Dict[str, Any]:
        # log.debug('Getting vars for {}'.format(func))
        params = inspect.signature(func).parameters.copy()
        kwargs: Dict[str, Any] = {}
        # log.debug('Got {}'.format(params))

        for name, param in params.items():
            # log.debug(
            #     'Checking arg {}, type {}'.format(name, param.kind)
            # )
            if (
                param.kind is param.POSITIONAL_OR_KEYWORD
                and param.default is None
            ):
                # log.debug('Using var {}'.format(name))
                kwargs[name] = _get_variable(name)
                # log.debug(
                #     'Collected var for kwarg \'{}\': {}'
                #     .format(name, kwargs[name])
                # )

        return kwargs


class Serializable:
    _class_signature = ('__class__', '__module__', 'data')

    def _enclose_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            '__class__': self.__class__.__qualname__,
            '__module__': self.__module__,
            'data': data,
        }

    # Perhaps convert this into some sort of decorator
    @staticmethod
    def _bad(arg) -> NoReturn:
        raise TypeError('Argument "{}" must not be None'.format(arg))

    def serialize(self, *, cls=Serializer, **kwargs) -> str:
        return json.dumps(self, cls=cls, **kwargs)

    def __json__(self) -> NoReturn:
        raise NotImplementedError

    @classmethod
    def _deserialize(cls, data, **kwargs) -> NoReturn:
        raise NotImplementedError
