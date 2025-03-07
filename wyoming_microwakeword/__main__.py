#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Dict, List, Set

from pymicro_wakeword import MicroWakeWord, Model
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, WakeModel, WakeProgram
from wyoming.server import AsyncEventHandler, AsyncServer, AsyncTcpServer
from wyoming.wake import Detect, Detection, NotDetected

from . import __version__

_LOGGER = logging.getLogger()
_DIR = Path(__file__).parent

DEFAULT_MODEL = Model.OKAY_NABU


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

    parser.add_argument("--custom-model-dir", action="append", default=[], help="Path to directory with custom wake word models and json configuration files")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, format=args.log_format
    )
    _LOGGER.debug(args)

    _LOGGER.info("Ready")

    # Start server
    server = AsyncServer.from_uri(args.uri)

    if args.zeroconf:
        if not isinstance(server, AsyncTcpServer):
            raise ValueError("Zeroconf requires tcp:// uri")

        from wyoming.zeroconf import register_server

        tcp_server: AsyncTcpServer = server
        await register_server(
            name=args.zeroconf, port=tcp_server.port, host=tcp_server.host
        )
        _LOGGER.debug("Zeroconf discovery enabled")

    try:
        await server.run(partial(MicroWakeWordEventHandler, args))
    except KeyboardInterrupt:
        pass


# -----------------------------------------------------------------------------


@dataclass
class Detector:
    name: str
    mww: MicroWakeWord
    detected: bool = False

@dataclass
class CustomModel:
    name: str
    path: Path
    wake_word: str
    author: str
    website: str
    version: int | str
    languages: List[str]


class MicroWakeWordEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        cli_args: argparse.Namespace,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.client_id = str(time.monotonic_ns())
        self.converter = AudioChunkConverter(rate=16000, width=2, channels=1)
        self.detectors: List[Detector] = []
        self.models: Set[Model] = set()
        self.custom_models: Dict[str, CustomModel] = {}

        if cli_args.custom_model_dir:
            self._load_custom_models(Path(cli_args.custom_model_dir[0]))

        _LOGGER.debug("Client connected: %s", self.client_id)

    def _load_custom_models(self, custom_model_dir: Path) -> None:
        """Load custom models from directory."""
        if not custom_model_dir.is_dir():
            _LOGGER.error("Custom model directory does not exist: %s", custom_model_dir)
            return
        
        for model_path  in custom_model_dir.glob("*.json"):
            model_name = model_path.stem
            
            wake_word = None
            author = None
            website = None
            languages = []
            version = None
            try:
                with open(model_path, "r") as f:
                    model_config = json.load(f)
                    wake_word = model_config.get("wake_word")
                    author = model_config.get("author")
                    website = model_config.get("website")
                    languages = model_config.get("languages")
                    version = model_config.get("version")

            except Exception as e:
                _LOGGER.error("Failed to parse model config %s: %s", model_path, e)
            self.custom_models[model_name] = CustomModel(
                name=model_name, 
                path=model_path, 
                wake_word=wake_word if wake_word else "Unknown",
                author=author if author else "Unknown",
                website=website if website else "Unknown",
                languages=languages if languages else [],
                version=version if version else "Unknown"
            )
            _LOGGER.debug("Loaded custom model: %s (%s)", model_name, model_path)
    
    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            wyoming_info = self._get_info()
            await self.write_event(wyoming_info.event())
            _LOGGER.debug("Sent info to client: %s", self.client_id)
            return True

        if Detect.is_type(event.type):
            detect = Detect.from_event(event)
            self.models.clear()
            if detect.names:
                for name in detect.names:
                    try:
                        self.models.add(Model(name))
                        continue
                    except ValueError:
                        pass

                    if name in self.custom_models.keys():
                        pass
                    else:
                        _LOGGER.warning("Unknown model name: %s", name)
        elif AudioStart.is_type(event.type):
            self.detectors = []
            if not self.models and not self.custom_models:
                # Default
                self.models.add(DEFAULT_MODEL)
            
            for model in self.models:
                self.detectors.append(
                    Detector(name=model.value, mww=MicroWakeWord.from_builtin(model))
                )
            
            for model in self.custom_models.values():
                self.detectors.append(
                    Detector(name=model.name, mww=MicroWakeWord.from_config(model.path))
                )
            _LOGGER.debug("Loaded models: %s", self.models)
        elif AudioChunk.is_type(event.type):
            chunk = self.converter.convert(AudioChunk.from_event(event))
            for detector in self.detectors:
                if detector.mww.process_streaming(chunk.audio):
                    _LOGGER.debug(
                        "Detected %s from client %s",
                        detector.mww.wake_word,
                        self.client_id,
                    )
                    await self.write_event(
                        Detection(name=detector.name, timestamp=chunk.timestamp).event()
                    )

        elif AudioStop.is_type(event.type):
            # Inform client if not detections occurred
            if not any(d.detected for d in self.detectors):
                # No wake word detections
                await self.write_event(NotDetected().event())

                _LOGGER.debug(
                    "Audio stopped without detection from client: %s", self.client_id
                )
        else:
            _LOGGER.debug("Unexpected event: type=%s, data=%s", event.type, event.data)

        return True

    async def disconnect(self) -> None:
        _LOGGER.debug("Client disconnected: %s", self.client_id)

    def _get_info(self) -> Info:
        wake_models = [
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

        for model_name, custom_model in self.custom_models.items():
            wake_models.append(
                WakeModel(
                    name=model_name,
                    description=custom_model.wake_word,
                    phrase=custom_model.wake_word,
                    attribution=Attribution(
                        name=custom_model.author,
                        url=custom_model.website,
                    ),
                    installed=True,
                    languages=custom_model.languages,
                    version=custom_model.version,
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
                    models=wake_models,
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
