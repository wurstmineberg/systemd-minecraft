use {
    std::{
        fmt,
        fs::File,
        io::{
            self,
            BufReader,
            prelude::*
        },
        num::ParseIntError,
        path::{
            Path,
            PathBuf
        },
        process::Command,
        sync::{
            Arc,
            Mutex
        },
        thread,
        time::Duration
    },
    crossbeam_channel::select,
    derive_more::From,
    itertools::Itertools as _,
    serde::Deserialize,
    signal_hook::{
        SIGTERM,
        iterator::Signals
    }
};

const WORLDS_DIR: &str = "/opt/wurstmineberg/world";

#[derive(Debug, From)]
pub enum Error {
    Io(io::Error),
    ParseInt(ParseIntError),
    Rcon(rcon::Error),
    RconDisabled,
    SerDe(serde_json::Error),
    ServerPropertiesParse
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => e.fmt(f),
            Error::ParseInt(e) => e.fmt(f),
            Error::Rcon(e) => e.fmt(f),
            Error::RconDisabled => write!(f, "no RCON password is configured for this world"),
            Error::SerDe(e) => e.fmt(f),
            Error::ServerPropertiesParse => write!(f, "failed to parse server.properties")
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    #[serde(rename = "memMaxMB")]
    mem_max_mb: usize,
    #[serde(rename = "memMinMB")]
    mem_min_mb: usize
}

impl Config {
    pub fn load(path: impl AsRef<Path>) -> Result<Config, Error> {
        Ok(if path.as_ref().exists() {
            serde_json::from_reader(File::open(path.as_ref())?)?
        } else {
            Config::default()
        })
    }
}

impl Default for Config {
    fn default() -> Config {
        Config {
            mem_max_mb: 1536, // the recommended default for Linode 2GB
            mem_min_mb: 1024 // the recommended default for Linode 2GB
        }
    }
}

#[derive(Debug)]
pub struct ServerProperties {
    rcon_password: Option<String>,
    rcon_port: u16
}

impl ServerProperties {
    fn load<P: AsRef<Path>>(path: P) -> Result<ServerProperties, Error> {
        let file = BufReader::new(File::open(path)?);
        let mut prop = ServerProperties::default();
        for line in file.lines() {
            let line = line?;
            if line.starts_with('#') { continue; }
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
            rcon_port: 22575
        }
    }
}

#[derive(Debug)]
pub struct World(String);

impl World {
    pub fn all() -> io::Result<Vec<World>> {
        Path::new(WORLDS_DIR).read_dir()?
            .map_results(|entry| World::new(entry.file_name().to_string_lossy()))
            .collect()
    }

    pub fn all_running() -> io::Result<Vec<World>> {
        let mut running = Vec::default();
        for world in Self::all()? {
            if world.is_running()? {
                running.push(world);
            }
        }
        Ok(running)
    }

    pub fn new(name: impl ToString) -> Self {
        World(name.to_string()) //TODO check if world is configured
    }

    pub fn command(&self, cmd: &str) -> Result<String, Error> {
        let prop = self.properties()?;
        //TODO wait until world is running
        let mut conn = rcon::Connection::connect(("localhost", prop.rcon_port), &prop.rcon_password.ok_or(Error::RconDisabled)?)?;
        Ok(conn.cmd(cmd)?)
    }

    pub fn config(&self) -> Result<Config, Error> {
        Config::load(self.dir().join("systemd-minecraft.json"))
    }

    pub fn dir(&self) -> PathBuf {
        Path::new(WORLDS_DIR).join(&self.0)
    }

    pub fn is_running(&self) -> io::Result<bool> { //TODO async?
        Command::new("systemctl")
            .arg("is-active")
            .arg("--quiet")
            .arg(format!("minecraft@{}", self.0))
            .status()
            .map(|status| status.success())
    }

    pub fn properties(&self) -> Result<ServerProperties, Error> {
        ServerProperties::load(self.dir().join("server.properties"))
    }

    pub fn run(&self) {
        let signals = Signals::new(&[SIGTERM]).expect("failed to set up signal handler");
        let (sigterm_tx, sigterm_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || {
            for _ in signals.forever() {
                let _ = sigterm_tx.send(());
            }
        });
        //TODO check if already running, refuse to start another server
        let config = self.config().expect("failed to load systemd-minecraft world config");
        let mut java = Command::new("/usr/bin/java");
        java.arg(format!("-Xms{}M", config.mem_min_mb))
            .arg(format!("-Xmx{}M", config.mem_max_mb))
            .arg("-Dlog4j.configurationFile=log4j2.xml") //TODO make configurable
            .arg("-jar")
            .arg(self.dir().join("minecraft_server.jar"))
            .current_dir(self.dir());
        let java = Arc::new(Mutex::new(java.spawn().expect("failed to spawn java command")));
        let java_clone = Arc::clone(&java);
        let (status_tx, status_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || {
            loop {
                if let Some(status) = java_clone.lock().expect("failed to lock subcommand mutex for polling exit status").try_wait().expect("failed to wait for java command") {
                    let _ = status_tx.send(status);
                    break;
                } else {
                    thread::sleep(Duration::from_secs(1));
                }
            }
        });
        select! {
            recv(status_rx) -> status => {
                let status = status.expect("failed to receive exit status");
                if !status.success() { panic!("java exited with status code {}", status); }
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
                        if !status.success() { panic!("java exited with status code {}", status); }
                    }
                    default(Duration::from_secs(67)) => {
                        eprintln!("The server could not be stopped! Killing...");
                        java.lock().expect("failed to lock subcommand mutex for killing").kill().expect("failed to kill server");
                    }
                }
            }
        }
    }

    pub fn say(&self, text: &str) -> Result<(), Error> {
        assert_eq!(self.command(&format!("say {}", text))?, String::default());
        Ok(())
    }
}

impl Default for World {
    fn default() -> World {
        World("wurstmineberg".to_string()) //TODO get from config
    }
}
