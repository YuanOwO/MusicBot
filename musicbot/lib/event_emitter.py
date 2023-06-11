from asyncio import ensure_future, get_event_loop, iscoroutinefunction
from collections import defaultdict
from traceback import print_exc
from typing import Callable, Any, DefaultDict, List


Callback = Callable[..., Any]


class EventEmitter:
    def __init__(self) -> None:
        self._events: DefaultDict[str, List[Callback]] = (
            defaultdict(list)
        )
        self.loop = get_event_loop()

    def emit(self, event: str, *args, **kwargs) -> None:
        if (event not in self._events):
            return

        for cb in list(self._events[event]):
            # noinspection PyBroadException
            try:
                if (iscoroutinefunction(cb)):
                    ensure_future(
                        cb(*args, **kwargs), loop=self.loop
                    )
                else:
                    cb(*args, **kwargs)

            except:
                print_exc()

    def on(self, event: str, cb: Callback):
        self._events[event].append(cb)
        return self

    def off(self, event: str, cb: Callback):
        self._events[event].remove(cb)

        if (not self._events[event]):
            del self._events[event]

        return self

    def once(self, event: str, cb: Callback):
        def callback(*args, **kwargs):
            self.off(event, cb)
            return cb(*args, **kwargs)

        return self.on(event, callback)
