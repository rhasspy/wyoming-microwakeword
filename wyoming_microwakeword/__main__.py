#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, WakeModel, WakeProgram
from wyoming.server import AsyncEventHandler, AsyncServer, AsyncTcpServer
from wyoming.wake import Detect, Detection, NotDetected

from . import __version__

_LOGGER = logging.getLogger()

DEFAULT_MODEL = Model.OKAY_NABU


@dataclass
class CustomModel:
    """Custom wake word model."""

    name: str
    config_path: Path
    model_path: Path

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other) -> bool:
        if isinstance(other, CustomModel):
            return self.name == other.name

        return NotImplemented


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="stdio://", help="unix:// or tcp://")
    #
    parser.add_argument(
        "--zeroconf",
        nargs="?",
        const="microWakeWord",
        help="Enable discovery over zeroconf with optional name (default: microWakeWord)",
    )
    #
    parser.add_argument(
        "--custom-model-dir",
        action="append",
        default=[],
        help="Path to directory with custom wake word models",
    )
    parser.add_argument(
        "--refractory-seconds",
        type=float,
        default=2.0,
        help="Seconds before a wake word can be detected again (default: 2)",
    )
    #
    parser.add_argument("--debug", action="store_true", help="Log DEBUG messages")
    parser.add_argument(
        "--log-format", default=logging.BASIC_FORMAT, help="Format for log messages"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="Print version and exit",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, format=args.log_format
    )
    _LOGGER.debug(args)

    # Find custom models
    custom_models: Dict[str, CustomModel] = {}
    for model_dir_str in args.custom_model_dir:
        model_dir = Path(model_dir_str)
        for config_path in model_dir.glob("*.json"):
            model_name = config_path.stem
            if model_name in custom_models:
                # Skip duplicate models in later directories
                continue

            with open(config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)

            if config.get("type") != "micro":
                _LOGGER.debug("Not a microWakeWord model: %s", config_path)
                continue

            model_path = model_dir / config["model"]
            if not model_path.exists():
                _LOGGER.debug("Missing tflite model: %s", model_path)
                continue

            custom_models[model_name] = CustomModel(
                name=model_name,
                config_path=config_path,
                model_path=model_path,
            )

    _LOGGER.info("Ready")

    # Start server
    server = AsyncServer.from_uri(args.uri)

    if args.zeroconf:
        if not isinstance(server, AsyncTcpServer):
            raise ValueError("Zeroconf requires tcp:// uri")

        from wyoming.zeroconf import HomeAssistantZeroconf

        tcp_server: AsyncTcpServer = server
        hass_zeroconf = HomeAssistantZeroconf(
            name=args.zeroconf, port=tcp_server.port, host=tcp_server.host
        )
        await hass_zeroconf.register_server()
        _LOGGER.debug("Zeroconf discovery enabled")

    try:
        await server.run(partial(MicroWakeWordEventHandler, args, custom_models))
    except KeyboardInterrupt:
        pass


# -----------------------------------------------------------------------------


@dataclass
class Detector:
    name: str
    mww: MicroWakeWord
    detected: bool = False
    last_detected: Optional[float] = None


class MicroWakeWordEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        cli_args: argparse.Namespace,
        custom_models: Dict[str, CustomModel],
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.custom_models = custom_models
        self.client_id = str(time.monotonic_ns())
        self.converter = AudioChunkConverter(rate=16000, width=2, channels=1)
        self.detectors: List[Detector] = []
        self.models: Set[Union[Model, CustomModel]] = set()
        self.mww_features = MicroWakeWordFeatures()

        _LOGGER.debug("Client connected: %s", self.client_id)

    async def handle_event(self, event: Event) -> bool:
        if AudioChunk.is_type(event.type):
            chunk = self.converter.convert(AudioChunk.from_event(event))
            for features in self.mww_features.process_streaming(chunk.audio):
                for detector in self.detectors:
                    if detector.mww.process_streaming(features):
                        if (detector.last_detected is not None) and (
                            (time.monotonic() - detector.last_detected)
                            < self.cli_args.refractory_seconds
                        ):
                            _LOGGER.debug(
                                "Skipping detection within refractory period for %s from client %s",
                                detector.mww.wake_word,
                                self.client_id,
                            )
                            continue

                        _LOGGER.debug(
                            "Detected %s from client %s",
                            detector.mww.wake_word,
                            self.client_id,
                        )
                        await self.write_event(
                            Detection(
                                name=detector.name, timestamp=chunk.timestamp
                            ).event()
                        )
                        detector.detected = True
                        detector.last_detected = time.monotonic()
        elif Detect.is_type(event.type):
            detect = Detect.from_event(event)
            self.models.clear()
            if detect.names:
                for name in detect.names:
                    custom_model = self.custom_models.get(name)
                    if custom_model is not None:
                        self.models.add(custom_model)
                        continue

                    try:
                        self.models.add(Model(name))
                    except ValueError:
                        _LOGGER.warning("Unknown model name: %s", name)
        elif AudioStart.is_type(event.type):
            if not self.models:
                # Default
                self.models.add(DEFAULT_MODEL)

            self.detectors = []
            for model in self.models:
                if isinstance(model, Model):
                    self.detectors.append(
                        Detector(
                            name=model.value, mww=MicroWakeWord.from_builtin(model)
                        )
                    )
                elif isinstance(model, CustomModel):
                    self.detectors.append(
                        Detector(
                            name=model.name,
                            mww=MicroWakeWord.from_config(model.config_path),
                        )
                    )

            _LOGGER.debug("Loaded models: %s", self.models)
        elif AudioStop.is_type(event.type):
            # Inform client if not detections occurred
            if not any(d.detected for d in self.detectors):
                # No wake word detections
                await self.write_event(NotDetected().event())

                _LOGGER.debug(
                    "Audio stopped without detection from client: %s", self.client_id
                )

            self.detectors.clear()
        elif Describe.is_type(event.type):
            wyoming_info = self._get_info()
            await self.write_event(wyoming_info.event())
            _LOGGER.debug("Sent info to client: %s", self.client_id)
        else:
            _LOGGER.debug("Unexpected event: type=%s, data=%s", event.type, event.data)

        return True

    async def disconnect(self) -> None:
        _LOGGER.debug("Client disconnected: %s", self.client_id)

    def _get_info(self) -> Info:
        # Builtin models
        models = [
            WakeModel(
                name=model.value,
                description=_model_phrase(model),
                phrase=_model_phrase(model),
                attribution=Attribution(
                    name="kahrendt",
                    url="https://github.com/kahrendt/microWakeWord/",
                ),
                installed=True,
                languages=["en"],
                version="2.0.0",
            )
            for model in Model
        ]

        # Custom models
        for model_id, custom_model in self.custom_models.items():
            with open(custom_model.config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)

            models.append(
                WakeModel(
                    name=model_id,
                    description=config["wake_word"],
                    phrase=config["wake_word"],
                    attribution=Attribution(
                        name=config.get("author", ""),
                        url=config.get("website", ""),
                    ),
                    installed=True,
                    languages=config.get("trained_languages", []),
                    version=config.get("version", ""),
                )
            )

        return Info(
            wake=[
                WakeProgram(
                    name="microWakeWord",
                    description="Tensorflow-based wake word detection",
                    attribution=Attribution(
                        name="kahrendt",
                        url="https://github.com/kahrendt/microWakeWord/",
                    ),
                    installed=True,
                    version=__version__,
                    models=models,
                )
            ],
        )


def _model_phrase(model: Model) -> str:
    words = model.value.split("_")
    phrase = " ".join(w.capitalize() for w in words)
    return phrase


# -----------------------------------------------------------------------------


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
