#![deny(rust_2018_idioms, unused, unused_import_braces, unused_qualifications, warnings)]

#[macro_use] extern crate clap;

use {
    clap::{
        Arg,
        SubCommand
    },
    systemd_minecraft::{
        Error,
        World
    }
};

#[tokio::main]
async fn main() -> Result<(), Error> {
    let matches = app_from_crate!()
        .subcommand(SubCommand::with_name("cmd")
            .arg(Arg::with_name("world")
                .takes_value(true)
            )
            .arg(Arg::with_name("command")
                .takes_value(true)
                .required(true)
            )
        )
        .subcommand(SubCommand::with_name("run")
            .arg(Arg::with_name("world")
                .takes_value(true)
            )
        )
        .get_matches();
    match matches.subcommand() {
        ("cmd", Some(sub_matches)) => {
            let world = sub_matches.value_of("world").map(World::new).unwrap_or_default();
            println!("{}", world.command(sub_matches.value_of("command").expect("missing command")).await?);
        }
        ("run", Some(sub_matches)) => {
            let world = sub_matches.value_of("world").map(World::new).unwrap_or_default();
            world.run();
        }
        _ => unreachable!()
    }
    Ok(())
}
