#![deny(rust_2018_idioms, unused, unused_import_braces, unused_qualifications, warnings)]
//#![deny(missing_docs)] //TODO uncomment
#![forbid(unsafe_code)]

use {
    std::{
        convert::Infallible as Never,
        fmt,
        num::ParseIntError,
        path::{
            Path,
            PathBuf,
        },
        str::FromStr,
        time::Duration,
    },
    futures::stream::TryStreamExt as _,
    itertools::Itertools as _,
    serde::Deserialize,
    tokio::{
        io::{
            AsyncBufReadExt as _,
            BufReader,
        },
        process::Command,
    },
    tokio_stream::wrappers::LinesStream,
    wheel::{
        fs::{
            self,
            File,
        },
        traits::{
            AsyncCommandOutputExt as _,
            IoResultExt as _,
        },
    },
};
#[cfg(unix)] use {
    std::{
        sync::{
            Arc,
            Mutex,
        },
        thread,
    },
    crossbeam_channel::select,
    signal_hook::{
        consts::signal::SIGTERM,
        iterator::Signals,
    },
};

mod launcher_data;
mod util;

const BASE_DIR: &str = "/opt/wurstmineberg";
const WORLDS_DIR: &str = "/opt/wurstmineberg/world";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)] ParseInt(#[from] ParseIntError),
    #[error(transparent)] Json(#[from] serde_json::Error),
    #[error(transparent)] Rcon(#[from] rcon::Error),
    #[error(transparent)] Reqwest(#[from] reqwest::Error),
    #[error(transparent)] Wheel(#[from] wheel::Error),
    #[error("no RCON password is configured for this world")]
    RconDisabled,
    #[error("failed to parse server.properties")]
    ServerPropertiesParse,
    #[error("given version spec does not match any Minecraft version")]
    VersionSpec,
}

#[derive(Debug, Deserialize)]
#[serde(default, deny_unknown_fields, rename_all = "camelCase")]
pub struct Config {
    extra_args: Vec<String>,
    #[serde(rename = "memMaxMB")]
    mem_max_mb: usize,
    #[serde(rename = "memMinMB")]
    mem_min_mb: usize,
    modded: bool,
}

impl Config {
    pub fn load(path: impl AsRef<Path> + Copy) -> Result<Config, Error> {
        Ok(if path.as_ref().exists() {
            serde_json::from_reader(std::fs::File::open(path).at(path)?)? //TODO use async_json?
        } else {
            Config::default()
        })
    }
}

impl Default for Config {
    fn default() -> Config {
        Config {
            extra_args: Vec::default(),
            mem_max_mb: 1536, // the recommended default for Linode 2GB
            mem_min_mb: 1024, // the recommended default for Linode 2GB
            modded: false,
        }
    }
}

#[derive(Debug)]
pub struct ServerProperties {
    rcon_password: Option<String>,
    rcon_port: u16,
}

impl ServerProperties {
    async fn load(path: impl AsRef<Path> + Copy) -> Result<ServerProperties, Error> {
        let file = BufReader::new(File::open(path).await?);
        let mut prop = ServerProperties::default();
        let mut lines = LinesStream::new(file.lines());
        while let Some(line) = lines.try_next().await.at(path)? {
            if line.starts_with('#') { continue }
            let (key, value) = line.splitn(2, '=').collect_tuple().ok_or(Error::ServerPropertiesParse)?;
            match key {
                "rcon.password" => { prop.rcon_password = Some(value.to_string()); }
                "rcon.port" => { prop.rcon_port = value.parse()?; }
                _ => {} //TODO parse remaining keys, reject invalid keys
            }
        }
        Ok(prop)
    }
}

impl Default for ServerProperties {
    fn default() -> ServerProperties {
        ServerProperties {
            rcon_password: None,
            rcon_port: 22575,
        }
    }
}

/// A specification of acceptable Minecraft versions.
///
/// Used in `World::update`.
#[derive(Debug, Clone)]
pub enum VersionSpec {
    /// Update to the version with this exact name.
    Exact(String),
    /// Update to the latest release, as reported by Mojang.
    LatestRelease,
    /// Update to the latest snapshot, as reported by Mojang. Note that this will be a release version if no snapshot has been published since the latest release.
    LatestSnapshot,
}

impl Default for VersionSpec {
    fn default() -> VersionSpec {
        VersionSpec::LatestRelease
    }
}

#[derive(Debug, Clone)]
pub struct World(String);

impl World {
    pub async fn all() -> Result<Vec<World>, Error> {
        fs::read_dir(WORLDS_DIR)
            .map_ok(|entry| World::new(entry.file_name().to_string_lossy()))
            .err_into()
            .try_collect().await
    }

    pub async fn all_running() -> Result<Vec<World>, Error> {
        let mut running = Vec::default();
        for world in Self::all().await? {
            if world.is_running().await? {
                running.push(world);
            }
        }
        Ok(running)
    }

    pub fn new(name: impl ToString) -> Self {
        World(name.to_string()) //TODO check if world is configured
    }

    pub async fn command(&self, cmd: &str) -> Result<String, Error> {
        let prop = self.properties().await?;
        //TODO wait until world is running
        let mut conn = rcon::Connection::connect(("localhost", prop.rcon_port), &prop.rcon_password.ok_or(Error::RconDisabled)?).await?;
        Ok(conn.cmd(cmd).await?)
    }

    pub fn config(&self) -> Result<Config, Error> {
        Config::load(&self.dir().join("systemd-minecraft.json"))
    }

    pub fn dir(&self) -> PathBuf {
        Path::new(WORLDS_DIR).join(&self.0)
    }

    pub async fn is_running(&self) -> Result<bool, Error> {
        Command::new("systemctl")
            .arg("is-active")
            .arg("--quiet")
            .arg(format!("minecraft@{}", self.0))
            .status()
            .await
            .map(|status| status.success())
            .at_command("systemctl")
            .map_err(Error::Wheel)
    }

    pub async fn properties(&self) -> Result<ServerProperties, Error> {
        ServerProperties::load(&self.dir().join("server.properties")).await
    }

    #[cfg(unix)]
    pub fn run(&self) {
        let mut signals = Signals::new(&[SIGTERM]).expect("failed to set up signal handler");
        let (sigterm_tx, sigterm_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || {
            for _ in signals.forever() {
                let _ = sigterm_tx.send(());
            }
        });
        //TODO check if already running, refuse to start another server
        let config = self.config().expect("failed to load systemd-minecraft world config");
        let mut java = std::process::Command::new("/usr/bin/java"); //TODO replace with tokio command? (not necessarily required since run is intended to be called from systemd only)
        java.arg(format!("-Xms{}M", config.mem_min_mb));
        java.arg(format!("-Xmx{}M", config.mem_max_mb));
        java.arg("-Dlog4j.configurationFile=log4j2.xml"); //TODO make configurable
        for arg in config.extra_args {
            java.arg(arg);
        }
        java.arg("-jar");
        java.arg(self.dir().join("minecraft_server.jar"));
        java.current_dir(self.dir());
        let java = Arc::new(Mutex::new(java.spawn().expect("failed to spawn java command")));
        let java_clone = Arc::clone(&java);
        let (status_tx, status_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || {
            loop {
                if let Some(status) = java_clone.lock().expect("failed to lock subcommand mutex for polling exit status").try_wait().expect("failed to wait for java command") {
                    let _ = status_tx.send(status);
                    break
                } else {
                    thread::sleep(Duration::from_secs(1));
                }
            }
        });
        select! {
            recv(status_rx) -> status => {
                let status = status.expect("failed to receive exit status");
                if !status.success() { panic!("java exited with status code {}", status) }
            }
            recv(sigterm_rx) -> sigterm => {
                let () = sigterm.expect("failed to receive SIGTERM");
                let _ = self.say("SERVER SHUTTING DOWN IN 10 SECONDS. Saving map...");
                let _ = self.command("save-all");
                thread::sleep(Duration::from_secs(10));
                let _ = self.command("stop");
                select! {
                    recv(status_rx) -> status => {
                        let status = status.expect("failed to receive exit status");
                        if !status.success() { panic!("java exited with status code {}", status) }
                    }
                    default(Duration::from_secs(67)) => {
                        eprintln!("The server could not be stopped! Killing...");
                        java.lock().expect("failed to lock subcommand mutex for killing").kill().expect("failed to kill server");
                    }
                }
            }
        }
    }

    pub async fn say(&self, text: &str) -> Result<(), Error> {
        assert_eq!(self.command(&format!("say {text}")).await?, String::default());
        Ok(())
    }

    async fn start(&self) -> Result<(), Error> {
        Command::new("sudo").arg("--non-interactive").arg("systemctl").arg("start").arg(format!("minecraft@{self}")).check("systemctl").await?;
        Ok(())
    }

    /// Stops the server for this world using `systemctl` and returns whether it was running.
    async fn stop(&self) -> Result<bool, Error> {
        let was_running = self.is_running().await?;
        Command::new("sudo").arg("--non-interactive").arg("systemctl").arg("stop").arg(format!("minecraft@{self}")).check("systemctl").await?;
        Ok(was_running)
    }

    pub async fn update(&self, target_version: VersionSpec) -> Result<(), Error> {
        let client = reqwest::Client::builder()
            .user_agent(concat!("systemd-minecraft/", env!("CARGO_PKG_VERSION")))
            .timeout(Duration::from_secs(30))
            .use_rustls_tls()
            .build()?;
        let version_manifest = client.get("https://launchermeta.mojang.com/mc/game/version_manifest.json").send().await?.error_for_status()?.json::<launcher_data::VersionManifest>().await?;
        let version = version_manifest.get(target_version).ok_or(Error::VersionSpec)?;
        let server_jar_path = Path::new(BASE_DIR).join("jar").join(format!("minecraft_server.{}.jar", version.id));
        if !server_jar_path.exists() {
            let version_info = client.get(version.url.clone()).send().await?.error_for_status()?.json::<launcher_data::VersionInfo>().await?;
            crate::util::download(
                &client,
                version_info.downloads.server.url,
                &mut File::create(&server_jar_path).await?
            ).await?;
        }
        //TODO also back up world in parallel, once wurstminebackup is working correctly
        let was_running = self.stop().await?;
        let service_path = self.dir().join("minecraft_server.jar");
        if fs::symlink_metadata(&service_path).await.is_ok() {
            fs::remove_file(&service_path).await?;
        }
        #[cfg(unix)] fs::symlink(server_jar_path, &service_path).await?;
        #[cfg(windows)] fs::symlink_file(server_jar_path, &service_path).await?;
        if was_running { self.start().await?; }
        Ok(())
    }
}

impl Default for World {
    fn default() -> World {
        World("wurstmineberg".to_string()) //TODO get from config
    }
}

impl FromStr for World {
    type Err = Never;

    fn from_str(s: &str) -> Result<World, Never> {
        Ok(World::new(s))
    }
}

impl fmt::Display for World {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}
