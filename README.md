This is **a systemd init script for one or more Notchian (vanilla) [Minecraft][] servers**, with some [Wurstmineberg][]-specific extras.

This is version 3.2.3 ([semver][Semver]) of the init script. The versioned API includes a CLI, as found in the docstring of [`minecraft.py`](minecraft.py), as well as a Python API including all documented functions defined in minecraft.py.

Requirements
============

*   systemd
*   [Python][] 3.4
*   The current version of the Minecraft server, available from [here][MinecraftServerDownload] or using the `minecraft update` command.
*   [docopt][Docopt]
*   [lazyjson][LazyJSON] 1.0 (for whitelist management)
*   [mcrcon][MCRCON]
*   [loops][PythonLoops] 1.1
*   [more-itertools][MoreItertools] 2.1
*   [requests][Requests] 2.1

Configuration
=============

1.  Clone the repository somewhere on your system.
2.  Create a symlink to `minecraft.py` in your Python 3 module search path or add the repository to the module search path.
3.  Optionally, create a symlink to `minecraft.sh` called `minecraft` in your `PATH`. This will allow you to use commands like `minecraft update`.
4.  To immediately start the Minecraft server, `systemctl start minecraft`. To automatically start the Minecraft server with the system, `systemctl enable minecraft`.

To make this work for another server, you may have to modify the paths and other things in the config file.

[Docopt]: https://github.com/docopt/docopt (github: docopt: docopt)
[LazyJSON]: https://github.com/fenhl/lazyjson (github: fenhl: lazyjson)
[MCRCON]: https://github.com/barneygale/MCRcon (github: barneygale: MCRcon)
[Minecraft]: http://minecraft.net/ (Minecraft)
[MinecraftServerDownload]: https://minecraft.net/en-us/download/server (Minecraft: Download server)
[MoreItertools]: http://pypi.python.org/pypi/more-itertools (PyPI: more-itertools)
[Python]: http://python.org/ (Python)
[PythonLoops]: https://github.com/fenhl/python-loops (github: fenhl: python-loops)
[Requests]: http://www.python-requests.org/ (Requests)
[Semver]: http://semver.org/ (Semantic Versioning 2.0.0)
[Wurstmineberg]: http://wurstmineberg.de/ (Wurstmineberg)
