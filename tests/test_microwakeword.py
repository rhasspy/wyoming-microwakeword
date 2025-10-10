"""Tests for wyoming-microwakeword"""

import asyncio
import sys
import wave
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import Set

import pytest
from wyoming.audio import wav_to_chunks
from wyoming.event import async_read_event, async_write_event
from wyoming.info import Describe, Info
from wyoming.wake import Detect, Detection, NotDetected

_DIR = Path(__file__).parent
_CUSTOM_MODEL_DIR = _DIR.parent / "wakewords"
_SAMPLES_PER_CHUNK = 1024
_DETECTION_TIMEOUT = 5


@pytest.mark.asyncio
async def test_microwakeword() -> None:
    """Test a detection with sample audio."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wyoming_microwakeword",
        "--uri",
        "stdio://",
        "--custom-model-dir",
        str(_CUSTOM_MODEL_DIR),
        stdin=PIPE,
        stdout=PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    # Check info
    model_names: Set[str] = set()
    await async_write_event(Describe().event(), proc.stdin)
    while True:
        event = await asyncio.wait_for(
            async_read_event(proc.stdout), timeout=_DETECTION_TIMEOUT
        )
        assert event is not None

        if not Info.is_type(event.type):
            continue

        info = Info.from_event(event)
        assert len(info.wake) == 1, "Expected one wake service"
        wake = info.wake[0]
        assert len(wake.models) > 0, "Expected at least one model"

        for ww_model in wake.models:
            model_names.add(ww_model.name)

        assert model_names, "No models"
        break

    for active_model in model_names:
        # We want to use the 'okay nabu' model
        await async_write_event(Detect(names=[active_model]).event(), proc.stdin)

        # Test positive WAV
        with wave.open(str(_DIR / f"{active_model}.wav"), "rb") as wav_file:
            for event in wav_to_chunks(
                wav_file, _SAMPLES_PER_CHUNK, start_event=True, stop_event=False
            ):
                await async_write_event(event.event(), proc.stdin)

        while True:
            event = await asyncio.wait_for(
                async_read_event(proc.stdout), timeout=_DETECTION_TIMEOUT
            )
            assert event is not None, "Unexpected disconnection"

            if not Detection.is_type(event.type):
                continue

            detection = Detection.from_event(event)
            assert detection.name == active_model
            break

        for inactive_model in model_names:
            if active_model == inactive_model:
                continue

            # Test negative WAV
            with wave.open(str(_DIR / f"{inactive_model}.wav"), "rb") as wav_file:
                for event in wav_to_chunks(
                    wav_file, _SAMPLES_PER_CHUNK, start_event=True, stop_event=True
                ):
                    await async_write_event(event.event(), proc.stdin)

            while True:
                event = await asyncio.wait_for(
                    async_read_event(proc.stdout), timeout=_DETECTION_TIMEOUT
                )
                assert event is not None, "Unexpected disconnection"

                if not NotDetected.is_type(event.type):
                    continue

                # Should receive a not-detected message after audio-stop
                break

    # Need to close stdin for graceful termination
    proc.stdin.close()
    await proc.communicate()

    assert proc.returncode == 0
