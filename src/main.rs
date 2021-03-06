#![deny(rust_2018_idioms, unused, unused_import_braces, unused_qualifications, warnings)]

use {
    structopt::StructOpt,
    systemd_minecraft::{
        Error,
        World,
    }
};

#[derive(StructOpt)]
enum Args {
    Cmd {
        world: World,
        command: String,
    },
    Run {
        world: World,
    },
}

#[wheel::main]
async fn main(args: Args) -> Result<(), Error> {
    match args {
        Args::Cmd { world, command } => {
            println!("{}", world.command(&command).await?);
        }
        Args::Run { world } => {
            world.run();
        }
    }
    Ok(())
}
