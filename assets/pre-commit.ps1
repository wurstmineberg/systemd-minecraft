#!/usr/bin/env pwsh

cargo check
if (-not $?)
{
    throw 'Native Failure'
}

# copy the tree to the WSL file system to improve compile times
wsl rsync --delete -av /mnt/c/Users/fenhl/git/github.com/wurstmineberg/systemd-minecraft/stage/ /home/fenhl/wslgit/github.com/wurstmineberg/systemd-minecraft/ --exclude target
if (-not $?)
{
    throw 'Native Failure'
}

wsl env -C /home/fenhl/wslgit/github.com/wurstmineberg/systemd-minecraft cargo check
if (-not $?)
{
    throw 'Native Failure'
}
