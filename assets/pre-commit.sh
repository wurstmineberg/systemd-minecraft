#!/bin/sh

set -e

cargo check
cargo check --all-features
