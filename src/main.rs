#![deny(rust_2018_idioms, unused, unused_import_braces, unused_qualifications, warnings)]
#![forbid(unsafe_code)]

use {
    structopt::StructOpt,
    systemd_minecraft::{
        Error,
        VersionSpec,
        World,
    }
};

#[derive(StructOpt)]
enum Args {
    /// Runs a Minecraft console command on a world.
    Cmd {
        world: World,
        command: String,
    },
    #[cfg(unix)]
    /// Runs a Minecraft world. Should not be used directly, use `systemctl start minecraft@worldname` instead.
    Run {
        world: World,
    },
    /// Updates Minecraft for a world.
    Update {
        world: World,
        version: Option<String>,
        #[structopt(long, conflicts_with = "version")]
        snapshot: bool,
    },
}

#[wheel::main]
async fn main(args: Args) -> Result<(), Error> {
    match args {
        Args::Cmd { world, command } => {
            println!("{}", world.command(&command).await?);
        }
        #[cfg(unix)]
        Args::Run { world } => {
            world.run();
        }
        Args::Update { world, version, snapshot } => {
            let target_version = if let Some(version) = version {
                VersionSpec::Exact(version)
            } else if snapshot {
                VersionSpec::LatestSnapshot
            } else {
                VersionSpec::LatestRelease
            };
            world.update(target_version).await?;
        }
    }
    Ok(())
}
