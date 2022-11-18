from contextlib import _AsyncGeneratorContextManager
from enum import Enum
from logging import getLogger
from typing import Awaitable, Callable, Type

import trio

import pyctor.ref
import pyctor.signals
import pyctor.types

logger = getLogger(__name__)


class BehaviorHandlerImpl(pyctor.types.BehaviorHandler[pyctor.types.T], pyctor.types.Behavior[pyctor.types.T]):
    """
    Class that all Behaviors need to implement if they want to be handled by Pyctor.
    This class fullfills the Protocol requirements.
    """

    _behavior: pyctor.types.BehaviorFunction[pyctor.types.T]
    _type: Type[pyctor.types.T] | None = None

    def __init__(self, behavior: pyctor.types.BehaviorFunction[pyctor.types.T], type_check: Type[pyctor.types.T] | None = None) -> None:
        self._behavior = behavior
        self._type = type_check

    async def handle(self, msg: pyctor.types.T) -> pyctor.types.Behavior[pyctor.types.T]:
        # if the type is given, we assert on it here
        if self._type:
            assert isinstance(msg, self._type), "Can only handle messages of type " + str(self._type) + ", got " + str(type(msg))
        return await self._behavior(msg)


class SuperviseStrategy(Enum):
    Restart = 1
    Stop = 2
    Ignore = 3


class LoggingBehaviorHandlerImpl(pyctor.types.BehaviorHandler[pyctor.types.T], pyctor.types.Behavior[pyctor.types.T]):
    """
    Logs every message that goes through the behavior
    """

    _behavior: pyctor.types.BehaviorHandler[pyctor.types.T]

    def __init__(self, behavior: pyctor.types.Behavior[pyctor.types.T]) -> None:
        self._behavior = behavior  # type: ignore

    async def handle(self, msg: pyctor.types.T) -> pyctor.types.Behavior[pyctor.types.T]:
        logger.info(f"Start handling: %s", msg)
        b = await self._behavior.handle(msg=msg)
        logger.info(f"End handling: %s", msg)
        return b


class SuperviseBehaviorHandlerImpl(pyctor.types.BehaviorHandler[pyctor.types.T], pyctor.types.Behavior[pyctor.types.T]):
    """
    Will wrap a BehaviorHandler in a supervise strategy
    """

    _strategy: Callable[[Exception], Awaitable[SuperviseStrategy]]
    _behavior: pyctor.types.BehaviorHandler[pyctor.types.T]

    def __init__(self, strategy: Callable[[Exception], Awaitable[SuperviseStrategy]], behavior: pyctor.types.BehaviorHandler[pyctor.types.T]) -> None:
        self._strategy = strategy
        self._behavior = behavior

    async def handle(self, msg: pyctor.types.T) -> pyctor.types.Behavior[pyctor.types.T]: # type: ignore
        try:
            return await self._behavior.handle(msg)
        except Exception as e:
            # run strategy
            now_what = await self._strategy(e)
            match now_what:
                case SuperviseStrategy.Restart:
                    return Behaviors.Restart
                case SuperviseStrategy.Stop:
                    return Behaviors.Stop
                case _, SuperviseStrategy.Ignore:
                    return Behaviors.Same


class BehaviorProcessorImpl(pyctor.types.BehaviorProcessor[pyctor.types.T]):
    _nursery: trio.Nursery

    _send: trio.abc.SendChannel[pyctor.types.T]
    _receive: trio.abc.ReceiveChannel[pyctor.types.T]

    _behavior: Callable[[], _AsyncGeneratorContextManager[pyctor.types.BehaviorHandler[pyctor.types.T]]]

    def __init__(self, nursery: trio.Nursery, behavior: Callable[[], _AsyncGeneratorContextManager[pyctor.types.BehaviorHandler[pyctor.types.T]]], name: str) -> None:
        super().__init__()
        self._nursery = nursery
        self._send, self._receive = trio.open_memory_channel(0)
        self._behavior = behavior
        self._ref = pyctor.ref.LocalRef[pyctor.types.T](self)
        self._name = name

    def ref(self) -> pyctor.types.Ref[pyctor.types.T]:
        return self._ref

    async def handle(self, msg: pyctor.types.T) -> None:
        try:
            await self._send.send(msg)
        except trio.ClosedResourceError:
            logger.warning("Could not send message, Behavior already terminated")
            pass

    def handle_nowait(self, msg: pyctor.types.T) -> None:
        # put into channel
        self._nursery.start_soon(self.handle, msg)

    async def behavior_task(self) -> None:
        """
        The main entry point for each behavior and therefore each actor.
        This method is a single task in the trio concept.
        Everything below this Behavior happens in this task.
        """
        behavior = self._behavior
        run = True
        while run:
            async with behavior() as b:
                try:
                    while True:
                        msg = await self._receive.receive()
                        new_behavior = await b.handle(msg)
                        match new_behavior:
                            case Behaviors.Ignored:
                                print(f"Message ignored: {msg}")
                            case Behaviors.Same:
                                pass
                            case Behaviors.Stop:
                                await self.stop()
                            case Behaviors.Restart:
                                # restart the behavior
                                break
                            case pyctor.types.BehaviorHandler():
                                # TODO: Needs to be tested if that works
                                behavior = new_behavior
                except trio.EndOfChannel:
                    # actor will be stopped
                    # catch exception to enable teardown of behavior
                    run = False
                    pass

    async def stop(self) -> None:
        """
        Stops the behavior and
        """
        await self._send.aclose()


class Behaviors:
    Same: pyctor.signals.BehaviorSignal = pyctor.signals.BehaviorSignal(1)
    """
    Indicates that the Behavior should stay the same for the next message.
    """

    Stop: pyctor.signals.BehaviorSignal = pyctor.signals.BehaviorSignal(2)
    """
    Indicates that the Behavior wants to be stopped. 
    A Behavior will get a final 'Stopped' LifecycleSignal and will then be terminated.
    """

    Restart: pyctor.signals.BehaviorSignal = pyctor.signals.BehaviorSignal(3)
    """
    Indicates that a Behavior wants to be restarted. 
    That means that the Behavior receives a 'Stopped' and then 'Started' LifecycleSignal.
    Also means that the setup (if available) of the Behavior will be executed again.
    """

    Ignored: pyctor.signals.BehaviorSignal = pyctor.signals.BehaviorSignal(4)
    """
    Indicates that the message was not handled and ignored. 
    """

    @staticmethod
    def receive(func: Callable[[pyctor.types.T], Awaitable[pyctor.types.Behavior[pyctor.types.T]]], type_check: Type[pyctor.types.T] | None = None) -> pyctor.types.Behavior[pyctor.types.T]:
        """
        Defines a Behavior that handles custom messages as well as lifecycle signals.
        """
        return BehaviorHandlerImpl(behavior=func, type_check=type_check)

    @staticmethod
    def supervise(strategy: Callable[[Exception], Awaitable[SuperviseStrategy]], behavior: pyctor.types.Behavior[pyctor.types.T]) -> pyctor.types.Behavior[pyctor.types.T]:
        # narrow class down to a BehaviorImpl
        assert isinstance(behavior, pyctor.types.BehaviorHandler), "The supervised behavior needs to implement the BehaviorHandler"
        return SuperviseBehaviorHandlerImpl(strategy=strategy, behavior=behavior)
