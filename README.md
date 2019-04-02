This is **a systemd init script for one or more vanilla [Minecraft](https://minecraft.net/) servers**, with some [Wurstmineberg](https://wurstmineberg.de/)-specific extras.

This is version 5.0.0 ([semver](https://semver.org/)) of the init script. The versioned API is a command-line interface, as described by `minecraft --help`, as well as a configuration file format, described below.

# Requirements

* systemd
* [Rust](https://www.rust-lang.org/) 1.32
* The current version of the Minecraft server, available from [here](https://minecraft.net/en-us/download/server) or using the `minecraft update` command (returning soon™).

# Configuration

1. `cargo install --git=https://github.com/wurstmineberg/systemd-minecraft --branch=riir`
2. Enable RCON using the [`enable-rcon`](https://minecraft.gamepedia.com/Server.properties#enable-rcon), [`rcon.password`](https://minecraft.gamepedia.com/Server.properties#rcon.password), and optionally [`rcon.port`](https://minecraft.gamepedia.com/Server.properties#rcon.port) server properties
3. To automatically start a Minecraft world with the system, `sudo systemctl enable minecraft@worldname` (replace `worldname` with the world name you chose in step 2). To immediately start a Minecraft world, `sudo systemctl start minecraft@worldname`.
