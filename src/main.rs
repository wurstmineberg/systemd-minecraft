#![warn(trivial_casts)]
#![deny(unused)]
#![deny(rust_2018_idioms)] // this badly-named lint actually produces errors when Rust 2015 idioms are used
#![forbid(unused_import_braces)]

#[macro_use] extern crate clap;

use std::{
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
};
use clap::{
    Arg,
    SubCommand
};
use crossbeam_channel::select;
use itertools::Itertools;
use signal_hook::{
    SIGTERM,
    iterator::Signals
};
use wrapped_enum::wrapped_enum;

enum OtherError {
    RconDisabled,
    ServerPropertiesParse
}

wrapped_enum! {
    enum Error {
        Io(io::Error),
        Other(OtherError),
        ParseInt(ParseIntError),
        Rcon(rcon::Error)
    }
}

#[derive(Debug)]
struct ServerProperties {
    rcon_password: Option<String>,
    rcon_port: u16
}

impl ServerProperties {
    fn read<P: AsRef<Path>>(path: P) -> Result<ServerProperties, Error> {
        let file = BufReader::new(File::open(path)?);
        let mut prop = ServerProperties::default();
        for line in file.lines() {
            let line = line?;
            if line.starts_with('#') { continue; }
            let (key, value) = line.splitn(2, '=').collect_tuple().ok_or(OtherError::ServerPropertiesParse)?;
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
struct World(String);

impl World {
    fn new(name: impl ToString) -> Self {
        World(name.to_string()) //TODO check if world is configured
    }

    fn command(&self, cmd: &str) -> Result<String, Error> {
        let prop = self.properties()?;
        //TODO wait until world is running
        let mut conn = rcon::Connection::connect(("localhost", prop.rcon_port), &prop.rcon_password.ok_or(OtherError::RconDisabled)?)?;
        Ok(conn.cmd(cmd)?)
    }

    fn dir(&self) -> PathBuf {
        Path::new("/opt/wurstmineberg/world").join(&self.0)
    }

    fn properties(&self) -> Result<ServerProperties, Error> {
        ServerProperties::read(self.dir().join("server.properties"))
    }

    fn run(&self) {
        let signals = Signals::new(&[SIGTERM]).expect("failed to set up signal handler");
        let (sigterm_tx, sigterm_rx) = crossbeam_channel::bounded(1);
        thread::spawn(move || {
            for _ in signals.forever() {
                let _ = sigterm_tx.send(());
            }
        });
        //TODO check if already running, refuse to start another server
        let mut java = Command::new("/usr/bin/java");
        java.arg("-Xms1024M") //TODO make configurable
            .arg("-Xmx1536M") //TODO make configurable
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

    fn say(&self, text: &str) -> Result<(), Error> {
        assert_eq!(self.command(&format!("say {}", text))?, String::default());
        Ok(())
    }
}

impl Default for World {
    fn default() -> World {
        World("wurstmineberg".to_string()) //TODO get from config
    }
}

fn main() {
    let matches = app_from_crate!()
        .subcommand(SubCommand::with_name("run")
            .arg(Arg::with_name("world")
                .takes_value(true)))
        .get_matches();
    match matches.subcommand() {
        ("run", Some(sub_matches)) => {
            let world = sub_matches.value_of("world").map(World::new).unwrap_or_default();
            world.run();
        }
        _ => unreachable!()
    }
}
