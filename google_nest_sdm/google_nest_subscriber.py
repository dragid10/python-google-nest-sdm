"""Subscriber for the Smart Device Management event based API."""
import asyncio
import concurrent.futures
import json
import logging
from abc import ABC, abstractmethod

from google.api_core.exceptions import GoogleAPIError, NotFound, Unauthenticated
from google.cloud import pubsub_v1

from .auth import AbstractAuth
from .device_manager import DeviceManager
from .event import EventCallback, EventMessage
from .exceptions import AuthException, SubscriberException
from .google_nest_api import GoogleNestAPI

_LOGGER = logging.getLogger(__name__)


# Used to catch a topic misconfiguration
EXPECTED_TOPIC_PREFIX = "projects/sdm-prod/"


class AbstractSusbcriberFactory(ABC):
    """Abstract class for creating a subscriber, to facilitate testing."""

    @abstractmethod
    async def new_subscriber(
        self, creds, subscription_name, callback
    ) -> pubsub_v1.subscriber.futures.StreamingPullFuture:
        """Create a new event subscriber."""


class DefaultSubscriberFactory(AbstractSusbcriberFactory):
    """Default implementation that creates Google Pubsub subscriber."""

    async def new_subscriber(
        self, creds, subscription_name, callback
    ) -> pubsub_v1.subscriber.futures.StreamingPullFuture:
        subscriber = pubsub_v1.SubscriberClient(credentials=creds)
        # Verify creds and subscription name are correct at startup to make it
        # easier to detect a misconfiguration.  You can't easily tell from the
        # subscribe streaming future if it was connected initially.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                executor, self._verify_subscriber_config, subscriber, subscription_name
            )

        return subscriber.subscribe(subscription_name, callback)

    def _verify_subscriber_config(self, subscriber, subscription_name):
        """Issues a command to verify subscriber creds are correct."""
        subscription = subscriber.get_subscription(subscription=subscription_name)
        if subscription.topic:
            if not subscription.topic.startswith(EXPECTED_TOPIC_PREFIX):
                _LOGGER.warning(
                    (
                        "Subscription misconfigured. Expected topic name with "
                        "prefix '%s' but was '%s'."
                    ),
                    EXPECTED_TOPIC_PREFIX,
                    subscription.topic,
                )
            else:
                _LOGGER.debug(
                    "Susbcriber '%s' configured on topic '%s'",
                    subscription_name,
                    subscription.topic,
                )


class GoogleNestSubscriber:
    """Subscribes to events from the Google Nest feed."""

    def __init__(
        self,
        auth: AbstractAuth,
        project_id: str,
        subscriber_id: str,
        subscriber_factory: AbstractSusbcriberFactory = DefaultSubscriberFactory(),
        watchdog_delay: float = 10,
    ):
        """Initialize the subscriber for the specified topic"""
        self._auth = auth
        self._subscriber_id = subscriber_id
        self._api = GoogleNestAPI(auth, project_id)
        self._subscriber_factory = subscriber_factory
        self._subscriber_future = None
        self._callback = None
        self._device_manager_task = asyncio.create_task(
            self._async_create_device_manager()
        )
        self._healthy = True
        self._watchdog_delay = watchdog_delay
        if self._watchdog_delay > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog())
        else:
            self._watchdog_task = None

    def set_update_callback(self, callback: EventCallback):
        """Register a callback invoked when new messages are received."""
        self._callback = callback

    async def start_async(self):
        """Starts the subscriber."""
        creds = await self._auth.async_get_creds()
        try:
            self._subscriber_future = await self._subscriber_factory.new_subscriber(
                creds, self._subscriber_id, self._message_callback
            )
        except NotFound as err:
            raise SubscriberException(
                f"Failed to create subscriber '{self._subscriber_id}' id was not found"
            ) from err
        except Unauthenticated as err:
            raise AuthException("Failed to authenticate subscriber") from err
        except GoogleAPIError as err:
            raise SubscriberException(
                f"Failed to create subscriber '{self._subscriber_id}'"
            ) from err

        if not self._healthy:
            _LOGGER.info("Subscriber reconnected")
            self._healthy = True
        if self._subscriber_future:
            self._subscriber_future.add_done_callback(self._done_callback)

    async def _watchdog(self):
        """Background task that watches the subscriber and restarts it."""
        _LOGGER.debug("Starting background watchdog thread")
        while True:
            if self._subscriber_future and self._subscriber_future.done():
                _LOGGER.debug("Watchdog: subscriber shut down; restarting")
                await self.start_async()
            await asyncio.sleep(self._watchdog_delay)

    def wait(self):
        """Blocks on the subscriber."""
        self._subscriber_future.result()

    def stop_async(self):
        """Tells the subscriber to start shutting down."""
        if self._device_manager_task:
            self._device_manager_task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._subscriber_future:
            self._subscriber_future.cancel()

    async def async_get_device_manager(self) -> DeviceManager:
        """Return the DeviceManger with the current state of devices."""
        return await self._device_manager_task

    async def _async_create_device_manager(self):
        """Creates a DeviceManager, populated with initial state."""
        device_manager = DeviceManager()
        structures = await self._api.async_get_structures()
        for structure in structures:
            device_manager.add_structure(structure)
        # Subscriber starts after a device fetch
        devices = await self._api.async_get_devices()
        for device in devices:
            device_manager.add_device(device)
        return device_manager

    def _done_callback(self, future):
        """Invoked when the subscriber is completed."""
        if future.exception():
            ex = future.exception()
            if self._healthy:
                self._healthy = False
                _LOGGER.warning(
                    "Subscriber disconnected, will restart: %s: %s", type(ex), ex
                )
            else:
                _LOGGER.debug("Subscriber failure: %s: %s", type(ex), ex)

    def _message_callback(self, message: pubsub_v1.subscriber.message.Message):
        """Invoked when a message is received."""

        payload = json.loads(bytes.decode(message.data))
        event = EventMessage(payload, self._auth)
        # Only accept device events once the Device Manager has been loaded.
        # We are ok with missing messages on startup since the device manager
        # will do a live read.
        if self._device_manager_task.done():
            self._device_manager_task.result().handle_event(event)
        if self._callback:
            self._callback.handle_event(event)
        message.ack()
