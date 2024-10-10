#!/usr/bin/env python3
from pathlib import Path

import setuptools
from setuptools import setup

this_dir = Path(__file__).parent
module = "wyoming_microwakeword"
module_dir = this_dir / module

requirements = []
requirements_path = this_dir / "requirements.txt"
if requirements_path.is_file():
    with open(requirements_path, "r", encoding="utf-8") as requirements_file:
        requirements = requirements_file.read().splitlines()

version_path = module_dir / "VERSION"
version = version_path.read_text(encoding="utf-8").strip()
data_files = [version_path]

# -----------------------------------------------------------------------------

setup(
    name=module,
    version=version,
    description="Wyoming server for microWakeWord",
    url="http://github.com/rhasspy/wyoming-microwakeword",
    author="Michael Hansen",
    author_email="mike@rhasspy.org",
    packages=setuptools.find_packages(),
    package_data={module: [str(p.relative_to(module_dir)) for p in data_files]},
    install_requires=requirements,
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    keywords="rhasspy wyoming microWakeWord",
    entry_points={
        "console_scripts": [
            "wyoming-microwakeword = wyoming_microwakeword.__main__:run"
        ]
    },
)
