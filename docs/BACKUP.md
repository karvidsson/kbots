# Backup and restore

The engine can be reinstalled from git in minutes. The things that cannot be
rebuilt at all are small and live in the overlay:

- `config/secrets.enc` plus its `.salt` and `.kdf` — the vault
- the configuration YAML, team roster and agent definitions
- the databases: memories, sessions, goals, alert state

Media, logs, caches, checkouts and `tmp/` are excluded on purpose. They are
reproducible or disposable, and including them would turn a 20 MB daily archive
into a 15 GB one that nobody keeps off-machine.

## Taking a backup

```sh
scripts/backup.sh
```

Writes one encrypted archive per run:

| Variable | Default | Meaning |
|---|---|---|
| `KBOTS_BACKUP_DIR` | `~/kbots-backups` | where archives land |
| `KBOTS_BACKUP_KEEP` | `14` | how many archives to keep |
| `KBOTS_BACKUP_KEY_FILE` | `~/.config/kbots-backup-key` | the encryption key |

Databases are copied with `sqlite3 .backup`, never with `cp`: a live WAL
database copied byte-for-byte can restore as a corrupt file.

**The backup key is generated on first run and is not the vault passphrase.**
That separation is the point: an archive that reaches another disk, or another
person, still cannot open the vault. It also means a key that exists only on
this machine cannot restore this machine, so copy it into a password manager the
first time you see it printed. The same applies to the vault key file.

**Put the archives somewhere else.** Set `KBOTS_BACKUP_DIR` to an external
volume, or sync the directory to remote storage. Archives beside the originals
survive a mistake, not a dead disk.

## Daily, unattended

launchd (macOS), running as the install user:

```xml
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>/Users/<user>/kbots/scripts/backup.sh</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
```

systemd: a `kbots-backup.timer` with `OnCalendar=daily`.

## Proving it works

```sh
scripts/restore-drill.sh            # newest archive
scripts/restore-drill.sh <archive>  # a specific one
```

An untested backup is a belief. The drill decrypts an archive into a throwaway
`0700` directory and checks what decides whether this machine could be rebuilt:

1. the vault opens with the vault key file, and holds secrets
2. every database passes SQLite's `integrity_check` and has tables
3. the configuration parses as YAML

It never touches the live install, never prints a secret value, and removes the
decrypted copy on every exit path. It exits non-zero when any check fails, so a
scheduled run can page you.

Run it after any change to the vault, the store layout or the backup script
itself, and on a schedule if you want the guarantee rather than the hope.

## Restoring for real

1. Install the engine as usual (git clone, `scripts/self-deploy.sh`).
2. Decrypt the archive into a scratch directory:
   ```sh
   openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
     -pass file:~/.config/kbots-backup-key -in <archive> | tar xzf - -C <scratch>
   ```
3. Stop the service. Copy `config/` and `data/` from the scratch directory into
   the overlay, keeping paths. Restore the vault key file from your password
   manager.
4. Start the service and check `platform_version` plus one agent's memory.

A restore that has never been rehearsed takes longer than the outage that caused
it. The drill exists so step 2 is not the first time you try.
