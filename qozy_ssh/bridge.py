import asyncio
import logging

import paramiko

from qozyd.plugins.bridge import BridgePlugin
from qozyd.models.things import Thing
from qozyd.models.channels import Channel
from qozyd.utils import as_coroutine
from qozyd.utils.json import JsonSchema, ChannelSchema


def decode_value(channel: Channel, value):
    if channel.TYPE_NAME == "Switch":
        # value: 0 or 1
        return bool(int(value))
    elif channel.TYPE_NAME == "Color":
        # value: r, g, b
        return tuple(int(x) for x in value.split(","))
    elif channel.TYPE_NAME == "String":
        # value: string
        return value
    elif channel.TYPE_NAME == "Number":
        # value: int or float
        try:
            return int(value)
        except ValueError:
            return float(value)
    else:
        raise Exception(f"Unknown channel type \"{channel.TYPE_NAME}\"")


def encode_value(channel: Channel, value):
    if channel.TYPE_NAME == "Switch":
        # value: True/False to 1/0
        return str(int(value))
    elif channel.TYPE_NAME == "Color":
        # value: tuple(r, g, b) to "r,g,b"
        return ",".join((str(x) for x in value))
    elif channel.TYPE_NAME == "String":
        # value: string
        return value
    elif channel.TYPE_NAME == "Number":
        # value: int or float
        return str(value)
    else:
        raise Exception(f"Unknown channel type \"{channel.TYPE_NAME}\"")


class SSH(BridgePlugin):
    VENDOR_PREFIX = "ssh"
    SETTINGS_SCHEMA = JsonSchema.object(
        properties=JsonSchema.properties(
            server=JsonSchema.string(title="Server"),
            username=JsonSchema.string(title="Username"),
            password=JsonSchema.string(title="Password"),
            things=JsonSchema.array(
                title="Things",
                items=JsonSchema.object(
                    properties=JsonSchema.properties(
                        channels=JsonSchema.array(
                            title="Channels",
                            items=ChannelSchema.all(
                                extend_all={
                                    "properties": JsonSchema.properties(
                                        set_state=JsonSchema.string(title="Set State (Script)",
                                                                    description="Channel value is available through environment variable QOZY_VALUE"),
                                        get_state=JsonSchema.string(title="Get State (Script)"),
                                    ),
                                    "required": ["get_state"]
                                }
                            )
                        )
                    ),
                )
            )
        )
    )

    def __init__(self, bridge):
        super().__init__(bridge)

        self.ssh_client = None

    async def connect(self):
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(self.settings["server"], username=self.settings["username"],
                    password=self.settings.get("password", None), timeout=5)

        self.ssh_client = ssh_client

    def _is_online_and_connected(self):
        return self.ssh_client is not None and self.ssh_client.get_transport().is_active()

    async def start(self, connection):
        while not self.stopped:
            if not self._is_online_and_connected():
                await self.connect()

            for thing in self.things.values():
                if self.is_online(thing):
                    with connection.transaction_manager:
                        await self.update_state(thing)

            await asyncio.sleep(1)

    async def scan(self):
        for index, thing_settings in enumerate(self.settings.get("things", []), start=1):
            thing = Thing(self.bridge, str(index))

            # bind items to channels
            for channel_settings in thing_settings.get("channels", []):
                channel_type = Channel.type_by_name(channel_settings["type"])

                sensor = "set_state" not in channel_settings or channel_settings["set_state"] is None or channel_settings["set_state"] == ""

                channel = channel_type(thing, channel_settings["channel"], sensor, channel_settings)
                thing.add_channel(channel)

            yield thing

    async def update_state(self, thing):
        for channel in thing.channels.values():
            get_state_script = channel.settings["get_state"]

            try:
                _, ssh_stdout, _ = await self.execute_command(get_state_script)

                try:
                    result = decode_value(channel, ssh_stdout.read().decode().strip())
                    await channel.set(result)
                except ValueError:
                    pass
            except TimeoutError:
                # Connection lost
                self.ssh_client = None

    async def apply(self, thing, channel, value):
        set_state_script = channel.settings.get("set_state")

        await self.execute_command(f"QOZY_VALUE=\"{encode_value(channel, value)}\"\n{set_state_script}")
        await channel.set(value)

    @as_coroutine
    def execute_command(self, command):
        return self.ssh_client.exec_command(command)

    def is_online(self, thing):
        return self._is_online_and_connected()
