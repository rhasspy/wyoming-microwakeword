# Wyoming microWakeWord

[Wyoming protocol](https://github.com/rhasspy/wyoming) server for the [microWakeWord](https://github.com/kahrendt/microWakeWord/) wake word detection system.


## Home Assistant Add-on

[![Show add-on](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=47701997_microwakeword&repository_url=https%3A%2F%2Fgithub.com%2Frhasspy%2Fhassio-addons)

[Source](https://github.com/rhasspy/hassio-addons/tree/master/microwakeword)


## Local Install

Clone the repository and set up Python virtual environment:

``` sh
git clone https://github.com/rhasspy/wyoming-microwakeword.git
cd wyoming-microwakeword
script/setup
```

Run a server that anyone can connect to:

``` sh
script/run --uri 'tcp://0.0.0.0:10400'
```

See `script/run --help` for more options.


## Docker Image

``` sh
docker run -it -p 10400:10400 rhasspy/wyoming-microwakeword
```

[Source](https://github.com/rhasspy/wyoming-addons/tree/master/microwakeword)
