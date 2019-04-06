This is **a systemd init script for one or more vanilla [Minecraft](https://minecraft.net/) servers**, with some [Wurstmineberg](https://wurstmineberg.de/)-specific extras.

This is version 5.0.0 ([semver](https://semver.org/)) of the init script. The versioned API is a command-line interface, as described by `minecraft --help`, as well as a configuration file format, described below.

# Requirements

* systemd
* [Rust](https://www.rust-lang.org/) 1.32 (but see installation step 1)
* The current version of the Minecraft server, available from [here](https://minecraft.net/en-us/download/server) or using the `minecraft update` command (returning soon™).

# Installation

1. Create a system user named `wurstmineberg` (we recommend setting the user's home directory to `/opt/wurstmineberg`) and install Rust as that user.
2. `sudo -u wurstmineberg cargo install --git=https://github.com/wurstmineberg/systemd-minecraft --branch=riir`
3. `sudo cp ~wurstmineberg/.cargo/git/checkouts/systemd-minecraft-*/*/minecraft@.service /etc/systemd/system/minecraft@.service`
4. Place your server directory (with files like `minecraft_server.jar` and `server.properties`) into `/opt/wurstmineberg/world/worldname` (replace `worldname` with a name of your choice).
5. Enable RCON using the [`enable-rcon`](https://minecraft.gamepedia.com/Server.properties#enable-rcon), [`rcon.password`](https://minecraft.gamepedia.com/Server.properties#rcon.password), and optionally [`rcon.port`](https://minecraft.gamepedia.com/Server.properties#rcon.port) server properties.
6. Repeat steps 4 and 5 for any additional worlds you would like to configure.

# Configuration

* To automatically start a Minecraft world with the system, `sudo systemctl enable minecraft@worldname` (replace `worldname` with the world name you chose in the installation).
* To immediately start a Minecraft world, `sudo systemctl start minecraft@worldname`.
* To do both at the same time, `sudo systemctl enable --now minecraft@worldname`.

# Updating

1. `sudo -u wurstmineberg cargo install-update --all --git` (if not present, you can install the `cargo install-update` subcommand using `sudo -u wurstmineberg cargo install cargo-update`)
2. `sudo cp ~wurstmineberg/.cargo/git/checkouts/systemd-minecraft-*/COMMIT/minecraft@.service /etc/systemd/system/minecraft@.service` (replace `COMMIT` with the first 7 characters of the new git commit hash as shown by `cargo install-update`)
3. `sudo systemctl daemon-reload`
